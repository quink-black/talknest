# LLM VRAM budget: KV cache vs. layers (2026-09-13, black)

Host: x86_64, RTX 3060 12G, i7-12700F. Stack: ASR vLLM fp8 @ 0.36 util,
TTS qwen-tts sidecar, llama.cpp Qwen3.5-9B Q4_K_M (MTP draft).

## What led here

"First audio" grew from ~4 s to ~17 s over a 19-turn session. The
context was not the cause: `n_tokens` stayed at 840-880 and llama.cpp
reused the slot (only 16-50 new tokens per turn). The cause was the
decode rate collapsing from 18 tok/s (idle host) to 4-6 tok/s under
sustained concurrent load, because a large share of the 33 layers ran on
the CPU and competed with ASR, TTS and the browser for cores.

## Where the VRAM went

Qwen3.5-9B Q4_K_M is 5.87 GB. llama.cpp had 4720 MiB of VRAM total, and
the KV cache at `-c 8192 --kv-unified` alone was 1.03 GiB:

    KV = 33 layers * 4 KV heads * head_dim 256 * 2 B = 132 KB/token
       = 1.03 GiB @ 8192, 0.52 GiB @ 4096, 0.26 GiB @ 2048

A turn needs ~700 tokens (system prompt 386 + 20 history messages) plus
the reply: measured peak 880, worst case 999. So 4096 keeps 4x headroom
and 2048 would have been only 2x.

## The surprise: -fit does not spend freed VRAM on layers

With `-c 4096` and `-ctk/-ctv q8_0` (KV 0.26 GiB) llama.cpp used **less**
VRAM (3962 MiB vs 4720) instead of offloading more layers: ~2.0 GB of
the card stayed free. Pinning `TN9B_NGL` is what actually moves layers
off the CPU.

## Measurements

Decode throughput, `scripts/bench_llm.py`, median of 3 rounds:

| Config | 128 tok | 512 tok | TTFT |
|---|---|---|---|
| before: `-fit`, ctx 8192, KV f16 (4720 MiB) | 18.0 | 17.7 | 121 ms |
| ctx 4096 + KV q8_0, `-fit` (3962 MiB) | 11.3-12.6 | 10.5-16.9 | 137 ms |
| **after: ctx 4096, KV f16, NGL 26 (4724 MiB)** | **17.6** | **20.5** | **112 ms** |

Under 12 CPU-spinning processes (what a live session looks like on the
CPU side):

| Config | 128 tok | 512 tok |
|---|---|---|
| before | 10.5 | 7.4 |
| after | **14.7** | **12.8** |

End-to-end, 14-turn WebSocket session (no browser): median decode
13.1 -> 15.1 tok/s, but "first audio" was unchanged (6.16 s -> 6.48 s
median), because that metric is dominated by TTS, not by the LLM.

## Why first audio barely moved

`tts.pcm_chunks()` slices the response locally, but the sidecar calls
`model.generate_custom_voice()` and returns only when the whole sentence
is synthesized; qwen-tts has no true streaming (`non_streaming_mode=false`
merely simulates streamed *text input*). Measured TTS latency:

| Input | Time |
|---|---|
| "Good job!" (10 chars) | 1.17 s |
| "Nice! You saw a dog." (21) | 1.85 s |
| 76-char sentence | 4.08 s |

So a turn is ASR 0.13 s + prefill 0.5 s + decode of the first sentence
1-2 s + **TTS 2-4 s**. Cutting the first TTS chunk (emit at clause
boundaries, not only at sentence ends) and speeding TTS up are the
remaining levers; the VRAM change only helps the decode slice.
