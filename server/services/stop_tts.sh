#!/usr/bin/env bash
# Stop ASR (port 8001). Usage: stop_asr.sh | stop_tts.sh
# (dispatches on script name).
set -u
case "$(basename "$0")" in
    stop_asr.sh) TAG=asr; PORT=8001 ;;
    stop_tts.sh) TAG=tts; PORT=8002 ;;
    *) echo "unknown dispatch name" >&2; exit 1 ;;
esac
if command -v fuser >/dev/null 2>&1; then
    fuser -k "$PORT/tcp" 2>/dev/null || true
fi
echo "[stop_$TAG] port $PORT freed"
