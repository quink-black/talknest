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
declare -A STOP=(
    [asr]="$REPO_ROOT/server/services/stop_asr.sh"
    [tts]="$REPO_ROOT/server/services/stop_tts.sh"
    [llm]="$REPO_ROOT/server/services/stop_llm.sh"
    [web]="$SCRIPT_DIR/stop_web.sh"
)

if [ "$#" -gt 0 ]; then
    SERVICES=("$@")
fi

for name in "${SERVICES[@]}"; do
    tmux kill-window -t "$SESSION:$name" 2>/dev/null || true
    "${STOP[$name]}" || true
    echo "[tmux_stop] $name stopped"
done
