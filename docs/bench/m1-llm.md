# M1 LLM baseline (talknest)

- Date: 2026-09-12 18:51
- Host: DGX Spark (GB10, 120G unified memory)
- Endpoint: http://127.0.0.1:9999/v1  model: qwen3.5-9b
- Rounds per measurement: 3 (median); warmup run excluded
- Sampling: temp 0.7 / top_p 0.8 / top_k 20 / presence 1.5 (non-thinking)
- Non-thinking check: pass (no <think> tags)

## Results

| Metric | 128-token budget | 512-token budget |
|---|---|---|
| TTFT (median) | 72 ms | 72 ms |
| Total (median) | 0.87 s | 1.02 s |
| Tokens (median) | 29 | 27 |
| Throughput | 33.4 tok/s | 26.5 tok/s |

## 4-turn dialog e2e (per turn, s)

| Turn 1 | Turn 2 | Turn 3 | Turn 4 |
|---|---|---|---|
| 0.943 | 1.078 | 0.726 | 0.764 |

> Numbers are measured on this host at this date with the server flags
> recorded in logs/llama.current.log. Not comparable across hosts.
