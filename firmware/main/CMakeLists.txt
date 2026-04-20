idf_component_register(
    SRCS "main.c" "nrf24l01.c" "link_features.c" "ssd1306.c"
    INCLUDE_DIRS "."
    REQUIRES board freertos esp_timer log
             esp_driver_spi esp_driver_gpio esp_driver_uart driver
)
target_link_libraries(${COMPONENT_LIB} PUBLIC m)
