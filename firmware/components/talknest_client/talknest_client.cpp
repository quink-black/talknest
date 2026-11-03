#include "talknest_client.hpp"

#include <algorithm>
#include <cstring>
#include <ctime>

#include "cJSON.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"

namespace {
constexpr char TAG[] = "talknest";
constexpr uint32_t WIFI_WAIT_MS = 20000;
constexpr uint32_t TIME_WAIT_MS = 15000;
}

TalkNestClient::TalkNestClient(const AppConfig &config, AudioPipeline &audio, UiStatus &ui)
    : config_(config), audio_(audio), ui_(ui)
{
}

esp_err_t TalkNestClient::initialize()
{
    if (initialized_) {
        return ESP_OK;
    }
    events_ = xEventGroupCreate();
    send_mutex_ = xSemaphoreCreateMutex();
    if (events_ == nullptr || send_mutex_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "Initialize network failed");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "Create event loop failed");
    netif_ = esp_netif_create_default_wifi_sta();
    ESP_RETURN_ON_FALSE(netif_ != nullptr, ESP_FAIL, TAG, "Create station interface failed");
    wifi_init_config_t wifi_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&wifi_config), TAG, "Initialize Wi-Fi failed");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(
                            WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event, this, nullptr),
                        TAG, "Register Wi-Fi events failed");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(
                            IP_EVENT, IP_EVENT_STA_GOT_IP, &ip_event, this, nullptr),
                        TAG, "Register IP events failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "Set station mode failed");
    initialized_ = true;
    return ESP_OK;
}

esp_err_t TalkNestClient::start()
{
    if (!initialized_ || running_) {
        return running_ ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    wifi_config_t station = {};
    std::strncpy(reinterpret_cast<char *>(station.sta.ssid), config_.wifi_ssid.c_str(),
                 sizeof(station.sta.ssid) - 1);
    std::strncpy(reinterpret_cast<char *>(station.sta.password), config_.wifi_password.c_str(),
                 sizeof(station.sta.password) - 1);
    station.sta.threshold.authmode = config_.wifi_password.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
    station.sta.pmf_cfg.capable = true;
    station.sta.pmf_cfg.required = false;
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &station), TAG, "Configure Wi-Fi failed");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "Start Wi-Fi failed");
    ESP_RETURN_ON_ERROR(esp_wifi_connect(), TAG, "Start Wi-Fi connection failed");

    running_ = true;
    xEventGroupClearBits(events_, MANAGER_STOPPED | SENDER_STOPPED);
    if (xTaskCreatePinnedToCore(manager_task_entry, "talknest_net", 8192, this, 5,
                                &manager_task_, 0) != pdPASS ||
        xTaskCreatePinnedToCore(sender_task_entry, "talknest_send", 6144, this, 6,
                                &sender_task_, 0) != pdPASS) {
        stop();
        return ESP_ERR_NO_MEM;
    }
    ui_.set_state("Connecting");
    return ESP_OK;
}

void TalkNestClient::stop()
{
    if (!initialized_) {
        return;
    }
    if (connected_) {
        send_json("stop");
    }
    running_ = false;
    connected_ = false;
    initial_messages_pending_ = false;
    xEventGroupSetBits(events_, WIFI_CONNECTED);
    esp_wifi_disconnect();
    EventBits_t expected_stops = 0;
    if (manager_task_ != nullptr) {
        expected_stops |= MANAGER_STOPPED;
    }
    if (sender_task_ != nullptr) {
        expected_stops |= SENDER_STOPPED;
    }
    if (expected_stops != 0) {
        xEventGroupWaitBits(events_, expected_stops, pdFALSE, pdTRUE, portMAX_DELAY);
        vTaskDelay(1);
    }
    manager_task_ = nullptr;
    sender_task_ = nullptr;
    audio_.cancel_playback();
    audio_.clear_upstream();
    esp_wifi_stop();
    xEventGroupClearBits(events_, WIFI_CONNECTED);
}

