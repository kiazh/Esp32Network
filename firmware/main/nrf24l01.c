#include "nrf24l01.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include <string.h>

/* ── Register map ──────────────────────────────────────────────────────────── */
#define REG_CONFIG      0x00
#define REG_EN_AA       0x01
#define REG_EN_RXADDR   0x02
#define REG_SETUP_AW    0x03
#define REG_RF_CH       0x05
#define REG_RF_SETUP    0x06
#define REG_STATUS      0x07
#define REG_RX_ADDR_P0  0x0A
#define REG_TX_ADDR     0x10
#define REG_RX_PW_P0    0x11

/* ── SPI commands ──────────────────────────────────────────────────────────── */
#define CMD_R(r)        (0x00 | (r))
#define CMD_W(r)        (0x20 | (r))
#define CMD_R_RX_PLD    0x61
#define CMD_FLUSH_RX    0xE2
#define CMD_NOP         0xFF

/* ── STATUS bits ───────────────────────────────────────────────────────────── */
#define STATUS_RX_DR    (1 << 6)
#define STATUS_TX_DS    (1 << 5)
#define STATUS_MAX_RT   (1 << 4)

/* Default pipe-0 address (must match transmitter) */
static const uint8_t PIPE0_ADDR[5] = {0xE7, 0xE7, 0xE7, 0xE7, 0xE7};

static spi_device_handle_t s_spi;

/* ── Low-level SPI ─────────────────────────────────────────────────────────── */

static void spi_xfer(const uint8_t *tx, uint8_t *rx, int len)
{
    spi_transaction_t t = {
        .length    = (size_t)len * 8,
        .tx_buffer = tx,
        .rx_buffer = rx,
    };
    esp_err_t err = spi_device_transmit(s_spi, &t);
    if (err != ESP_OK)
        ESP_LOGE("nrf24", "spi_device_transmit failed: %s", esp_err_to_name(err));
}

static uint8_t read_reg(uint8_t reg)
{
    uint8_t tx[2] = {CMD_R(reg), CMD_NOP};
    uint8_t rx[2] = {0, 0};
    spi_xfer(tx, rx, 2);
    return rx[1];
}

static void write_reg(uint8_t reg, uint8_t val)
{
    uint8_t tx[2] = {CMD_W(reg), val};
    spi_xfer(tx, NULL, 2);
}

static void write_reg_buf(uint8_t reg, const uint8_t *buf, int len)
{
    /* max NRF24 multi-byte register is 5 bytes (address) */
    if (len < 1 || len > 5) return;
    uint8_t tx[6];
    tx[0] = CMD_W(reg);
    memcpy(&tx[1], buf, len);
    spi_xfer(tx, NULL, len + 1);
}

/* ── Public API ────────────────────────────────────────────────────────────── */

esp_err_t nrf24_init_rx(void)
{
    /* CE GPIO */
    gpio_config_t ce_cfg = {
        .pin_bit_mask = (1ULL << NRF24_PIN_CE),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&ce_cfg);
    gpio_set_level(NRF24_PIN_CE, 0);

    /* SPI bus — SPI2 (HSPI) */
    spi_bus_config_t buscfg = {
        .mosi_io_num   = NRF24_PIN_MOSI,
        .miso_io_num   = NRF24_PIN_MISO,
        .sclk_io_num   = NRF24_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 33,
    };
    esp_err_t ret = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_DISABLED);
    if (ret != ESP_OK) return ret;

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 4 * 1000 * 1000,  /* 4 MHz (NRF24 max 10 MHz) */
        .mode           = 0,                  /* CPOL=0 CPHA=0 */
        .spics_io_num   = NRF24_PIN_CSN,
        .queue_size     = 1,
        .pre_cb         = NULL,
    };
    ret = spi_bus_add_device(SPI2_HOST, &devcfg, &s_spi);
    if (ret != ESP_OK) return ret;

    vTaskDelay(pdMS_TO_TICKS(5));  /* power-on settling ≥ 1.5 ms */

    /* Configure as receiver */
    write_reg(REG_CONFIG,    0x0F);  /* PWR_UP=1 PRIM_RX=1 EN_CRC=1 CRCO=1bit */
    write_reg(REG_EN_AA,     0x01);  /* auto-ACK on pipe 0 */
    write_reg(REG_EN_RXADDR, 0x01);  /* enable pipe 0 */
    write_reg(REG_SETUP_AW,  0x03);  /* 5-byte address */
    write_reg(REG_RF_CH,     NRF24_CHANNEL);
    write_reg(REG_RF_SETUP,  0x06);  /* 1 Mbps, 0 dBm */
    write_reg_buf(REG_RX_ADDR_P0, PIPE0_ADDR, 5);
    write_reg_buf(REG_TX_ADDR,    PIPE0_ADDR, 5);  /* TX addr = RX addr for ACK */
    write_reg(REG_RX_PW_P0, NRF24_PAYLOAD_LEN);

    /* Clear any stale status flags */
    write_reg(REG_STATUS, STATUS_RX_DR | STATUS_TX_DS | STATUS_MAX_RT);

    /* Flush RX FIFO */
    uint8_t cmd = CMD_FLUSH_RX;
    spi_xfer(&cmd, NULL, 1);

    vTaskDelay(pdMS_TO_TICKS(2));

    /* CE high → active RX (Tdelay-CE = 130 µs) */
    gpio_set_level(NRF24_PIN_CE, 1);
    vTaskDelay(pdMS_TO_TICKS(1));

    return ESP_OK;
}

bool nrf24_packet_available(void)
{
    return (read_reg(REG_STATUS) & STATUS_RX_DR) != 0;
}

void nrf24_read_payload(uint8_t *buf)
{
    uint8_t tx[NRF24_PAYLOAD_LEN + 1];
    uint8_t rx[NRF24_PAYLOAD_LEN + 1];
    tx[0] = CMD_R_RX_PLD;
    memset(tx + 1, CMD_NOP, NRF24_PAYLOAD_LEN);
    spi_xfer(tx, rx, NRF24_PAYLOAD_LEN + 1);
    memcpy(buf, rx + 1, NRF24_PAYLOAD_LEN);
    /* Clear RX_DR */
    write_reg(REG_STATUS, STATUS_RX_DR);
}

void nrf24_flush_rx(void)
{
    uint8_t cmd = CMD_FLUSH_RX;
    spi_xfer(&cmd, NULL, 1);
}
