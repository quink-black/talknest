#!/usr/bin/env bash
# Start TTS: Qwen3-TTS-0.6B-CustomVoice via the official qwen-tts package
# (transformers backend, CUDA) in its dedicated venv (venv-tts), wrapped
# by an OpenAI-compatible sidecar on port 8002. Runs in the foreground.
#
# Why a sidecar instead of `vllm serve`: vLLM online serving for
# Qwen3-TTS is not released (vLLM-Omni is offline-only as of 2026-09),
# and the shared qwen venv cannot hold qwen-tts (it would downgrade
# transformers — forbidden by venv_reuse V2).
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/local_env.sh
. "$REPO_ROOT/scripts/local_env.sh"

exec "$REPO_ROOT/venv-tts/bin/uvicorn" qwen_tts_server:app \
    --app-dir "$SCRIPT_DIR" --host 127.0.0.1 --port 8002
