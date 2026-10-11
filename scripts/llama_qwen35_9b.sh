#!/usr/bin/env bash
# Foreground launcher for llama.cpp serving Qwen3.5-9B (talknest M1 LLM).
#
# Run inside tmux so logs stream to the pane. Listens on 127.0.0.1 on the
# talknest LLM port (see scripts/ports.sh, shared with the vLLM 27B
# profile -- the two cannot be resident at the same time on unified
# memory).
#
# Tunables (override via env):
#   LLM_PORT           listen port (default: TALKNEST_PORT + 3, i.e. 8893
#                      with the default orchestrator port)
#   TN9B_HOST          bind address (default 127.0.0.1)
#   TN9B_API_KEY       Bearer key; a non-loopback host without one is
#                      rejected
#   TN9B_MODEL_DIR     directory holding the GGUF files
#   TN9B_QUANT         GGUF quant suffix (default Q4_K_M; try Q8_0)
#   TN9B_BIN_DIR       llama-server build dir (set it in local.env)
#   TN9B_CTX           total KV budget (default 8192 -- voice dialog is
#                      short; raise only with MTP off and spare memory)
#   TN9B_SLOTS         concurrent slots (default 1; MTP requires np=1)
#   TN9B_SPEC          speculative type (default draft-mtp, "none" off)
#   TN9B_SPEC_N_MAX    max draft tokens (default 6, per unsloth guide)
#   TN9B_NGL           GPU layers (unset = auto-fit to free VRAM, or all
#                      of them on macOS; see below)
#   TN9B_FIT_MARGIN    free-MiB headroom --fit leaves per device (512)
#   TN9B_THREADS / TN9B_FLASH_ATTN   hardware knobs
#   TN9B_ALIAS         API model id (default qwen3.5-9b; keep in sync
#                      with server/config.yaml llm profile)
#
# Non-thinking mode: Qwen3.5 defaults to thinking, which ruins voice
# latency. We pass enable_thinking:false via chat template kwargs and
# reasoning-budget 0 as a second guard. Server sampling fallbacks follow
# the official non-thinking settings; the orchestrator still sends its
# own sampling params per request.

set -euo pipefail

usage() {
    # Print the header comment block, so edits to it cannot cut -h short.
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit 0
}
case "${1:-}" in
    -h|--help) usage ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=ports.sh
. "$SCRIPT_DIR/ports.sh"

MODEL_ROOT="${TALKNEST_MODEL_ROOT:-$(dirname -- "$REPO_ROOT")/model}"
MODEL_DIR="${TN9B_MODEL_DIR:-$MODEL_ROOT/Qwen3.5-9B-MTP-GGUF}"
QUANT="${TN9B_QUANT:-Q4_K_M}"
MODEL_PATH="$MODEL_DIR/Qwen3.5-9B-$QUANT.gguf"
BIN_DIR="${TN9B_BIN_DIR:-}"
LOG_DIR="${TN9B_LOG_DIR:-$REPO_ROOT/logs}"

PORT="$LLM_PORT"
HOST="${TN9B_HOST:-127.0.0.1}"
API_KEY="${TN9B_API_KEY:-}"
CTX="${TN9B_CTX:-8192}"
SLOTS="${TN9B_SLOTS:-1}"
# Left unset, llama.cpp fits the offloaded layers to whatever VRAM is
# free; set TN9B_NGL to pin a count instead. -fit sizes the offload for a
# card that has to be shared, which is not a constraint unified memory
# has, so on macOS every layer goes to the GPU by default.
NGL="${TN9B_NGL:-}"
if [ -z "$NGL" ] && [ "$(uname -s)" = "Darwin" ]; then
    NGL=-1
fi
FIT_MARGIN="${TN9B_FIT_MARGIN:-512}"
THREADS="${TN9B_THREADS:-8}"
FLASH="${TN9B_FLASH_ATTN:-1}"
SPEC="${TN9B_SPEC:-draft-mtp}"
SPEC_N_MAX="${TN9B_SPEC_N_MAX:-6}"
ALIAS="${TN9B_ALIAS:-qwen3.5-9b}"

TEMP="${TN9B_TEMP:-0.7}"
TOP_P="${TN9B_TOP_P:-0.8}"
TOP_K="${TN9B_TOP_K:-20}"
PRESENCE_PENALTY="${TN9B_PRESENCE_PENALTY:-1.5}"

if [ ! -f "$MODEL_PATH" ]; then
    echo "[llama_qwen35_9b] ERROR: $MODEL_PATH not found." >&2
    echo "[llama_qwen35_9b] Download it first (see ../model/README.md) or set TN9B_MODEL_DIR / TN9B_QUANT." >&2
    exit 1
fi
if [ ! -x "${BIN_DIR:-}/llama-server" ]; then
    echo "[llama_qwen35_9b] ERROR: no llama-server at ${BIN_DIR:-<unset>}." >&2
    echo "[llama_qwen35_9b] Set TN9B_BIN_DIR in local.env to the llama.cpp build/bin directory." >&2
    exit 1
fi

# A non-loopback bind is only safe behind a Bearer key.
case "$HOST" in
    127.*|localhost|::1|\[::1\]) ;;
    *)
        if [ -z "$API_KEY" ]; then
            echo "[llama_qwen35_9b] ERROR: host $HOST requires TN9B_API_KEY." >&2
            exit 1
        fi
        ;;
esac

echo "[llama_qwen35_9b] model=$MODEL_PATH bin=$BIN_DIR"
echo "[llama_qwen35_9b] host=$HOST port=$PORT alias=$ALIAS ctx=$CTX slots=$SLOTS spec=$SPEC/$SPEC_N_MAX"

ARGS=(
    -m "$MODEL_PATH"
    -a "$ALIAS"
    -c "$CTX"
    -t "$THREADS"
    --host "$HOST"
    --port "$PORT"
    -np "$SLOTS"
    --kv-unified
    --temp "$TEMP"
    --top-p "$TOP_P"
    --top-k "$TOP_K"
    --presence-penalty "$PRESENCE_PENALTY"
    --jinja
    --chat-template-kwargs '{"enable_thinking": false}'
    --reasoning-budget 0
)
if [ -n "$NGL" ]; then
    ARGS+=( -ngl "$NGL" )
else
    ARGS+=( -fit on -fitt "$FIT_MARGIN" )
fi
if [ -n "$API_KEY" ]; then
    ARGS+=( --api-key "$API_KEY" )
fi
if [ "$FLASH" = "1" ]; then
    ARGS+=( -fa on )
fi
if [ "$SPEC" != "none" ]; then
    ARGS+=( --spec-type "$SPEC" --spec-draft-n-max "$SPEC_N_MAX" )
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/llama_qwen35_9b_${QUANT}_$(date +%Y%m%d-%H%M%S).log"
ln -sfn "$LOG_FILE" "$LOG_DIR/llama.current.log"
echo "[llama_qwen35_9b] logging to $LOG_FILE"

export LD_LIBRARY_PATH="$BIN_DIR:${LD_LIBRARY_PATH:-}"
exec "$BIN_DIR/llama-server" "${ARGS[@]}" 2>&1 | tee "$LOG_FILE"
