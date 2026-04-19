#pragma once
/*
 * board_init.h — minimal ESP32 board init helpers.
 * Self-contained, no external framework required.
 */
#include "nvs_flash.h"
#include "esp_log.h"

static inline void board_common_init(void)
{
    esp_log_level_set("*", ESP_LOG_WARN);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
}
