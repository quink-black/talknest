#!/usr/bin/env bash
# Stop the orchestrator: whatever holds port 8890.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=local_env.sh
. "$SCRIPT_DIR/local_env.sh"
PORT="${TALKNEST_PORT:-8890}"
if command -v fuser >/dev/null 2>&1; then
    fuser -k "$PORT/tcp" 2>/dev/null || true
fi
echo "[stop_web] port $PORT freed"
