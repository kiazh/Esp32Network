idf_component_register(
    SRCS "main.c" "nrf24l01.c" "link_features.c" "ssd1306.c"
    INCLUDE_DIRS "."
    REQUIRES board driver freertos esp_timer
)
target_link_libraries(${COMPONENT_LIB} PUBLIC m)
