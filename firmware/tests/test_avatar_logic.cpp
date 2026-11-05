#include <cassert>
#include <cstring>
#include <iostream>

#include "avatar_frames.h"
#include "avatar_logic.hpp"

using namespace avatar_logic;

int main()
{
    // State parsing covers the wire values, the client's own connection
    // phases, and unknown/empty.
    assert(parse_state("listening") == State::Listening);
    assert(parse_state("hearing") == State::Hearing);
    assert(parse_state("thinking") == State::Thinking);
    assert(parse_state("preparing") == State::Thinking);
    assert(parse_state("speaking") == State::Speaking);
    assert(parse_state("connecting") == State::Waiting);
    assert(parse_state("reconnecting") == State::Waiting);
    assert(parse_state("sleeping") == State::Waiting);
    // The callers send capitalised phase names; the mapping must not
    // drift apart from those literals (regression for the case-sensitive
    // strcmp that made the Waiting branch unreachable).
    assert(parse_state("Connecting") == State::Waiting);
    assert(parse_state("Reconnecting") == State::Waiting);
    assert(parse_state("Sleeping") == State::Waiting);
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
    // with wall-clock time so the character is never static.
    Pose a = compute_pose({State::Listening, 0, 0});
    Pose b = compute_pose({State::Listening, 0, 600});
    assert(a.scale_y != b.scale_y);
    assert(compute_pose({State::Thinking, 0, 0}).translate_x == 4);
    assert(compute_pose({State::Hearing, 0, 0}).translate_y < 0);
    assert(compute_pose({State::Speaking, 200, 0}).scale_y >
           compute_pose({State::Speaking, 0, 0}).scale_y);
    assert(compute_pose({State::Speaking, 200, 0}).translate_y < 0);
    // Breathing repeats on a 2400 ms period regardless of frame rate.
    assert(compute_pose({State::Idle, 0, 100}).scale_y ==
           compute_pose({State::Idle, 0, 2500}).scale_y);
    for (uint32_t ms = 0; ms < 4800; ms += 37) {
        Pose p = compute_pose({State::Idle, 0, ms});
        assert(p.scale_y >= 256 && p.scale_y <= 280);
    }

    std::cout << "avatar_logic tests passed" << std::endl;
    return 0;
}
