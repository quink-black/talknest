#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$ROOT/build/host-tests"
mkdir -p "$BUILD_DIR"
CXX="${CXX:-c++}"
FLAGS=(-std=c++17 -Wall -Wextra -Werror)

"$CXX" "${FLAGS[@]}" -I"$ROOT/components/audio_pipeline/include" \
    "$ROOT/tests/test_pcm_format.cpp" -o "$BUILD_DIR/test_pcm_format"
"$CXX" "${FLAGS[@]}" -I"$ROOT/components/talknest_client/include" \
    "$ROOT/tests/test_profile_logic.cpp" -o "$BUILD_DIR/test_profile_logic"
"$CXX" "${FLAGS[@]}" -I"$ROOT/components/ui_avatar/include" \
    "$ROOT/tests/test_avatar_logic.cpp" \
    "$ROOT/components/ui_avatar/avatar_logic.cpp" \
    -o "$BUILD_DIR/test_avatar_logic"
"$CXX" "${FLAGS[@]}" -I"$ROOT/components/ui_avatar/include" \
    -I"$ROOT/components/volume/include" \
    "$ROOT/tests/test_volume.cpp" \
    "$ROOT/components/ui_avatar/avatar_logic.cpp" \
    -o "$BUILD_DIR/test_volume"
"$BUILD_DIR/test_pcm_format"
"$BUILD_DIR/test_profile_logic"
"$BUILD_DIR/test_avatar_logic"
"$BUILD_DIR/test_volume"
