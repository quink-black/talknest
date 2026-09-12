#!/usr/bin/env bash
# Start the M1 LLM: Qwen3.5-9B GGUF via llama.cpp, in the foreground.
#
# Listens on 127.0.0.1:9999, the slot shared with the vLLM 27B profile;
# the two cannot be resident at the same time on unified memory, so stop
# one before starting the other (stop_llm.sh).
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/local_env.sh
. "$REPO_ROOT/scripts/local_env.sh"

exec "$SCRIPT_DIR/../../scripts/llama_qwen35_9b.sh"
