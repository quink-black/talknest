"""Qwen3-TTS sidecar — OpenAI-compatible /v1/audio/speech endpoint.

Runs the official qwen-tts package (transformers backend) in its own
dedicated venv (venv-tts), because the shared qwen venv cannot hold
qwen-tts (would downgrade transformers) and vLLM online serving for
Qwen3-TTS is not released yet. The orchestrator sees the same API shape
as any OpenAI-style TTS server, so swapping runtimes never touches the
pipeline. The device follows whatever the machine offers: CUDA, then
MPS, then CPU.

Two entry points:

  POST /v1/audio/speech         one WAV, once the whole sentence is done
  POST /v1/audio/speech/stream  raw int16 PCM, chunk by chunk, while the
                                sentence is still being generated

Synthesis is the largest single term in the reply latency, and the
non-streaming endpoint cannot hand over a single sample until the last
frame exists. The streaming one emits audio a few frames at a time.

Run via server/services/start_tts.sh (the TTS port; see scripts/ports.sh).

Environment:
  QWEN_TTS_GEN_ARGS   JSON object of generation arguments forwarded to
                      generate_custom_voice, e.g.
                      '{"subtalker_dosample": false}'. Unknown keys are
                      rejected at startup.
  QWEN_TTS_STREAM_FRAMES  codec frames per streamed chunk (default 4,
                      i.e. ~320 ms of audio at 12.5 Hz).
  QWEN_TTS_DEVICE     device_map passed to from_pretrained; default is
                      cuda:0 when CUDA is present, else mps, else cpu.
  QWEN_TTS_DTYPE      torch dtype name, e.g. bfloat16, float16, float32.
"""

import asyncio
import io
import json
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import soundfile as sf
import torch
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from qwen_tts import Qwen3TTSModel

# Sibling of this file; uvicorn runs the app with its directory on the path.
import qwen_tts_stream  # noqa: E402

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

GEN_ARG_KEYS = frozenset({
    "do_sample", "top_k", "top_p", "temperature", "repetition_penalty",
    "subtalker_dosample", "subtalker_top_k", "subtalker_top_p",
    "subtalker_temperature", "max_new_tokens",
})


def load_gen_args() -> dict:
    """Generation overrides from the environment.

    These trade speed against voice character, which is a judgement only a
    listener can make, so they stay out of the request body and off by
    default: an unset environment reproduces the library defaults exactly.
    """
    raw = os.environ.get("QWEN_TTS_GEN_ARGS", "").strip()
    if not raw:
        return {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"QWEN_TTS_GEN_ARGS is not valid JSON: {e}") from e
    if not isinstance(args, dict):
        raise RuntimeError("QWEN_TTS_GEN_ARGS must be a JSON object")
    unknown = sorted(set(args) - GEN_ARG_KEYS)
    if unknown:
        raise RuntimeError(
            f"unknown QWEN_TTS_GEN_ARGS keys: {', '.join(unknown)}"
        )
    return args


GEN_ARGS = load_gen_args()
STREAM_FRAMES = max(1, int(os.environ.get("QWEN_TTS_STREAM_FRAMES", "4")))


def default_device() -> tuple[str, str]:
    """Device and dtype name to use when the environment says nothing.

    Both accelerators keep bfloat16, the dtype the shared 12G card was
    measured with. MPS cannot take float16: the sampler overflows there
    and rejects its own probability tensor, which the wider bfloat16
    exponent avoids.
    """
    if torch.cuda.is_available():
        return "cuda:0", "bfloat16"
    if torch.backends.mps.is_available():
        return "mps", "bfloat16"
    return "cpu", "float32"


_DEFAULT_DEVICE, _DEFAULT_DTYPE = default_device()
DEVICE = os.environ.get("QWEN_TTS_DEVICE", "").strip() or _DEFAULT_DEVICE
_DTYPE_NAME = os.environ.get("QWEN_TTS_DTYPE", "").strip() or _DEFAULT_DTYPE
DTYPE = getattr(torch, _DTYPE_NAME, None)
if DTYPE is None:
    # A near miss like "bf16" reaches getattr and dies there, with a
    # traceback that names torch rather than the setting to fix.
    raise RuntimeError(
        f"QWEN_TTS_DTYPE={_DTYPE_NAME!r} is not a torch dtype; "
        "use one of bfloat16, float16, float32"
    )

app = FastAPI(title="qwen3-tts-sidecar")
model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map=DEVICE, dtype=DTYPE)
# One worker: the model is a single device-bound object, so concurrency
# would only interleave requests. The executor exists to keep the blocking
# synthesis off the event loop.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")


class SpeechRequest(BaseModel):
    model: str = "Qwen3-TTS-0.6B-CustomVoice"
    input: str
    voice: str = DEFAULT_SPEAKER
    language: str = DEFAULT_LANGUAGE
    instruct: str | None = None


def build_kwargs(req: SpeechRequest) -> dict:
    kwargs = dict(
        text=req.input,
        language=req.language,
        speaker=req.voice if req.voice else DEFAULT_SPEAKER,
    )
    if req.instruct:
        kwargs["instruct"] = req.instruct
    kwargs.update(GEN_ARGS)
    return kwargs


OUTPUT_SR = int(model.model.speech_tokenizer.get_output_sample_rate())


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
    kwargs = build_kwargs(req)
    loop = asyncio.get_running_loop()
    wavs, sr = await loop.run_in_executor(
        _EXECUTOR, lambda: model.generate_custom_voice(**kwargs)
    )
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.post("/v1/audio/speech/stream")
async def speech_stream(req: SpeechRequest):
    """Raw mono int16 PCM at the tokenizer's rate, chunk by chunk."""
    kwargs = build_kwargs(req)
    chunks: queue.Queue = queue.Queue()
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(_EXECUTOR, qwen_tts_stream.synthesize_stream,
                                model, kwargs, chunks, STREAM_FRAMES)

    async def body():
        while True:
            item = await loop.run_in_executor(None, chunks.get)
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(OUTPUT_SR)},
    )
