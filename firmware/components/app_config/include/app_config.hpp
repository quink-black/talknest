#pragma once

#include <string>

#include "esp_err.h"

struct AppConfig {
    std::string wifi_ssid;
    std::string wifi_password;
    std::string server_url;
    std::string mode = "kid";
    std::string learner = "default";
    bool full_duplex = false;
    int speaker_volume = 65;
    float mic_gain_db = 24.0F;
    float capture_gain = 8.0F;
    int playback_prebuffer_ms = 500;
};

esp_err_t app_config_load(AppConfig &config);
esp_err_t app_config_load_overrides(AppConfig &config);
const char *app_config_ca_certificate();
