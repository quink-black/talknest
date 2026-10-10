#!/usr/bin/env bash
# Kill whatever listens on a TCP port and report whether it let go.
#
#   free_port.sh <port>
#
# Exits non-zero when the port is still held afterwards. The stop_*.sh
# scripts share this so the lookup lives in one place.
#
# Linux `fuser -k PORT/tcp` would be the obvious tool, but macOS also
# ships a fuser -- a Perl script that rejects both -k and PORT/tcp --
# so a `command -v fuser` guard passes there and then kills nothing
# while reporting success. lsof exists on both and answers the same
# question, so it does the lookup here. When lsof is missing the port
# owner cannot be found at all, and saying so beats a false "freed".
set -u

PORT="${1:?usage: free_port.sh <port>}"

if ! command -v lsof >/dev/null 2>&1; then
    echo "[free_port] ERROR: lsof is required to find who holds $PORT" >&2
    exit 1
fi

# PIDs listening on the port, one per line.
listeners() {
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null
}

pids="$(listeners)"
if [ -n "$pids" ]; then
    # SIGKILL, matching what fuser -k sent: these are servers being
    # recycled, not ones being asked to drain.
    # shellcheck disable=SC2086 # word splitting is the point here
    kill -9 $pids 2>/dev/null || true
    sleep 1
fi

if [ -n "$(listeners)" ]; then
    echo "[free_port] WARNING: something still listens on $PORT" >&2
    exit 1
fi
echo "[free_port] port $PORT free"
