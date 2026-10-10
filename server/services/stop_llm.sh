#!/usr/bin/env bash
# Stop whatever LLM occupies the talknest slot (port 9999). Port 9999 is
# the agreed single-LLM slot on this host (vLLM and llama.cpp launchers
# alike), so this only ever hits that slot.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../../scripts/free_port.sh" 9999
