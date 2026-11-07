#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>

#include "board_audio.hpp"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

constexpr size_t TALKNEST_PCM_FRAME_SAMPLES = 480;

struct AudioPipelineConfig {
    int speaker_volume = 65;
    float mic_gain_db = 24.0F;
    float capture_gain = 8.0F;
    int playback_prebuffer_ms = 500;
};

class AudioPipeline {
public:
    AudioPipeline();
    ~AudioPipeline();

    esp_err_t initialize(const AudioPipelineConfig &config,
                         std::function<void()> playback_finished_callback,
                         std::function<void(uint8_t)> playback_level_callback = {});
    esp_err_t start();
    void stop();

    bool read_upstream(int16_t *pcm, TickType_t timeout);
    /* The terminal is half duplex: the microphone stays closed while a reply
     * plays and for a short settle time after it, so the speaker never
     * reaches the server as speech. */
    bool upstream_allowed() const;
    void clear_upstream();

    void begin_playback(uint32_t source_sample_rate);
    void enqueue_playback(const uint8_t *pcm, size_t byte_count);
    void end_playback();
    bool cancel_playback();
    bool playback_active() const;
    /* Mono samples actually written to the speaker since begin_playback.
     * Zero padding that covers an underrun is not source audio and is not
     * counted, so the value maps back to a position in the stream. */
    uint32_t playback_position_ms() const;

private:
    static constexpr size_t PLAYBACK_BLOCK_SAMPLES = 480;
    static constexpr size_t UPSTREAM_BUFFER_BYTES = 9600;
    /* Hold a whole turn so a slow 2.4 GHz link cannot overflow the queue. */
    static constexpr size_t PLAYBACK_BUFFER_BYTES = 1440000;
    static constexpr EventBits_t CAPTURE_STOPPED = 1U << 0;
    static constexpr EventBits_t PLAYBACK_STOPPED = 1U << 1;

    AudioPipelineConfig config_;
    std::function<void()> playback_finished_callback_;
    /* Called from the playback task with the RMS of each 20 ms block.
     * The callee must only do an atomic store, never touch LVGL. */
    std::function<void(uint8_t)> playback_level_callback_;
    StreamBufferHandle_t upstream_buffer_ = nullptr;
    StreamBufferHandle_t playback_buffer_ = nullptr;
    StaticStreamBuffer_t *playback_buffer_struct_ = nullptr;
    uint8_t *playback_buffer_storage_ = nullptr;
    SemaphoreHandle_t upstream_mutex_ = nullptr;
    SemaphoreHandle_t upstream_ready_ = nullptr;
    SemaphoreHandle_t playback_mutex_ = nullptr;
    EventGroupHandle_t stopped_events_ = nullptr;
    TaskHandle_t capture_task_ = nullptr;
    TaskHandle_t playback_task_ = nullptr;
    std::atomic<bool> initialized_{false};
    std::atomic<bool> running_{false};
    std::atomic<bool> playback_active_{false};
    std::atomic<bool> playback_ended_{false};
    std::atomic<bool> playback_started_{false};
    std::atomic<uint32_t> playback_sample_rate_{BOARD_AUDIO_SAMPLE_RATE};
    std::atomic<uint32_t> playback_generation_{0};
    std::atomic<uint64_t> played_samples_{0};
    size_t playback_prebuffer_bytes_ = 0;
    TickType_t playback_begin_tick_ = 0;
    TickType_t last_playback_write_ = 0;
    /* Tick at which the microphone may open again; written by the playback
     * task and the control task, read by the sender task. */
    std::atomic<TickType_t> upstream_open_tick_{0};

    void capture_loop();
    void playback_loop();
    void finish_playback(uint32_t generation);
    static void capture_task_entry(void *argument);
    static void playback_task_entry(void *argument);
};
