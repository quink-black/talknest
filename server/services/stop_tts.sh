#!/usr/bin/env bash
# Stop ASR (port 8001). Usage: stop_asr.sh | stop_tts.sh
# (dispatches on script name).
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "$(basename "$0")" in
    stop_asr.sh) PORT=8001 ;;
    stop_tts.sh) PORT=8002 ;;
    *) echo "unknown dispatch name" >&2; exit 1 ;;
esac
exec "$SCRIPT_DIR/../../scripts/free_port.sh" "$PORT"
