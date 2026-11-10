#!/usr/bin/env bash
# Build and flash the ESP32-S3 firmware in one step.
#
#   scripts/flash_firmware.sh [PORT]
#
# PORT defaults to TALKNEST_FLASH_PORT, or /dev/ttyACM0 when that is unset.
# TALKNEST_IDF_ENV points at the ESP-IDF activation script. When idf.py is
# already on PATH, that environment is used as-is. Both values are read from
# local.env, which is git-ignored; see local.env.example.
#
# Flashing overwrites the image already on the device. No backup is taken.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=local_env.sh
. "$REPO_ROOT/scripts/local_env.sh"

PORT="${1:-${TALKNEST_FLASH_PORT:-/dev/ttyACM0}}"

if [ ! -e "$PORT" ]; then
    echo "flash_firmware: serial port does not exist: $PORT" >&2
    exit 1
fi

if ! command -v idf.py >/dev/null 2>&1; then
    IDF_ENV="${TALKNEST_IDF_ENV:-${IDF_PATH:+$IDF_PATH/export.sh}}"
    if [ -z "${IDF_ENV:-}" ] || [ ! -f "$IDF_ENV" ]; then
        echo "flash_firmware: set TALKNEST_IDF_ENV in local.env to the ESP-IDF activation script" >&2
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$IDF_ENV"
fi

if ! command -v idf.py >/dev/null 2>&1; then
    echo "flash_firmware: idf.py is unavailable after activating ESP-IDF" >&2
    exit 1
fi

cd "$REPO_ROOT/firmware"
idf.py build
idf.py -p "$PORT" flash
