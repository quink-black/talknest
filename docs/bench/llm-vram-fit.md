# LLM VRAM budget: KV cache vs. layers (2026-09-13)

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

`tts.pcm_chunks()` sliced the response locally, but the sidecar called
`model.generate_custom_voice()` and returned only when the whole sentence
was synthesized; qwen-tts has no streaming API (`non_streaming_mode=false`
merely simulates streamed *text input*). Measured TTS latency:

| Input | Time |
|---|---|
| "Good job!" (10 chars) | 1.17 s |
| "Nice! You saw a dog." (21) | 1.85 s |
| 76-char sentence | 4.08 s |

So a turn was ASR 0.13 s + prefill 0.5 s + decode of the first sentence
1-2 s + **TTS 2-4 s**: synthesis was the largest single term, and the
VRAM change only touches the decode slice.

## The ceiling: three models, one card

TTS later gained a streaming route, which decodes the growing prefix once
per chunk. That costs VRAM the 12G card does not have, and TTS started
raising `torch.OutOfMemoryError` mid-reply with ~40 MiB left:

    TTS 2.3 GiB + ASR 3.4 GiB + LLM 4.6 GiB = 10.3 GiB of 11.6 GiB
    usable; ~1.3 GiB more goes to CUDA contexts and vLLM's reserved pool.

Every configuration below was run as the same 14-turn WebSocket session
(no browser), first audio in ms:

| Config | VRAM left | First audio (median) | Result |
|---|---|---|---|
| 26 layers, MTP on | ~0.45 GiB | 1800 (8 turns) | OOM on turn 9 |
| 22 layers, MTP on | ~1.2 GiB | 5400 | slow, rising |
| 24 layers, MTP on | ~0.7 GiB | 5300 | slow, rising |
| **26 layers, MTP off** | ~0.95 GiB | **2800, flat** | 14/14 turns |

MTP speculative decoding is faster per token, but its draft context is
~0.4 GiB and that is the 0.4 GiB the card is short of. Buying VRAM by
removing layers costs more decode speed than MTP wins back, and the
remaining CPU layers still degrade as the session goes on.

What is on the card now: `TN9B_CTX=4096`, `TN9B_NGL=26`,
`TN9B_SPEC=none`, `ASR_GPU_UTIL=0.36`, `ASR_MAX_SEQS=1`,
`QWEN_TTS_STREAM_FRAMES=8`, and
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` for the TTS sidecar
(its peak churns small blocks while streaming).

Net effect of the two changes together: first audio went from a 6.2 s
median that climbed past 15 s, to ~2.8 s that stays flat.
