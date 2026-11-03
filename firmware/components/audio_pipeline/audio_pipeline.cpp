#include "audio_pipeline.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <new>
#include <vector>

#include "afe_processor.hpp"
#include "board_audio.hpp"
#include "esp_ae_rate_cvt.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/idf_additions.h"
#include "pcm_format.hpp"

namespace {
constexpr char TAG[] = "audio";
constexpr TickType_t PLAYBACK_TAIL = pdMS_TO_TICKS(150);
constexpr size_t MAX_AFE_SAMPLES = 4096;

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
        std::array<uint8_t, TALKNEST_PCM_FRAME_SAMPLES * sizeof(int16_t)> discarded;
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

esp_ae_rate_cvt_handle_t open_rate_converter(uint32_t source_rate, uint32_t destination_rate)
{
    esp_ae_rate_cvt_cfg_t config = {};
    config.src_rate = source_rate;
    config.dest_rate = destination_rate;
    config.channel = 1;
    config.bits_per_sample = ESP_AE_BIT16;
    config.complexity = 2;
    config.perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED;
    esp_ae_rate_cvt_handle_t converter = nullptr;
    if (esp_ae_rate_cvt_open(&config, &converter) != ESP_AE_ERR_OK) {
        return nullptr;
    }
    return converter;
}

bool convert_rate(esp_ae_rate_cvt_handle_t converter, const int16_t *input,
                  uint32_t input_samples, int16_t *output, uint32_t &output_samples)
{
    return esp_ae_rate_cvt_process(
               converter, reinterpret_cast<esp_ae_sample_t>(const_cast<int16_t *>(input)),
               input_samples, reinterpret_cast<esp_ae_sample_t>(output),
               &output_samples) == ESP_AE_ERR_OK;
}
} // namespace

struct AudioProcessingState {
    AfeProcessor afe;
    bool afe_ready = false;
    esp_ae_rate_cvt_handle_t input_converters[3] = {};
    esp_ae_rate_cvt_handle_t output_converter = nullptr;
    std::array<int16_t, TALKNEST_PCM_FRAME_SAMPLES> planar[3];
    std::array<int16_t, 1024> converted[3];
    std::array<int16_t, 3072> interleaved;
    std::array<int16_t, MAX_AFE_SAMPLES> upsampled;
    std::vector<int16_t> reference_delay;
    size_t reference_delay_index = 0;

