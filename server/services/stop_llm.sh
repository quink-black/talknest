#!/usr/bin/env bash
# Stop whatever LLM occupies the talknest slot (port 9999). Port 9999 is
# the agreed single-LLM slot on this host (vLLM and llama.cpp launchers
# alike), so fuser here only ever hits that slot.
set -u
if command -v fuser >/dev/null 2>&1; then
    fuser -k 9999/tcp 2>/dev/null || true
fi
sleep 1
if command -v fuser >/dev/null 2>&1 && fuser 9999/tcp 2>/dev/null | grep -q '[0-9]'; then
    echo "[stop_llm] WARNING: something still listens on 9999" >&2
    exit 1
fi
echo "[stop_llm] port 9999 free"
