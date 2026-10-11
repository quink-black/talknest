#!/usr/bin/env bash
# Control the talknest stack through its systemd user units.
#
# Usage: services.sh {start|stop|restart|status} [asr|tts|llm|web ...]
#
# With no service names, every service is acted on. start clears any
# running instance of the selected services first -- both the units
# themselves and anything started by scripts/start.sh or tmux, through
# scripts/stop.sh -- because the modes must not share one port, then
# starts the units in one transaction so the
# After= health gates order them (LLM last). Stopping ASR or TTS also
# stops the LLM unit (Requires=), but never the orchestrator.
#
# Logs: journalctl --user -u talknest-asr -f   (etc.)
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$(dirname -- "$SCRIPT_DIR")")"

ACTION="${1:-}"
case "$ACTION" in
    start|stop|restart|status) ;;
    *)
        echo "Usage: $0 {start|stop|restart|status} [asr|tts|llm|web ...]" >&2
        exit 2
        ;;
esac
shift

SERVICES=("$@")
if [ "${#SERVICES[@]}" -eq 0 ]; then
    SERVICES=(asr tts llm web)
fi
units=()
for s in "${SERVICES[@]}"; do
    case "$s" in
        asr|tts|llm|web) units+=("talknest-$s.service") ;;
        *)
            echo "$0: unknown service '$s' (asr|tts|llm|web)" >&2
            exit 2
            ;;
    esac
done

# talknest-llm has Requires= on asr and tts, so stopping either stops it
# too. Naming only asr/tts would therefore leave the LLM down -- stranded,
# and a later start can hit the unit's start limit. Bring it along.
if [ "$ACTION" != status ] && [ "${#SERVICES[@]}" -lt 4 ]; then
    case " ${SERVICES[*]} " in
        *" llm "*) ;;
        *)
            for s in "${SERVICES[@]}"; do
                if [ "$s" = asr ] || [ "$s" = tts ]; then
                    SERVICES+=(llm); units+=("talknest-llm.service")
                    break
                fi
            done
            ;;
    esac
fi

case "$ACTION" in
    start)
        # A clean unit stop first: without it, a stray fuser kill would
        # just trigger the units' Restart=on-failure and race the
        # restart for the port. reset-failed clears a latched start
        # limit -- manual starts count toward it too, so a unit that
        # was restarted a few times in the last 5 minutes would
        # otherwise refuse to start.
        systemctl --user stop "${units[@]}" >/dev/null 2>&1 || true
        systemctl --user reset-failed "${units[@]}" >/dev/null 2>&1 || true
        # Free the ports from a tmux-mode (or manual) instance.
        "$REPO_ROOT/scripts/stop.sh" "${SERVICES[@]}" >/dev/null 2>&1 || true
        systemctl --user start "${units[@]}"
        ;;
    stop)
        systemctl --user stop "${units[@]}"
        ;;
    restart)
        systemctl --user stop "${units[@]}" >/dev/null 2>&1 || true
        systemctl --user reset-failed "${units[@]}" >/dev/null 2>&1 || true
        "$REPO_ROOT/scripts/stop.sh" "${SERVICES[@]}" >/dev/null 2>&1 || true
        systemctl --user start "${units[@]}"
        ;;
    status)
        exec systemctl --user --no-pager status "${units[@]}"
        ;;
esac

if [ "$ACTION" != status ]; then
    echo
    for u in "${units[@]}"; do
        printf '%-26s %s\n' "$u" "$(systemctl --user is-active "$u")"
    done
    echo "logs: journalctl --user -u talknest-asr -f   (etc.)"
fi
