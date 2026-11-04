#include <cassert>
#include <cstring>
#include <iostream>

#include "avatar_frames.h"
#include "avatar_logic.hpp"

using namespace avatar_logic;

int main()
{
    // State parsing covers the five wire values plus unknown/empty.
    assert(parse_state("listening") == State::Listening);
    assert(parse_state("hearing") == State::Hearing);
    assert(parse_state("thinking") == State::Thinking);
    assert(parse_state("preparing") == State::Thinking);
    assert(parse_state("speaking") == State::Speaking);
    assert(parse_state("idle") == State::Idle);
    assert(parse_state(nullptr) == State::Idle);
    assert(parse_state("") == State::Idle);

    // Frame selection: mouth opens only with real audio above the floor.
    assert(select_frame({State::Idle, 0, false, false}) == AVATAR_FRAME_IDLE);
    assert(select_frame({State::Speaking, 0, false, true}) == AVATAR_FRAME_IDLE);
    assert(select_frame({State::Speaking, 100, false, true}) == AVATAR_FRAME_TALK);
    assert(select_frame({State::Speaking, 100, false, false}) == AVATAR_FRAME_IDLE);
    assert(select_frame({State::Listening, 0, true, false}) == AVATAR_FRAME_BLINK);
    assert(select_frame({State::Speaking, 100, true, true}) == AVATAR_FRAME_BLINK_TALK);
    assert(select_frame({State::Idle, 200, true, false}) == AVATAR_FRAME_BLINK);
    assert(select_frame({State::Speaking, 23, false, true}) == AVATAR_FRAME_IDLE);
    assert(select_frame({State::Speaking, 24, false, true}) == AVATAR_FRAME_TALK);

    // Every state produces a distinct, non-frozen pose; breathing varies
    // across ticks so the character is never static.
    Pose a = compute_pose({State::Listening, 0, 0});
    Pose b = compute_pose({State::Listening, 0, 20});
    assert(a.scale_y != b.scale_y);
    assert(compute_pose({State::Thinking, 0, 0}).translate_x == 4);
    assert(compute_pose({State::Hearing, 0, 0}).translate_y < 0);
    assert(compute_pose({State::Speaking, 200, 0}).scale_y >
           compute_pose({State::Speaking, 0, 0}).scale_y);
    assert(compute_pose({State::Speaking, 200, 0}).translate_y < 0);
    for (uint32_t t = 0; t < 200; ++t) {
        Pose p = compute_pose({State::Idle, 0, t});
        assert(p.scale_y >= 256 && p.scale_y <= 280);
    }

    std::cout << "avatar_logic tests passed" << std::endl;
    return 0;
}
