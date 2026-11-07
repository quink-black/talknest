idf_component_register(
    SRCS "volume.cpp"
    INCLUDE_DIRS "include"
    REQUIRES board_audio nvs_flash
)
