#!/usr/bin/env bash
# Stop the orchestrator: whatever holds port 8890.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=local_env.sh
. "$SCRIPT_DIR/local_env.sh"
exec "$SCRIPT_DIR/free_port.sh" "${TALKNEST_PORT:-8890}"
