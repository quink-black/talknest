#!/usr/bin/env bash
# Start talknest services in one tmux session, one window per service.
# scripts/start.sh starts the same services without tmux; this helper is
# here when you want each one in its own window.
#
# The service scripts themselves know nothing about tmux: each one just
# runs its service in the foreground, so it can equally be started from a
# plain shell, systemd, or a container entrypoint. This helper and
# scripts/start.sh are the only places that decide how those processes are
# supervised, and both share the tables in scripts/lib/services.sh.
#
# Usage: scripts/tmux_start.sh [asr|tts|llm|web ...]
# No argument starts every service and waits for each to answer on its
# health URL before starting the next; LLM comes last so llama.cpp -fit
# sees the VRAM that ASR and TTS already took. Re-running a service
# replaces its window. Only the session named by TALKNEST_SESSION is ever
# touched -- other sessions and the tmux server are left alone.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/services.sh
. "$SCRIPT_DIR/lib/services.sh"

SESSION="${TALKNEST_SESSION:-talknest}"

# Whatever order the names were typed in, the services start in the
# dependency order, and an unknown name is rejected, exactly as in
# scripts/start.sh: -fit sizes the LLM offload to the VRAM that is free
# when the LLM starts, so the LLM has to come after ASR and TTS.
tn_resolve_services "$@" || exit 2
SERVICES=("${TN_REQUESTED[@]}")

# A tmux window closes as soon as the service pipeline exits, so the
# window's existence, not a PID, says whether the service is still coming
# up. This overrides the PID check in lib/services.sh. A here-string
# rather than a pipe: under `set -o pipefail`, grep -q exiting early can
# otherwise make a live window look dead.
tn_service_alive() {
    local name="$1" windows
    windows="$(tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null || true)"
    grep -qx "$name" <<< "$windows"
}

# A tmux window inherits the tmux server's environment, not the caller's,
# so overrides given on the command line would be dropped whenever the
# session already exists. The list is the tunable prefixes plus the few
# standalone names a service script reads; a variable outside it has to
# come from local.env, which every service sources for itself.
TMUX_ENV=()
while IFS='=' read -r name value; do
    TMUX_ENV+=( -e "$name=$value" )
done < <(env | grep -E '^(TALKNEST_|ASR_|TTS_|LLM_|TN9B_|QWEN38_|TLS_|RUN_QWEN38|HF_HOME|PYTORCH_CUDA_ALLOC_CONF)' || true)

# Services running under systemd would fight for the ports: the stop
# scripts below fuser-kill them into Restart=on-failure loops. Stop the
# units cleanly first; a no-op when none are running. Only the services
# being taken over are stopped: replacing one window must not pull the
# other units down.
tn_stop_systemd_units "${SERVICES[@]}"

start_one() {
    local name="$1" script log cmd
    script="$(tn_start_script "$name")"
    if [ ! -x "$script" ]; then
        tn_log "ERROR: $script is missing or not executable" >&2
        return 1
    fi
    # Free the port even when the service was started outside tmux.
    tmux kill-window -t "$SESSION:$name" 2>/dev/null || true
    "$(tn_stop_script "$name")" >/dev/null 2>&1 || true

    log="$(tn_log_path "$name")"
    cmd="'$script' 2>&1 | tee '$log'"
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux new-session -d -s "$SESSION" -n "$name" -c "$REPO_ROOT" \
            "${TMUX_ENV[@]}" "$cmd"
    else
        tmux new-window -t "$SESSION" -n "$name" -c "$REPO_ROOT" \
            "${TMUX_ENV[@]}" "$cmd"
    fi
    tn_log "$SESSION:$name <- $script (log: $log)"
    tn_wait_ready "$name"
}

# Block until the service answers, so the next one starts against the
# memory the previous one actually took. A service that never comes up
# stops the sequence instead: continuing would hand the LLM an empty card.
for name in "${SERVICES[@]}"; do
    start_one "$name" || exit 1
done
echo "[tmux_start] attach with: tmux attach -t $SESSION"
