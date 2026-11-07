#pragma once

#include <atomic>
#include <cstdint>
#include <functional>

#include "avatar_frames.h"
#include "avatar_logic.hpp"
#include "esp_err.h"
#include "lvgl.h"

/* Dialogue-driven avatar in a layered underwater scene. The character is
 * the only permanent subject; ambient shapes, one status indicator and one
 * contextual hint frame it without exposing connection diagnostics.
 *
 * Threading contract: every method except initialize()/suspend()/resume()
 * may be called from any task and only touches atomics. The LVGL timer
 * (on_timer) is the sole place that calls lv_*. */
class UiAvatar {
public:
    explicit UiAvatar(std::function<void()> interrupt_callback = {});

    esp_err_t initialize(
        std::function<void()> interrupt_callback = {},
        std::function<int(int)> volume_change_callback = {},
        std::function<void()> volume_commit_callback = {});
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
    std::function<int(int)> volume_change_callback_;
    std::function<void()> volume_commit_callback_;
    lv_obj_t *image_ = nullptr;
    lv_obj_t *status_panel_ = nullptr;
    lv_obj_t *status_label_ = nullptr;
    lv_obj_t *activity_[3] = {};
    lv_obj_t *ripples_[3] = {};
    lv_obj_t *touch_ripple_ = nullptr;
    lv_obj_t *hint_label_ = nullptr;
    lv_obj_t *message_label_ = nullptr;
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
    // Set by the touch event (any task), consumed by the LVGL timer. -1 means
    // no pending volume to display.
    std::atomic<int> volume_display_{-1};

    // LVGL-task-only state. All deadlines are lv_tick_get() milliseconds.
    uint32_t now_ms_ = 0;
    uint32_t blink_at_ms_ = 0;
    uint32_t tap_until_ms_ = 0;
    uint32_t hearing_until_ms_ = 0;
    uint32_t message_until_ms_ = 0;
    uint32_t last_envelope_ms_ = 0;
    uint32_t last_envelope_seq_ = 0;
    uint32_t last_activity_ms_ = 0;
    int current_frame_ = -1;
    int visible_state_ = -1;
    avatar_logic::TouchGesture gesture_{};
    avatar_logic::VolumeSwipe volume_swipe_{};
    bool can_interrupt_ = false;
    bool volume_window_ = false;
    bool interrupt_latched_ = false;
    bool display_locked_ = false;
    bool hearing_active_ = false;

    void apply_events();
    void apply_state(avatar_logic::State state);
    void apply_activity(avatar_logic::State state, uint8_t rms);
    void apply_ripples(uint8_t rms);
    void update_gesture(avatar_logic::TouchEvent event, int16_t x, int16_t y);
    void apply_frame(int frame);
    void apply_pose(int32_t scale_y, int32_t dx, int32_t dy);
    static void on_timer(lv_timer_t *timer);
    static void on_screen_touch(lv_event_t *event);
    static void on_screen_clicked(lv_event_t *event);
};
