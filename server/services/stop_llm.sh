#!/usr/bin/env bash
# Stop whatever LLM occupies the talknest LLM slot (the LLM_PORT of
# scripts/ports.sh). Both the vLLM and the llama.cpp profile use that one
# slot, so this only ever hits it.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/ports.sh
. "$REPO_ROOT/scripts/ports.sh"
exec "$SCRIPT_DIR/../../scripts/free_port.sh" "$LLM_PORT"