    ~AudioProcessingState()
    {
        for (auto &converter : input_converters) {
            if (converter != nullptr) {
                esp_ae_rate_cvt_close(converter);
            }
        }
        if (output_converter != nullptr) {
            esp_ae_rate_cvt_close(output_converter);
        }
    }
};

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

    if (config_.full_duplex) {
        processing_state_.reset(new (std::nothrow) AudioProcessingState());
        if (processing_state_ == nullptr) {
            return ESP_ERR_NO_MEM;
        }
        auto &state = *processing_state_;
        for (auto &converter : state.input_converters) {
            converter = open_rate_converter(24000, 16000);
            if (converter == nullptr) {
                processing_state_.reset();
                config_.full_duplex = false;
                break;
            }
        }
        if (config_.full_duplex) {
            state.output_converter = open_rate_converter(16000, 24000);
            if (state.output_converter == nullptr) {
                processing_state_.reset();
                config_.full_duplex = false;
            }
        }
        if (config_.full_duplex) {
            state.reference_delay.assign(config_.aec_delay_ms * 24, 0);
            state.afe_ready = state.afe.initialize();
            if (!state.afe_ready) {
                processing_state_.reset();
                config_.full_duplex = false;
            }
        }
        if (!config_.full_duplex) {
            ESP_LOGE(TAG, "Full duplex requested but AFE is unavailable; using half duplex");
        }
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

    upstream_buffer_ = xStreamBufferCreate(UPSTREAM_BUFFER_BYTES, 1);
    playback_buffer_ = xStreamBufferCreateWithCaps(
        PLAYBACK_BUFFER_BYTES, 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    upstream_mutex_ = xSemaphoreCreateMutex();
    playback_mutex_ = xSemaphoreCreateMutex();
    stopped_events_ = xEventGroupCreate();
    if (upstream_buffer_ == nullptr || playback_buffer_ == nullptr ||
        upstream_mutex_ == nullptr || playback_mutex_ == nullptr || stopped_events_ == nullptr) {
        ESP_LOGE(TAG, "Audio buffer allocation failed");
        stop();
        return ESP_ERR_NO_MEM;
    }

    if (config_.full_duplex && processing_state_ != nullptr) {
        auto &state = *processing_state_;
        for (auto &converter : state.input_converters) {
            esp_ae_rate_cvt_reset(converter);
        }
        esp_ae_rate_cvt_reset(state.output_converter);
        state.afe.reset();
        std::fill(state.reference_delay.begin(), state.reference_delay.end(), 0);
        state.reference_delay_index = 0;
    }
    playback_active_ = false;
    playback_ended_ = false;
    playback_started_ = false;
    running_ = true;
    BaseType_t capture_ok = xTaskCreatePinnedToCore(
        capture_task_entry, "capture", 8192, this, 8, &capture_task_, 1);
    BaseType_t playback_ok = xTaskCreatePinnedToCore(
        playback_task_entry, "playback", 8192, this, 7, &playback_task_, 1);
    BaseType_t afe_ok = pdPASS;
    if (config_.full_duplex) {
        afe_ok = xTaskCreatePinnedToCore(afe_task_entry, "afe_fetch", 8192, this, 8,
                                         &afe_task_, 1);
    }
    if (capture_ok != pdPASS || playback_ok != pdPASS || afe_ok != pdPASS) {
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
    if (afe_task_ != nullptr) {
        expected_stops |= AFE_STOPPED;
    }
    if (stopped_events_ != nullptr && expected_stops != 0) {
        xEventGroupWaitBits(stopped_events_, expected_stops, pdFALSE, pdTRUE, portMAX_DELAY);
        vTaskDelay(1);
    }
    capture_task_ = nullptr;
    playback_task_ = nullptr;
    afe_task_ = nullptr;
    board_audio_close();
    if (upstream_buffer_ != nullptr) {
        vStreamBufferDelete(upstream_buffer_);
        upstream_buffer_ = nullptr;
    }
    if (playback_buffer_ != nullptr) {
        vStreamBufferDeleteWithCaps(playback_buffer_);
        playback_buffer_ = nullptr;
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
    const size_t expected = TALKNEST_PCM_FRAME_SAMPLES * sizeof(int16_t);
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
    last_playback_write_ = xTaskGetTickCount();
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
    while (running_) {
        esp_err_t result = board_audio_read(captured.data(), TALKNEST_PCM_FRAME_SAMPLES);
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "Capture failed: %s", esp_err_to_name(result));
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        if (config_.full_duplex && processing_state_ != nullptr) {
            auto &state = *processing_state_;
            for (size_t frame = 0; frame < TALKNEST_PCM_FRAME_SAMPLES; ++frame) {
                int16_t mic1 = pcm_format::scale_sample(
                    captured[frame * BOARD_AUDIO_CAPTURE_CHANNELS], config_.capture_gain);
                int16_t reference = captured[frame * BOARD_AUDIO_CAPTURE_CHANNELS + 1];
                int16_t mic2 = pcm_format::scale_sample(
                    captured[frame * BOARD_AUDIO_CAPTURE_CHANNELS + 2], config_.capture_gain);
                if (!state.reference_delay.empty()) {
                    int16_t delayed = state.reference_delay[state.reference_delay_index];
                    state.reference_delay[state.reference_delay_index] = reference;
                    state.reference_delay_index =
                        (state.reference_delay_index + 1) % state.reference_delay.size();
                    reference = delayed;
                }
                state.planar[0][frame] = mic1;
                state.planar[1][frame] = mic2;
                state.planar[2][frame] = reference;
            }
            uint32_t converted_samples = 0;
            bool converted_ok = true;
            for (size_t channel = 0; channel < 3; ++channel) {
                uint32_t channel_samples = state.converted[channel].size();
                converted_ok = convert_rate(state.input_converters[channel],
                                            state.planar[channel].data(),
                                            TALKNEST_PCM_FRAME_SAMPLES,
                                            state.converted[channel].data(), channel_samples);
                if (!converted_ok ||
                    (converted_samples != 0 && converted_samples != channel_samples)) {
                    converted_ok = false;
                    break;
                }
                converted_samples = channel_samples;
            }
            if (!converted_ok) {
                continue;
            }
            for (size_t frame = 0; frame < converted_samples; ++frame) {
                for (size_t channel = 0; channel < 3; ++channel) {
                    state.interleaved[frame * 3 + channel] = state.converted[channel][frame];
                }
            }
            state.afe.feed(state.interleaved.data(), converted_samples);
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

void AudioPipeline::afe_loop()
{
    auto &state = *processing_state_;
    while (running_) {
        const auto &cleaned = state.afe.fetch();
        if (cleaned.empty()) {
            continue;
        }
        uint32_t output_samples = state.upsampled.size();
        if (convert_rate(state.output_converter, cleaned.data(), cleaned.size(),
                         state.upsampled.data(), output_samples) && output_samples > 0) {
            send_stream(upstream_buffer_, upstream_mutex_,
                        reinterpret_cast<const uint8_t *>(state.upsampled.data()),
                        output_samples * sizeof(int16_t));
        }
    }
    xEventGroupSetBits(stopped_events_, AFE_STOPPED);
    vTaskDelete(nullptr);
}

void AudioPipeline::playback_loop()
{
    std::array<int16_t, PLAYBACK_BLOCK_SAMPLES> mono;
    std::array<int16_t, PLAYBACK_BLOCK_SAMPLES * BOARD_AUDIO_PLAYBACK_CHANNELS> stereo;
    while (running_) {
        if (!playback_active_) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        const size_t block_bytes = mono.size() * sizeof(int16_t);
        uint32_t generation = playback_generation_.load();
        size_t received = 0;
        if (xSemaphoreTake(playback_mutex_, portMAX_DELAY) == pdTRUE) {
            size_t available = xStreamBufferBytesAvailable(playback_buffer_);
            if (!playback_started_ && !playback_ended_) {
                xSemaphoreGive(playback_mutex_);
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            size_t requested = playback_ended_ ? std::min(available, block_bytes) : block_bytes;
            received = xStreamBufferReceive(playback_buffer_, mono.data(), requested, 0);
            xSemaphoreGive(playback_mutex_);
        }
        if (received < block_bytes) {
            std::memset(mono.data() + received / sizeof(int16_t), 0, block_bytes - received);
        }
        if (received > 0) {
            playback_started_ = true;
            last_playback_write_ = xTaskGetTickCount();
        }
        pcm_format::mono_to_stereo(mono.data(), stereo.data(), mono.size());
        esp_err_t result = board_audio_write(stereo.data(), mono.size());
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "Playback failed: %s", esp_err_to_name(result));
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

void AudioPipeline::afe_task_entry(void *argument)
{
    static_cast<AudioPipeline *>(argument)->afe_loop();
}
