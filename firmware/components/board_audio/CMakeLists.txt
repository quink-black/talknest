idf_component_register(
    SRCS "board_audio.cpp"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_codec_dev waveshare__esp32_s3_touch_amoled_2_06
)
