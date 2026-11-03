#include "power_manager.hpp"

#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_log.h"

namespace {
constexpr gpio_num_t POWER_BUTTON = GPIO_NUM_10;
constexpr uint32_t LONG_PRESS_MS = 2000;
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
    bool was_pressed = false;
    TickType_t pressed_at = 0;
    while (true) {
        bool pressed = gpio_get_level(POWER_BUTTON) != 0;
        if (pressed && !was_pressed) {
            pressed_at = xTaskGetTickCount();
        }
        if (!pressed && was_pressed && sleep_callback_) {
            uint32_t held_ms = pdTICKS_TO_MS(xTaskGetTickCount() - pressed_at);
            if (held_ms >= 40 && held_ms < LONG_PRESS_MS) {
                sleep_callback_();
            }
        }
        was_pressed = pressed;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void PowerManager::button_task_entry(void *argument)
{
    static_cast<PowerManager *>(argument)->button_loop();
}
