#include "volume.hpp"

#include <atomic>

#include "board_audio.hpp"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "volume_logic.hpp"

namespace {
constexpr char TAG[] = "volume";
constexpr char NVS_NAMESPACE[] = "talknest";
constexpr char NVS_KEY[] = "speaker_volume";

std::atomic<int> current_{volume_logic::kDefault};
std::atomic<bool> dirty_{false};

// NVS already uses a "namespace/key" layout; the flash partition is defined
// by partitions.csv and initialized once in app_main.
esp_err_t open_nvs(nvs_open_mode_t mode, nvs_handle_t *handle)
{
    return nvs_open(NVS_NAMESPACE, mode, handle);
}
} // namespace

namespace volume {

esp_err_t init(int config_default)
{
    int value = volume_logic::clamp(config_default);
    nvs_handle_t handle;
    if (open_nvs(NVS_READONLY, &handle) == ESP_OK) {
        int32_t saved = 0;
        if (nvs_get_i32(handle, NVS_KEY, &saved) == ESP_OK) {
            value = volume_logic::clamp(static_cast<int>(saved));
        }
        nvs_close(handle);
    }
    current_.store(value, std::memory_order_relaxed);
    dirty_.store(false, std::memory_order_relaxed);
    ESP_LOGI(TAG, "Speaker volume restored at %d", value);
    return ESP_OK;
}

int get()
{
    return current_.load(std::memory_order_relaxed);
}

esp_err_t apply()
{
    return board_audio_set_volume(current_.load(std::memory_order_relaxed));
}

int change(int steps)
{
    const int next = volume_logic::apply_steps(current_.load(std::memory_order_relaxed), steps);
    const int previous = current_.exchange(next, std::memory_order_relaxed);
    if (next == previous) {
        return next;
    }
    dirty_.store(true, std::memory_order_relaxed);
    const esp_err_t applied = board_audio_set_volume(next);
    if (applied != ESP_OK) {
        ESP_LOGW(TAG, "Apply volume %d failed: %s", next, esp_err_to_name(applied));
    }
    ESP_LOGI(TAG, "Speaker volume %d", next);
    return next;
}

esp_err_t commit()
{
    if (!dirty_.exchange(false, std::memory_order_relaxed)) {
        return ESP_OK;
    }
    nvs_handle_t handle;
    esp_err_t result = open_nvs(NVS_READWRITE, &handle);
    if (result != ESP_OK) {
        dirty_.store(true, std::memory_order_relaxed);
        return result;
    }
    result = nvs_set_i32(handle, NVS_KEY, current_.load(std::memory_order_relaxed));
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    if (result != ESP_OK) {
        dirty_.store(true, std::memory_order_relaxed);
    }
    nvs_close(handle);
    return result;
}

} // namespace volume
