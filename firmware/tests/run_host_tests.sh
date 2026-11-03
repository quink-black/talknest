#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$ROOT/build/host-tests"
mkdir -p "$BUILD_DIR"
"${CXX:-c++}" -std=c++17 -Wall -Wextra -Werror \
    -I"$ROOT/components/audio_pipeline/include" \
    "$ROOT/tests/test_pcm_format.cpp" -o "$BUILD_DIR/test_pcm_format"
"$BUILD_DIR/test_pcm_format"
