#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

namespace avatar_logic {

/* Dialogue states as seen on the wire. "hearing" is a transient barge-in
 * override, not a peer of the other four, so it maps to a short reaction.
 * Waiting covers the client's own connection phases (connecting,
 * reconnecting, sleeping): the session is not up and the screen must not
 * claim readiness. */
enum class State : uint8_t {
    Listening,
    Thinking,
    Speaking,
    Hearing,
    Waiting,
    Idle,
};

State parse_state(const char *value);

/* Pick one of the four expression frames from the current state, the
 * playback envelope and the blink timer. Kept free of LVGL so the host
 * tests can exercise every branch. */
struct FrameInput {
    State state;
    uint8_t envelope;      // 0..255, RMS of the 20 ms playback block
    bool blinking;         // eyes closed for this animation tick
    bool playing;          // audio is actually being written to I2S
};

int select_frame(const FrameInput &in);

/* Continuous pose parameters, in the units LVGL expects:
 * scale is 256 = 100%, translate is pixels. */
struct Pose {
    int32_t scale_y;
    int32_t translate_x;
    int32_t translate_y;
};

struct PoseInput {
    State state;
    uint8_t envelope;
    uint32_t elapsed_ms;   // wall-clock time, not animation frames
};

Pose compute_pose(const PoseInput &in);

constexpr uint32_t INTERRUPT_HOLD_MS = 600;
constexpr int16_t TOUCH_EDGE_PX = 20;
constexpr int16_t TOUCH_MOVE_CANCEL_PX = 24;

enum class TouchEvent : uint8_t {
    Press,
    Move,
    Release,
    Tick,
};

enum class TouchResult : uint8_t {
    None,
    Interrupt,
};

struct TouchGesture {
    bool active = false;
    bool triggered = false;
    uint32_t started_ms = 0;
    int16_t start_x = 0;
    int16_t start_y = 0;
    int16_t x = 0;
    int16_t y = 0;
};

struct TouchSample {
    TouchEvent event;
    int16_t x;
    int16_t y;
    uint32_t elapsed_ms;
    bool enabled;
    int16_t screen_width;
    int16_t screen_height;
};

TouchResult update_touch_gesture(TouchGesture &gesture, const TouchSample &sample);
uint8_t touch_progress(const TouchGesture &gesture, uint32_t elapsed_ms);

} // namespace avatar_logic
