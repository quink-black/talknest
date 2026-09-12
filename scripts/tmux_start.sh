#!/usr/bin/env bash
# Start talknest services in one tmux session, one window per service.
#
# The service scripts themselves know nothing about tmux: each one just
# runs its service in the foreground, so it can equally be started from a
# plain shell, systemd, or a container entrypoint. This helper is the only
# place that decides how those processes are supervised.
#
# Usage: scripts/tmux_start.sh [asr|tts|llm|web ...]
# No argument starts every service and waits for each to answer on its
# health URL before starting the next; LLM comes last so llama.cpp -fit
# sees the VRAM that ASR and TTS already took. Re-running a service
# replaces its window. Only the session named by TALKNEST_SESSION is ever
# touched -- other sessions and the tmux server are left alone.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=local_env.sh
. "$SCRIPT_DIR/local_env.sh"

SESSION="${TALKNEST_SESSION:-talknest}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

SERVICES=(asr tts llm web)
declare -A START=(
    [asr]="$REPO_ROOT/server/services/start_asr.sh"
    [tts]="$REPO_ROOT/server/services/start_tts.sh"
    [llm]="$REPO_ROOT/server/services/start_llm_llamacpp_9b.sh"
    [web]="$SCRIPT_DIR/start_web.sh"
)
declare -A STOP=(
    [asr]="$REPO_ROOT/server/services/stop_asr.sh"
    [tts]="$REPO_ROOT/server/services/stop_tts.sh"
    [llm]="$REPO_ROOT/server/services/stop_llm.sh"
    [web]="$SCRIPT_DIR/stop_web.sh"
)
# Services must come up one at a time: llama.cpp -fit sizes the offload to
# the VRAM that is free *at its own startup*, so starting it before ASR and
# TTS have claimed their pools makes it grab the whole card and OOM the
# host. The default order is the dependency order for that reason.
# start_web.sh enables TLS whenever cert material is configured or the
# dev cert exists, and uvicorn then refuses plain HTTP; the probe has to
# speak the same scheme (-k: the dev certificate is self-signed).
WEB_SCHEME=http
if { [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; } \
   || { [ -z "${TLS_CERT:-}" ] && [ -f "$REPO_ROOT/certs/dev.pem" ] \
        && [ -f "$REPO_ROOT/certs/dev.key" ]; }; then
    WEB_SCHEME=https
fi
declare -A READY=(
    [asr]="http://127.0.0.1:8001/health"
    [tts]="http://127.0.0.1:8002/v1/models"
    [llm]="http://127.0.0.1:9999/health"
    [web]="${WEB_SCHEME}://127.0.0.1:${TALKNEST_PORT:-8890}/"
)
READY_TIMEOUT="${TALKNEST_READY_TIMEOUT:-600}"

if [ "$#" -gt 0 ]; then
    SERVICES=("$@")
fi

# A tmux window inherits the tmux server's environment, not the caller's,
# so overrides given on the command line would be dropped whenever the
# session already exists.
TMUX_ENV=()
while IFS='=' read -r name value; do
    TMUX_ENV+=( -e "$name=$value" )
done < <(env | grep -E '^(TALKNEST_|ASR_|TN9B_|QWEN38_|TLS_)' || true)

# Services running under systemd would fight for the ports: the stop
# scripts below fuser-kill them into Restart=on-failure loops. Stop the
# units cleanly first; a no-op when none are running.
if command -v systemctl >/dev/null 2>&1; then
    for svc in asr tts llm web; do
        if systemctl --user is-active "talknest-$svc.service" \
                >/dev/null 2>&1; then
            echo "[tmux_start] stopping talknest systemd units (tmux mode takes over)"
            systemctl --user stop talknest-{asr,tts,llm,web}.service \
                >/dev/null 2>&1 || true
            break
        fi
    done
fi

start_one() {
    local name="$1" script="${START[$1]}"
    if [ ! -x "$script" ]; then
        echo "[tmux_start] ERROR: $script is missing or not executable" >&2
        return 1
    fi
    # Free the port even when the service was started outside tmux.
    tmux kill-window -t "$SESSION:$name" 2>/dev/null || true
    "${STOP[$name]}" >/dev/null 2>&1 || true

    local log="$LOG_DIR/${name}_$(date +%Y%m%d-%H%M%S).log"
    ln -sfn "$log" "$LOG_DIR/${name}.current.log"
    local cmd="'$script' 2>&1 | tee '$log'"
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux new-session -d -s "$SESSION" -n "$name" -c "$REPO_ROOT" \
            "${TMUX_ENV[@]}" "$cmd"
    else
        tmux new-window -t "$SESSION" -n "$name" -c "$REPO_ROOT" \
            "${TMUX_ENV[@]}" "$cmd"
    fi
    echo "[tmux_start] $SESSION:$name <- $script (log: $log)"
    wait_ready "$name"
}

# Block until the service answers, so the next one starts against the
# memory the previous one actually took. A service that never comes up
# stops the sequence instead: continuing would hand the LLM an empty card.
wait_ready() {
    local name="$1" url="${READY[$1]:-}" waited=0
    if [ -z "$url" ]; then
        return 0
    fi
    while [ "$waited" -lt "$READY_TIMEOUT" ]; do
        if curl -kfsS --max-time 3 "$url" >/dev/null 2>&1; then
            echo "[tmux_start] $name ready after ${waited}s"
            return 0
        fi
        # The window closes as soon as the service process exits, so a
        # crashed startup fails fast instead of blocking for the timeout.
        if ! tmux list-windows -t "$SESSION" -F '#{window_name}' \
                2>/dev/null | grep -qx "$name"; then
            echo "[tmux_start] ERROR: $name exited before becoming ready; see $LOG_DIR/$name.current.log" >&2
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "[tmux_start] ERROR: $name not ready at $url after ${waited}s" >&2
    return 1
}

for name in "${SERVICES[@]}"; do
    start_one "$name" || exit 1
done
echo "[tmux_start] attach with: tmux attach -t $SESSION"
