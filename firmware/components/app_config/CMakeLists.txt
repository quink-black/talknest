set(CONFIG_FILE "${CMAKE_CURRENT_LIST_DIR}/../../config/talknest.json")
set(CA_FILE "${CMAKE_CURRENT_LIST_DIR}/../../config/ca.pem")

if(NOT EXISTS "${CONFIG_FILE}" OR NOT EXISTS "${CA_FILE}")
    message(FATAL_ERROR "Create config/talknest.json and config/ca.pem from the documented examples before building")
endif()

idf_component_register(
    SRCS "app_config.cpp"
    INCLUDE_DIRS "include"
    REQUIRES espressif__cjson waveshare__esp32_s3_touch_amoled_2_06
    EMBED_FILES "${CONFIG_FILE}" "${CA_FILE}"
)
