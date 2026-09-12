#!/usr/bin/env python3
"""LLM latency benchmark for talknest (OpenAI-compatible endpoint).

Measures TTFT, generation throughput (128/512 token budgets) and a
4-turn simulated kid-mode dialog. Each measurement runs 3 times, the
median is reported. Also asserts the reply contains no <think> block
(non-thinking mode check).

Usage:
  ./venv/bin/python scripts/bench_llm.py [--base-url URL] [--model ID]
      [--rounds N] [--out docs/bench/m1-llm.md]
"""

import argparse
import datetime
import json
import platform
import statistics
import time

import httpx

TTFT_PROMPT = [
    {"role": "system", "content": "You are a warm English buddy for a child. Reply in 1-2 short sentences."},
    {"role": "user", "content": "Hello! I have a cat. His name is Doudou."},
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


def stream_once(client, base_url, model, messages, max_tokens):
    """One streamed request. Returns (ttft_s, total_s, n_tokens, text)."""
    t0 = time.perf_counter()
    ttft = None
    n_tokens = 0
    chunks = []
    with client.stream(
        "POST",
        f"{base_url}/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
        },
        timeout=120.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            delta = json.loads(payload)["choices"][0]["delta"]
            content = delta.get("content")
            if content:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                n_tokens += 1
                chunks.append(content)
    total = time.perf_counter() - t0
    return ttft, total, n_tokens, "".join(chunks)


def measure(client, base_url, model, rounds, max_tokens):
    ttfts, totals, counts, texts = [], [], [], []
    for _ in range(rounds):
        ttft, total, n, text = stream_once(client, base_url, model, TTFT_PROMPT, max_tokens)
        ttfts.append(ttft)
        totals.append(total)
        counts.append(n)
        texts.append(text)
    return {
        "ttft_med_s": statistics.median(ttfts),
        "total_med_s": statistics.median(totals),
        "tokens_med": statistics.median(counts),
        "tok_per_s": statistics.median(counts) / statistics.median(totals),
        "last_text": texts[-1],
    }


def dialog_e2e(client, base_url, model):
    """4-turn scripted dialog; returns list of per-turn total seconds."""
    messages = [{"role": "system", "content": SYSTEM}]
    turns = []
    for user_text in DIALOG:
        messages.append({"role": "user", "content": user_text})
        t0 = time.perf_counter()
        _, total, _, text = stream_once(client, base_url, model, messages, 150)
        turns.append(round(total, 3))
        messages.append({"role": "assistant", "content": text})
    return turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:9999/v1")
    ap.add_argument("--model", default="qwen3.5-9b")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default="docs/bench/m1-llm.md")
    args = ap.parse_args()

    with httpx.Client(trust_env=False) as client:
        # Warmup (model load, caches).
        stream_once(client, args.base_url, args.model, TTFT_PROMPT, 16)

        r128 = measure(client, args.base_url, args.model, args.rounds, 128)
        r512 = measure(client, args.base_url, args.model, args.rounds, 512)
        dialog = dialog_e2e(client, args.base_url, args.model)
        sample_texts = [r128["last_text"], r512["last_text"]]
        think_leak = any("<think>" in t or "</think>" in t for t in sample_texts)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# M1 LLM baseline (talknest)",
        "",
        f"- Date: {now}",
        f"- Host: {platform.node()}",
        f"- Endpoint: {args.base_url}  model: {args.model}",
        f"- Rounds per measurement: {args.rounds} (median); warmup run excluded",
        f"- Sampling: temp 0.7 / top_p 0.8 / top_k 20 / presence 1.5 (non-thinking)",
        f"- Non-thinking check: {'FAIL — <think> tags leaked' if think_leak else 'pass (no <think> tags)'}",
        "",
        "## Results",
        "",
        "| Metric | 128-token budget | 512-token budget |",
        "|---|---|---|",
        f"| TTFT (median) | {r128['ttft_med_s']*1000:.0f} ms | {r512['ttft_med_s']*1000:.0f} ms |",
        f"| Total (median) | {r128['total_med_s']:.2f} s | {r512['total_med_s']:.2f} s |",
        f"| Tokens (median) | {r128['tokens_med']:.0f} | {r512['tokens_med']:.0f} |",
        f"| Throughput | {r128['tok_per_s']:.1f} tok/s | {r512['tok_per_s']:.1f} tok/s |",
        "",
        f"## 4-turn dialog e2e (per turn, s)",
        "",
        "| Turn 1 | Turn 2 | Turn 3 | Turn 4 |",
        "|---|---|---|---|",
        "| " + " | ".join(str(t) for t in dialog) + " |",
        "",
        "> Numbers are measured on this host at this date with the server flags",
        "> recorded in logs/llama.current.log. Not comparable across hosts.",
        "",
    ]
    report = "\n".join(lines)
    print(report)
    if args.out:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    main()
