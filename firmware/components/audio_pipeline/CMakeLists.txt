idf_component_register(
    SRCS "audio_pipeline.cpp" "afe_processor.cpp"
    INCLUDE_DIRS "include"
    REQUIRES board_audio esp_timer esp_partition espressif__esp-sr espressif__esp_audio_effects
)
