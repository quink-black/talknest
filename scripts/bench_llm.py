#!/usr/bin/env python3
"""LLM latency benchmark for talknest (OpenAI-compatible endpoint).

Measures TTFT, generation throughput (128/512 token budgets) and a
4-turn simulated kid-mode dialog. Each measurement runs 3 times, the
median is reported. Also asserts the reply carries no reasoning (the
non-thinking mode check).

Passing --profile reads the endpoint, model, API key and extra request
fields out of server/config.yaml, so two configured backends are measured
with the same script and the same prompts. Give --profile more than once
to compare them in one report.

Usage:
  ./venv/bin/python scripts/bench_llm.py --profile llamacpp-qwen35-9b \
      --profile vllm-qwen38-flash-lan [--rounds N] [--out docs/bench/x.md]
  ./venv/bin/python scripts/bench_llm.py [--base-url URL] [--model ID]
      [--api-key KEY] [--rounds N]
"""

import argparse
import datetime
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

TTFT_PROMPT = [
    {"role": "system", "content": "You are a warm English buddy for a child. Reply in 1-2 short sentences."},
    {"role": "user", "content": "Hello! I have a cat. His name is Doudou."},
]

# A reply long enough that the cap, not the model's own stop, decides when
# it ends: only then does throughput say something about the backend rather
# than about how talkative it is.
LONG_PROMPT = [
    {"role": "system", "content": "You are a storyteller for young children."},
    {"role": "user", "content": "Tell me a story about a cat and a dog who "
                                "become friends, about 200 words."},
]

DIALOG = [
    "Hi! My name is Mingming. I am nine.",
    "I like playing football with my friends.",
    "Yesterday I go to the park with my mother.",
    "What is your favorite animal?",
]

SYSTEM = (
    "You are a warm, encouraging English conversation buddy for a child. "
    "Use very simple words and short sentences. Keep replies to 1-3 short sentences."
)

SAMPLING_DEFAULTS = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "presence_penalty": 1.5,
}


def voice_messages() -> list[dict]:
    """What a real turn sends: the default mode's system prompt, which is
    an order of magnitude longer than the toy prompts above and is what
    the backend has to prefill before it can say anything."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    from orchestrator.config import load

    cfg = load()
    mode = cfg.get("default_mode") or next(iter(cfg["modes"]))
    return [
        {"role": "system", "content": cfg["modes"][mode]["system_prompt"]},
        {"role": "user", "content": "今天我去了公园，看到了一只小狗。"},
    ]


def profile_endpoints(names: list[str]) -> list[dict]:
    """Endpoint descriptions for the named llm profiles in config.yaml."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    from orchestrator.config import load   # same env expansion the server uses

    llm = load()["llm"]
    sampling = dict(SAMPLING_DEFAULTS)
    sampling.update(llm.get("sampling", {}))
    endpoints = []
    for name in names:
        if name not in llm["profiles"]:
            raise SystemExit(f"no such llm profile in config.yaml: {name}")
        p = llm["profiles"][name]
        headers = {}
        if key := p.get("api_key"):
            headers["Authorization"] = f"Bearer {key}"
        endpoints.append({
            "name": name,
            "label": p.get("label", name),
            "base_url": p["base_url"].rstrip("/"),
            "model": p["model"],
            "headers": headers,
            "extra": dict(p.get("extra_body") or {}),
            "sampling": sampling,
        })
    return endpoints


