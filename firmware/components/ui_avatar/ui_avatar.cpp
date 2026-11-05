#include "ui_avatar.hpp"

#include <cstring>

#include "avatar_frames.h"
#include "bsp/esp32_s3_touch_amoled_2_06.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_lvgl_port.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

extern const uint8_t avatar_frames_bin_start[] asm("_binary_avatar_frames_bin_start");
extern const uint8_t avatar_frames_bin_end[] asm("_binary_avatar_frames_bin_end");

namespace {
constexpr char TAG[] = "avatar";
constexpr uint32_t TIMER_MS = 30;        // LV_DEF_REFR_PERIOD
constexpr uint32_t MESSAGE_MS = 3000;    // short error message on screen
constexpr uint32_t HEARING_MS = 750;     // barge-in reaction before fallback
constexpr uint32_t BLINK_MS = 150;       // how long eyes stay closed
constexpr uint32_t TAP_MS = 240;         // tap bounce
constexpr uint32_t ENVELOPE_TIMEOUT_MS = 150;
constexpr uint32_t ACTIVITY_MS = 100;
constexpr int SCREEN_W = 410;
constexpr int SCREEN_H = 502;
constexpr uint32_t SCREEN_TOP = 0x142337;
constexpr uint32_t SCREEN_BOTTOM = 0x0B121C;
constexpr uint32_t PANEL_BG = 0x1B2D42;
constexpr uint32_t HINT_COLOR = 0xA9BCD0;

lv_obj_t *create_shape(lv_obj_t *parent, int width, int height,
                       uint32_t color, lv_opa_t opacity)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_set_size(obj, width, height);
    lv_obj_set_style_bg_color(obj, lv_color_hex(color), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(obj, opacity, LV_PART_MAIN);
    lv_obj_set_style_border_width(obj, 0, LV_PART_MAIN);
    lv_obj_set_style_outline_width(obj, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(obj, 0, LV_PART_MAIN);
    lv_obj_set_clickable(obj, false);
    return obj;
}

uint32_t next_blink_ms(uint32_t now)
{
    return now + 1500 + ((now * 37u) % 3000u); // 1.5 s .. 4.5 s
}

avatar_logic::State load_state(const std::atomic<uint8_t> &cell)
{
    return static_cast<avatar_logic::State>(cell.load(std::memory_order_relaxed));
}
} // namespace

UiAvatar::UiAvatar(std::function<void()> interrupt_callback)
    : interrupt_callback_(std::move(interrupt_callback))
{
}

esp_err_t UiAvatar::initialize(std::function<void()> interrupt_callback)
{
    if (interrupt_callback) {
        interrupt_callback_ = std::move(interrupt_callback);
    }
    if (bsp_display_start() == nullptr) {
        ESP_LOGE(TAG, "Display initialization failed");
        return ESP_FAIL;
    }
    const size_t total = static_cast<size_t>(avatar_frames_bin_end -
                                             avatar_frames_bin_start);
    if (total < AVATAR_FRAME_BYTES * AVATAR_FRAME_COUNT) {
        ESP_LOGE(TAG, "Avatar frames are truncated: %u bytes",
                 static_cast<unsigned>(total));
        return ESP_ERR_INVALID_SIZE;
    }
    for (int i = 0; i < AVATAR_FRAME_COUNT; ++i) {
        frames_[i].header.magic = LV_IMAGE_HEADER_MAGIC;
        frames_[i].header.cf = LV_COLOR_FORMAT_RGB565A8;
        frames_[i].header.w = AVATAR_FRAME_W;
        frames_[i].header.h = AVATAR_FRAME_H;
        frames_[i].header.stride = AVATAR_FRAME_STRIDE;
        frames_[i].data_size = AVATAR_FRAME_BYTES;
        frames_[i].data = avatar_frames_bin_start + i * AVATAR_FRAME_BYTES;
    }

    if (!bsp_display_lock(2000)) {
        return ESP_ERR_TIMEOUT;
    }

    lv_obj_t *screen = lv_screen_active();
    // Force an opaque gradient so no power-on RAM can show through. The
    // lighter upper field separates the transparent silhouette from the
    // background without adding a bitmap or an animated full-screen layer.
    lv_obj_set_style_bg_color(screen, lv_color_hex(SCREEN_TOP), LV_PART_MAIN);
    lv_obj_set_style_bg_grad_color(screen, lv_color_hex(SCREEN_BOTTOM),
                                   LV_PART_MAIN);
    lv_obj_set_style_bg_grad_dir(screen, LV_GRAD_DIR_VER, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, LV_PART_MAIN);

    lv_obj_t *stage = create_shape(screen, 320, 104, 0x21415A, LV_OPA_40);
    lv_obj_set_style_radius(stage, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_align(stage, LV_ALIGN_CENTER, 0, 143);

    // Static ambient shapes keep idle animation cost with the character.
    constexpr int bubbles[][4] = {
        {30, 142, 8, 45}, {374, 178, 6, 40}, {24, 286, 5, 35},
        {380, 314, 9, 40}, {48, 366, 4, 30}, {358, 104, 4, 30},
    };
    for (const auto &bubble : bubbles) {
        lv_obj_t *obj = create_shape(screen, bubble[2], bubble[2], 0x79CFFF,
                                     static_cast<lv_opa_t>(bubble[3]));
        lv_obj_set_style_radius(obj, LV_RADIUS_CIRCLE, LV_PART_MAIN);
        lv_obj_align(obj, LV_ALIGN_TOP_LEFT, bubble[0], bubble[1]);
    }

    // The character has a transparent silhouette (RGB565A8), so it blends
    // into the scene instead of showing a square edge.
    image_ = lv_image_create(screen);
    lv_obj_align(image_, LV_ALIGN_CENTER, 0, -2);
    lv_image_set_src(image_, &frames_[AVATAR_FRAME_IDLE]);
    lv_image_set_inner_align(image_, LV_IMAGE_ALIGN_CENTER);
    current_frame_ = AVATAR_FRAME_IDLE;

    status_panel_ = create_shape(screen, 268, 52, PANEL_BG, LV_OPA_90);
    lv_obj_set_style_radius(status_panel_, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_border_width(status_panel_, 1, LV_PART_MAIN);
    lv_obj_set_style_border_color(status_panel_, lv_color_hex(HINT_COLOR),
                                  LV_PART_MAIN);
    lv_obj_set_style_border_opa(status_panel_, LV_OPA_70, LV_PART_MAIN);
    lv_obj_align(status_panel_, LV_ALIGN_TOP_MID, 0, 18);

    for (int i = 0; i < 3; ++i) {
        activity_[i] = create_shape(status_panel_, 8, 8, HINT_COLOR, LV_OPA_60);
        lv_obj_set_style_radius(activity_[i], LV_RADIUS_CIRCLE, LV_PART_MAIN);
        lv_obj_align(activity_[i], LV_ALIGN_LEFT_MID, 22 + i * 12, 0);
    }

    status_label_ = lv_label_create(status_panel_);
    lv_obj_set_style_text_font(status_label_, &lv_font_montserrat_24,
                               LV_PART_MAIN);
    lv_obj_set_style_text_color(status_label_, lv_color_hex(HINT_COLOR),
                                LV_PART_MAIN);
    lv_obj_align(status_label_, LV_ALIGN_LEFT_MID, 64, 0);
    lv_obj_set_clickable(status_label_, false);

    hint_label_ = lv_label_create(screen);
    lv_obj_set_width(hint_label_, 380);
    lv_obj_set_style_text_font(hint_label_, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(hint_label_, lv_color_hex(HINT_COLOR),
                                LV_PART_MAIN);
    lv_obj_set_style_text_align(hint_label_, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_align(hint_label_, LV_ALIGN_BOTTOM_MID, 0, -24);
    lv_obj_set_clickable(hint_label_, false);

    // Transient information lives on the top layer: it never reserves space
    // and only appears when there is something to say.
    lv_obj_t *overlay = lv_layer_top();

    // Only short ASCII messages go here. Reply sentences are Chinese and the
    // firmware has no CJK font, so they stay in the serial log.
    message_label_ = lv_label_create(overlay);
    lv_obj_set_width(message_label_, 380);
    lv_obj_set_style_text_font(message_label_, &lv_font_montserrat_24,
                               LV_PART_MAIN);
    lv_obj_set_style_text_color(message_label_, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_text_align(message_label_, LV_TEXT_ALIGN_CENTER,
                                LV_PART_MAIN);
    lv_label_set_long_mode(message_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_bg_color(message_label_, lv_color_hex(PANEL_BG),
                              LV_PART_MAIN);
    lv_obj_set_style_bg_opa(message_label_, LV_OPA_90, LV_PART_MAIN);
    lv_obj_set_style_radius(message_label_, 12, LV_PART_MAIN);
    lv_obj_set_style_pad_ver(message_label_, 8, LV_PART_MAIN);
    lv_obj_align(message_label_, LV_ALIGN_BOTTOM_MID, 0, -20);
    lv_label_set_text(message_label_, "");
    lv_obj_set_hidden(message_label_, true);
    lv_obj_set_clickable(message_label_, false);

    // The click layer lives on the same top layer and is raised above the
    // labels, so it owns every tap regardless of label hit-testing.
    touch_layer_ = lv_obj_create(overlay);
    lv_obj_set_size(touch_layer_, SCREEN_W, SCREEN_H);
    lv_obj_set_style_bg_opa(touch_layer_, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_width(touch_layer_, 0, LV_PART_MAIN);
    lv_obj_set_style_outline_width(touch_layer_, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(touch_layer_, 0, LV_PART_MAIN);
    lv_obj_align(touch_layer_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_move_foreground(touch_layer_);
    lv_obj_add_event_cb(touch_layer_, on_screen_clicked, LV_EVENT_CLICKED, this);

    now_ms_ = lv_tick_get();
    blink_at_ms_ = next_blink_ms(now_ms_);
    last_envelope_ms_ = now_ms_;
    apply_state(load_state(state_));
    lv_obj_invalidate(screen);
    bsp_display_unlock();
    bsp_display_brightness_set(70);

    timer_ = lv_timer_create(on_timer, TIMER_MS, this);
    return ESP_OK;
}

/* ---- cross-task entry points: atomics only ---- */

void UiAvatar::set_state(const char *value)
{
    state_.store(static_cast<uint8_t>(avatar_logic::parse_state(value)),
                 std::memory_order_relaxed);
}

void UiAvatar::on_audio_start()
{
    playing_.store(true, std::memory_order_relaxed);
}

void UiAvatar::on_audio_end()
{
    playing_.store(false, std::memory_order_relaxed);
}

void UiAvatar::set_error(const char *message)
{
    const char *text = message != nullptr ? message : "Error";
    std::strncpy(error_text_, text, sizeof(error_text_) - 1);
    error_text_[sizeof(error_text_) - 1] = '\0';
    error_pending_.store(true, std::memory_order_release);
}

void UiAvatar::set_sleeping(bool sleeping)
{
    bsp_display_brightness_set(sleeping ? 0 : 70);
}

/* ---- LVGL-task-only work ---- */

void UiAvatar::apply_events()
{
    avatar_logic::State state = load_state(state_);
    if (state == avatar_logic::State::Hearing) {
        if (!hearing_active_) {
            hearing_until_ms_ = now_ms_ + HEARING_MS;
            hearing_active_ = true;
        }
    } else {
        hearing_active_ = false;
        hearing_until_ms_ = 0;
    }
    if (hearing_until_ms_ != 0 && now_ms_ >= hearing_until_ms_) {
        hearing_until_ms_ = 0;
        state_.store(static_cast<uint8_t>(avatar_logic::State::Listening),
                     std::memory_order_relaxed);
        state = avatar_logic::State::Listening;
    }
    can_interrupt_ = state == avatar_logic::State::Speaking;

    if (error_pending_.exchange(false, std::memory_order_acquire)) {
        lv_label_set_text(message_label_, error_text_);
        lv_obj_set_hidden(message_label_, false);
        lv_obj_set_hidden(hint_label_, true);
        message_until_ms_ = now_ms_ + MESSAGE_MS;
    } else if (message_until_ms_ != 0 && now_ms_ >= message_until_ms_) {
        message_until_ms_ = 0;
        lv_label_set_text(message_label_, "");
        lv_obj_set_hidden(message_label_, true);
        lv_obj_set_hidden(hint_label_, false);
    }

    if (visible_state_ != static_cast<int>(state)) {
        apply_state(state);
    }
}

void UiAvatar::apply_state(avatar_logic::State state)
{
    const char *status = "HELLO";
    const char *hint = "Say hello";
    uint32_t color = HINT_COLOR;

    switch (state) {
    case avatar_logic::State::Listening:
        status = "LISTENING";
        hint = "Talk to me";
        color = 0x72E4C1;
        break;
    case avatar_logic::State::Thinking:
        status = "THINKING";
        hint = "One moment...";
        color = 0xFFBF69;
        break;
    case avatar_logic::State::Speaking:
        status = "SPEAKING";
        hint = "Tap to stop";
        color = 0xC5B5FF;
        break;
    case avatar_logic::State::Hearing:
        status = "HEARD YOU";
        hint = "Talk to me";
        color = 0x79CFFF;
        break;
    case avatar_logic::State::Waiting:
        // The session is not up; nothing here may claim readiness and no
        // action is available to promise.
        status = "WAIT";
        hint = "";
        color = HINT_COLOR;
        break;
    case avatar_logic::State::Idle:
    default:
        break;
    }

    visible_state_ = static_cast<int>(state);
    lv_label_set_text(status_label_, status);
    lv_label_set_text(hint_label_, hint);
    lv_obj_set_style_text_color(status_label_, lv_color_hex(color), LV_PART_MAIN);
    lv_obj_set_style_border_color(status_panel_, lv_color_hex(color), LV_PART_MAIN);
    for (lv_obj_t *obj : activity_) {
        lv_obj_set_style_bg_color(obj, lv_color_hex(color), LV_PART_MAIN);
    }
    last_activity_ms_ = now_ms_ - ACTIVITY_MS;
}

void UiAvatar::apply_activity(avatar_logic::State state, uint8_t rms)
{
    if (now_ms_ - last_activity_ms_ < ACTIVITY_MS) {
        return;
    }
    last_activity_ms_ = now_ms_;

    int heights[3] = {8, 8, 8};
    lv_opa_t opacities[3] = {LV_OPA_60, LV_OPA_60, LV_OPA_60};
    const int phase = static_cast<int>((now_ms_ / 300) % 3);
    switch (state) {
    case avatar_logic::State::Listening:
    case avatar_logic::State::Thinking:
        opacities[phase] = LV_OPA_COVER;
        break;
    case avatar_logic::State::Speaking:
        heights[0] = 8 + static_cast<int>(rms) * 12 / 255;
        heights[1] = 8 + static_cast<int>(rms) * 24 / 255;
        heights[2] = 8 + static_cast<int>(rms) * 16 / 255;
        opacities[0] = opacities[1] = opacities[2] = LV_OPA_COVER;
        break;
    case avatar_logic::State::Hearing:
        heights[0] = heights[1] = heights[2] = 10;
        opacities[0] = opacities[1] = opacities[2] = LV_OPA_COVER;
        break;
    case avatar_logic::State::Idle:
    default:
        opacities[0] = LV_OPA_COVER;
        break;
    }

    for (int i = 0; i < 3; ++i) {
        lv_obj_set_height(activity_[i], heights[i]);
        lv_obj_set_style_bg_opa(activity_[i], opacities[i], LV_PART_MAIN);
    }
}

void UiAvatar::apply_frame(int frame)
{
    if (frame == current_frame_) {
        return;
    }
    current_frame_ = frame;
    lv_image_set_src(image_, &frames_[frame]);
}

void UiAvatar::apply_pose(int32_t scale_y, int32_t dx, int32_t dy)
{
    // A style scale would make LVGL allocate a transform layer sized to the
    // whole image, and the extra SPI bounce buffer fails to allocate at this
    // frame size. Express breathing as a small vertical float instead: a
    // translate only moves coordinates and never needs an intermediate layer.
    int32_t float_y = dy - (scale_y - 256) / 4;
    if (tap_until_ms_ != 0 && now_ms_ < tap_until_ms_) {
        float_y -= 6;
    } else {
        tap_until_ms_ = 0;
    }
    lv_obj_set_style_translate_x(image_, dx, LV_PART_MAIN);
    lv_obj_set_style_translate_y(image_, float_y, LV_PART_MAIN);
}

void UiAvatar::on_timer(lv_timer_t *timer)
{
    auto *self = static_cast<UiAvatar *>(lv_timer_get_user_data(timer));
    if (self == nullptr || self->display_locked_) {
        return;
    }
    // The LVGL task already owns the display lock (lv_timer_handler runs
    // under it), so a zero timeout succeeds here. Never called elsewhere.
    if (!bsp_display_lock(0)) {
        return;
    }

    self->now_ms_ = lv_tick_get();
    self->apply_events();

    // Detect a stalled audio task: any path that stops playback without a
    // normal playback_finished leaves playing_ true, but the envelope stops
    // being refreshed. Close the mouth after a short silence.
    const uint32_t seq = self->envelope_seq_.load(std::memory_order_relaxed);
    if (seq != self->last_envelope_seq_) {
        self->last_envelope_seq_ = seq;
        self->last_envelope_ms_ = self->now_ms_;
    }
    const bool fresh = self->now_ms_ - self->last_envelope_ms_ < ENVELOPE_TIMEOUT_MS;
    const bool playing = self->playing_.load(std::memory_order_relaxed) && fresh;
    const uint8_t rms = playing ?
        self->envelope_.load(std::memory_order_relaxed) : 0;
    const avatar_logic::State state = load_state(self->state_);
    self->apply_activity(state, rms);

    const bool blinking = self->now_ms_ >= self->blink_at_ms_ &&
                          self->now_ms_ < self->blink_at_ms_ + BLINK_MS;
    if (self->now_ms_ >= self->blink_at_ms_ + BLINK_MS) {
        self->blink_at_ms_ = next_blink_ms(self->now_ms_);
    }

    avatar_logic::FrameInput fi = {state, rms, blinking, playing};
    const int frame = avatar_logic::select_frame(fi);
    avatar_logic::PoseInput pi = {state, rms, self->now_ms_};
    const avatar_logic::Pose pose = avatar_logic::compute_pose(pi);
    self->apply_frame(frame);
    self->apply_pose(pose.scale_y, pose.translate_x, pose.translate_y);

    bsp_display_unlock();
}

void UiAvatar::on_screen_clicked(lv_event_t *event)
{
    auto *self = static_cast<UiAvatar *>(lv_event_get_user_data(event));
    if (self == nullptr) {
        return;
    }
    if (self->can_interrupt_ && self->interrupt_callback_) {
        self->interrupt_callback_();
        return;
    }
    // Non-speaking tap: a brief bounce on the character itself.
    self->tap_until_ms_ = self->now_ms_ + TAP_MS;
}

void UiAvatar::suspend()
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

void UiAvatar::resume()
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
