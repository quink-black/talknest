idf_component_register(
    SRCS "ui_avatar.cpp" "avatar_logic.cpp"
    INCLUDE_DIRS "include"
    REQUIRES waveshare__esp32_s3_touch_amoled_2_06 esp_lvgl_port esp_driver_i2c
    EMBED_FILES "assets/avatar_frames.bin"
)
