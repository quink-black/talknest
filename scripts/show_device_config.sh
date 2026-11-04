#!/usr/bin/env bash
# Print device config files with Wi-Fi credentials redacted. Use this
# instead of cat when inspecting a real config: the files hold plaintext
# network passwords and must never appear in a log, a reply or a commit.
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$REPO_ROOT/firmware/config"

if ! command -v jq >/dev/null 2>&1; then
    echo "show_device_config: jq is required" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    FILES=("$@")
else
    shopt -s nullglob
    FILES=("$CONFIG_DIR"/talknest*.json)
fi

found=0
for file in "${FILES[@]}"; do
    case "$(basename "$file")" in
        *example*) continue ;;
    esac
    if [ ! -f "$file" ]; then
        echo "show_device_config: not a file: $file" >&2
        continue
    fi
    echo "== $(basename "$file") =="
    jq 'if has("wifi_password") then .wifi_password = "<redacted>" else . end' "$file"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "show_device_config: no real config files found" >&2
    exit 1
fi
