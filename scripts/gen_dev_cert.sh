#!/usr/bin/env bash
# Generate a self-signed dev certificate (certs/dev.pem + certs/dev.key)
# with SANs for localhost, this host's mDNS name and LAN IPv4 addresses,
# then print the TLS run command.
#
# Additional DNS names and IPv4 addresses come from the environment (put
# them in local.env, which is git-ignored), so no deployment-specific host
# name or address is written into the repository:
#   TALKNEST_CERT_DNS=host1.local,host2.local
#   TALKNEST_CERT_IPS=10.0.0.10,10.0.0.11
#
# Chrome requires a SubjectAltName covering the visited host; the cert is
# untrusted (self-signed), so the browser will show a one-click warning.
# To silence it entirely, import certs/dev.pem into the client trust
# store (macOS: Keychain Access -> System -> trust for SSL).
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Load the git-ignored machine settings, as the service scripts do. Without
# this, the two variables documented in local.env.example would only work
# when exported by hand on the command line.
. "$REPO_ROOT/scripts/local_env.sh"
CERT_DIR="$REPO_ROOT/certs"
mkdir -p "$CERT_DIR"

# Collect LAN IPv4s (best effort) for the SAN list. macOS hostname has no
# -I, and the error goes to /dev/null, so its list silently comes back
# empty and the cert ends up without a SAN an iPad can match.
if [ "$(uname -s)" = "Darwin" ]; then
    IPS=$(ifconfig 2>/dev/null | awk '$1 == "inet" && $2 != "127.0.0.1" {print $2}' | head -n 4)
else
    IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -n 4)
fi
SHORT_HOST="$(hostname -s 2>/dev/null || hostname)"
EXTRA_DNS="${TALKNEST_CERT_DNS:-}"
EXTRA_IPS="${TALKNEST_CERT_IPS:-}"
SAN="DNS:localhost,DNS:${SHORT_HOST}.local,IP:127.0.0.1"
# Comma-separated extras; validate so a stray value cannot inject an
# openssl extension or produce an invalid SAN.
for name in ${EXTRA_DNS//,/ }; do
    name="$(printf '%s' "$name" | tr -d '[:space:]')"
    if [ -z "$name" ]; then continue; fi
    if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
        echo "[gen_dev_cert] invalid DNS name in TALKNEST_CERT_DNS: $name" >&2
        exit 1
    fi
    SAN="$SAN,DNS:$name"
done
for ip in $IPS; do
    SAN="$SAN,IP:$ip"
done
for ip in ${EXTRA_IPS//,/ }; do
    ip="$(printf '%s' "$ip" | tr -d '[:space:]')"
    if [ -z "$ip" ]; then continue; fi
    if [[ ! "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "[gen_dev_cert] invalid IP in TALKNEST_CERT_IPS: $ip" >&2
        exit 1
    fi
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
