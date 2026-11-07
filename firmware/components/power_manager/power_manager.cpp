#include "power_manager.hpp"

#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_log.h"

namespace {
constexpr gpio_num_t POWER_BUTTON = GPIO_NUM_10;
constexpr uint32_t SAMPLE_MS = 20;
constexpr uint8_t DEBOUNCE_SAMPLES = 3;
constexpr uint32_t MIN_PRESS_MS = 50;
constexpr uint32_t LONG_PRESS_MS = 2000;
constexpr char TAG[] = "power";
}

esp_err_t PowerManager::initialize(std::function<void()> sleep_callback)
{
    sleep_callback_ = std::move(sleep_callback);
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << POWER_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&config), "power", "Configure power button failed");
    if (xTaskCreatePinnedToCore(button_task_entry, "power_button", 8192, this, 4,
                                &button_task_, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void PowerManager::button_loop()
{
    /* A mechanical button bounces for a few milliseconds. Accepting the
     * first sample of a transition made sleep depend on where the poll
     * landed inside the bounce, so only a level held for several samples
     * counts as a press or a release. */
    bool stable_pressed = false;
    uint8_t same_samples = 0;
    TickType_t pressed_at = 0;
    while (true) {
        bool raw_pressed = gpio_get_level(POWER_BUTTON) != 0;
        if (raw_pressed == stable_pressed) {
            same_samples = 0;
        } else if (++same_samples >= DEBOUNCE_SAMPLES) {
            stable_pressed = raw_pressed;
            same_samples = 0;
            if (stable_pressed) {
                pressed_at = xTaskGetTickCount();
            } else {
                uint32_t held_ms = pdTICKS_TO_MS(xTaskGetTickCount() - pressed_at);
                if (held_ms >= MIN_PRESS_MS && held_ms < LONG_PRESS_MS) {
                    if (sleep_callback_) {
                        sleep_callback_();
                    }
                } else {
                    ESP_LOGW(TAG, "PWR held %u ms, ignored", held_ms);
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(SAMPLE_MS));
    }
}

void PowerManager::button_task_entry(void *argument)
{
    static_cast<PowerManager *>(argument)->button_loop();
}
