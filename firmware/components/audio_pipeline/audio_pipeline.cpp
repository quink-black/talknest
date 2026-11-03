#include "audio_pipeline.hpp"

#include <algorithm>
#include <array>
#include <cstring>

#include "board_audio.hpp"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "pcm_format.hpp"

namespace {
constexpr char TAG[] = "audio";
constexpr TickType_t PLAYBACK_TAIL = pdMS_TO_TICKS(150);

void drain_stream(StreamBufferHandle_t stream, SemaphoreHandle_t mutex)
{
    std::array<uint8_t, 960> discarded;
    if (stream == nullptr || mutex == nullptr) {
        return;
    }
    if (xSemaphoreTake(mutex, portMAX_DELAY) == pdTRUE) {
        while (xStreamBufferReceive(stream, discarded.data(), discarded.size(), 0) > 0) {
        }
        xSemaphoreGive(mutex);
    }
}

size_t stream_bytes(StreamBufferHandle_t stream, SemaphoreHandle_t mutex)
{
    if (stream == nullptr || mutex == nullptr ||
        xSemaphoreTake(mutex, portMAX_DELAY) != pdTRUE) {
        return 0;
    }
    size_t bytes = xStreamBufferBytesAvailable(stream);
    xSemaphoreGive(mutex);
    return bytes;
}

bool send_stream(StreamBufferHandle_t stream, SemaphoreHandle_t mutex,
                 const uint8_t *data, size_t byte_count)
{
    if (stream == nullptr || mutex == nullptr ||
        xSemaphoreTake(mutex, portMAX_DELAY) != pdTRUE) {
        return false;
    }
    while (xStreamBufferSpacesAvailable(stream) < byte_count) {
        /* Small on purpose: this lands on the audio task stacks, which
         * also carry the large capture and interleaving arrays. */
        std::array<uint8_t, 512> discarded;
        if (xStreamBufferReceive(stream, discarded.data(), discarded.size(), 0) == 0) {
            break;
        }
    }
    size_t written = 0;
    if (xStreamBufferSpacesAvailable(stream) >= byte_count) {
        written = xStreamBufferSend(stream, data, byte_count, 0);
    }
    xSemaphoreGive(mutex);
    return written == byte_count;
}
} // namespace

AudioPipeline::AudioPipeline() = default;

AudioPipeline::~AudioPipeline()
{
    stop();
}

esp_err_t AudioPipeline::initialize(const AudioPipelineConfig &config,
                                    std::function<void()> playback_finished_callback)
{
    config_ = config;
    playback_finished_callback_ = std::move(playback_finished_callback);
    upstream_channels_ = config_.full_duplex ? 2 : 1;
    playback_prebuffer_bytes_ =
        static_cast<size_t>(config_.playback_prebuffer_ms) *
        BOARD_AUDIO_SAMPLE_RATE * sizeof(int16_t) / 1000;
    ESP_LOGI(TAG, "Playback prebuffer: %d ms (%u bytes)",
             config_.playback_prebuffer_ms,
             static_cast<unsigned int>(playback_prebuffer_bytes_));
    if (config_.full_duplex) {
        ESP_LOGI(TAG, "Full duplex upstream: mic + reference stereo, server AEC");
    }

    esp_err_t result = board_audio_initialize();
    if (result != ESP_OK) {
        return result;
    }
    initialized_ = true;
    return ESP_OK;
}

