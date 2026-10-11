#!/usr/bin/env bash
# Switch profile: Qwen3.8-27B-NVFP4 via vLLM (prewired, NOT used in M1).
#
# Reuses a proven external launcher with reduced context/concurrency for
# voice duty:
#   QWEN38_MAX_MODEL_LEN=8192 QWEN38_MAX_SEQS=2 QWEN38_MAX_BATCHED=2048
# and the model's own chat template (the launcher otherwise forces a
# codex template).
#
# After this server is up, set llm.active: vllm-qwen38-27b in
# server/config.yaml. Unified memory rule: stop the 9B first
# (stop_llm.sh) -- the two share the LLM slot and the same VRAM. The
# external launcher picks its own listen port, so point LLM_PORT at it.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/ports.sh
. "$REPO_ROOT/scripts/ports.sh"
# Both live outside the repo, so local.env is where they come from.
RUN_QWEN38="${RUN_QWEN38:-}"
MODEL_ROOT="${TALKNEST_MODEL_ROOT:-$(dirname -- "$REPO_ROOT")/model}"
[ -n "$RUN_QWEN38" ] && [ -x "$RUN_QWEN38" ] || {
    echo "[vllm_qwen38_27b] ERROR: no executable launcher at ${RUN_QWEN38:-<unset>}." >&2
    echo "[vllm_qwen38_27b] Set RUN_QWEN38 in local.env." >&2
    exit 1
}

export QWEN38_MAX_MODEL_LEN="${QWEN38_MAX_MODEL_LEN:-8192}"
export QWEN38_MAX_SEQS="${QWEN38_MAX_SEQS:-2}"
export QWEN38_MAX_BATCHED="${QWEN38_MAX_BATCHED:-2048}"
export QWEN38_MTP="${QWEN38_MTP:-2}"
export QWEN38_ATTN_BACKEND="${QWEN38_ATTN_BACKEND:-}"
export QWEN38_CHAT_TEMPLATE="${QWEN38_CHAT_TEMPLATE:-$MODEL_ROOT/qwen/Qwen3.8-27B-NVFP4/chat_template.jinja}"

exec "$RUN_QWEN38" nvfp4
