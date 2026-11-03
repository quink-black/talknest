#include "app_config.hpp"

#include <cmath>

#include "cJSON.h"
#include "esp_log.h"

extern const char talknest_json_start[] asm("_binary_talknest_json_start");
extern const char talknest_json_end[] asm("_binary_talknest_json_end");
extern const char ca_pem_start[] asm("_binary_ca_pem_start");

namespace {
constexpr char TAG[] = "config";

bool read_string(cJSON *root, const char *name, std::string &value, bool required)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return !required;
    }
    value = item->valuestring;
    return !value.empty();
}

esp_err_t invalid(const char *field)
{
    ESP_LOGE(TAG, "Invalid or missing configuration field: %s", field);
    return ESP_ERR_INVALID_ARG;
}
} // namespace

esp_err_t app_config_load(AppConfig &config)
{
    const size_t length = static_cast<size_t>(talknest_json_end - talknest_json_start);
    cJSON *root = cJSON_ParseWithLength(talknest_json_start, length);
    if (root == nullptr) {
        ESP_LOGE(TAG, "The embedded JSON configuration is malformed");
        return ESP_ERR_INVALID_ARG;
    }

    bool ok = read_string(root, "wifi_ssid", config.wifi_ssid, true) &&
              read_string(root, "wifi_password", config.wifi_password, false) &&
              read_string(root, "server_url", config.server_url, true) &&
              read_string(root, "mode", config.mode, false) &&
              read_string(root, "learner", config.learner, false);
    if (ok && config.server_url.rfind("wss://", 0) != 0) {
        ok = false;
        ESP_LOGE(TAG, "server_url must use wss://");
    }

    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, "full_duplex");
    if (cJSON_IsBool(item)) {
        config.full_duplex = cJSON_IsTrue(item);
    }
    item = cJSON_GetObjectItemCaseSensitive(root, "speaker_volume");
    if (cJSON_IsNumber(item)) {
        config.speaker_volume = item->valueint;
    }
    item = cJSON_GetObjectItemCaseSensitive(root, "mic_gain_db");
    if (cJSON_IsNumber(item)) {
        config.mic_gain_db = static_cast<float>(item->valuedouble);
    }
    item = cJSON_GetObjectItemCaseSensitive(root, "capture_gain");
    if (cJSON_IsNumber(item)) {
        config.capture_gain = static_cast<float>(item->valuedouble);
    }
    item = cJSON_GetObjectItemCaseSensitive(root, "aec_delay_ms");
    if (cJSON_IsNumber(item)) {
        config.aec_delay_ms = item->valueint;
    }
    cJSON_Delete(root);

    if (!ok) {
        return invalid("required strings");
    }
    if (config.speaker_volume < 0 || config.speaker_volume > 100) {
        return invalid("speaker_volume");
    }
    if (!std::isfinite(config.mic_gain_db) || config.mic_gain_db < 0.0F ||
        config.mic_gain_db > 40.0F) {
        return invalid("mic_gain_db");
    }
    if (!std::isfinite(config.capture_gain) || config.capture_gain < 0.1F ||
        config.capture_gain > 16.0F) {
        return invalid("capture_gain");
    }
    if (config.aec_delay_ms < 0 || config.aec_delay_ms > 500) {
        return invalid("aec_delay_ms");
    }
    if (config.wifi_ssid.size() > 32 || config.wifi_password.size() > 63) {
        return invalid("Wi-Fi credential length");
    }
    return ESP_OK;
}

const char *app_config_ca_certificate()
{
    return ca_pem_start;
}