esp_err_t AudioPipeline::start()
{
    if (!initialized_ || running_) {
        return running_ ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    esp_err_t result = board_audio_open(config_.speaker_volume, config_.mic_gain_db);
    if (result != ESP_OK) {
        return result;
    }

    upstream_buffer_ = xStreamBufferCreate(
        UPSTREAM_BUFFER_BYTES_PER_CHANNEL * upstream_channels_, 1);
    playback_buffer_storage_ = static_cast<uint8_t *>(heap_caps_malloc(
        PLAYBACK_BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    playback_buffer_struct_ = static_cast<StaticStreamBuffer_t *>(heap_caps_calloc(
        1, sizeof(StaticStreamBuffer_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (playback_buffer_storage_ != nullptr && playback_buffer_struct_ != nullptr) {
        playback_buffer_ = xStreamBufferCreateStatic(
            PLAYBACK_BUFFER_BYTES, 1, playback_buffer_storage_, playback_buffer_struct_);
    }
    upstream_mutex_ = xSemaphoreCreateMutex();
    playback_mutex_ = xSemaphoreCreateMutex();
    stopped_events_ = xEventGroupCreate();
    if (upstream_buffer_ == nullptr || playback_buffer_ == nullptr ||
        upstream_mutex_ == nullptr || playback_mutex_ == nullptr || stopped_events_ == nullptr) {
        ESP_LOGE(TAG, "Audio buffer allocation failed");
        stop();
        return ESP_ERR_NO_MEM;
    }

    playback_active_ = false;
    playback_ended_ = false;
    playback_started_ = false;
    running_ = true;
    /* The capture frame buffers (TDM capture, mono mixdown, stereo
     * upstream) alone are ~7 KB; 12 KB leaves room for the codec read
     * path below them. */
    BaseType_t capture_ok = xTaskCreatePinnedToCore(
        capture_task_entry, "capture", 12288, this, 8, &capture_task_, 1);
    BaseType_t playback_ok = xTaskCreatePinnedToCore(
        playback_task_entry, "playback", 8192, this, 9, &playback_task_, 1);
    if (capture_ok != pdPASS || playback_ok != pdPASS) {
        ESP_LOGE(TAG, "Audio task creation failed");
        stop();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void AudioPipeline::stop()
{
    running_ = false;
    EventBits_t expected_stops = 0;
    if (capture_task_ != nullptr) {
        expected_stops |= CAPTURE_STOPPED;
    }
    if (playback_task_ != nullptr) {
        expected_stops |= PLAYBACK_STOPPED;
    }
    if (stopped_events_ != nullptr && expected_stops != 0) {
        xEventGroupWaitBits(stopped_events_, expected_stops, pdFALSE, pdTRUE, portMAX_DELAY);
        vTaskDelay(1);
    }
    capture_task_ = nullptr;
    playback_task_ = nullptr;
    board_audio_close();
    if (upstream_buffer_ != nullptr) {
        vStreamBufferDelete(upstream_buffer_);
        upstream_buffer_ = nullptr;
    }
    if (playback_buffer_ != nullptr) {
        vStreamBufferDelete(playback_buffer_);
        playback_buffer_ = nullptr;
    }
    if (playback_buffer_storage_ != nullptr) {
        heap_caps_free(playback_buffer_storage_);
        playback_buffer_storage_ = nullptr;
    }
    if (playback_buffer_struct_ != nullptr) {
        heap_caps_free(playback_buffer_struct_);
        playback_buffer_struct_ = nullptr;
    }
    if (stopped_events_ != nullptr) {
        vEventGroupDelete(stopped_events_);
        stopped_events_ = nullptr;
    }
    if (upstream_mutex_ != nullptr) {
        vSemaphoreDelete(upstream_mutex_);
        upstream_mutex_ = nullptr;
    }
    if (playback_mutex_ != nullptr) {
        vSemaphoreDelete(playback_mutex_);
        playback_mutex_ = nullptr;
    }
}

bool AudioPipeline::read_upstream(int16_t *pcm, TickType_t timeout)
{
    const size_t expected =
        TALKNEST_PCM_FRAME_SAMPLES * upstream_channels_ * sizeof(int16_t);
    TickType_t started = xTaskGetTickCount();
    while (running_ && upstream_buffer_ != nullptr) {
        if (xSemaphoreTake(upstream_mutex_, portMAX_DELAY) == pdTRUE) {
            bool complete = xStreamBufferBytesAvailable(upstream_buffer_) >= expected;
            size_t received = complete ?
                xStreamBufferReceive(upstream_buffer_, pcm, expected, 0) : 0;
            xSemaphoreGive(upstream_mutex_);
            if (received == expected) {
                return true;
            }
        }
        if (xTaskGetTickCount() - started >= timeout) {
            return false;
        }
        vTaskDelay(1);
    }
    return false;
}

bool AudioPipeline::upstream_allowed() const
{
    return config_.full_duplex || !playback_active_;
}

void AudioPipeline::clear_upstream()
{
    if (upstream_buffer_ != nullptr) {
        drain_stream(upstream_buffer_, upstream_mutex_);
    }
}

void AudioPipeline::begin_playback(uint32_t source_sample_rate)
{
    if (playback_buffer_ == nullptr) {
        return;
    }
    if (source_sample_rate != BOARD_AUDIO_SAMPLE_RATE) {
        ESP_LOGE(TAG, "Unsupported reply sample rate %lu; audio will be dropped",
                 static_cast<unsigned long>(source_sample_rate));
    }
    if (playback_active_) {
        finish_playback(playback_generation_.load());
    }
    drain_stream(playback_buffer_, playback_mutex_);
    playback_sample_rate_ = source_sample_rate;
    playback_ended_ = false;
    playback_started_ = false;
    playback_generation_++;
    playback_active_ = true;
    playback_begin_tick_ = xTaskGetTickCount();
    last_playback_write_ = playback_begin_tick_;
    ESP_LOGI(TAG, "Playback start: sample_rate=%lu",
             static_cast<unsigned long>(source_sample_rate));
}

void AudioPipeline::enqueue_playback(const uint8_t *pcm, size_t byte_count)
{
    if (!playback_active_ || playback_buffer_ == nullptr || pcm == nullptr ||
        byte_count == 0 || byte_count % sizeof(int16_t) != 0 ||
        playback_sample_rate_ != BOARD_AUDIO_SAMPLE_RATE) {
        return;
    }
    if (!send_stream(playback_buffer_, playback_mutex_, pcm, byte_count)) {
        ESP_LOGW(TAG, "Playback buffer full; dropped an audio block");
    }
}

void AudioPipeline::end_playback()
{
    playback_ended_ = true;
}

bool AudioPipeline::cancel_playback()
{
    bool was_active = playback_active_.exchange(false);
    playback_ended_ = false;
    playback_started_ = false;
    if (playback_buffer_ != nullptr) {
        drain_stream(playback_buffer_, playback_mutex_);
    }
    if (!config_.full_duplex) {
        clear_upstream();
    }
    return was_active;
}

bool AudioPipeline::playback_active() const
{
    return playback_active_;
}

void AudioPipeline::capture_loop()
{
    std::array<int16_t, TALKNEST_PCM_FRAME_SAMPLES * BOARD_AUDIO_CAPTURE_CHANNELS> captured;
    std::array<int16_t, TALKNEST_PCM_FRAME_SAMPLES> mono;
    std::array<int16_t, TALKNEST_PCM_FRAME_SAMPLES * 2> stereo;
    while (running_) {
        esp_err_t result = board_audio_read(captured.data(), TALKNEST_PCM_FRAME_SAMPLES);
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "Capture failed: %s", esp_err_to_name(result));
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        if (config_.full_duplex) {
            pcm_format::tdm_to_upstream_stereo(captured.data(), stereo.data(),
                                               TALKNEST_PCM_FRAME_SAMPLES,
                                               config_.capture_gain);
            send_stream(upstream_buffer_, upstream_mutex_,
                        reinterpret_cast<const uint8_t *>(stereo.data()),
                        stereo.size() * sizeof(int16_t));
            continue;
        }

        for (size_t frame = 0; frame < TALKNEST_PCM_FRAME_SAMPLES; ++frame) {
            mono[frame] = pcm_format::scale_sample(
                pcm_format::mix_tdm_microphones(captured.data(), frame), config_.capture_gain);
        }
        send_stream(upstream_buffer_, upstream_mutex_,
                    reinterpret_cast<const uint8_t *>(mono.data()),
                    mono.size() * sizeof(int16_t));
    }
    xEventGroupSetBits(stopped_events_, CAPTURE_STOPPED);
    vTaskDelete(nullptr);
}

void AudioPipeline::playback_loop()
{
    std::array<int16_t, PLAYBACK_BLOCK_SAMPLES> mono;
    std::array<int16_t, PLAYBACK_BLOCK_SAMPLES * BOARD_AUDIO_PLAYBACK_CHANNELS> stereo;
    uint32_t underruns = 0;
    TickType_t last_report = xTaskGetTickCount();
    while (running_) {
        if (!playback_active_) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        const size_t block_bytes = mono.size() * sizeof(int16_t);
        uint32_t generation = playback_generation_.load();
        size_t received = 0;
        size_t available = 0;
        if (xSemaphoreTake(playback_mutex_, portMAX_DELAY) == pdTRUE) {
            available = xStreamBufferBytesAvailable(playback_buffer_);
            if (!playback_started_ && !playback_ended_ &&
                available < playback_prebuffer_bytes_) {
                xSemaphoreGive(playback_mutex_);
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            if (playback_started_ && !playback_ended_ && available < block_bytes) {
                ++underruns;
            }
            size_t requested = playback_ended_ ? std::min(available, block_bytes) : block_bytes;
            received = xStreamBufferReceive(playback_buffer_, mono.data(), requested, 0);
            xSemaphoreGive(playback_mutex_);
        }
        if (received < block_bytes) {
            std::memset(mono.data() + received / sizeof(int16_t), 0, block_bytes - received);
        }
        if (received > 0) {
            if (!playback_started_) {
                TickType_t waited = xTaskGetTickCount() - playback_begin_tick_;
                ESP_LOGI(TAG, "First playback block after %lu ms",
                         static_cast<unsigned long>(waited * 1000 / configTICK_RATE_HZ));
            }
            playback_started_ = true;
            last_playback_write_ = xTaskGetTickCount();
        }
        pcm_format::mono_to_stereo(mono.data(), stereo.data(), mono.size());
        esp_err_t result = board_audio_write(stereo.data(), mono.size());
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "Playback failed: %s", esp_err_to_name(result));
        }
        if (xTaskGetTickCount() - last_report >= pdMS_TO_TICKS(2000)) {
            ESP_LOGI(TAG, "Playback: available=%u underruns=%lu",
                     static_cast<unsigned int>(available),
                     static_cast<unsigned long>(underruns));
            last_report = xTaskGetTickCount();
        }
        if (playback_ended_ && stream_bytes(playback_buffer_, playback_mutex_) == 0 &&
            xTaskGetTickCount() - last_playback_write_ >= PLAYBACK_TAIL) {
            finish_playback(generation);
        }
    }
    xEventGroupSetBits(stopped_events_, PLAYBACK_STOPPED);
    vTaskDelete(nullptr);
}

void AudioPipeline::finish_playback(uint32_t generation)
{
    if (generation != playback_generation_ || !playback_active_.exchange(false)) {
        return;
    }
    playback_ended_ = false;
    playback_started_ = false;
    if (!config_.full_duplex) {
        clear_upstream();
    }
    if (playback_finished_callback_) {
        playback_finished_callback_();
    }
}

void AudioPipeline::capture_task_entry(void *argument)
{
    static_cast<AudioPipeline *>(argument)->capture_loop();
}

void AudioPipeline::playback_task_entry(void *argument)
{
    static_cast<AudioPipeline *>(argument)->playback_loop();
}
