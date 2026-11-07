#include <memory>
#include <string>
#include <vector>

#include "app_config.hpp"
#include "audio_pipeline.hpp"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "power_manager.hpp"
#include "talknest_client.hpp"
#include "ui_avatar.hpp"
#include "volume.hpp"

namespace {
constexpr char TAG[] = "main";

AppConfig config;
UiAvatar ui;
AudioPipeline audio;
PowerManager power;
std::unique_ptr<TalkNestClient> client;

void fatal_error(const char *stage, esp_err_t error);

void wait_for_power_button_release()
{
    /* Require the line to stay low for several samples. A bounce right
     * after release leaves it high at the moment the ext1 wake source is
     * armed, which makes light sleep return ESP_ERR_SLEEP_REJECT and the
     * press look like it was ignored. */
    int stable_low = 0;
    while (stable_low < 3) {
        if (gpio_get_level(GPIO_NUM_10) == 0) {
            ++stable_low;
        } else {
            stable_low = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void sleep_cycle()
{
    ESP_LOGI(TAG, "Preparing light sleep");
    ui.set_state("Sleeping");
    client->stop();
    audio.stop();
    ui.suspend();
    ui.set_sleeping(true);
    wait_for_power_button_release();
    /* ext1 does not make the IDF keep RTC_PERIPH powered, unlike ext0 and
     * the GPIO wakeup sources. Without it the internal pull-down on the
     * button line is lost in sleep, so the line floats high and ANY_HIGH
     * wakes the chip again almost immediately. */
    esp_err_t sleep_result = esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,
                                                 ESP_PD_OPTION_ON);
    if (sleep_result == ESP_OK) {
        sleep_result = esp_sleep_enable_ext1_wakeup_io(
            1ULL << GPIO_NUM_10, ESP_EXT1_WAKEUP_ANY_HIGH);
    }
    if (sleep_result == ESP_OK) {
        sleep_result = esp_light_sleep_start();
        if (sleep_result == ESP_ERR_SLEEP_REJECT) {
            /* The wake source was seen as active at entry, which is a level
             * glitch rather than a held button: settle the line and retry
             * instead of losing the press. */
            ESP_LOGW(TAG, "Light sleep rejected, retrying after settle");
            wait_for_power_button_release();
            sleep_result = esp_light_sleep_start();
        }
        esp_sleep_disable_ext1_wakeup_io(1ULL << GPIO_NUM_10);
    }
    if (sleep_result != ESP_OK) {
        ESP_LOGW(TAG, "Light sleep did not run: %s",
                 esp_err_to_name(sleep_result));
    }
    wait_for_power_button_release();
    ui.set_sleeping(false);
    ui.resume();
    ESP_LOGI(TAG, "Power button woke the device");
    esp_err_t audio_result = audio.start();
    if (audio_result != ESP_OK) {
        fatal_error("Audio resume failed", audio_result);
        return;
    }
    // board_audio_open() re-applies the config default; restore the saved
    // volume so a sleep/wake cycle does not silently reset the user's level.
    volume::apply();
    esp_err_t client_result = client->start();
    if (client_result != ESP_OK) {
        fatal_error("Network resume failed", client_result);
        client->stop();
        audio.stop();
    }
}

void fatal_error(const char *stage, esp_err_t error)
{
    ESP_LOGE(TAG, "%s: %s", stage, esp_err_to_name(error));
    ui.set_error(stage);
}
} // namespace

extern "C" void app_main()
{
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        result = nvs_flash_init();
    }
    ESP_ERROR_CHECK(result);
    result = ui.initialize(
        []() { client->interrupt(); },
        [](int steps) { return volume::change(steps); },
        []() { volume::commit(); });
    if (result != ESP_OK) {
        fatal_error("Display init failed", result);
        return;
    }
    std::vector<AppConfig> profiles;
    std::vector<std::string> profile_names;
    std::string forced_profile;
    size_t selected = 0;
    result = app_config_load_profiles(profiles, profile_names);
    if (result != ESP_OK) {
        fatal_error("Config invalid", result);
        return;
    }
    result = app_config_load_card_profile(forced_profile);
    if (result != ESP_OK && result != ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "SD profile selection not applied: %s", esp_err_to_name(result));
    }

    client = std::make_unique<TalkNestClient>(config, audio, ui);
    result = client->initialize();
    if (result != ESP_OK) {
        fatal_error("Network init failed", result);
        return;
    }
    result = client->select_profile(profiles, profile_names, forced_profile, selected);
    if (result != ESP_OK) {
        fatal_error("Profile select failed", result);
        return;
    }
    config = profiles[selected];
    result = app_config_load_overrides(config);
    if (result != ESP_OK && result != ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "SD configuration not applied: %s", esp_err_to_name(result));
    }
    client->set_profile(config);
    // Config supplies the factory default; a previously saved level wins.
    volume::init(config.speaker_volume);
    ESP_LOGI(TAG, "Effective audio config: volume=%d mic_gain=%.1f capture_gain=%.1f prebuffer=%d",
             config.speaker_volume, config.mic_gain_db, config.capture_gain,
             config.playback_prebuffer_ms);
    AudioPipelineConfig audio_config = {
        .speaker_volume = config.speaker_volume,
        .mic_gain_db = config.mic_gain_db,
        .capture_gain = config.capture_gain,
        .playback_prebuffer_ms = config.playback_prebuffer_ms,
    };
    /* The playback task only writes an atomic envelope value here; the
     * avatar's LVGL timer reads it. No LVGL call from the audio task. */
    result = audio.initialize(
        audio_config,
        []() { client->playback_finished(); },
        [](uint8_t rms) { ui.set_envelope(rms); });
    if (result != ESP_OK) {
        fatal_error("Audio init failed", result);
        return;
    }
    result = audio.start();
    if (result != ESP_OK) {
        fatal_error("Audio start failed", result);
        audio.stop();
        return;
    }
    volume::apply();
    result = client->start();
    if (result != ESP_OK) {
        fatal_error("Network start failed", result);
        client->stop();
        audio.stop();
        return;
    }
    result = power.initialize(sleep_cycle);
    if (result != ESP_OK) {
        fatal_error("Power init failed", result);
    }
}
