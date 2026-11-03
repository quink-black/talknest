#include "ui_status.hpp"

#include <cstring>

#include "bsp/esp32_s3_touch_amoled_2_06.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_lvgl_port.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {
constexpr char TAG[] = "ui";

uint32_t state_color(const std::string &state)
{
    if (state == "listening") {
        return 0x35C98F;
    }
    if (state == "hearing") {
        return 0x42A5F5;
    }
    if (state == "speaking") {
        return 0xAB7DF8;
    }
    if (state == "thinking" || state == "preparing") {
        return 0xFFB74D;
    }
    return 0x78909C;
}
} // namespace

esp_err_t UiStatus::initialize(std::function<void()> interrupt_callback)
{
    interrupt_callback_ = std::move(interrupt_callback);
    if (bsp_display_start() == nullptr) {
        ESP_LOGE(TAG, "Display initialization failed");
        return ESP_FAIL;
    }
    if (!bsp_display_lock(2000)) {
        return ESP_ERR_TIMEOUT;
    }

    lv_obj_t *screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x10141C), LV_PART_MAIN);
    lv_obj_add_event_cb(screen, on_screen_clicked, LV_EVENT_CLICKED, this);

    lv_obj_t *title = lv_label_create(screen);
    lv_label_set_text(title, "TalkNest");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_32, LV_PART_MAIN);
    lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 82);

    indicator_ = lv_obj_create(screen);
    lv_obj_set_size(indicator_, 92, 92);
    lv_obj_align(indicator_, LV_ALIGN_CENTER, 0, -20);
    lv_obj_set_style_radius(indicator_, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_border_width(indicator_, 0, LV_PART_MAIN);
    lv_obj_set_style_bg_color(indicator_, lv_color_hex(0x78909C), LV_PART_MAIN);

    status_label_ = lv_label_create(screen);
    lv_obj_set_style_text_font(status_label_, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(status_label_, lv_color_white(), LV_PART_MAIN);
    lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 92);
    lv_label_set_text(status_label_, "Starting");

    hint_label_ = lv_label_create(screen);
    lv_obj_set_style_text_font(hint_label_, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(hint_label_, lv_color_hex(0xAAB4C3), LV_PART_MAIN);
    lv_obj_align(hint_label_, LV_ALIGN_BOTTOM_MID, 0, -62);
    lv_label_set_text(hint_label_, "PWR: sleep");

    bsp_display_unlock();
    bsp_display_brightness_set(70);
    return ESP_OK;
}

void UiStatus::set_state(const std::string &state)
{
    if (status_label_ == nullptr || !bsp_display_lock(200)) {
        return;
    }
    can_interrupt_ = state == "speaking";
    lv_label_set_text(status_label_, state.c_str());
    lv_obj_set_style_bg_color(indicator_, lv_color_hex(state_color(state)), LV_PART_MAIN);
    lv_label_set_text(hint_label_, can_interrupt_ ? "Tap to interrupt" : "PWR: sleep");
    bsp_display_unlock();
}

void UiStatus::set_error(const std::string &message)
{
    if (status_label_ == nullptr || !bsp_display_lock(200)) {
        return;
    }
    can_interrupt_ = false;
    lv_label_set_text(status_label_, message.c_str());
    lv_obj_set_style_bg_color(indicator_, lv_color_hex(0xEF5350), LV_PART_MAIN);
    lv_label_set_text(hint_label_, "Check config and server");
    bsp_display_unlock();
}

void UiStatus::set_sleeping(bool sleeping)
{
    bsp_display_brightness_set(sleeping ? 0 : 70);
}

void UiStatus::suspend()
{
    display_locked_ = bsp_display_lock(0);
    if (!display_locked_) {
        ESP_LOGW(TAG, "Could not lock display before sleep");
        return;
    }
    if (BSP_LCD_TOUCH_INT >= 0) {
        gpio_intr_disable(static_cast<gpio_num_t>(BSP_LCD_TOUCH_INT));
    }
    lvgl_port_stop();
}

void UiStatus::resume()
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus != nullptr) {
        esp_err_t result = i2c_master_bus_reset(bus);
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "I2C bus reset failed: %s", esp_err_to_name(result));
        }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    if (BSP_LCD_TOUCH_INT >= 0) {
        gpio_intr_enable(static_cast<gpio_num_t>(BSP_LCD_TOUCH_INT));
    }
    lvgl_port_resume();
    if (display_locked_) {
        bsp_display_unlock();
        display_locked_ = false;
    }
}

void UiStatus::on_screen_clicked(lv_event_t *event)
{
    auto *ui = static_cast<UiStatus *>(lv_event_get_user_data(event));
    if (ui != nullptr && ui->can_interrupt_ && ui->interrupt_callback_) {
        ui->interrupt_callback_();
    }
}
