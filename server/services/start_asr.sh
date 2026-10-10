#!/usr/bin/env bash
# Start ASR on port 8001, in the foreground.
#
# ASR_BACKEND picks the runtime:
#   vllm      Qwen3-ASR-1.7B through vLLM. CUDA only: vLLM has no Metal
#             backend, so this is unavailable on Apple Silicon.
#   llamacpp  the same recogniser as a GGUF through llama.cpp, which
#             covers macOS. scripts/llama_qwen3_asr.sh holds the details.
#
# vLLM backend: ASR_GPU_UTIL bounds the pool vLLM grabs for weights plus
# KV cache; on a 12G card it has to leave room for the LLM and TTS.
#
# Prefers qwen-asr-serve, which registers Qwen3ASR with the vLLM model
# registry before delegating to `vllm serve`, and falls back to plain
# vllm. MAX_JOBS=4 bounds flashinfer JIT ninja parallelism (unbounded JIT
# OOM-killed the dev host on 2026-08-20).
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/local_env.sh
. "$REPO_ROOT/scripts/local_env.sh"

ASR_BACKEND="${ASR_BACKEND:-vllm}"
case "$ASR_BACKEND" in
    llamacpp)
        exec "$REPO_ROOT/scripts/llama_qwen3_asr.sh"
        ;;
    vllm)
        ;;
    *)
        echo "[start_asr] ERROR: ASR_BACKEND must be vllm or llamacpp, got '$ASR_BACKEND'." >&2
        exit 1
        ;;
esac

# The venv is machine-local (it holds vLLM), so it comes from local.env;
# ASR_BIN can point at a specific binary instead.
ASR_VENV="${ASR_VENV:-}"
MODEL_ROOT="${TALKNEST_MODEL_ROOT:-$(dirname -- "$REPO_ROOT")/model}"
ASR_MODEL="${ASR_MODEL:-$MODEL_ROOT/Qwen3-ASR-1.7B}"
# Fraction of *total* VRAM, so it needs retuning per card: 0.50 is the
# smallest pool on a 12G card that still leaves ASR a usable KV cache
# (~7500 tokens); 0.45 and below fail to start.
ASR_GPU_UTIL="${ASR_GPU_UTIL:-0.50}"
# Batching bounds the activation peak vLLM profiles for; voice duty is one
# short utterance at a time, so the defaults only waste VRAM.
ASR_MAX_BATCHED="${ASR_MAX_BATCHED:-2048}"
ASR_MAX_SEQS="${ASR_MAX_SEQS:-2}"
# Quantization of the weights, e.g. fp8 to halve them and hand the freed
# VRAM to the LLM; unset keeps the checkpoint's own dtype. Weights are
# only half of the pool, so pair it with a lower ASR_GPU_UTIL to
# actually free memory.
ASR_QUANT="${ASR_QUANT:-}"

ASR_BIN="${ASR_BIN:-}"
if [ -z "$ASR_BIN" ] && [ -n "$ASR_VENV" ]; then
    for cand in "$ASR_VENV/bin/qwen-asr-serve" "$ASR_VENV/bin/vllm"; do
        if [ -x "$cand" ]; then
            ASR_BIN="$cand"
            break
        fi
    done
fi
if [ -z "$ASR_BIN" ]; then
    echo "[start_asr] ERROR: no qwen-asr-serve or vllm in ${ASR_VENV:-<unset venv>}." >&2
    echo "[start_asr] Set ASR_VENV (or ASR_BIN) in local.env." >&2
    exit 1
fi

# qwen-asr-serve injects the "serve" subcommand itself.
case "$(basename -- "$ASR_BIN")" in
    qwen-asr-serve) ASR_CMD=("$ASR_BIN" "$ASR_MODEL") ;;
    *) ASR_CMD=("$ASR_BIN" serve "$ASR_MODEL") ;;
esac

export MAX_JOBS="${MAX_JOBS:-4}"
export HF_HOME="${HF_HOME:-$MODEL_ROOT/.cache/hf}"
ARGS=(
    --served-model-name Qwen3-ASR-1.7B --host 127.0.0.1 --port 8001
    --max-model-len 4096 --gpu-memory-utilization "$ASR_GPU_UTIL"
    --max-num-batched-tokens "$ASR_MAX_BATCHED" --max-num-seqs "$ASR_MAX_SEQS"
)
if [ -n "$ASR_QUANT" ]; then
    ARGS+=( --quantization "$ASR_QUANT" )
fi
exec "${ASR_CMD[@]}" "${ARGS[@]}"
