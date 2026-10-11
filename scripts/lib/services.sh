#!/usr/bin/env bash
# Service tables and start helpers shared by the talknest supervisors:
# scripts/start.sh, scripts/tmux_start.sh and scripts/stop.sh. Sourced,
# never run directly.
#
# Deliberately sets no shell options: this file is sourced, so `set -u`
# here would quietly change the caller.
#
# bash 3.2, the /bin/bash macOS ships, has no associative arrays, so the
# per-service tables are case statements rather than maps.

SCRIPTS_DIR="$(dirname -- "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)")"
REPO_ROOT="$(dirname -- "$SCRIPTS_DIR")"

# shellcheck source=../local_env.sh
. "$SCRIPTS_DIR/local_env.sh"
# The health URLs and the port the stop scripts free come from the same
# table the services themselves bind, so a port moved in local.env is
# followed here instead of being hardcoded a second time.
# shellcheck source=../ports.sh
. "$SCRIPTS_DIR/ports.sh"

# The dependency order: ASR and TTS claim their VRAM pools first, because
# llama.cpp -fit sizes the LLM offload to whatever is free when the LLM
# itself starts.
TN_SERVICES=(asr tts llm web)
TN_LOG_DIR="$REPO_ROOT/logs"

# Prefix of the progress and error lines the supervisor prints. It defaults
# to the name of the script that sourced this file, so start.sh prints
# [start] and tmux_start.sh [tmux_start] without either repeating itself.
TN_TAG="${TN_TAG:-$(basename -- "${BASH_SOURCE[1]:-services}" .sh)}"

tn_log() { printf '[%s] %s\n' "$TN_TAG" "$*"; }

tn_start_script() {
    case "$1" in
        asr) printf '%s\n' "$REPO_ROOT/server/services/start_asr.sh" ;;
        tts) printf '%s\n' "$REPO_ROOT/server/services/start_tts.sh" ;;
        llm) printf '%s\n' "$REPO_ROOT/server/services/start_llm_llamacpp_9b.sh" ;;
        web) printf '%s\n' "$SCRIPTS_DIR/start_web.sh" ;;
    esac
}

tn_stop_script() {
    case "$1" in
        asr) printf '%s\n' "$REPO_ROOT/server/services/stop_asr.sh" ;;
        tts) printf '%s\n' "$REPO_ROOT/server/services/stop_tts.sh" ;;
        llm) printf '%s\n' "$REPO_ROOT/server/services/stop_llm.sh" ;;
        web) printf '%s\n' "$SCRIPTS_DIR/stop_web.sh" ;;
    esac
}

# start_web.sh enables TLS whenever cert material is configured or the dev
# cert exists, and uvicorn then refuses plain HTTP, so the readiness probe
# has to speak the same scheme (-k: the dev certificate is self-signed).
tn_web_scheme() {
    if { [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; } \
       || { [ -z "${TLS_CERT:-}" ] && [ -f "$REPO_ROOT/certs/dev.pem" ] \
            && [ -f "$REPO_ROOT/certs/dev.key" ]; }; then
        printf 'https\n'
    else
        printf 'http\n'
    fi
}

tn_ready_url() {
    case "$1" in
        asr) printf '%s\n' "http://127.0.0.1:${ASR_PORT}/health" ;;
        tts) printf '%s\n' "http://127.0.0.1:${TTS_PORT}/v1/models" ;;
        llm) printf '%s\n' "http://127.0.0.1:${LLM_PORT}/health" ;;
        web) printf '%s\n' "$(tn_web_scheme)://127.0.0.1:${TALKNEST_PORT}/" ;;
    esac
}

# Reserve a timestamped log for one service and point
# logs/<name>.current.log at it, so the newest log has a stable name for
# both the tmux and the tmux-free supervisor. Prints the log path.
tn_log_path() {
    local name="$1" log
    mkdir -p "$TN_LOG_DIR"
    log="$TN_LOG_DIR/${name}_$(date +%Y%m%d-%H%M%S).log"
    ln -sfn "$log" "$TN_LOG_DIR/${name}.current.log"
    printf '%s\n' "$log"
}

