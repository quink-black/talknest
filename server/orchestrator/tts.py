"""TTS client — Qwen3-TTS served via vLLM.

Two candidate API shapes, resolved on first call and cached:
  1. OpenAI-style POST /v1/audio/speech {model, input, voice} -> audio bytes
  2. chat completions whose response embeds audio (base64 data URL)

Non-audio responses raise TTSError so the failure is visible early.

Streaming: POST /audio/speech/stream answers with raw int16 PCM while the
sentence is still being generated, which is what keeps the wait for the
first sample short. A server without that route, or a stream that fails
before any audio arrives, falls back to the WAV endpoint."""

import base64
import io

import httpx
import numpy as np
import soundfile as sf


class TTSError(RuntimeError):
    pass


def time_stretch(audio: np.ndarray, speed: float, sr: int) -> np.ndarray:
    """Pitch-preserving WSOLA time stretch.

    speed < 1 slows playback (speed 0.8 -> duration x 1.25). The Qwen3-TTS
    0.6B API exposes no rate control, so pacing is done on the PCM here.
    """
    if abs(speed - 1.0) < 1e-3:
        return audio
    frame = int(sr * 0.04)          # 40 ms synthesis window
    out_hop = frame // 2            # 50% overlap, Hann sums to ~1
    in_hop = max(1, round(out_hop * speed))
    search = int(sr * 0.01)         # +/-10 ms alignment tolerance
    step = max(1, search // 8)
    fade = np.hanning(frame)
    x = audio.astype(np.float64)
    n_frames = len(x) // in_hop + 2
    out = np.zeros(n_frames * out_hop + frame)
    opos = 0
    ipos = 0                        # nominal input position, no feedback
    while opos + frame <= len(out):
        if opos == 0:
            start = 0
        else:
            tail = out[opos:opos + out_hop]
            best_off, best_corr = 0, -np.inf
            for off in range(-search, search + 1, step):
                s = ipos + off
                if s < 0 or s + out_hop > len(x):
                    continue
                corr = float(x[s:s + out_hop] @ tail)
                if corr > best_corr:
                    best_corr, best_off = corr, off
            start = ipos + best_off
        if start < 0 or start + frame > len(x):
            break
        out[opos:opos + frame] += x[start:start + frame] * fade
        opos += out_hop
        ipos += in_hop
    y = out[:opos + frame].astype(audio.dtype)
    return np.clip(y, np.iinfo(audio.dtype).min, np.iinfo(audio.dtype).max)


class TTSClient:
    def __init__(self, base_url: str, model: str, voice: str,
                 stream: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.stream = stream
        self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)
        self._mode: str | None = None   # "speech" | "chat"
        self._streaming: bool | None = None  # server offers the PCM route
        self.sample_rate: int | None = None

    async def _synthesize(self, text: str, voice: str | None,
                          language: str, speed: float) -> bytes:
        payload = {"model": self.model, "input": text,
                   "voice": voice or self.voice,
                   "language": language, "speed": speed,
                   "response_format": "wav"}
        if self._mode in (None, "speech"):
            resp = await self._client.post(
                f"{self.base_url}/audio/speech", json=payload,
            )
            if resp.status_code == 200:
                self._mode = "speech"
                return resp.content
            if self._mode == "speech":
                raise TTSError(f"/audio/speech failed: {resp.status_code} {resp.text[:200]}")
        # chat-completions fallback
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json={"model": self.model,
                  "messages": [{"role": "user", "content": text}]},
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        audio_b64 = None
        if isinstance(msg.get("audio"), dict):
            audio_b64 = msg["audio"].get("data")
        if audio_b64 is None:
            # some servers attach audio in choices[0].audio.data or delta
            audio_b64 = data.get("audio", {}).get("data")
        if not audio_b64:
            raise TTSError(f"no audio in TTS response: {str(data)[:300]}")
        self._mode = "chat"
        return base64.b64decode(audio_b64)

    async def _stream_pcm(self, text: str, voice: str | None,
                          language: str, chunk_s: float):
        """Yield (int16 numpy, sample_rate) as the server produces them.

        Yields nothing if the server cannot stream, so the caller falls back
        to the WAV route.
        """
        payload = {"model": self.model, "input": text,
                   "voice": voice or self.voice, "language": language}
        got_audio = False
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/audio/speech/stream", json=payload,
            ) as resp:
                if resp.status_code != 200:
                    self._streaming = False
                    return
                sr = int(resp.headers.get("x-sample-rate", 0)) or 24000
                self.sample_rate = sr
                step = max(1, int(sr * chunk_s))
                carry = b""
                async for block in resp.aiter_bytes():
                    carry += block
                    # int16 frames must not straddle a network block.
                    usable = len(carry) - (len(carry) % 2)
                    if not usable:
                        continue
                    audio = np.frombuffer(carry[:usable], dtype="<i2")
                    carry = carry[usable:]
                    got_audio = True
                    for i in range(0, len(audio), step):
                        yield audio[i : i + step], sr
        except httpx.HTTPError:
            # A break after audio was already played is not a "cannot
            # stream" verdict: falling back would speak the sentence twice.
            if not got_audio:
                self._streaming = False
            return
        self._streaming = got_audio

    async def pcm_chunks(self, text: str, voice: str | None = None,
                         language: str = "English", speed: float = 1.0,
                         chunk_s: float = 0.1):
        """Yield (int16 numpy, sample_rate) in ~chunk_s slices.

        Slicing here instead of at the socket keeps each frame small enough
        to start playing before the whole sentence is on the wire, and
        gives interruption a chance to land between slices.
        """
        # WSOLA needs the whole waveform to resample coherently, so a
        # non-default rate takes the slower one-shot route.
        if self.stream and speed == 1.0 and self._streaming is not False:
            async for chunk in self._stream_pcm(text, voice, language,
                                                chunk_s):
                yield chunk
            # Only an explicit "cannot stream" falls back. Abandoning the
            # stream mid-sentence (barge-in) must not re-synthesize, or the
            # first half would be spoken twice.
            if self._streaming is not False:
                return

        raw = await self._synthesize(text, voice, language, speed)
        audio, sr = sf.read(io.BytesIO(raw), dtype="int16")
        if speed != 1.0 and self._mode == "speech":
            audio = time_stretch(audio, speed, sr)
        self.sample_rate = sr
        step = max(1, int(sr * chunk_s))
        for i in range(0, len(audio), step):
            yield audio[i : i + step], sr
