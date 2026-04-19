/*
 * NRF24 Wireless Link Monitor — ESP32-WROOM-32U
 *
 * Pipeline:
 *   NRF24L01 → feat_push() → feat_compute() → model_predict() → OLED + serial
 *
 * Serial commands (115200 baud):
 *   i  → inference mode (default)        — displays predicted link state
 *   c  → collect mode                    — prints CSV for training
 *   0  → label NORMAL    (collect mode)
 *   1  → label WEAK      (collect mode)
 *   2  → label INTERF    (collect mode)
 *   s  → print status
 *
 * Simulation mode (no transmitter needed for initial testing):
 *   Set SIM_MODE 1 and recompile.  The rx_task generates synthetic packets
 *   from all three link conditions in rotation so the full pipeline can be
 *   exercised on the bench before real hardware is wired up.
 */

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/uart.h"
#include "board_init.h"

#include "nrf24l01.h"
#include "link_features.h"
#include "link_model.h"
#include "ssd1306.h"

/* Set via: idf.py menuconfig → NRF24 Link Monitor → Simulation mode
 * Or:       idf.py -DCONFIG_NRF24_SIM_MODE=y build              */
#ifdef CONFIG_NRF24_SIM_MODE
#  define SIM_MODE 1
#else
#  define SIM_MODE 0
#endif

typedef enum { MODE_INFER = 0, MODE_COLLECT = 1 } app_mode_t;

static volatile app_mode_t g_mode       = MODE_INFER;
static volatile int        g_label      = LINK_NORMAL;
static volatile int        g_last_cls   = LINK_NORMAL;
static volatile bool       g_oled_ok    = false;

/* ── Simulation helpers ─────────────────────────────────────────────────── */

#if SIM_MODE
#include <stdlib.h>   /* rand() — no extra IDF component needed */
/*
 * Generates synthetic packet arrivals for three link conditions.
 * Cycles through NORMAL→WEAK→INTERFERENCE every 20 s each.
 */
static void sim_rx_task(void *arg)
{
    uint8_t seq = 0;
    srand(42);
    for (;;) {
        int64_t now_s = esp_timer_get_time() / 1000000;
        int phase = (now_s / 20) % 3;   /* 0=NORMAL 1=WEAK 2=INTERF */

        uint32_t delay_ms;
        bool drop = false;

        if (phase == 0) {           /* NORMAL */
            delay_ms = 95 + (rand() % 10);
        } else if (phase == 1) {    /* WEAK — high loss, long delay */
            delay_ms = 200 + (rand() % 300);
            drop = (rand() % 100) < 55;
        } else {                    /* INTERFERENCE — bursty */
            if ((rand() % 10) < 3) {
                /* burst: 3 packets in quick succession */
                for (int b = 0; b < 3; b++) {
                    feat_push(seq++);
                    vTaskDelay(pdMS_TO_TICKS(5));
                }
            }
            delay_ms = 50 + (rand() % 400);
            drop = (rand() % 100) < 10;
        }

        vTaskDelay(pdMS_TO_TICKS(delay_ms));
        if (!drop) {
            feat_push(seq);
        }
        seq++;   /* advance seq regardless of drop → creates loss */
    }
}
#endif /* SIM_MODE */

/* ── RX task: poll NRF24 → push packets into feature window ────────────── */

static void rx_task(void *arg)
{
    uint8_t pkt[NRF24_PAYLOAD_LEN];
    for (;;) {
        if (nrf24_packet_available()) {
            nrf24_read_payload(pkt);
            feat_push(pkt[0]);          /* byte[0] is the sequence number */
        }
        vTaskDelay(pdMS_TO_TICKS(1));   /* poll at ~1 kHz */
    }
}

/* ── Feature + inference task: runs every 1 s ───────────────────────────── */

