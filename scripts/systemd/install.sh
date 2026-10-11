#!/usr/bin/env bash
# Install the talknest systemd user units.
#
# Renders the .service.in templates with this checkout's paths and the
# TLS scheme start_web.sh would pick, writes them to
# ~/.config/systemd/user/ and reloads the user manager.
#
# Usage: scripts/systemd/install.sh [--enable]
#   --enable  also enable the units for boot. Boot-without-login
#             additionally needs `loginctl enable-linger $USER`;
#             attempted here, printed when it needs authorization.
#
# Re-run after moving the checkout or changing the TLS setup or any port:
# the health URLs are baked in at render time.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"
# shellcheck source=../../scripts/ports.sh
. "$REPO_ROOT/scripts/ports.sh"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(asr tts llm web)

CURL="$(command -v curl || true)"
if [ -z "$CURL" ]; then
    echo "[install] ERROR: curl is required by the health gates" >&2
    exit 1
fi

# Mirror start_web.sh: TLS whenever cert material is configured or the
# dev cert exists; uvicorn then refuses plain HTTP.
WEB_SCHEME=http
if { [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; } \
   || { [ -z "${TLS_CERT:-}" ] && [ -f "$REPO_ROOT/certs/dev.pem" ] \
        && [ -f "$REPO_ROOT/certs/dev.key" ]; }; then
    WEB_SCHEME=https
fi
WEB_HEALTH_URL="${WEB_SCHEME}://127.0.0.1:${TALKNEST_PORT}/"
echo "[install] ports: orchestrator ${TALKNEST_PORT}, asr ${ASR_PORT}, tts ${TTS_PORT}, llm ${LLM_PORT}"

declare -A START=(
    [asr]="$REPO_ROOT/server/services/start_asr.sh"
    [tts]="$REPO_ROOT/server/services/start_tts.sh"
    [llm]="$REPO_ROOT/server/services/start_llm_llamacpp_9b.sh"
    [web]="$REPO_ROOT/scripts/start_web.sh"
)
for name in "${UNITS[@]}"; do
    if [ ! -x "${START[$name]}" ]; then
        echo "[install] ERROR: ${START[$name]} is missing or not executable" >&2
        exit 1
    fi
done

mkdir -p "$UNIT_DIR"
unit_files=()
for name in "${UNITS[@]}"; do
    dst="$UNIT_DIR/talknest-$name.service"
    sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
        -e "s|@CURL@|$CURL|g" \
        -e "s|@WEB_HEALTH_URL@|$WEB_HEALTH_URL|g" \
        -e "s|@ASR_PORT@|$ASR_PORT|g" \
        -e "s|@TTS_PORT@|$TTS_PORT|g" \
        -e "s|@LLM_PORT@|$LLM_PORT|g" \
        "$SCRIPT_DIR/talknest-$name.service.in" > "$dst"
    unit_files+=("$dst")
    echo "[install] wrote $dst"
done
systemctl --user daemon-reload

if [ "${1:-}" = "--enable" ]; then
    systemctl --user enable talknest-{asr,tts,llm,web}.service
    if loginctl enable-linger "$USER" 2>/dev/null; then
        echo "[install] lingering enabled for $USER (stack starts at boot)"
    else
        echo "[install] NOTE: boot-without-login needs: loginctl enable-linger $USER"
    fi
fi

echo "[install] done; start with: scripts/systemd/services.sh start"
echo "[install] logs: journalctl --user -u talknest-asr -f   (etc.)"
