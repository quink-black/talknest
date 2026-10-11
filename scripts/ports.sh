#!/usr/bin/env bash
# Port table for the four talknest services.
#
# Sourced by the start and stop scripts, the tmux helpers and the systemd
# installer; not run directly. Every port can be overridden from local.env:
#
#   TALKNEST_PORT   8890   orchestrator: web UI and WebSocket. The only
#                          port a client outside this host needs.
#   ASR_PORT        8891   Qwen3-ASR, OpenAI-compatible transcription
#   TTS_PORT        8892   Qwen3-TTS sidecar
#   LLM_PORT        8893   llama.cpp / vLLM chat completions
#
# The three backends bind 127.0.0.1, so only the orchestrator's port has to
# be reachable from the LAN. The offsets are relative on purpose: setting
# TALKNEST_PORT moves the whole block to somewhere free, while an explicit
# ASR_PORT / TTS_PORT / LLM_PORT pins that one service inside it.
#
# Deliberately sets no shell options: this file is sourced, so `set -u`
# here would quietly change the caller.
: "${REPO_ROOT:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

# shellcheck source=local_env.sh
. "$REPO_ROOT/scripts/local_env.sh"

TALKNEST_PORT="${TALKNEST_PORT:-8890}"
ASR_PORT="${ASR_PORT:-$((TALKNEST_PORT + 1))}"
TTS_PORT="${TTS_PORT:-$((TALKNEST_PORT + 2))}"
LLM_PORT="${LLM_PORT:-$((TALKNEST_PORT + 3))}"
# Exported: the backend URLs in server/config.yaml are written with ${...}
# placeholders, so the orchestrator process has to see these too.
export TALKNEST_PORT ASR_PORT TTS_PORT LLM_PORT
