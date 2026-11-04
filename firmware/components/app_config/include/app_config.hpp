#pragma once

#include <string>
#include <vector>

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

/* Parse every embedded environment profile. names[i] is the identifier
 * derived from the file name (talknest.json -> "default"). */
esp_err_t app_config_load_profiles(std::vector<AppConfig> &profiles,
                                   std::vector<std::string> &names);
/* Read the optional "profile" key from the TF card; an absent card or key
 * leaves forced_profile untouched. */
esp_err_t app_config_load_card_profile(std::string &forced_profile);
/* Overlay the TF card fields onto one already-selected profile. */
esp_err_t app_config_load_overrides(AppConfig &config);
const char *app_config_ca_certificate();
