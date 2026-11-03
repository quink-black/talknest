#pragma once

#include <functional>
#include <string>

#include "esp_err.h"
#include "lvgl.h"

class UiStatus {
public:
    esp_err_t initialize(std::function<void()> interrupt_callback);
    void set_state(const std::string &state);
    void set_error(const std::string &message);
    void set_sleeping(bool sleeping);
    void suspend();
    void resume();

private:
    lv_obj_t *indicator_ = nullptr;
    lv_obj_t *status_label_ = nullptr;
    lv_obj_t *hint_label_ = nullptr;
    std::function<void()> interrupt_callback_;
    bool can_interrupt_ = false;
    bool display_locked_ = false;

    static void on_screen_clicked(lv_event_t *event);
};
