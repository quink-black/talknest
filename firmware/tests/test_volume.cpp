#include <cassert>
#include <iostream>

#include "avatar_logic.hpp"
#include "volume_logic.hpp"

using namespace avatar_logic;

namespace {
TouchSample vtouch(TouchEvent event, int16_t x, int16_t y, uint32_t elapsed_ms,
                   bool enabled)
{
    return {event, x, y, elapsed_ms, enabled, 410, 502};
}
} // namespace

int main()
{
    // Pure volume arithmetic: clamp bounds, notch scaling, and no drift at
    // the extremes even for large step counts.
    assert(volume_logic::clamp(-5) == 0);
    assert(volume_logic::clamp(0) == 0);
    assert(volume_logic::clamp(100) == 100);
    assert(volume_logic::clamp(137) == 100);
    assert(volume_logic::apply_steps(80, 0) == 80);
    assert(volume_logic::apply_steps(80, 1) == 85);
    assert(volume_logic::apply_steps(80, -1) == 75);
    assert(volume_logic::apply_steps(80, 4) == 100);
    assert(volume_logic::apply_steps(80, 100) == 100);
    assert(volume_logic::apply_steps(5, -100) == 0);
    assert(volume_logic::apply_steps(0, -1) == 0);
    assert(volume_logic::apply_steps(100, 1) == 100);

    VolumeSwipe swipe;

    // A press outside the 20 px edge margin must not arm the swipe.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 19, 100, 0, true)) == 0);
    assert(!swipe.active);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 100, 19, 0, true)) == 0);
    assert(!swipe.active);

    // A valid press arms, but produces no change by itself.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 200, 300, 0, true)) == 0);
    assert(swipe.active);

    // Timer ticks carry stale coordinates and must be ignored.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Tick, 200, 10, 16, true)) == 0);
    assert(swipe.active);

    // Downward movement is negative (quieter); one notch per 24 px.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 323, 32, true)) == 0);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 324, 48, true)) == -1);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 348, 64, true)) == -1);

    // Upward movement is positive (louder), including a multi-notch jump.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 300, 80, true)) == 2);

    // Large horizontal drift is a different gesture; the swipe is cancelled.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 230, 300, 96, true)) == 0);
    assert(!swipe.active);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 250, 112, true)) == 0);

    // Release always ends the gesture and produces no notch.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 200, 300, 200, true)) == 0);
    assert(swipe.active);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Release, 200, 300, 216, true)) == 0);
    assert(!swipe.active);

    // When playback owns the touch (enabled=false), volume stays inert and
    // any in-progress swipe is cancelled so the two gestures cannot mix.
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 200, 300, 300, true)) == 0);
    assert(swipe.active);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Move, 200, 276, 316, false)) == 0);
    assert(!swipe.active);
    assert(update_volume_swipe(swipe, vtouch(TouchEvent::Press, 200, 300, 400, false)) == 0);
    assert(!swipe.active);

    std::cout << "volume tests passed" << std::endl;
    return 0;
}
