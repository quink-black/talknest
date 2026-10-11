#!/usr/bin/env bash
# Stop ASR or TTS: whatever holds that service's port (see
# scripts/ports.sh). Usage: stop_asr.sh | stop_tts.sh (dispatches on script
# name).
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/ports.sh
. "$REPO_ROOT/scripts/ports.sh"
case "$(basename "$0")" in
    stop_asr.sh) PORT="$ASR_PORT" ;;
    stop_tts.sh) PORT="$TTS_PORT" ;;
    *) echo "unknown dispatch name" >&2; exit 1 ;;
esac
exec "$SCRIPT_DIR/../../scripts/free_port.sh" "$PORT"
