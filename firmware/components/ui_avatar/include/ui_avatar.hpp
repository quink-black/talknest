#pragma once

#include <atomic>
#include <cstdint>
#include <functional>

#include "avatar_frames.h"
#include "avatar_logic.hpp"
#include "esp_err.h"
#include "lvgl.h"

/* Dialogue-driven avatar. The character is the only permanent element on
 * the screen. Connection and configuration diagnostics stay in the serial
 * log; a short error message briefly overlays the screen when needed.
 *
 * Threading contract: every method except initialize()/suspend()/resume()
 * may be called from any task and only touches atomics. The LVGL timer
 * (on_timer) is the sole place that calls lv_*. */
class UiAvatar {
public:
    explicit UiAvatar(std::function<void()> interrupt_callback = {});

    esp_err_t initialize(std::function<void()> interrupt_callback = {});
    void set_state(const char *state);
    void set_envelope(uint8_t rms)
    {
        envelope_.store(rms, std::memory_order_relaxed);
        envelope_seq_.fetch_add(1, std::memory_order_relaxed);
    }
    void on_audio_start();
    void on_audio_end();
    void set_error(const char *message);
    void set_sleeping(bool sleeping);
    void suspend();
    void resume();

private:
    static constexpr size_t TEXT_SIZE = 96;
    /* If no fresh envelope has arrived within this wall-clock window, the
     * mouth closes. This recovers every path that stops playback without a
     * normal playback_finished (cancel, disconnect, sleep). */

    std::function<void()> interrupt_callback_;
    lv_obj_t *image_ = nullptr;
    lv_obj_t *message_label_ = nullptr;
    lv_obj_t *thought_ = nullptr;
    lv_obj_t *touch_layer_ = nullptr;
    lv_timer_t *timer_ = nullptr;
    lv_image_dsc_t frames_[AVATAR_FRAME_COUNT] = {};

    // Written by other tasks, read by the LVGL timer.
    std::atomic<uint8_t> envelope_{0};
    std::atomic<uint32_t> envelope_seq_{0};
    std::atomic<uint8_t> state_{(uint8_t)avatar_logic::State::Idle};
    std::atomic<bool> playing_{false};
    char error_text_[TEXT_SIZE] = {};
    std::atomic<bool> error_pending_{false};

    // LVGL-task-only state. All deadlines are lv_tick_get() milliseconds.
    uint32_t now_ms_ = 0;
    uint32_t blink_at_ms_ = 0;
    uint32_t tap_until_ms_ = 0;
    uint32_t hearing_until_ms_ = 0;
    uint32_t message_until_ms_ = 0;
    uint32_t last_envelope_ms_ = 0;
    uint32_t last_envelope_seq_ = 0;
    int current_frame_ = -1;
    bool can_interrupt_ = false;
    bool display_locked_ = false;
    bool hearing_active_ = false;

    void apply_events();
    void apply_frame(int frame);
    void apply_pose(int32_t scale_y, int32_t dx, int32_t dy);
    static void on_timer(lv_timer_t *timer);
    static void on_screen_clicked(lv_event_t *event);
};