# Whether a service that is starting is still running. Takes the service
# name and, when the caller knows it, the PID. scripts/tmux_start.sh
# overrides this: there the service runs inside a tmux window, and only
# the window's existence says whether the pipeline is still alive.
tn_service_alive() {
    [ -n "$2" ] && kill -0 "$2" 2>/dev/null
}

# Block until the service answers its health URL, so the next one starts
# against the memory the previous one actually took. A service that never
# comes up fails the wait instead of burning the whole timeout.
# Usage: tn_wait_ready <name> [pid]
tn_wait_ready() {
    local name="$1" pid="${2:-}" url waited=0
    url="$(tn_ready_url "$name")"
    if [ -z "$url" ]; then
        return 0
    fi
    while [ "$waited" -lt "${TALKNEST_READY_TIMEOUT:-600}" ]; do
        if curl -kfsS --max-time 3 "$url" >/dev/null 2>&1; then
            tn_log "$name ready after ${waited}s"
            return 0
        fi
        if ! tn_service_alive "$name" "$pid"; then
            tn_log "ERROR: $name exited before becoming ready; see $TN_LOG_DIR/$name.current.log" >&2
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    tn_log "ERROR: $name not ready at $url after ${waited}s" >&2
    return 1
}

# Put the requested service names back in the dependency order and reject
# unknown ones, so a supervisor never starts the LLM before ASR and TTS
# have claimed their pools. Prints one name per line; no argument means
# every service.
# Usage: tn_order_services [name ...]
tn_order_services() {
    local want req known
    for req in "$@"; do
        known=0
        for want in "${TN_SERVICES[@]}"; do
            if [ "$req" = "$want" ]; then
                known=1
            fi
        done
        if [ "$known" -eq 0 ]; then
            tn_log "ERROR: unknown service '$req' (${TN_SERVICES[*]})" >&2
            return 1
        fi
    done
    if [ "$#" -eq 0 ]; then
        printf '%s\n' "${TN_SERVICES[@]}"
        return 0
    fi
    for want in "${TN_SERVICES[@]}"; do
        for req in "$@"; do
            if [ "$req" = "$want" ]; then
                printf '%s\n' "$want"
                # One line per service however often it was named: a
                # duplicate would start the service, free its port and
                # start it again, and then be counted twice.
                break
            fi
        done
    done
}

# The same resolution as tn_order_services, with the result in an array,
# so the supervisors do not each carry the loop. Fills TN_REQUESTED and
# returns 2 on an unknown name; the caller owns the exit code. No
# argument means every service.
# Usage: tn_resolve_services [name ...]   -> TN_REQUESTED
tn_resolve_services() {
    local ordered name
    if ! ordered="$(tn_order_services "$@")"; then
        return 2
    fi
    TN_REQUESTED=()
    while IFS= read -r name; do
        TN_REQUESTED+=("$name")
    done <<< "$ordered"
}

# Services running under systemd would fight for the ports: killing the
# listener pushes the units into their Restart=on-failure loop, so stop
# the units cleanly first. A no-op when none are running.
# Usage: tn_stop_systemd_units [name ...]   (default: every service)
tn_stop_systemd_units() {
    local names=("$@") units=() name
    if [ "${#names[@]}" -eq 0 ]; then
        names=("${TN_SERVICES[@]}")
    fi
    command -v systemctl >/dev/null 2>&1 || return 0
    for name in "${names[@]}"; do
        if systemctl --user is-active "talknest-$name.service" >/dev/null 2>&1; then
            units+=("talknest-$name.service")
        fi
    done
    if [ "${#units[@]}" -eq 0 ]; then
        return 0
    fi
    tn_log "stopping systemd units: ${units[*]}"
    systemctl --user stop "${units[@]}" >/dev/null 2>&1 || true
}