def stream_once(client, ep, messages, max_tokens):
    """One streamed request. Returns (ttft_s, total_s, n_tokens, text, reasoning).

    Token count comes from the final usage chunk when the server sends one
    and is the number of content deltas otherwise: vLLM answers with fewer
    chunks than tokens, so counting deltas undercounts it by more than
    half and makes its generation look slower than it is.
    """
    body = {
        "model": ep["model"],
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    body.update(ep["sampling"])
    body.update(ep["extra"])
    t0 = time.perf_counter()
    ttft = None
    n_tokens = 0
    used = None
    reasoning = 0
    chunks = []
    with client.stream(
        "POST",
        f"{ep['base_url']}/chat/completions",
        json=body,
        headers=ep["headers"],
        timeout=120.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if usage := chunk.get("usage"):
                used = usage.get("completion_tokens")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0]["delta"]
            if delta.get("reasoning"):
                reasoning += 1
            content = delta.get("content")
            if content:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                n_tokens += 1
                chunks.append(content)
    total = time.perf_counter() - t0
    return ttft, total, (used if used else n_tokens), "".join(chunks), reasoning


def measure(client, ep, messages, rounds, max_tokens):
    """Median of `rounds` runs. Returns TTFT, total, tokens and the decode
    rate over the stretch after the first token -- dividing tokens by the
    total would charge the wait for the first token to generation, which
    makes a terse model look slow.

    A run that emits no content has no TTFT and no decode rate to take a
    median of, so those runs are counted in `no_content` and left out of
    the medians. This is the backend the orchestrator reports as an empty
    reply, so the script has to name it instead of failing on the subtraction.
    """
    runs = [stream_once(client, ep, messages, max_tokens) for _ in range(rounds)]
    seen = [r for r in runs if r[0] is not None]
    med = statistics.median
    stats = {
        "no_content": len(runs) - len(seen),
        "reasoning": sum(r[4] for r in runs),
        "last_text": runs[-1][3],
    }
    if not seen:
        stats.update({"ttft_med_s": None, "total_med_s": med(r[1] for r in runs),
                      "tokens_med": 0, "decode_tok_s": None})
        return stats
    stats.update({
        "ttft_med_s": med(r[0] for r in seen),
        "total_med_s": med(r[1] for r in seen),
        "tokens_med": med(r[2] for r in seen),
        "decode_tok_s": med(n / max(total - ttft, 1e-9)
                            for ttft, total, n, _, _ in seen),
    })
    return stats


def _ms(value):
    return "n/a" if value is None else f"{value * 1000:.0f} ms"


def _rate(value):
    return "n/a" if value is None else f"{value:.1f}"


def dialog_e2e(client, ep):
    """4-turn scripted dialog; returns list of per-turn total seconds."""
    messages = [{"role": "system", "content": SYSTEM}]
    turns = []
    for user_text in DIALOG:
        messages.append({"role": "user", "content": user_text})
        _, total, _, text, _ = stream_once(client, ep, messages, 150)
        turns.append(round(total, 3))
        messages.append({"role": "assistant", "content": text})
    return turns


def run(client, ep, rounds, voice):
    # Warmup (model load, caches, prefix cache) is not measured.
    stream_once(client, ep, TTFT_PROMPT, 16)
    return {
        "endpoint": ep,
        "r128": measure(client, ep, TTFT_PROMPT, rounds, 128),
        "r512": measure(client, ep, TTFT_PROMPT, rounds, 512),
        "rlong": measure(client, ep, LONG_PROMPT, rounds, 400),
        "rvoice": measure(client, ep, voice, rounds, 300) if voice else None,
        "dialog": dialog_e2e(client, ep),
    }


def report(results, rounds):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# LLM latency: " + " vs ".join(r["endpoint"]["label"] for r in results),
        "",
        f"- Date: {now}",
        f"- Host: {platform.node()}",
        f"- Rounds per measurement: {rounds} (median); warmup run excluded",
        "- Sampling: temp 0.7 / top_p 0.8 / top_k 20 / presence 1.5"
        " (config llm.sampling when it sets them)",
        "- Tokens are the server's own completion token count, not the number"
        " of streamed deltas: vLLM packs several tokens into one delta.",
        "- Decode rate counts only the stretch after the first token, so it"
        " compares generation rather than how much a model likes to say.",
        "",
        "## 短回复（语音场景的样子，128 token 上限）",
        "",
        "| Backend | Endpoint | TTFT | Total | Tokens | Decode tok/s |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        ep, a = r["endpoint"], r["r128"]
        lines.append(
            f"| {ep['label']} | `{ep['base_url']}` {ep['model']} "
            f"| {_ms(a['ttft_med_s'])} | {a['total_med_s']:.2f} s "
            f"| {a['tokens_med']:.0f} | {_rate(a['decode_tok_s'])} |"
        )
    lines += [
        "",
        "## 长回复（400 token 上限：到上限才停，解码速度才有意义）",
        "",
        "| Backend | Tokens | Total | Decode tok/s |",
        "|---|---|---|---|",
    ]
    for r in results:
        ep, a = r["endpoint"], r["rlong"]
        lines.append(
            f"| {ep['label']} | {a['tokens_med']:.0f} "
            f"| {a['total_med_s']:.2f} s | {_rate(a['decode_tok_s'])} |"
        )
    if results[0]["rvoice"] is not None:
        lines += [
            "",
            "## 一个真实回合（默认模式的系统提示词，上限 300 token）",
            "",
            "| Backend | TTFT | Total | Tokens | Decode tok/s |",
            "|---|---|---|---|---|",
        ]
        for r in results:
            a = r["rvoice"]
            lines.append(
                f"| {r['endpoint']['label']} | {_ms(a['ttft_med_s'])} "
                f"| {a['total_med_s']:.2f} s | {a['tokens_med']:.0f} "
                f"| {_rate(a['decode_tok_s'])} |"
            )
    lines += [
        "",
        "## 4 轮对话（每轮上限 150 token，秒）",
        "",
        "| Backend | " + " | ".join(f"Turn {i+1}" for i in range(4)) + " |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['endpoint']['label']} | "
                     + " | ".join(f"{t:.2f}" for t in r["dialog"]) + " |")
    lines.append("")
    for r in results:
        ep, a, b, c = r["endpoint"], r["r128"], r["rlong"], r["r512"]
        thought = a["reasoning"] or b["reasoning"] or c["reasoning"]
        leak = any("<think>" in t or "</think>" in t
                   for t in (a["last_text"], b["last_text"]))
        empty = a["no_content"] or b["no_content"] or c["no_content"]
        if empty:
            verdict = f"FAIL — {empty} run(s) returned no content"
        elif thought or leak:
            verdict = "FAIL — the model reasoned before answering"
        else:
            verdict = "pass (no reasoning tokens)"
        lines += [
            f"## {ep['label']}",
            "",
            f"- Profile `{ep['name']}`: `{ep['base_url']}` model `{ep['model']}`",
            f"- Extra request fields: {ep['extra'] or 'none'}",
            f"- 512-token budget: {c['total_med_s']:.2f} s, "
            f"{c['tokens_med']:.0f} tokens",
            f"- Non-thinking check: {verdict}",
            f"- Sample reply: {a['last_text'].strip()[:160]!r}",
            "",
        ]
    lines += [
        "> Measured on this host at this date with the prompts in this",
        "> script. A backend on another host also pays for the network: its",
        "> TTFT includes the round trip.",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="append", default=[], metavar="NAME",
                    help="llm profile in server/config.yaml; repeat to compare")
    ap.add_argument("--base-url",
                    default=f"http://127.0.0.1:{os.environ.get('LLM_PORT', 8893)}/v1")
    ap.add_argument("--model", default="qwen3.5-9b")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default="docs/bench/m1-llm.md")
    args = ap.parse_args()

    voice = None
    if args.profile:
        endpoints = profile_endpoints(args.profile)
        try:
            voice = voice_messages()
        except Exception as e:   # no config, no modes: skip that section
            print(f"[bench] no voice-turn prompt: {e}")
    else:
        endpoints = [{
            "name": "cli", "label": args.model,
            "base_url": args.base_url.rstrip("/"), "model": args.model,
            "headers": ({"Authorization": f"Bearer {args.api_key}"}
                        if args.api_key else {}),
            "extra": {}, "sampling": dict(SAMPLING_DEFAULTS),
        }]

    with httpx.Client(trust_env=False) as client:
        results = [run(client, ep, args.rounds, voice) for ep in endpoints]
    out = report(results, args.rounds)
    print(out)
    if args.out:
        path = Path(args.out)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(out)


if __name__ == "__main__":
    main()