static void feat_task(void *arg)
{
    char line[32];
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));

        link_features_t f   = feat_compute();
        int             cls = model_predict(f.pkt_rate, f.loss_rate,
                                            f.avg_iat_ms, f.burst_score);
        g_last_cls = cls;

        if (g_mode == MODE_COLLECT) {
            /* CSV output: DATA,pkt_rate,loss_rate,avg_iat_ms,burst_score,label */
            printf("DATA,%.2f,%.3f,%.1f,%.3f,%d\n",
                   f.pkt_rate, f.loss_rate,
                   f.avg_iat_ms, f.burst_score,
                   g_label);
        } else {
            printf("[INFER] state=%-6s rate=%5.1f loss=%4.1f%% "
                   "iat=%5.0fms burst=%.2f total=%lu\n",
                   LINK_STATE_STR[cls],
                   f.pkt_rate, f.loss_rate * 100.0f,
                   f.avg_iat_ms, f.burst_score,
                   (unsigned long)feat_total_pkts());
        }

        /* Update OLED display */
        if (g_oled_ok) {
            snprintf(line, sizeof(line), "State: %-7s", LINK_STATE_STR[cls]);
            ssd1306_print_line(2, line);
            snprintf(line, sizeof(line), "Rate:  %5.1f p/s", f.pkt_rate);
            ssd1306_print_line(3, line);
            snprintf(line, sizeof(line), "Loss:  %4.1f%%    ", f.loss_rate * 100.0f);
            ssd1306_print_line(4, line);
            snprintf(line, sizeof(line), "IAT:   %5.0f ms  ", f.avg_iat_ms);
            ssd1306_print_line(5, line);
            snprintf(line, sizeof(line), "Burst: %.2f      ", f.burst_score);
            ssd1306_print_line(6, line);
            snprintf(line, sizeof(line), "Pkts:  %lu       ", (unsigned long)feat_total_pkts());
            ssd1306_print_line(7, line);
        }
    }
}

/* ── app_main ───────────────────────────────────────────────────────────── */

void app_main(void)
{
    board_common_init();

    /* OLED — optional; continue without display if absent */
    if (ssd1306_init() == ESP_OK) {
        g_oled_ok = true;
        ssd1306_print_line(0, "NRF24 LinkMonitor");
        ssd1306_print_line(1, "Mode: INFER      ");
    } else {
        printf("[WARN] SSD1306 not found — serial output only\n");
    }

#if SIM_MODE
    printf("[INFO] SIM_MODE enabled — generating synthetic packets\n");
    feat_init();
    xTaskCreatePinnedToCore(sim_rx_task, "sim_rx", 4096, NULL, 5, NULL, 1);
#else
    if (nrf24_init_rx() != ESP_OK) {
        printf("[ERROR] NRF24 init failed — check wiring (CE=GPIO4 CSN=GPIO5)\n");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    printf("[INFO] NRF24 ready — ch=%d payload=%d bytes\n",
           NRF24_CHANNEL, NRF24_PAYLOAD_LEN);
    feat_init();
    xTaskCreatePinnedToCore(rx_task, "rx", 4096, NULL, 5, NULL, 1);
#endif

    xTaskCreatePinnedToCore(feat_task, "feat", 8192, NULL, 3, NULL, 0);

    /* Serial command interface (UART0 = USB) */
    uart_config_t uart_cfg = {
        .baud_rate  = 115200,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
    };
    uart_param_config(UART_NUM_0, &uart_cfg);
    uart_driver_install(UART_NUM_0, 256, 0, 0, NULL, 0);

    printf("Commands: [i]=infer [c]=collect [0/1/2]=set-label [s]=status\n");

    uint8_t ch;
    for (;;) {
        if (uart_read_bytes(UART_NUM_0, &ch, 1, pdMS_TO_TICKS(200)) <= 0)
            continue;

        switch (ch) {
        case 'i': case 'I':
            g_mode = MODE_INFER;
            printf("[CMD] mode → INFER\n");
            if (g_oled_ok) ssd1306_print_line(1, "Mode: INFER      ");
            break;
        case 'c': case 'C':
            g_mode = MODE_COLLECT;
            printf("[CMD] mode → COLLECT  label=%d (%s)\n",
                   g_label, LINK_STATE_STR[g_label]);
            if (g_oled_ok) ssd1306_print_line(1, "Mode: COLLECT    ");
            break;
        case '0': case '1': case '2':
            g_label = ch - '0';
            printf("[CMD] label → %d (%s)\n", g_label, LINK_STATE_STR[g_label]);
            break;
        case 's': case 'S':
            printf("[STAT] mode=%s label=%d last=%s total=%lu\n",
                   g_mode == MODE_INFER ? "INFER" : "COLLECT",
                   g_label,
                   LINK_STATE_STR[g_last_cls],
                   (unsigned long)feat_total_pkts());
            break;
        default:
            break;
        }
    }
}
