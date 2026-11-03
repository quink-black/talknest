#pragma once

#include <atomic>
#include <string>
#include <vector>

#include "app_config.hpp"
#include "audio_pipeline.hpp"
#include "esp_err.h"
#include "esp_netif.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "ui_status.hpp"

class TalkNestClient {
public:
    TalkNestClient(const AppConfig &config, AudioPipeline &audio, UiStatus &ui);

    esp_err_t initialize();
    esp_err_t start();
    void stop();
    void interrupt();
    void playback_finished();

private:
    static constexpr EventBits_t WIFI_CONNECTED = 1U << 0;
    static constexpr EventBits_t MANAGER_STOPPED = 1U << 1;
    static constexpr EventBits_t SENDER_STOPPED = 1U << 2;

    AppConfig config_;
    AudioPipeline &audio_;
    UiStatus &ui_;
    esp_netif_t *netif_ = nullptr;
    EventGroupHandle_t events_ = nullptr;
    SemaphoreHandle_t send_mutex_ = nullptr;
    esp_websocket_client_handle_t websocket_ = nullptr;
    TaskHandle_t manager_task_ = nullptr;
    TaskHandle_t sender_task_ = nullptr;
    std::atomic<bool> initialized_{false};
    std::atomic<bool> running_{false};
    std::atomic<bool> connected_{false};
    std::atomic<bool> initial_messages_pending_{false};
    std::string text_message_;
    std::vector<uint8_t> binary_message_;
    uint8_t message_opcode_ = 0;

    void manager_loop();
    void sender_loop();
    void send_initial_messages();
    void send_start();
    void send_json(const char *type, const char *value = nullptr);
    void send_binary(const int16_t *pcm, size_t sample_count);
    void handle_text(const char *data, size_t length);
    void handle_data(esp_websocket_event_data_t *data);
    void websocket_event(void *event_argument, esp_event_base_t base, int32_t id, void *event_data);

    static void wifi_event(void *argument, esp_event_base_t base, int32_t id, void *data);
    static void ip_event(void *argument, esp_event_base_t base, int32_t id, void *data);
    static void websocket_event_entry(void *argument, esp_event_base_t base, int32_t id, void *data);
    static void manager_task_entry(void *argument);
    static void sender_task_entry(void *argument);
};
