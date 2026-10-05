#!/usr/bin/env python3
"""End-to-end smoke test for speaker recognition.

Synthesizes three different TTS voices to stand in for the child, dad and
mom, enrolls them through the voiceprint REST endpoints, then speaks a new
sentence in each voice over the WebSocket and checks that the `user_text`
event names the right person.

The orchestrator should run on a scratch data directory, because this
script enrolls voices for the learner it uses:

  TALKNEST_DATA_DIR=/tmp/talknest-e2e ./venv/bin/uvicorn orchestrator.main:app \
      --app-dir server --port 8891
  ./venv/bin/python tests/ws_speaker_smoke.py --base http://127.0.0.1:8891

Exit 0 if every voice was named correctly, 1 otherwise.
"""

import argparse
import asyncio
import json

import httpx
import numpy as np
import websockets

from ws_smoke import CLIENT_SR, push, push_silence, tts_wav

VOICES = {"child": "dylan", "dad": "uncle_fu", "mom": "vivian"}
ENROLL_TEXTS = [
    "Hello, my name is Ming. I like apples and I like to play with blocks.",
    "Today the weather is very nice, so we can go to the park and fly a kite.",
    "I have a red ball and a blue car. Can you help me find my little cat?",
]
TEST_TEXTS = {
    "child": "Can I have some water, please? I am thirsty after school.",
    "dad": "Let us read a short story together before it is time to sleep.",
    "mom": "Remember to wash your hands, then come to the table for dinner.",
}


def to_client_rate(audio: np.ndarray, sr: int) -> np.ndarray:
    if sr == CLIENT_SR:
        return audio.astype(np.int16)
    n = int(len(audio) * CLIENT_SR / sr)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)),
                     audio.astype(np.float32)).astype(np.int16)


async def synth(text: str, voice: str) -> np.ndarray:
    audio, sr = await tts_wav(text, voice, "English")
    return to_client_rate(audio, sr)


async def enroll(base: str, learner: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=60.0,
                                 trust_env=False) as http:
        for role, voice in VOICES.items():
            await http.delete(f"/learners/{learner}/speakers/{role}")
            for text in ENROLL_TEXTS:
                pcm = (await synth(text, voice))[: 8 * CLIENT_SR]
                r = await http.post(f"/learners/{learner}/speakers/{role}",
                                    content=pcm.astype("<i2").tobytes())
                print(f"[speaker] enroll {role}: {r.status_code} {r.json()}"
                      if r.status_code != 200 else
                      f"[speaker] enroll {role}: {r.json()['seconds']} s voiced")
        status = (await http.get(f"/learners/{learner}/speakers")).json()
        print(f"[speaker] enabled={status['enabled']}")
        if not status["enabled"]:
            raise SystemExit("enrollment did not enable matching")


async def heard_as(ws_url: str, learner: str, pcm: np.ndarray) -> str | None:
    """Speak one utterance and return the speaker the server reports."""
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "learner", "value": learner}))
        await ws.send(json.dumps({"type": "start"}))

        async def feed():
            await push(ws, pcm)
            await push_silence(ws, 1.5)
            while True:
                await push_silence(ws, 0.5)

        feeder = asyncio.create_task(feed())
        try:
            async with asyncio.timeout(90):
                async for msg in ws:
                    if isinstance(msg, bytes):
                        continue
                    ev = json.loads(msg)
                    if ev["type"] == "user_text":
                        print(f"[speaker] asr: {ev['text']!r}")
                        speaker = ev.get("speaker")
                        # Stop the reply and give the server a moment to
                        # wind the turn down, so it is not left writing to
                        # a closed socket.
                        await ws.send(json.dumps({"type": "interrupt"}))
                        await asyncio.sleep(1.0)
                        return speaker
                    elif ev["type"] == "error":
                        print(f"[speaker] error: {ev['message']}")
        finally:
            feeder.cancel()
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8891")
    ap.add_argument("--learner", default="speaker-smoke")
    args = ap.parse_args()
    ws_url = args.base.replace("http", "ws", 1) + "/ws"

    await enroll(args.base, args.learner)
    failures = 0
    for role, voice in VOICES.items():
        pcm = await synth(TEST_TEXTS[role], voice)
        got = await heard_as(ws_url, args.learner, pcm)
        ok = got == role
        failures += not ok
        print(f"[speaker] {role} ({voice}) -> {got}: {'ok' if ok else 'WRONG'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
