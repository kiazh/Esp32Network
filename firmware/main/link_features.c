#include "link_features.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include <math.h>
#include <string.h>

typedef struct {
    int64_t ts_us;
    uint8_t seq;
} pkt_rec_t;

static pkt_rec_t        s_buf[FEAT_WINDOW_SIZE];
static int              s_head  = 0;   /* next write slot */
static int              s_count = 0;   /* occupied entries */
static uint32_t         s_total = 0;   /* lifetime total */
static SemaphoreHandle_t s_mu;

void feat_init(void)
{
    s_mu = xSemaphoreCreateMutex();
    memset(s_buf, 0, sizeof(s_buf));
}

void feat_push(uint8_t seq)
{
    xSemaphoreTake(s_mu, portMAX_DELAY);
    s_buf[s_head] = (pkt_rec_t){ .ts_us = esp_timer_get_time(), .seq = seq };
    s_head = (s_head + 1) % FEAT_WINDOW_SIZE;
    if (s_count < FEAT_WINDOW_SIZE) s_count++;
    s_total++;
    xSemaphoreGive(s_mu);
}

uint32_t feat_total_pkts(void) { return s_total; }

link_features_t feat_compute(void)
{
    link_features_t f = {0};

    xSemaphoreTake(s_mu, portMAX_DELAY);
    int count = s_count;
    pkt_rec_t snap[FEAT_WINDOW_SIZE];
    /* Unwrap ring buffer → chronological order */
    int oldest = (count < FEAT_WINDOW_SIZE) ? 0 : s_head;
    for (int i = 0; i < count; i++)
        snap[i] = s_buf[(oldest + i) % FEAT_WINDOW_SIZE];
    xSemaphoreGive(s_mu);

    if (count < 2) return f;

    /* Find first packet inside the 2-second window. */
    int64_t now    = esp_timer_get_time();
    int64_t cutoff = now - (int64_t)FEAT_WINDOW_MS * 1000;
    int wi = count;   /* sentinel: no packet found in window */
    for (int i = 0; i < count; i++) {
        if (snap[i].ts_us >= cutoff) { wi = i; break; }
    }
    if (wi >= count - 1) return f;   /* fewer than 2 packets in window */

    int n = count - wi;  /* packets inside window */

    /* pkt_rate */
    float span_s = (snap[count-1].ts_us - snap[wi].ts_us) / 1e6f;
    f.pkt_rate = (span_s > 0.05f) ? ((float)(n - 1) / span_s) : 0.0f;

    /* IAT */
    float iats[FEAT_WINDOW_SIZE];
    int   ni = 0;
    for (int i = wi + 1; i < count; i++) {
        float iat = (float)(snap[i].ts_us - snap[i-1].ts_us) / 1000.0f;
        if (iat > 0.0f && iat < 5000.0f)
            iats[ni++] = iat;
    }
    if (ni > 0) {
        float sum = 0.0f;
        for (int i = 0; i < ni; i++) sum += iats[i];
        float mean = sum / ni;
        float sum2 = 0.0f;
        for (int i = 0; i < ni; i++) sum2 += (iats[i] - mean) * (iats[i] - mean);
        float stddev = sqrtf(sum2 / ni);
        f.avg_iat_ms  = mean;
        f.burst_score = (mean > 1.0f) ? (stddev / mean) : 0.0f;
        if (f.burst_score > 3.0f) f.burst_score = 3.0f;
    }

    /* loss_rate: seq gaps within window */
    int seq_span  = ((int)snap[count-1].seq - (int)snap[wi].seq + 256) % 256 + 1;
    if (seq_span > n) {
        f.loss_rate = (float)(seq_span - n) / (float)seq_span;
        if (f.loss_rate > 1.0f) f.loss_rate = 1.0f;
    }

    return f;
}
