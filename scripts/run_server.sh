#!/usr/bin/env bash
# Start the talknest orchestrator (serves web/ + WebSocket on the
# orchestrator's port, the first of the four: see scripts/ports.sh).
# Host, port and TLS come from local.env; see local.env.example.
# (Self-signed cert generation: scripts/gen_dev_cert.sh)
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=ports.sh
. "$REPO_ROOT/scripts/ports.sh"
HOST="${TALKNEST_HOST:-0.0.0.0}"
PORT="$TALKNEST_PORT"
ARGS=(--host "$HOST" --port "$PORT")
if [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; then
    # local.env commonly holds repo-relative cert paths; resolve them here
    # so the server starts the same way from any working directory.
    case "$TLS_CERT" in /*) ;; *) TLS_CERT="$REPO_ROOT/$TLS_CERT" ;; esac
    case "$TLS_KEY" in /*) ;; *) TLS_KEY="$REPO_ROOT/$TLS_KEY" ;; esac
    ARGS+=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
fi
exec "$REPO_ROOT/venv/bin/uvicorn" orchestrator.main:app --app-dir "$REPO_ROOT/server" "${ARGS[@]}"
