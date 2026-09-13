#!/usr/bin/env python3
"""End-to-end WebSocket smoke test for the talknest free-talk loop.

Synthesizes a user utterance with the TTS service and pushes it over the
orchestrator WebSocket as a live mic would: PCM at real-time pace, then
silence so the streaming VAD closes the turn. Verifies VAD turn taking ->
ASR -> LLM stream -> TTS reply audio.

With --barge the user speaks again once the reply starts playing, which
must cut the assistant off and open a second turn.

Usage:
  ./venv/bin/python tests/ws_smoke.py [--url ws://127.0.0.1:8890/ws]
      [--text "Hello, I am Mingming."] [--mode kid] [--barge]
      [--save /tmp/reply.wav]

Exit 0 if the loop produced a reply and audio, 1 otherwise.
"""

import argparse
import asyncio
import contextlib
import io
import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
import websockets

TTS_URL = "http://127.0.0.1:8002/v1/audio/speech"
CLIENT_SR = 24000  # matches orchestrator CLIENT_SR (Qwen3-ASR native rate)
CHUNK = 1024       # samples per frame, as sent by the browser worklet
SILENCE_S = 1.5    # trailing silence the VAD needs to close a turn


async def tts_wav(text: str, voice: str, language: str) -> tuple[np.ndarray, int]:
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        r = await client.post(TTS_URL, json={"input": text, "voice": voice,
                                             "language": language})
        r.raise_for_status()
        audio, sr = sf.read(io.BytesIO(r.content), dtype="int16")
    return audio, sr


async def push(ws, pcm: np.ndarray):
    for i in range(0, len(pcm), CHUNK):
        await ws.send(pcm[i : i + CHUNK].astype("<i2").tobytes())
        await asyncio.sleep(CHUNK / CLIENT_SR)


async def push_silence(ws, seconds: float):
    await push(ws, np.zeros(int(seconds * CLIENT_SR), dtype=np.int16))


def loudest_window(pcm: np.ndarray, seconds: float) -> np.ndarray:
    """The most energetic `seconds` of a clip.

    Synthesized speech opens with hundreds of milliseconds of room tone,
    so taking the head of the clip hands the VAD a mostly silent burst
    that it drops as noise instead of a short utterance.
    """
    n = int(seconds * CLIENT_SR)
    if len(pcm) <= n:
        return pcm
    rms = np.sqrt(np.convolve(pcm.astype(np.float64) ** 2,
                              np.ones(n) / n, mode="valid"))
    start = int(np.argmax(rms))
    return pcm[start : start + n]


async def pump(ws, pcm, reply_started: asyncio.Event, stop: asyncio.Event,
               barge: bool, reask: bool, transcribed: asyncio.Event):
    """Play the utterance, then keep the stream alive with silence."""
    await push(ws, pcm)
    await push_silence(ws, SILENCE_S)
    if reask:
        # Speak again while the answer is still being produced: no audio
        # has played yet, and an impatient "喂?" must not throw away a
        # reply that is seconds away from being spoken.
        try:
            async with asyncio.timeout(60):
                await transcribed.wait()
        except TimeoutError:
            print("[smoke] TIMEOUT waiting for the transcription")
            return
        await asyncio.sleep(0.6)
        # Short enough to be impatience rather than a new question, long
        # enough that the VAD accepts it as an utterance at all.
        print("[smoke] re-asking before the reply has sounded (impatience)")
        await push(ws, loudest_window(pcm, 0.6))
        await push_silence(ws, SILENCE_S)
    if barge:
        try:
            async with asyncio.timeout(60):
                await reply_started.wait()
        except TimeoutError:
            print("[smoke] TIMEOUT waiting for the reply to start playing")
            return
        print("[smoke] speaking over the reply (barge-in)")
        await push(ws, pcm)
        await push_silence(ws, SILENCE_S)
    silence = np.zeros(CHUNK, dtype=np.int16).astype("<i2").tobytes()
    while not stop.is_set():
        await ws.send(silence)
        await asyncio.sleep(CHUNK / CLIENT_SR)


