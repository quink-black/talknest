#!/usr/bin/env python3
"""Service health probe for AI debugging.

Checks every talknest backend, prints a compact JSON summary with the
API shape each service exposes. Exit code 0 only if all required
services answer.

Usage: ./venv/bin/python tests/api_probe.py [--full]
"""

import argparse
import json
import sys

import httpx

TARGETS = {
    "llm": "http://127.0.0.1:9999/v1/models",
    "asr": "http://127.0.0.1:8001/v1/models",
    "tts": "http://127.0.0.1:8002/v1/models",
    "tts_speakers": "http://127.0.0.1:8002/v1/speakers",
}


def probe(client: httpx.Client, url: str) -> dict:
    try:
        r = client.get(url, timeout=3.0)
        body = r.json()
        models = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
        return {"url": url, "status": r.status_code, "models": models or body}
    except Exception as e:
        return {"url": url, "status": "error", "error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="print full bodies")
    args = ap.parse_args()
    with httpx.Client(trust_env=False) as client:
        result = {name: probe(client, url) for name, url in TARGETS.items()}
    ok = all(v.get("status") == 200 for k, v in result.items() if k in ("llm", "asr", "tts"))
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.full else None))
    print("ALL_SERVICES_UP" if ok else "SERVICES_DEGRADED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
