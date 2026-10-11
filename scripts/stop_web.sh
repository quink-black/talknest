#!/usr/bin/env bash
# Stop the orchestrator: whatever holds the orchestrator's port.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=ports.sh
. "$SCRIPT_DIR/ports.sh"
exec "$SCRIPT_DIR/free_port.sh" "$TALKNEST_PORT"
