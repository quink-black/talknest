"""Qwen3-TTS sidecar — OpenAI-compatible /v1/audio/speech endpoint.

Runs the official qwen-tts package (transformers backend, CUDA) in its
own dedicated venv (venv-tts), because the shared qwen venv cannot hold
qwen-tts (would downgrade transformers) and vLLM online serving for
Qwen3-TTS is not released yet. The orchestrator sees the same API shape
as any OpenAI-style TTS server, so swapping runtimes never touches the
pipeline.

Run via server/services/start_tts.sh (:8002).
"""

import io
import os
from pathlib import Path

import soundfile as sf
import torch
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

from qwen_tts import Qwen3TTSModel

# TALKNEST_MODEL_ROOT overrides the sibling-of-checkout default.
_MODEL_ROOT = Path(
    os.environ.get(
        "TALKNEST_MODEL_ROOT", Path(__file__).resolve().parents[2].parent / "model"
    )
)

MODEL_DIR = os.environ.get(
    "QWEN_TTS_DIR", str(_MODEL_ROOT / "Qwen3-TTS-12Hz-0.6B-CustomVoice")
)
DEFAULT_SPEAKER = os.environ.get("QWEN_TTS_SPEAKER", "Aiden")
DEFAULT_LANGUAGE = os.environ.get("QWEN_TTS_LANGUAGE", "English")

app = FastAPI(title="qwen3-tts-sidecar")
model = Qwen3TTSModel.from_pretrained(
    MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16
)


class SpeechRequest(BaseModel):
    model: str = "Qwen3-TTS-0.6B-CustomVoice"
    input: str
    voice: str = DEFAULT_SPEAKER
    language: str = DEFAULT_LANGUAGE
    instruct: str | None = None


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "Qwen3-TTS-0.6B-CustomVoice",
            "object": "model", "owned_by": "talknest"}]}


@app.get("/v1/speakers")
def speakers():
    return {"speakers": model.get_supported_speakers(),
            "languages": model.get_supported_languages()}


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    kwargs = dict(
        text=req.input,
        language=req.language,
        speaker=req.voice if req.voice else DEFAULT_SPEAKER,
    )
    if req.instruct:
        kwargs["instruct"] = req.instruct
    wavs, sr = model.generate_custom_voice(**kwargs)
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")
