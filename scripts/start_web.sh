#!/usr/bin/env bash
# Start the orchestrator: web UI and WebSocket on :TALKNEST_PORT, in the
# foreground.
#
# TLS_CERT / TLS_KEY turn on HTTPS. A browser only exposes the microphone
# in a secure context, so a LAN client needs them; when they are unset but
# scripts/gen_dev_cert.sh has produced certs/dev.pem, use those rather
# than silently serving a mic-less page over plain HTTP.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=ports.sh
. "$SCRIPT_DIR/ports.sh"

CERT="${TLS_CERT:-}"
KEY="${TLS_KEY:-}"
if [ -z "$CERT" ] && [ -f "$REPO_ROOT/certs/dev.pem" ] \
   && [ -f "$REPO_ROOT/certs/dev.key" ]; then
    CERT="$REPO_ROOT/certs/dev.pem"
    KEY="$REPO_ROOT/certs/dev.key"
fi

export TLS_CERT="$CERT" TLS_KEY="$KEY"
echo "[start_web] orchestrator on port ${TALKNEST_PORT}${CERT:+ (TLS)}"
exec "$SCRIPT_DIR/run_server.sh"