void TalkNestClient::manager_loop()
{
    while (running_) {
        EventBits_t bits = xEventGroupWaitBits(events_, WIFI_CONNECTED, pdFALSE, pdTRUE,
                                               pdMS_TO_TICKS(WIFI_WAIT_MS));
        if (!running_) {
            break;
        }
        if ((bits & WIFI_CONNECTED) == 0) {
            ui_.set_error("Wi-Fi timeout");
            continue;
        }

        esp_sntp_config_t time_config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
        esp_netif_sntp_init(&time_config);
        esp_err_t time_result = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(TIME_WAIT_MS));
        esp_netif_sntp_deinit();
        if (time_result != ESP_OK) {
            ui_.set_error("Time sync failed");
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        if (!running_) {
            break;
        }

        ui_.set_state("Connecting");
        esp_websocket_client_config_t websocket_config = {};
        websocket_config.uri = config_.server_url.c_str();
        websocket_config.cert_pem = app_config_ca_certificate();
        websocket_config.buffer_size = 2048;
        websocket_config.task_stack = 8192;
        websocket_config.network_timeout_ms = 10000;
        websocket_config.reconnect_timeout_ms = 3000;
        websocket_config.disable_auto_reconnect = false;
        websocket_ = esp_websocket_client_init(&websocket_config);
        if (websocket_ == nullptr) {
            ESP_LOGE(TAG, "WebSocket initialization failed");
            ui_.set_error("Client init failed");
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        esp_websocket_register_events(websocket_, WEBSOCKET_EVENT_ANY, websocket_event_entry, this);
        esp_err_t result = esp_websocket_client_start(websocket_);
        if (result != ESP_OK) {
            ESP_LOGE(TAG, "WebSocket start failed: %s", esp_err_to_name(result));
            ui_.set_error("TLS connect failed");
        }
        while (result == ESP_OK && running_ &&
               (xEventGroupGetBits(events_) & WIFI_CONNECTED) != 0) {
            vTaskDelay(pdMS_TO_TICKS(250));
        }
        connected_ = false;
        esp_websocket_client_handle_t old_websocket = websocket_;
        if (xSemaphoreTake(send_mutex_, portMAX_DELAY) == pdTRUE) {
            websocket_ = nullptr;
            xSemaphoreGive(send_mutex_);
        }
        if (old_websocket != nullptr) {
            esp_websocket_client_stop(old_websocket);
            esp_websocket_client_destroy(old_websocket);
        }
        if (result != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(3000));
        }
        audio_.cancel_playback();
        audio_.clear_upstream();
    }
    xEventGroupSetBits(events_, MANAGER_STOPPED);
    vTaskDelete(nullptr);
}

void TalkNestClient::sender_loop()
{
    int16_t pcm[TALKNEST_PCM_FRAME_SAMPLES];
    TickType_t last_ping = xTaskGetTickCount();
    while (running_) {
        if (initial_messages_pending_.exchange(false) && connected_) {
            send_initial_messages();
        }
        if (xTaskGetTickCount() - last_ping >= pdMS_TO_TICKS(15000)) {
            send_json("ping");
            last_ping = xTaskGetTickCount();
        }
        if (!audio_.read_upstream(pcm, pdMS_TO_TICKS(100))) {
            continue;
        }
        if (connected_ && audio_.upstream_allowed()) {
            send_binary(pcm, TALKNEST_PCM_FRAME_SAMPLES);
        }
    }
    xEventGroupSetBits(events_, SENDER_STOPPED);
    vTaskDelete(nullptr);
}

void TalkNestClient::send_initial_messages()
{
    message_opcode_ = 0;
    text_message_.clear();
    binary_message_.clear();
    audio_.clear_upstream();
    audio_.cancel_playback();
    send_json("mode", config_.mode.c_str());
    if (!config_.learner.empty()) {
        send_json("learner", config_.learner.c_str());
    }
    send_json("start");
}

void TalkNestClient::send_json(const char *type, const char *value)
{
    if (!connected_ && std::strcmp(type, "mode") != 0 && std::strcmp(type, "learner") != 0 &&
        std::strcmp(type, "start") != 0) {
        return;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == nullptr) {
        return;
    }
    cJSON_AddStringToObject(root, "type", type);
    if (value != nullptr) {
        cJSON_AddStringToObject(root, "value", value);
    }
    char *serialized = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (serialized == nullptr) {
        return;
    }
    if (xSemaphoreTake(send_mutex_, pdMS_TO_TICKS(1000)) == pdTRUE) {
        if (websocket_ != nullptr) {
            esp_websocket_client_send_text(websocket_, serialized, std::strlen(serialized),
                                           pdMS_TO_TICKS(1000));
        }
        xSemaphoreGive(send_mutex_);
    }
    cJSON_free(serialized);
}

