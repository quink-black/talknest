"""Incremental synthesis for Qwen3-TTS.

qwen-tts exposes no streaming API: generate_custom_voice() returns the
finished waveform and nothing before it. The talker's forward() does
already compute the whole 16-codebook frame on every step and carry it
out through hidden_states, so wrapping forward is enough to watch the
frames arrive without reimplementing any generation logic. How tokens
are chosen does not change, so streamed audio is the same audio.

Kept in its own module so it can be exercised against a CPU-loaded model
without importing the CUDA sidecar.
"""

import contextlib
import functools

import numpy as np
import torch


@contextlib.contextmanager
def observe_talker_frames(tts_model, on_frame):
    """Call on_frame(1-D int64 tensor of 16 codes) per generated frame.

    The EOS frame is dropped: the library trims it too, and leaving it in
    would put a click at the end of the streamed audio.

    The wrapper keeps the original signature, because generate() validates
    its keyword arguments against it.
    """
    talker = tts_model.model.talker
    original = talker.forward
    eos = tts_model.model.config.talker_config.codec_eos_token_id

    @functools.wraps(original)
    def forward(*args, **kwargs):
        out = original(*args, **kwargs)
        states = getattr(out, "hidden_states", None)
        codes = states[-1] if isinstance(states, tuple) and states else None
        # Prefill carries no frame of its own; codes is None there.
        if torch.is_tensor(codes) and codes.numel():
            frame = codes.detach().reshape(-1)
            if int(frame[0]) != eos:
                on_frame(frame.to("cpu", torch.long).clone())
        return out

    talker.forward = forward
    try:
        yield
    finally:
        talker.forward = original


def synthesize_stream(tts_model, kwargs: dict, chunks, frames_per_chunk: int = 4):
    """Generate in the calling thread, pushing int16 PCM bytes as they come.

    Ends the stream with a None sentinel, including on failure, so a caller
    that got nothing can fall back to the one-shot route.

    Decoding runs on the growing prefix every time, which is sound because
    the speech tokenizer is causal: samples already handed over do not
    move when later frames are appended.
    """
    tokenizer = tts_model.model.speech_tokenizer
    pending: list[torch.Tensor] = []
    emitted = 0     # samples already handed over
    flushed = 0     # frames already decoded

    def flush():
        nonlocal emitted, flushed
        if len(pending) == flushed:
            return
        codes = torch.stack(pending, dim=0)
        wavs, _sr = tokenizer.decode([{"audio_codes": codes}])
        # Same conversion soundfile applies when writing the WAV route.
        pcm = np.clip(np.asarray(wavs[0], dtype=np.float32) * 32768.0,
                      -32768.0, 32767.0).astype(np.int16)
        if len(pcm) > emitted:
            chunks.put(pcm[emitted:].tobytes())
            emitted = len(pcm)
        flushed = len(pending)

    def on_frame(frame):
        pending.append(frame)
        # Measured against what was already decoded, not against the chunk
        # size: pending keeps growing, so a plain length check would decode
        # the whole prefix again on every frame after the first threshold.
        if len(pending) - flushed >= frames_per_chunk:
            flush()

    try:
        with observe_talker_frames(tts_model, on_frame):
            tts_model.generate_custom_voice(**kwargs)
        flush()
    finally:
        chunks.put(None)
