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
import contextlib
import logging
import queue
import threading

import numpy as np

from .wiener_aec import WienerAec

try:
    import soxr
except ImportError:  # guarded again in UpstreamAec.start(); keep import safe
    soxr = None

log = logging.getLogger(__name__)

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
        # Linear pre-stage: removes the (static) linear echo at once at
        # every far-end onset, before the neural canceller re-converges.
        self._wiener = WienerAec()
        # Rolling diagnostic capture of the last seconds per channel,
        # dumped by dump() when the server suspects an echo leak.
        self._dbg: dict[str, list] = {"mic": [], "ref": [], "out": []}
        self._dbg_n: dict[str, int] = {"mic": 0, "ref": 0, "out": 0}

    def dump(self, path_prefix: str, max_wavs: int = 9,
             rotate_prefix: str = "aec-barge-") -> None:
        """Write the last seconds of mic, reference and cleaned output.

        Diagnostic for echo leaks: the periodic dBFS averages cannot show
        the short bursts that actually trip the VAD. Best effort from the
        event loop while the AEC thread keeps writing; the GIL keeps the
        list surgery from tearing.

        Files are created exclusively (no symlink following) and the dump
        directory is rotated so a long session cannot fill it.
        """
        import os
        import wave

        directory = os.path.dirname(path_prefix) or "."
        try:
            for name, chunks in self._dbg.items():
                samples = np.concatenate(chunks) if chunks else np.zeros(0, "<i2")
                path = f"{path_prefix}_{name}.wav"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                with os.fdopen(fd, "wb") as raw:
                    with wave.open(raw, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(CLIENT_SR)
                        wav.writeframes(samples.astype("<i2").tobytes())
            # Rotation must run even when a write failed: a leftover
            # dangling symlink must not let the directory grow forever.
            self._rotate_dumps(directory, rotate_prefix, max_wavs)
        except Exception as exc:  # a diagnostic must never break a turn
            log.warning("AEC dump failed: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                self._rotate_dumps(directory, rotate_prefix, max_wavs)

    @staticmethod
    def _rotate_dumps(directory: str, prefix: str, max_wavs: int) -> None:
        """Keep only the newest `max_wavs` barge-in wavs in the directory."""
        import os

        entries = []
        for name in os.listdir(directory):
            if not (name.startswith(prefix) and name.endswith(".wav")):
                continue
            path = os.path.join(directory, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:  # dangling symlink or a concurrent removal
                mtime = -1.0  # treat as oldest, so it is cleaned up first
            entries.append((mtime, path))
        if len(entries) <= max_wavs:
            return
        entries.sort(key=lambda item: item[0], reverse=True)
        for _, path in entries[max_wavs:]:
            with contextlib.suppress(OSError):
                os.remove(path)

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

    def clear(self) -> None:
        """Drop audio buffered while the server gated the uplink.

        Frames arriving during a turn are discarded before they reach the
        worker, so its partial stereo frame and any queued frames belong to
        an older stretch of the stream. Keeping them would splice two
        non-contiguous stretches together.
        """
        while True:
            try:
                self._in.get_nowait()
            except queue.Empty:
                break
        self._leftover = b""
        for name in self._dbg:
            self._dbg[name].clear()
            self._dbg_n[name] = 0

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
        # Levels for the periodic diagnostic: what went in on both channels
        # and what came out, so an echo leak can be told apart from a dead
        # reference channel.
        acc = {"mic": 0.0, "ref": 0.0, "out": 0.0}
        acc_n = 0
        report_at = threading.Event()
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
            mic = _pcm_to_float(frames[:, 0])
            ref = _pcm_to_float(frames[:, 1])
            mic16 = self._rs_mic.resample_chunk(mic, last=False)
            ref16 = self._rs_ref.resample_chunk(ref, last=False)
            n16 = min(mic16.size, ref16.size)
            # Linear stage first, then the neural canceller on what is left.
            linear = self._wiener.process(
                mic16[:n16].astype(np.float64), ref16[:n16].astype(np.float64))
            self._aec.push_reference(ref16[:n16])
            cleaned = self._aec.process(linear.astype(np.float32))
            out = self._rs_out.resample_chunk(cleaned, last=False)
            acc["mic"] += float(np.sum(mic ** 2))
            acc["ref"] += float(np.sum(ref ** 2))
            acc["out"] += float(np.sum(out ** 2))
            acc_n += mic.size
            for name, chunk in (("mic", frames[:, 0]), ("ref", frames[:, 1])):
                self._dbg[name].append(chunk.astype("<i2", copy=True))
                self._dbg_n[name] += chunk.size
            self._dbg["out"].append(_float_to_pcm(out))
            self._dbg_n["out"] += out.size
            for name in self._dbg:
                while self._dbg_n[name] > CLIENT_SR * 12:
                    dropped = self._dbg[name].pop(0)
                    self._dbg_n[name] -= dropped.size
            if acc_n >= CLIENT_SR * 5:
                rms = {k: (v / acc_n) ** 0.5 for k, v in acc.items()}
                db = {k: (20 * np.log10(v) if v > 1e-12 else -120.0)
                      for k, v in rms.items()}
                log.info("AEC levels: mic=%.0f dBFS ref=%.0f dBFS out=%.0f dBFS "
                         "(wiener solves=%d adopted=%d)",
                         db["mic"], db["ref"], db["out"],
                         self._wiener.solves, self._wiener.adopted)
                acc = {k: 0.0 for k in acc}
                acc_n = 0
            if out.size == 0:
                continue
            payload = _float_to_pcm(out).tobytes()
            # asyncio.Queue is not thread-safe; schedule the put on the loop.
            self._loop.call_soon_threadsafe(self._out.put_nowait, payload)
