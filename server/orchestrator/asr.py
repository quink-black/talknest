"""ASR client — posts a WAV to the vLLM OpenAI-compatible transcription
endpoint at Qwen3-ASR's native 24 kHz."""

import io

import httpx
import soundfile as sf

# Qwen3-ASR returns "language English<asr_text>transcribed words". The
# metadata prefix has to go before the text reaches the LLM.
_ASR_TEXT_TAG = "<asr_text>"


def strip_metadata(raw: str) -> str:
    if _ASR_TEXT_TAG in raw:
        raw = raw.split(_ASR_TEXT_TAG, 1)[1]
    return raw.strip()


class ASRClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def transcribe(self, pcm16, sr: int = 16000) -> str:
        buf = io.BytesIO()
        sf.write(buf, pcm16, sr, format="WAV", subtype="PCM_16")
        buf.seek(0)
        resp = await self._client.post(
            f"{self.base_url}/audio/transcriptions",
            files={"file": ("utterance.wav", buf, "audio/wav")},
            data={"model": self.model},
        )
        resp.raise_for_status()
        return strip_metadata(resp.json().get("text", ""))
