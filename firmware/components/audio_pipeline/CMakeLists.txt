idf_component_register(
    SRCS "audio_pipeline.cpp"
    INCLUDE_DIRS "include"
    REQUIRES board_audio esp_timer
)
