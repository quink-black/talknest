#pragma once

#include <functional>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

class PowerManager {
public:
    esp_err_t initialize(std::function<void()> sleep_callback);

private:
    std::function<void()> sleep_callback_;
    TaskHandle_t button_task_ = nullptr;

    void button_loop();
    static void button_task_entry(void *argument);
};
