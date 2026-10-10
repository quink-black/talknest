#!/usr/bin/env bash
# Foreground launcher for llama.cpp serving Qwen3-ASR (talknest ASR).
#
# Serves the OpenAI-compatible transcription endpoint on 127.0.0.1:8001.
# This is the macOS route: vLLM has no Metal backend, and with it the
# --gpu-memory-utilization and fp8 path in start_asr.sh is unavailable.
# llama.cpp ships a Qwen3-ASR audio encoder and answers the same
# "language English<asr_text>..." envelope the orchestrator parses, so
# the pipeline does not change. Select it with ASR_BACKEND=llamacpp.
#
# The weights are ggml-org's pre-converted GGUF, not the HF checkpoint:
# llama.cpp has no converter for this architecture.
#
# One gap against the vLLM backend: nothing in llama.cpp's transcription
# endpoint forces the output language. It does not read Qwen's
# to_language, and the language field only appends " (language: X)" to
# the prompt, which Qwen3-ASR ignores. The orchestrator's forced re-read
# therefore repeats the unforced request, so an utterance whose script
# is neither Latin nor CJK is dropped instead of being re-taken in
# English or Chinese.
#
# Tunables (override via env):
#   ASR_PORT        listen port (default 8001)
#   ASR_HOST        bind address (default 127.0.0.1)
#   ASR_LLAMA_BIN   llama-server binary (set it in local.env). Deliberately
#                   not ASR_BIN: start_asr.sh already reads that one, and
#                   it names a vLLM launcher there, so sharing it would
#                   let a backend switch run the wrong binary.
#   ASR_GGUF        ASR GGUF (default Q8_0 under the model root)
#   ASR_MMPROJ      audio projector GGUF (default: the mmproj sibling)
#   ASR_CTX         context (default 4096)
#   ASR_NGL         GPU layers (default -1, i.e. every layer)
#   ASR_THREADS     CPU threads (default 8)
#   ASR_ALIAS       API model id (default Qwen3-ASR-1.7B; keep in sync
#                   with asr.model in server/config.yaml)

set -euo pipefail

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}
case "${1:-}" in
    -h|--help) usage ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# shellcheck source=local_env.sh
. "$SCRIPT_DIR/local_env.sh"

MODEL_ROOT="${TALKNEST_MODEL_ROOT:-$(dirname -- "$REPO_ROOT")/model}"
GGUF_DIR="$MODEL_ROOT/Qwen3-ASR-1.7B-GGUF"
GGUF="${ASR_GGUF:-$GGUF_DIR/Qwen3-ASR-1.7B-Q8_0.gguf}"
MMPROJ="${ASR_MMPROJ:-$GGUF_DIR/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf}"
BIN="${ASR_LLAMA_BIN:-}"

PORT="${ASR_PORT:-8001}"
HOST="${ASR_HOST:-127.0.0.1}"
CTX="${ASR_CTX:-4096}"
NGL="${ASR_NGL:--1}"
THREADS="${ASR_THREADS:-8}"
ALIAS="${ASR_ALIAS:-Qwen3-ASR-1.7B}"

if [ ! -f "$GGUF" ]; then
    echo "[llama_qwen3_asr] ERROR: $GGUF not found." >&2
    echo "[llama_qwen3_asr] Set ASR_GGUF, or put ggml-org/Qwen3-ASR-1.7B-GGUF under $MODEL_ROOT." >&2
    exit 1
fi
if [ ! -f "$MMPROJ" ]; then
    echo "[llama_qwen3_asr] ERROR: $MMPROJ not found." >&2
    echo "[llama_qwen3_asr] Set ASR_MMPROJ to the audio projector GGUF." >&2
    exit 1
fi
if [ ! -x "$BIN" ]; then
    echo "[llama_qwen3_asr] ERROR: no llama-server at ${BIN:-<unset>}." >&2
    echo "[llama_qwen3_asr] Set ASR_LLAMA_BIN in local.env to the llama-server binary." >&2
    exit 1
fi

echo "[llama_qwen3_asr] model=$GGUF mmproj=$MMPROJ"
echo "[llama_qwen3_asr] host=$HOST port=$PORT alias=$ALIAS ctx=$CTX ngl=$NGL"

ARGS=(
    -m "$GGUF"
    --mmproj "$MMPROJ"
    -a "$ALIAS"
    -c "$CTX"
    -t "$THREADS"
    --host "$HOST"
    --port "$PORT"
    -ngl "$NGL"
    --jinja
)
exec "$BIN" "${ARGS[@]}"
