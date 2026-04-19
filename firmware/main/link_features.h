#pragma once
/*
 * Wireless link feature extractor.
 *
 * Maintains a circular buffer of recent packet timestamps and sequence
 * numbers.  feat_compute() derives four real-valued features over the
 * most recent FEAT_WINDOW_MS milliseconds:
 *
 *   pkt_rate   : packets/second
 *   loss_rate  : fraction of expected packets that were missed (0.0–1.0)
 *   avg_iat_ms : mean inter-arrival time between successive packets (ms)
 *   burst_score: IAT coefficient of variation (σ/µ), capped at 3.0
 *               high → bursty; low → evenly-spaced
 */

#include <stdint.h>

#define FEAT_WINDOW_SIZE  64     /* circular buffer depth */
#define FEAT_WINDOW_MS  2000     /* feature computation window (ms) */

typedef struct {
    float pkt_rate;     /* packets per second */
    float loss_rate;    /* 0.0 = no loss, 1.0 = total loss */
    float avg_iat_ms;   /* mean IAT in milliseconds */
    float burst_score;  /* σ(IAT)/µ(IAT), ∈ [0, 3] */
} link_features_t;

void           feat_init(void);
void           feat_push(uint8_t seq);       /* call from RX task each packet */
link_features_t feat_compute(void);          /* call from feature task (1 Hz) */
uint32_t       feat_total_pkts(void);        /* running total received */
