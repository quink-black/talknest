"""Per-session upstream AEC for clients that upload mic + reference.

The ESP32 terminal streams 24 kHz interleaved [microphone, playback
reference] int16. Cancelling that echo is neural-net work (DTLN, see
aec.py), so it runs on its own thread per session and hands the cleaned
mono PCM back to the session's asyncio queue via the event loop; the VAD
and everything downstream see the same mono stream a browser client
produces.

The two upstream channels come from one DMA stream on the device, so they
are sample-aligned by construction. Resampling both through identically
configured stateful resamplers keeps that alignment at the canceller's
16 kHz processing rate.
"""

import asyncio
import queue
import threading

import numpy as np

try:
    import soxr
except ImportError:  # guarded again in UpstreamAec.start(); keep import safe
    soxr = None

CLIENT_SR = 24000
# Bound the handoff queue: the AEC thread runs at worst real time, so a
# backlog can only mean the thread died; the oldest audio is stale then.
_IN_QUEUE_FRAMES = 300


def _pcm_to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def _float_to_pcm(x: np.ndarray) -> np.ndarray:
    scaled = np.where(x < 0, x * 32768.0, x * 32767.0)
    return np.clip(scaled, -32768.0, 32767.0).astype("<i2")


class UpstreamAec:
    """Thread that turns stereo upstream frames into cleaned mono PCM."""

    def __init__(self, aec):
        """``aec`` is an EchoCanceller instance from aec.py."""
        self._aec = aec
        self._in: queue.Queue[bytes] = queue.Queue(maxsize=_IN_QUEUE_FRAMES)
        self._leftover = b""
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._out: asyncio.Queue | None = None
        self._rs_mic = self._rs_ref = self._rs_out = None

    @property
    def proc_sr(self) -> int:
        return self._aec.proc_sr

    def start(self, loop: asyncio.AbstractEventLoop, out: asyncio.Queue) -> None:
        """Deliver cleaned mono int16 bytes into ``out`` on ``loop``."""
        if soxr is None:
            raise RuntimeError("soxr is not installed; cannot run upstream AEC")
        self._loop = loop
        self._out = out
        self._aec.reset()
        self._rs_mic = soxr.ResampleStream(CLIENT_SR, self._aec.proc_sr, 1,
                                           dtype="float32")
        self._rs_ref = soxr.ResampleStream(CLIENT_SR, self._aec.proc_sr, 1,
                                           dtype="float32")
        self._rs_out = soxr.ResampleStream(self._aec.proc_sr, CLIENT_SR, 1,
                                           dtype="float32")
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="upstream-aec")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def feed(self, data: bytes) -> None:
        """Hand one WebSocket binary frame to the AEC thread."""
        try:
            self._in.put_nowait(data)
        except queue.Full:
            # The AEC thread stalled; drop the oldest frame so the stream
            # stays at most one queue behind real time.
            try:
                self._in.get_nowait()
            except queue.Empty:
                pass
            try:
                self._in.put_nowait(data)
            except queue.Full:
                pass

    def _run(self) -> None:
        while self._running:
            try:
                data = self._in.get(timeout=0.5)
            except queue.Empty:
                continue
            self._leftover += data
            # Frames must stay whole stereo pairs; a partial sample at the
            # end waits for the next frame.
            usable = len(self._leftover) - len(self._leftover) % 4
            if usable == 0:
                continue
            pcm = np.frombuffer(self._leftover[:usable], dtype="<i2")
            self._leftover = self._leftover[usable:]
            frames = pcm.reshape(-1, 2)
            self._aec.push_reference(
                self._rs_ref.resample_chunk(_pcm_to_float(frames[:, 1]),
                                            last=False))
            cleaned = self._aec.process(
                self._rs_mic.resample_chunk(_pcm_to_float(frames[:, 0]),
                                            last=False))
            out = self._rs_out.resample_chunk(cleaned.astype(np.float32),
                                              last=False)
            if out.size == 0:
                continue
            payload = _float_to_pcm(out).tobytes()
            # asyncio.Queue is not thread-safe; schedule the put on the loop.
            self._loop.call_soon_threadsafe(self._out.put_nowait, payload)
