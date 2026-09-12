#!/usr/bin/env bash
# Generate a self-signed dev certificate (certs/dev.pem + certs/dev.key)
# with SANs for localhost and this host's LAN IPv4 addresses, then print
# the TLS run command.
#
# Chrome requires a SubjectAltName covering the visited host; the cert is
# untrusted (self-signed), so the browser will show a one-click warning.
# To silence it entirely, import certs/dev.pem into the client trust
# store (macOS: Keychain Access -> System -> trust for SSL).
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="$REPO_ROOT/certs"
mkdir -p "$CERT_DIR"

# Collect LAN IPv4s (best effort) for the SAN list.
IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -n 4)
SAN="DNS:localhost,IP:127.0.0.1"
for ip in $IPS; do
    SAN="$SAN,IP:$ip"
done

if [ -f "$CERT_DIR/dev.pem" ] && [ -f "$CERT_DIR/dev.key" ]; then
    echo "[gen_dev_cert] existing cert found: $CERT_DIR/dev.pem (delete to regenerate)"
else
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -keyout "$CERT_DIR/dev.key" -out "$CERT_DIR/dev.pem" \
        -subj "/CN=talknest-dev" -addext "subjectAltName=$SAN"
    chmod 600 "$CERT_DIR/dev.key"
    echo "[gen_dev_cert] wrote $CERT_DIR/dev.pem (SAN: $SAN)"
fi

echo
echo "Add these to local.env so the scripts pick TLS up without arguments:"
echo "  TLS_CERT=$CERT_DIR/dev.pem"
echo "  TLS_KEY=$CERT_DIR/dev.key"
echo "Then start the orchestrator with scripts/start_web.sh and open"
echo "https://<talknest-host>:8890, accepting the self-signed warning."
