#!/usr/bin/env bash
# Start the talknest services without tmux: one command for the whole
# stack, and nothing to install beyond what the services need.
#
# Usage: scripts/start.sh [-d|--daemon] [asr|tts|llm|web ...]
#
#   (default)  start the services in the background, follow their logs in
#              this terminal, and stop everything on Ctrl-C -- the entry
#              point for debugging
#   -d         start the services, wait until each answers its health URL,
#              then exit and leave them running
#
# No argument starts every service. They come up one at a time and each
# has to answer its health URL before the next one starts: llama.cpp -fit
# sizes the offload to the VRAM that is free at its own startup, so the
# LLM comes last and a service that fails stops the sequence rather than
# handing the next one a card whose memory is not yet accounted for.
# Naming services starts only those, in the same dependency order.
#
# The services are stopped with scripts/stop.sh, which frees their ports
# whoever started them.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/services.sh
. "$SCRIPT_DIR/lib/services.sh"

DAEMON=0
REQUESTED=()

usage() {
    cat <<'EOF'
Usage: scripts/start.sh [-d|--daemon] [asr|tts|llm|web ...]

  (default)  start in the background, follow the logs, Ctrl-C stops all
  -d         start, wait until ready, then exit leaving them running
  -h         this help

No service name starts all four, in the order ASR, TTS, LLM, web.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -d|--daemon) DAEMON=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "[start] ERROR: unknown option '$1'" >&2; usage >&2; exit 2 ;;
        *) REQUESTED+=("$1") ;;
    esac
    shift
done

# Whatever order the names were typed in, the services start in the
# dependency order: -fit sizes the LLM offload to the VRAM that is free
# when the LLM starts, so the LLM has to come after ASR and TTS.
if [ "${#REQUESTED[@]}" -gt 0 ]; then
    if ! ordered="$(tn_order_services "${REQUESTED[@]}")"; then
        exit 2
    fi
else
    ordered="$(tn_order_services)"
fi
SERVICES=()
while IFS= read -r name; do
    SERVICES+=("$name")
done <<< "$ordered"

STARTED=()
PIDS=()
DEAD=()
FOLLOW_PID=""

# Stop the started services by port, the same way scripts/stop.sh does.
stop_started() {
    local name
    if [ "${#STARTED[@]}" -eq 0 ]; then
        return 0
    fi
    for name in "${STARTED[@]}"; do
        "$(tn_stop_script "$name")" >/dev/null 2>&1 || true
    done
    tn_log "stopped: ${STARTED[*]}"
}

cleanup() {
    local code="$1"
    trap - INT TERM
    if [ -n "$FOLLOW_PID" ]; then
        kill "$FOLLOW_PID" 2>/dev/null || true
    fi
    stop_started
    exit "$code"
}
trap 'cleanup 130' INT
trap 'cleanup 143' TERM

# Deliberately no `set -m`: with job control on, the terminal belongs to
# the foreground command and a Ctrl-C never reaches this script's trap.
# Without it the services are asynchronous commands, and bash makes those
# ignore SIGINT, so Ctrl-C reaches this script and the log follower only,
# and the trap frees the ports.
start_one() {
    local name="$1" script log pid
    script="$(tn_start_script "$name")"
    if [ ! -x "$script" ]; then
        tn_log "ERROR: $script is missing or not executable" >&2
        return 1
    fi
    # Free the port even when the service was started elsewhere.
    "$(tn_stop_script "$name")" >/dev/null 2>&1 || true
    log="$(tn_log_path "$name")"
    if [ "$DAEMON" -eq 1 ]; then
        # nohup so the service survives the terminal that started it;
        # macOS has no setsid.
        nohup "$script" </dev/null >>"$log" 2>&1 &
    else
        "$script" </dev/null >>"$log" 2>&1 &
    fi
    pid=$!
    STARTED+=("$name")
    PIDS+=("$pid")
    tn_log "$name pid $pid (log: $TN_LOG_DIR/$name.current.log)"
    tn_wait_ready "$name" "$pid"
}

report() {
    local i
    for i in "${!STARTED[@]}"; do
        printf '%-4s pid %-7s %-34s %s\n' \
            "${STARTED[$i]}" "${PIDS[$i]}" \
            "$(tn_ready_url "${STARTED[$i]}")" \
            "$TN_LOG_DIR/${STARTED[$i]}.current.log"
    done
}

# Keep the terminal attached while any service is still running: a
# service that dies would otherwise leave a log follower that looks
# healthy. Lines from the several logs are interleaved, and tail labels
# each block with the file it came from.
watch() {
    local i pid alive
    while : ; do
        alive=0
        for i in "${!PIDS[@]}"; do
            pid="${PIDS[$i]}"
            if kill -0 "$pid" 2>/dev/null; then
                alive=$((alive + 1))
            elif [ -z "${DEAD[$i]:-}" ]; then
                DEAD[i]=1
                tn_log "WARNING: ${STARTED[$i]} (pid $pid) exited; see $TN_LOG_DIR/${STARTED[$i]}.current.log" >&2
            fi
        done
        if [ "$alive" -eq 0 ]; then
            tn_log "ERROR: no service is running any more" >&2
            cleanup 1
        fi
        sleep 2
    done
}

follow() {
    local logs=() name
    for name in "${STARTED[@]}"; do
        logs+=("$TN_LOG_DIR/$name.current.log")
    done
    # The follower ignores SIGINT like the services do, so the trap stops
    # it explicitly.
    tail -f "${logs[@]}" &
    FOLLOW_PID=$!
    tn_log "following ${#logs[@]} log(s); Ctrl-C stops the services"
    watch
}

# Services under systemd would fight for the ports; a no-op when none of
# the named units is running. Only the services being taken over are
# stopped: `start.sh web` must not pull the ASR, TTS and LLM units down.
tn_stop_systemd_units "${SERVICES[@]}"

for name in "${SERVICES[@]}"; do
    if ! start_one "$name"; then
        tn_log "ERROR: $name did not come up; the services already started are left running (stop them with scripts/stop.sh)" >&2
        exit 1
    fi
done

if [ "$DAEMON" -eq 1 ]; then
    report
    tn_log "${#STARTED[@]} service(s) running; stop with scripts/stop.sh"
    exit 0
fi

report
tn_log "Ctrl-C stops the services"
follow
