#pragma once
/*
 * NRF24L01+ SPI driver for ESP32-WROOM — receiver (RX) mode only.
 *
 * Wiring: NRF24L01 ↔ ESP32-WROOM
 *   VCC  → 3.3V        GND  → GND
 *   CE   → GPIO4        CSN  → GPIO5
 *   SCK  → GPIO18      MOSI → GPIO23   MISO → GPIO19
 *   IRQ  → NC  (polled mode)
 *
 * NOTE: NRF24L01 is a 3.3 V device. Do NOT connect to 5 V.
 *       Add a 10 µF cap between VCC and GND at the module if you see drops.
 */

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#define NRF24_PIN_MOSI  23
#define NRF24_PIN_MISO  19
#define NRF24_PIN_SCK   18
#define NRF24_PIN_CSN    5   /* SPI CS — managed by driver */
#define NRF24_PIN_CE     4   /* chip enable — GPIO controlled */

#define NRF24_CHANNEL      76   /* 2476 MHz — clear of most WiFi */
#define NRF24_PAYLOAD_LEN   8   /* fixed payload: byte[0]=seq, rest=pad */

/* Initialise NRF24L01 as RX receiver on pipe 0. */
esp_err_t nrf24_init_rx(void);

/* Returns true when at least one packet is waiting in the FIFO. */
bool      nrf24_packet_available(void);

/* Read one packet (NRF24_PAYLOAD_LEN bytes) from FIFO and clear RX_DR flag. */
void      nrf24_read_payload(uint8_t *buf);

/* Flush the RX FIFO. */
void      nrf24_flush_rx(void);
