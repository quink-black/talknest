"""Unit tests for the server-side AEC path (no services needed).

Run: ./venv/bin/python -m pytest tests/test_aec.py -v

The DTLN weights are machine-local (model root, see orchestrator config),
so the cancellation test skips when they are absent. The frame-layout and
import tests always run.
"""

import asyncio
import pathlib
import sys
import time

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "client"))

from orchestrator.aec import DtlnAec, erle_db  # noqa: E402
from orchestrator.aec_stream import UpstreamAec  # noqa: E402
from orchestrator.config import model_root  # noqa: E402
from orchestrator.wiener_aec import WienerAec  # noqa: E402

SR = 24000
MODEL_PREFIX = model_root() / "dtln-aec" / "dtln_aec_128"


def _scene():
    """Far-end signal, its 40 ms-delayed echo, and late near-end speech."""
    rng = np.random.default_rng(7)
    n = SR * 6
    t = np.arange(n, dtype=np.float32) / SR
    # Speech-like far end: syllable-rate amplitude modulation on noise,
    # bounded like a real DAC reference.
    far = np.clip(rng.standard_normal(n) * 0.25 *
                  (0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)),
                  -1.0, 1.0).astype(np.float32)
    near = np.zeros(n, dtype=np.float32)
    voice = slice(4 * SR, 6 * SR)
    near[voice] = 0.3 * np.sin(2 * np.pi * 220.0 * t[voice]) * \
        (0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t[voice]))
    echo = np.zeros(n, dtype=np.float32)
    delay = int(SR * 0.04)
    echo[delay:] = 0.5 * far[:-delay]
    mic = np.clip(echo + near, -1.0, 1.0)
    return far, near, mic


def _stereo_frames(mic: np.ndarray, ref: np.ndarray):
    """Interleaved [mic, reference] int16 in 20 ms frames, device-style."""
    pcm = np.empty(mic.size * 2, dtype="<i2")
    pcm[0::2] = np.clip(mic * 32768.0, -32768, 32767).astype("<i2")
    pcm[1::2] = np.clip(ref * 32768.0, -32768, 32767).astype("<i2")
    frame_bytes = SR * 2 * 2 // 50
    return [pcm[i:i + frame_bytes // 2].tobytes()
            for i in range(0, pcm.size, frame_bytes // 2)]


def test_cli_shim_exports_shared_module():
    import talknest_cli.aec as cli_aec

    assert cli_aec.DtlnAec is DtlnAec
    assert cli_aec.erle_db is erle_db


def test_wiener_converges_and_cancels_onsets_after_a_gap():
    """The linear stage must cancel immediately at a fresh far-end onset.

    That is the property the neural canceller lacks: its mask reconverges
    for a few hundred milliseconds after silence, which is exactly the
    window that used to trip the VAD.
    """
    rng = np.random.default_rng(11)
    path = np.zeros(2048)
    path[640:644] = [0.6, 0.2, 0.1, 0.05]
    sr = 16000

    def burst(seconds):
        ref = rng.standard_normal(int(seconds * sr)) * 0.2
        return ref, np.convolve(ref, path)[:ref.size]

    aec = WienerAec(taps=2048)
    ref1, mic1 = burst(2.5)
    out1 = np.zeros(0)
    for i in range(0, len(mic1), 128):
        out1 = np.concatenate([out1, aec.process(mic1[i:i + 128], ref1[i:i + 128])])
    settled = slice(int(1.5 * sr), int(2.5 * sr))
    assert erle_db(mic1[settled], out1[settled]) > 15.0

    gap = int(1.0 * sr)
    aec.process(np.zeros(gap), np.zeros(gap))
    ref2, mic2 = burst(1.0)
    out2 = np.zeros(0)
    for i in range(0, len(mic2), 128):
        out2 = np.concatenate([out2, aec.process(mic2[i:i + 128], ref2[i:i + 128])])
    # The estimate is frozen across the gap, so the new onset cancels from
    # its first samples, not after re-convergence.
    onset = slice(0, int(0.5 * sr))
    assert erle_db(mic2[onset], out2[onset]) > 10.0
    assert np.isfinite(out2).all()


def test_stereo_frame_layout_roundtrip():
    far, near, mic = _scene()
    frames = _stereo_frames(mic, far)
    rejoined = np.frombuffer(b"".join(frames), dtype="<i2").reshape(-1, 2)
    assert len(frames[0]) == SR * 2 * 2 // 50
    # int16 round trip only loses the float->int16 quantization.
    assert np.abs(rejoined[:, 0] / 32768.0 - mic).max() < 1e-3
    assert np.abs(rejoined[:, 1] / 32768.0 - far).max() < 1e-3


@pytest.mark.skipif(not (MODEL_PREFIX.parent / "dtln_aec_128_1.tflite").exists(),
                    reason="DTLN weights not downloaded")
def test_upstream_aec_cancels_echo_and_keeps_speech():
    far, near, mic = _scene()
    frames = _stereo_frames(mic, far)

    async def run():
        q: asyncio.Queue = asyncio.Queue()
        aec = UpstreamAec(DtlnAec(str(MODEL_PREFIX)))
        aec.start(asyncio.get_running_loop(), q)
        for frame in frames:
            aec.feed(frame)
            # Feed faster than real time but let the thread keep pace.
            await asyncio.sleep(0.001)
        out = bytearray()
        deadline = time.monotonic() + 120
        # Collect until the thread goes idle: resamplers and the DTLN
        # warmup hold back a stretch of samples that arrives only with the
        # last frames.
        while time.monotonic() < deadline:
            try:
                out += await asyncio.wait_for(q.get(), 3.0)
            except asyncio.TimeoutError:
                break
        aec.stop()
        return bytes(out)

    cleaned = np.frombuffer(asyncio.run(run()), dtype="<i2").astype(np.float32) / 32768.0
    assert cleaned.size >= SR * 4, "AEC thread produced too little audio"

    # The pipeline delays the output by a constant (DTLN warmup plus both
    # resamplers). Align by envelope correlation before comparing spans.
    hop = 240  # 10 ms
    env_mic = np.sqrt((mic[: len(mic) // hop * hop].reshape(-1, hop) ** 2).mean(axis=1))
    env_out = np.sqrt((cleaned[: len(cleaned) // hop * hop].reshape(-1, hop) ** 2).mean(axis=1))
    corr = np.correlate(env_out, env_mic, "full")
    delay = int(round((corr.argmax() - (len(env_mic) - 1)) * hop))
    assert abs(delay) < SR, f"implausible pipeline delay {delay} samples"

    # Echo-only stretch: the residual must be well below the mic signal.
    echo_span = slice(int(1.0 * SR), int(3.5 * SR))
    assert echo_span.stop < cleaned.size
    reduction = erle_db(mic[echo_span],
                        cleaned[echo_span.start + delay:echo_span.stop + delay])
    assert reduction > 6.0, f"echo reduction only {reduction:.1f} dB"

    # Near-end speech stretch: it must survive cancellation.
    voice = slice(int(4.2 * SR), int(5.8 * SR))
    out_voice = cleaned[voice.start + delay:voice.stop + delay]
    residual = float(np.sqrt(np.mean(out_voice ** 2)))
    speech = float(np.sqrt(np.mean(near[voice] ** 2)))
    assert residual >= 0.2 * speech, \
        f"near-end speech too weak after AEC: {residual:.4f} vs {speech:.4f}"