async def run(url: str, text: str, mode: str, save: str | None,
              barge: bool, reask: bool) -> bool:
    # Match the probe voice to the mode's conversation language: kid
    # modes speak Chinese, adult mode English.
    if mode == "adult":
        audio, sr = await tts_wav(text, "aiden", "English")
    else:
        audio, sr = await tts_wav(text, "vivian", "Auto")
    pcm16 = audio if sr == CLIENT_SR else (
        np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * CLIENT_SR / sr)),
                  np.arange(len(audio)), audio.astype(np.float32)).astype(np.int16)
    )
    print(f"[smoke] user audio: {len(pcm16)/CLIENT_SR:.1f}s @{CLIENT_SR} (tts sr {sr})")

    ssl_ctx = None
    if url.startswith("wss"):
        # Dev servers use self-signed certs; skip verification.
        import ssl
        ssl_ctx = ssl._create_unverified_context()
    events, audio_chunks, reply_text, user_texts = [], [], [], []
    reply_started, stop, transcribed = (asyncio.Event(), asyncio.Event(),
                                        asyncio.Event())
    # A real client keeps playing after the last frame arrives, so report
    # playback end only once the buffered audio would have drained.
    played: asyncio.Task | None = None
    started_at, pending_bytes, reply_sr = 0.0, 0, CLIENT_SR
    cut_seen = False

    async def report_ended(delay: float):
        await asyncio.sleep(delay)
        await ws.send(json.dumps({"type": "playback", "value": "ended"}))

    async with websockets.connect(url, max_size=8 * 1024 * 1024, ssl=ssl_ctx) as ws:
        await ws.send(json.dumps({"type": "mode", "value": mode}))
        await ws.send(json.dumps({"type": "start"}))
        feeder = asyncio.create_task(pump(ws, pcm16, reply_started, stop,
                                         barge, reask, transcribed))
        try:
            async with asyncio.timeout(120):
                async for msg in ws:
                    if isinstance(msg, bytes):
                        audio_chunks.append(msg)
                        pending_bytes += len(msg)
                        continue
                    ev = json.loads(msg)
                    events.append(ev)
                    if ev["type"] == "audio_start":
                        reply_started.set()
                        started_at = time.monotonic()
                        pending_bytes = 0
                        reply_sr = ev["sample_rate"]
                    elif ev["type"] == "audio_end":
                        seconds = pending_bytes / 2 / reply_sr
                        played = asyncio.create_task(
                            report_ended(max(0.0, started_at + seconds
                                             - time.monotonic())))
                    elif ev["type"] == "stop_playback":
                        cut_seen = True
                        if played is not None:
                            played.cancel()
                        await ws.send(json.dumps(
                            {"type": "playback", "value": "ended"}))
                    elif ev["type"] == "user_text":
                        user_texts.append(ev["text"])
                        transcribed.set()
                        print(f"[smoke] asr: {ev['text']!r}")
                    elif ev["type"] == "reply_delta":
                        reply_text.append(ev["text"])
                    elif ev["type"] in ("error", "tts_error"):
                        print(f"[smoke] {ev['type']}: {ev['message']}")
                    elif ev["type"] == "reply_done":
                        # The first reply may be cut short by the barge-in,
                        # so keep streaming until the interruption lands.
                        if barge and not (cut_seen and len(user_texts) >= 2):
                            continue
                        break
        except TimeoutError:
            print("[smoke] TIMEOUT waiting for reply_done")
        finally:
            stop.set()
            for t in (feeder, played):
                if t is not None:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t

    reply = "".join(reply_text)
    audio_bytes = sum(len(c) for c in audio_chunks)
    marks = next((e["marks"] for e in events if e["type"] == "reply_done"), {})
    print(f"[smoke] reply: {reply!r}")
    print(f"[smoke] audio bytes: {audio_bytes}")
    print(f"[smoke] marks: {json.dumps(marks)}")

    if save and audio_chunks:
        sr_reply = next((e["sample_rate"] for e in events
                         if e["type"] == "audio_start"), 24000)
        pcm = np.frombuffer(b"".join(audio_chunks), dtype=np.int16)
        sf.write(save, pcm, sr_reply, format="WAV", subtype="PCM_16")
        print(f"[smoke] reply audio saved: {save}")

    ok = bool(reply.strip()) and audio_bytes > 16000
    if reask:
        # The impatience must have been swallowed: one turn, one answer.
        print(f"[smoke] re-ask survived: turns={len(user_texts)}")
        ok = ok and len(user_texts) == 1
    if barge:
        print(f"[smoke] barge-in seen: {cut_seen}, turns: {len(user_texts)}")
        ok = ok and cut_seen and len(user_texts) >= 2
    print("[smoke] RESULT:", "PASS" if ok else "FAIL")
    return ok


def default_url() -> str:
    """Mirror start_web.sh's TLS decision so the default URL just works:
    the orchestrator serves TLS (only) whenever cert material is set or
    the dev certificate exists."""
    cert = os.environ.get("TLS_CERT", "")
    key = os.environ.get("TLS_KEY", "")
    repo = Path(__file__).resolve().parents[1]
    tls = bool(cert and key) or (
        not cert and (repo / "certs/dev.pem").exists()
        and (repo / "certs/dev.key").exists()
    )
    scheme = "wss" if tls else "ws"
    port = os.environ.get("TALKNEST_PORT", "8890")
    return f"{scheme}://127.0.0.1:{port}/ws"


async def main_async():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="WebSocket URL (default: auto-detect ws/wss)")
    ap.add_argument("--text", default="Hello! My name is Mingming. I am nine years old.")
    ap.add_argument("--mode", default="kid", choices=["kid", "kid_upper", "adult"])
    ap.add_argument("--barge", action="store_true",
                    help="speak over the reply and expect it to be cut off")
    ap.add_argument("--reask", action="store_true",
                    help="speak again before any audio has played; the "
                         "answer must still arrive")
    ap.add_argument("--save", default=None)
    args = ap.parse_args()
    url = args.url or default_url()
    print(f"[smoke] url: {url}")
    ok = await run(url, args.text, args.mode, args.save, args.barge,
                   args.reask)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main_async())
