#include "avatar_logic.hpp"

#include <cstring>

#include "avatar_frames.h"

namespace avatar_logic {
namespace {

/* Simple deterministic phase from the tick; no float, no sin table needed
 * on the host-testable side. */
int8_t breath(int32_t tick)
{
    // Triangle wave with a 2.4 s period at 30 ms per tick.
    const int32_t period = 80;
    const int32_t phase = tick % period;
    const int32_t half = period / 2;
    const int32_t v = phase < half ? phase : period - phase;
    return static_cast<int8_t>((v * 12) / half); // 0..12
}

} // namespace

State parse_state(const char *value)
{
    if (value == nullptr) {
        return State::Idle;
    }
    if (std::strcmp(value, "listening") == 0) {
        return State::Listening;
    }
    if (std::strcmp(value, "thinking") == 0 ||
        std::strcmp(value, "preparing") == 0) {
        return State::Thinking;
    }
    if (std::strcmp(value, "speaking") == 0) {
        return State::Speaking;
    }
    if (std::strcmp(value, "hearing") == 0) {
        return State::Hearing;
    }
    return State::Idle;
}

int select_frame(const FrameInput &in)
{
    const int eyes_closed = in.blinking ? AVATAR_FRAME_BLINK
                                        : AVATAR_FRAME_IDLE;
    // Mouth opens only while audio is actually playing and the envelope is
    // above the noise floor; a silent speaking state keeps the smile.
    const bool mouth_open = in.playing && in.envelope >= 24;
    return mouth_open ? eyes_closed + 1 : eyes_closed;
}

Pose compute_pose(const PoseInput &in)
{
    Pose pose = {256, 0, 0};
    const int8_t b = breath(static_cast<int32_t>(in.tick));

    // Breathing is always present: the character is never frozen.
    pose.scale_y = 256 + b;

    switch (in.state) {
    case State::Listening:
        // Lean slightly toward the child, with a slow sway.
        pose.translate_x = static_cast<int32_t>((in.tick / 20) % 3) - 1;
        pose.translate_y = -2;
        break;
    case State::Thinking:
        // Look up / tilt away while composing a reply.
        pose.translate_x = 4;
        pose.translate_y = -5 - b / 3;
        break;
    case State::Speaking:
        // Bounce with the voice, on top of the breathing scale.
        pose.scale_y += static_cast<int32_t>(in.envelope) / 12;
        pose.translate_y = -static_cast<int32_t>(in.envelope) / 32;
        break;
    case State::Hearing:
        // Brief surprised lean when the child cuts in.
        pose.translate_x = -6;
        pose.translate_y = -8;
        pose.scale_y += 6;
        break;
    case State::Idle:
    default:
        break;
    }
    return pose;
}

} // namespace avatar_logic
