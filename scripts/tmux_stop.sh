#!/usr/bin/env bash
# Stop talknest services: drop their tmux windows, then run each stop
# script so the port is free even for a service that was started outside
# tmux or that left children behind.
#
# Usage: scripts/tmux_stop.sh [asr|tts|llm|web ...]   (default: all)
set -uo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=local_env.sh
. "$SCRIPT_DIR/local_env.sh"

SESSION="${TALKNEST_SESSION:-talknest}"

SERVICES=(asr tts llm web)
# bash 3.2, the /bin/bash macOS ships, has no associative arrays, so the
# stop-script table is a case statement rather than a map.
stop_script() {
    case "$1" in
        asr) printf '%s\n' "$REPO_ROOT/server/services/stop_asr.sh" ;;
        tts) printf '%s\n' "$REPO_ROOT/server/services/stop_tts.sh" ;;
        llm) printf '%s\n' "$REPO_ROOT/server/services/stop_llm.sh" ;;
        web) printf '%s\n' "$SCRIPT_DIR/stop_web.sh" ;;
    esac
}

if [ "$#" -gt 0 ]; then
    SERVICES=("$@")
fi

for name in "${SERVICES[@]}"; do
    tmux kill-window -t "$SESSION:$name" 2>/dev/null || true
    "$(stop_script "$name")" || true
    echo "[tmux_stop] $name stopped"
done
