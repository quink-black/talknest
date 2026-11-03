idf_component_register(
    SRCS "talknest_client.cpp"
    INCLUDE_DIRS "include"
    REQUIRES app_config audio_pipeline ui_status esp_wifi esp_netif esp_event espressif__cjson lwip
    PRIV_REQUIRES espressif__esp_websocket_client
)
