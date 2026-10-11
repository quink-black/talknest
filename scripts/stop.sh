#!/usr/bin/env bash
# Stop talknest services however they were started: stop the systemd unit,
# drop the tmux window, then free the port. Each step is a no-op for a
# service that is not running that way, so one command covers services
# started by scripts/start.sh, by tmux, or by hand.
#
# Usage: scripts/stop.sh [asr|tts|llm|web ...]   (default: every service)
#
# The unit comes first on purpose: killing the listener of a running unit
# would only trigger its Restart=on-failure and race the restart for the
# port.
set -uo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/services.sh
. "$SCRIPT_DIR/lib/services.sh"

if [ "$#" -gt 0 ]; then
    SERVICES=("$@")
else
    SERVICES=("${TN_SERVICES[@]}")
fi

tn_stop_systemd_units "${SERVICES[@]}"

if command -v tmux >/dev/null 2>&1; then
    SESSION="${TALKNEST_SESSION:-talknest}"
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        for name in "${SERVICES[@]}"; do
            tmux kill-window -t "$SESSION:$name" 2>/dev/null || true
        done
    fi
fi

for name in "${SERVICES[@]}"; do
    "$(tn_stop_script "$name")" || true
    tn_log "$name stopped"
done
