idf_component_register(
    SRCS "app_main.cpp"
    INCLUDE_DIRS "."
    REQUIRES app_config board_audio audio_pipeline talknest_client ui_avatar power_manager nvs_flash
)