void TalkNestClient::send_binary(const int16_t *pcm, size_t sample_count)
{
    if (!connected_) {
        return;
    }
    if (xSemaphoreTake(send_mutex_, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (websocket_ != nullptr) {
            esp_websocket_client_send_bin(websocket_, reinterpret_cast<const char *>(pcm),
                                          sample_count * sizeof(int16_t), pdMS_TO_TICKS(100));
        }
        xSemaphoreGive(send_mutex_);
    }
}

void TalkNestClient::interrupt()
{
    if (connected_) {
        send_json("interrupt");
    }
}

void TalkNestClient::playback_finished()
{
    send_json("playback", "ended");
}

void TalkNestClient::handle_text(const char *data, size_t length)
{
    cJSON *root = cJSON_ParseWithLength(data, length);
    if (root == nullptr) {
        return;
    }
    const cJSON *type_item = cJSON_GetObjectItemCaseSensitive(root, "type");
    const cJSON *value_item = cJSON_GetObjectItemCaseSensitive(root, "value");
    const char *type = cJSON_IsString(type_item) ? type_item->valuestring : "";
    const char *value = cJSON_IsString(value_item) ? value_item->valuestring : "";

    if (std::strcmp(type, "state") == 0) {
        ESP_LOGI(TAG, "State: %s", value);
        ui_.set_state(value);
    } else if (std::strcmp(type, "audio_start") == 0) {
        const cJSON *rate = cJSON_GetObjectItemCaseSensitive(root, "sample_rate");
        uint32_t sample_rate = cJSON_IsNumber(rate) ? static_cast<uint32_t>(rate->valueint) : 24000;
        audio_.begin_playback(sample_rate);
    } else if (std::strcmp(type, "audio_end") == 0) {
        audio_.end_playback();
    } else if (std::strcmp(type, "stop_playback") == 0) {
        if (audio_.cancel_playback()) {
            send_json("playback", "ended");
        }
    } else if (std::strcmp(type, "error") == 0 || std::strcmp(type, "tts_error") == 0) {
        const cJSON *message = cJSON_GetObjectItemCaseSensitive(root, "message");
        ui_.set_error(cJSON_IsString(message) ? message->valuestring : "Server error");
    }
    cJSON_Delete(root);
}

void TalkNestClient::handle_data(esp_websocket_event_data_t *data)
{
    /* A frame larger than the client buffer arrives as several events that all
     * carry the frame's opcode; only the event at payload offset 0 starts a
     * message. */
    if (data->op_code != 0 && data->payload_offset == 0) {
        if (data->op_code != 0x1 && data->op_code != 0x2) {
            return;
        }
        message_opcode_ = data->op_code;
        text_message_.clear();
        binary_message_.clear();
    }
    if (message_opcode_ != 0x1 && message_opcode_ != 0x2) {
        return;
    }
    if (data->data_len > 0) {
        if (message_opcode_ == 0x1) {
            text_message_.append(data->data_ptr, data->data_len);
        } else {
            binary_message_.insert(binary_message_.end(), data->data_ptr,
                                   data->data_ptr + data->data_len);
        }
    }
    if (!data->fin || data->payload_offset + data->data_len < data->payload_len) {
        return;
    }
    uint8_t completed_opcode = message_opcode_;
    message_opcode_ = 0;
    if (completed_opcode == 0x1) {
        handle_text(text_message_.data(), text_message_.size());
        text_message_.clear();
    } else {
        audio_.enqueue_playback(binary_message_.data(), binary_message_.size());
        binary_message_.clear();
    }
}

void TalkNestClient::websocket_event(void *, esp_event_base_t, int32_t id, void *event_data)
{
    auto *data = static_cast<esp_websocket_event_data_t *>(event_data);
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        connected_ = true;
        initial_messages_pending_ = true;
        ESP_LOGI(TAG, "Secure WebSocket connected");
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        connected_ = false;
        initial_messages_pending_ = false;
        audio_.cancel_playback();
        audio_.clear_upstream();
        ui_.set_state("Reconnecting");
        break;
    case WEBSOCKET_EVENT_DATA:
        if (connected_ && data != nullptr) {
            handle_data(data);
        }
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGW(TAG, "WebSocket or TLS error");
        ui_.set_error("Connection error");
        break;
    default:
        break;
    }
}

void TalkNestClient::wifi_event(void *argument, esp_event_base_t, int32_t id, void *)
{
    auto *client = static_cast<TalkNestClient *>(argument);
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(client->events_, WIFI_CONNECTED);
        client->connected_ = false;
        client->ui_.set_state("Reconnecting");
        if (client->running_) {
            esp_wifi_connect();
        }
    }
}

void TalkNestClient::ip_event(void *argument, esp_event_base_t, int32_t, void *)
{
    auto *client = static_cast<TalkNestClient *>(argument);
    xEventGroupSetBits(client->events_, WIFI_CONNECTED);
}

void TalkNestClient::websocket_event_entry(void *argument, esp_event_base_t base,
                                           int32_t id, void *data)
{
    static_cast<TalkNestClient *>(argument)->websocket_event(argument, base, id, data);
}

void TalkNestClient::manager_task_entry(void *argument)
{
    static_cast<TalkNestClient *>(argument)->manager_loop();
}

void TalkNestClient::sender_task_entry(void *argument)
{
    static_cast<TalkNestClient *>(argument)->sender_loop();
}
