#include "app_config.hpp"

#include <cmath>
#include <cstdio>
#include <vector>

#include "bsp/esp32_s3_touch_amoled_2_06.h"
#include "cJSON.h"
#include "esp_log.h"

extern const char talknest_json_start[] asm("_binary_talknest_json_start");
extern const char talknest_json_end[] asm("_binary_talknest_json_end");
extern const char ca_pem_start[] asm("_binary_ca_pem_start");

namespace {
constexpr char TAG[] = "config";
constexpr size_t MAX_SD_CONFIG_BYTES = 8192;

bool read_string(cJSON *root, const char *name, std::string &value, bool required)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return !required;
    }
    value = item->valuestring;
    return !value.empty();
}

bool parse_config(cJSON *root, AppConfig &config, bool require_identity)
{
    bool ok = read_string(root, "wifi_ssid", config.wifi_ssid, require_identity) &&
              read_string(root, "wifi_password", config.wifi_password, false) &&
              read_string(root, "server_url", config.server_url, require_identity) &&
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
    item = cJSON_GetObjectItemCaseSensitive(root, "playback_prebuffer_ms");
    if (cJSON_IsNumber(item)) {
        config.playback_prebuffer_ms = item->valueint;
    }
    return ok;
}

esp_err_t invalid(const char *field)
{
    ESP_LOGE(TAG, "Invalid or missing configuration field: %s", field);
    return ESP_ERR_INVALID_ARG;
}

esp_err_t validate_config(const AppConfig &config)
{
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
    if (config.playback_prebuffer_ms < 200 ||
        config.playback_prebuffer_ms > 5000) {
        return invalid("playback_prebuffer_ms");
    }
    if (config.wifi_ssid.size() > 32 || config.wifi_password.size() > 63) {
        return invalid("Wi-Fi credential length");
    }
    return ESP_OK;
}

struct SdMountGuard {
    bool mounted = false;
    ~SdMountGuard()
    {
        if (mounted) {
            bsp_sdcard_unmount();
        }
    }
};
} // namespace

esp_err_t app_config_load(AppConfig &config)
{
    const size_t length = static_cast<size_t>(talknest_json_end - talknest_json_start);
    cJSON *root = cJSON_ParseWithLength(talknest_json_start, length);
    if (root == nullptr) {
        ESP_LOGE(TAG, "The embedded JSON configuration is malformed");
        return ESP_ERR_INVALID_ARG;
    }

    bool ok = parse_config(root, config, true);
    cJSON_Delete(root);

    if (!ok) {
        return invalid("required strings");
    }
    return validate_config(config);
}

esp_err_t app_config_load_overrides(AppConfig &config)
{
    SdMountGuard guard;
    esp_err_t result = bsp_sdcard_mount();
    if (result != ESP_OK) {
        ESP_LOGI(TAG, "No SD card configuration: %s", esp_err_to_name(result));
        return ESP_ERR_NOT_FOUND;
    }
    guard.mounted = true;

    const std::string path = std::string(BSP_SD_MOUNT_POINT) + "/talknest.json";
    FILE *file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) {
        ESP_LOGI(TAG, "No %s; using embedded configuration", path.c_str());
        return ESP_ERR_NOT_FOUND;
    }

    std::fseek(file, 0, SEEK_END);
    long file_size = std::ftell(file);
    std::rewind(file);
    if (file_size <= 0 || file_size > static_cast<long>(MAX_SD_CONFIG_BYTES)) {
        ESP_LOGE(TAG, "SD configuration size is invalid: %ld bytes", file_size);
        std::fclose(file);
        return ESP_ERR_INVALID_SIZE;
    }

    std::vector<char> data(static_cast<size_t>(file_size));
    size_t read_size = std::fread(data.data(), 1, data.size(), file);
    std::fclose(file);
    if (read_size != data.size()) {
        ESP_LOGE(TAG, "Could not read the whole SD configuration");
        return ESP_ERR_INVALID_SIZE;
    }

    cJSON *root = cJSON_ParseWithLength(data.data(), data.size());
    if (root == nullptr) {
        ESP_LOGE(TAG, "The SD JSON configuration is malformed");
        return ESP_ERR_INVALID_ARG;
    }

    AppConfig candidate = config;
    bool ok = parse_config(root, candidate, false);
    cJSON_Delete(root);
    if (!ok) {
        ESP_LOGE(TAG, "The SD configuration has invalid fields; keeping embedded values");
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t validation = validate_config(candidate);
    if (validation != ESP_OK) {
        ESP_LOGE(TAG, "The SD configuration failed validation; keeping embedded values");
        return validation;
    }

    config = candidate;
    ESP_LOGI(TAG, "Applied SD configuration: %s", path.c_str());
    return ESP_OK;
}

const char *app_config_ca_certificate()
{
    return ca_pem_start;
}
