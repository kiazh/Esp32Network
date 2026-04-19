#pragma once
/*
 * Minimal SSD1306 128×64 OLED driver — I2C, text-only.
 *
 * Wiring: SSD1306 ↔ ESP32-WROOM
 *   VCC → 3.3V    GND → GND
 *   SDA → GPIO21   SCL → GPIO22
 *
 * Default I2C address: 0x3C (most 128×64 modules).
 * Change SSD1306_I2C_ADDR to 0x3D if yours has SA0 = 1.
 */
#include "esp_err.h"
#include <stdint.h>

#define SSD1306_I2C_ADDR  0x3C
#define SSD1306_COLS      128
#define SSD1306_PAGES       8   /* 8 pages × 8 px = 64 rows */

/* Initialise I2C bus and OLED.  Returns ESP_OK or an I2C error code.
 * The rest of the API is safe to call even if this returns an error — it
 * will just silently no-op, letting the firmware run without a display. */
esp_err_t ssd1306_init(void);

/* Fill the entire framebuffer with zeros (blank screen). */
void ssd1306_clear(void);

/* Render a NUL-terminated ASCII string to page row (0–7).
 * Characters are 6 px wide (5 px glyph + 1 px gap).
 * Line is padded with spaces to the right edge. */
void ssd1306_print_line(uint8_t row, const char *text);
