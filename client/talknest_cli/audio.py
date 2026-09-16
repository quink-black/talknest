"""Capture, playback, resampling and the upstream DSP chain.

Three threads plus the asyncio loop:

  PortAudio callbacks   only move samples in and out of rings
  dsp thread            resample -> echo cancel -> resample -> frame
  asyncio sender        drains the framed queue onto the socket

The callbacks must not do resampling or run a neural net, so everything
heavy happens in the dsp thread.
"""

import queue
import threading
import time
import wave

import numpy as np

from . import CLIENT_SR, FRAME_SAMPLES
from .aec import EchoCanceller
from .portaudio import sd, require_portaudio

try:
    import soxr
except ImportError:
    soxr = None

_WARNED_LINEAR = False

# Capture and reference rings are preallocated, so their capacity is chosen
# at start(). This many device blocks keeps the margin the old
# queue.Queue(maxsize=512) had.
_RING_BLOCKS = 512
# Reply audio arrives faster than it is played, so the playback ring has to
# absorb the difference across a whole turn. 30 s of audio is far past any
# reply and still small enough for a Raspberry Pi.
_PLAY_RING_SECONDS = 30.0
# What the dsp thread sleeps for when the capture ring is empty. Short: a
# longer nap would show up as the same amount of extra upstream latency.
_DSP_IDLE_S = 0.002


def resample(x: np.ndarray, src_sr: float, dst_sr: float) -> np.ndarray:
    """Resample mono float32 in one shot. soxr when available, linear else.

    For chunked streams use :class:`Resampler` instead: a fresh filter per
    chunk leaves discontinuities at the block boundaries.
    """
    global _WARNED_LINEAR
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0 or abs(src_sr - dst_sr) < 1e-6:
        return x
    if soxr is not None:
        return soxr.resample(x, src_sr, dst_sr).astype(np.float32)
    if not _WARNED_LINEAR:
        print("[audio] soxr missing; falling back to linear resampling")
        _WARNED_LINEAR = True
    n = max(1, int(round(x.size * dst_sr / src_sr)))
    src_pos = np.arange(n, dtype=np.float64) * (src_sr / dst_sr)
    return np.interp(src_pos, np.arange(x.size, dtype=np.float64), x).astype(np.float32)


class Resampler:
    """Stateful chunked resampler; block boundaries stay continuous."""

    def __init__(self, src_sr: float, dst_sr: float):
        self.src_sr = float(src_sr)
        self.dst_sr = float(dst_sr)
        self.same = abs(self.src_sr - self.dst_sr) < 1e-6
        self._stream = None
        if not self.same and soxr is not None:
            stream_cls = getattr(soxr, "ResampleStream", None)
            if stream_cls is not None:
                self._stream = stream_cls(self.src_sr, self.dst_sr, 1,
                                          dtype="float32")

    def resample(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.same or x.size == 0:
            return x
        if self._stream is not None:
            return self._stream.resample_chunk(x, last=False).astype(np.float32)
        # Very old soxr without ResampleStream: fall back to the stateless
        # one-shot path (discontinuous across chunks, but so rare it does
        # not justify a hand-rolled streaming polyphase).
        return resample(x, self.src_sr, self.dst_sr)


def pcm_to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> np.ndarray:
    scaled = np.where(x < 0, x * 32768.0, x * 32767.0)
    return np.clip(scaled, -32768.0, 32767.0).astype("<i2")


def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM WAV as float32 mono. Returns (samples, sample_rate)."""
    with wave.open(path, "rb") as wav:
        sr = wav.getframerate()
        channels = wav.getnchannels()
        if wav.getsampwidth() != 2:
            raise SystemExit(f"[audio] {path}: {wav.getsampwidth() * 8}-bit WAV "
                             "not supported, use 16-bit PCM")
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm_to_float(pcm), sr


class SpscRing:
    """Lock-free single-producer single-consumer ring of float32 samples.

    Exactly one thread writes and exactly one thread reads. That is what
    makes plain integer indices sufficient: the writer alone moves the head
    and the reader alone moves the tail, so neither can observe a torn
    value. A reader that sees a slightly stale head just reads a little
    less, and a writer that sees a slightly stale tail just uses a little
    less space. This relies on the GIL; a free-threaded build would need
    atomics.

    Nothing here allocates or takes a lock after construction, so either
    end may run inside a PortAudio callback.
    """

    def __init__(self, capacity: int, drop_oldest: bool = False):
        self._buf = np.zeros(max(capacity, 1), dtype=np.float32)
        self._cap = len(self._buf)
        self._drop_oldest = drop_oldest
        self._head = 0
        self._tail = 0
        self._flush_to = 0

    @property
    def capacity(self) -> int:
        return self._cap

    def available(self) -> int:
        return self._head - max(self._tail, self._flush_to)

    def write(self, x: np.ndarray) -> int:
        """Append samples, returning how many were stored.

        Overflow follows the policy chosen at construction: either the
        oldest samples give way, or the new ones are dropped.
        """
        total = len(x)
        if total == 0:
            return 0
        cap = self._cap
        if total > cap:
            x = x[total - cap :]
            total = cap
        head = self._head
        # The tail can lag behind a discard the reader has not applied yet;
        # treating it as current would report the ring as fuller than it is.
        space = cap - (head - max(self._tail, self._flush_to))
        if total > space:
            if self._drop_oldest:
                # Give up the oldest samples. Only _flush_to moves, because
                # it belongs to the writer: the reader drops them for real
                # on its next read, so the two never race on the tail.
                self._flush_to = head + total - cap
            elif space <= 0:
                return 0
            else:
                total = space
                x = x[:total]
        start = head % cap
        first = min(total, cap - start)
        np.copyto(self._buf[start : start + first], x[:first])
        if total > first:
            np.copyto(self._buf[: total - first], x[first:total])
        self._head = head + total
        return total

    def read_into(self, out: np.ndarray) -> int:
        """Fill out from the ring, zeroing what the ring cannot cover.

        Returns how many samples were real audio rather than padding.
        """
        n = len(out)
        if self._flush_to > self._tail:
            self._tail = self._flush_to
        take = min(n, self._head - self._tail)
        if take:
            cap = self._cap
            start = self._tail % cap
            first = min(take, cap - start)
            np.copyto(out[:first], self._buf[start : start + first])
            if take > first:
                np.copyto(out[first:take], self._buf[: take - first])
            self._tail += take
        if take < n:
            out[take:] = 0.0
        return take

    def read(self, n: int) -> np.ndarray:
        """Read up to n samples into a new array. Not for the RT thread."""
        out = np.zeros(n, dtype=np.float32)
        return out[: self.read_into(out)]

    def discard(self) -> None:
        """Producer side: drop everything written so far.

        The reader applies it on its next read, so the writer never moves
        the read index and the two threads never write the same field.
        """
        self._flush_to = self._head


class AudioEngine:
    """Owns the device streams and produces 24 kHz int16 upstream frames."""

    def __init__(self, aec: EchoCanceller, input_device=None, output_device=None,
                 input_file: str | None = None, output_file: str | None = None,
                 blocksize: int = 0, play_tail_s: float = 0.15):
        self.aec = aec
        self.input_device = input_device
        self.output_device = output_device
        self.input_file = input_file
        self.output_file = output_file
        self._blocksize = blocksize
        # Silence the "playback ended" report only after the hardware buffer
        # has drained too; the playback ring empties while the last samples
        # are still in the device's latency buffer.
        self.play_tail_s = play_tail_s
        # Only _up stays a Queue: nothing real-time touches it, and the
        # sender needs a blocking get with a timeout.
        self._up: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=512)
        self._mic_ring = SpscRing(1)   # capture callback -> dsp thread
        self._ref_ring = SpscRing(1)   # playback callback -> dsp thread
        self._play_ring = SpscRing(1)  # reply audio -> playback callback
        # How much the dsp thread takes from the capture ring at a time:
        # one device block, so a stalled stretch drains block by block
        # instead of arriving as one late lump.
        self._dsp_block = FRAME_SAMPLES
        self._leftover = np.zeros(0, dtype=np.float32)
        self._play_started = False
        self._last_write = 0.0
        self._running = False
        self._dsp: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._streams = []
        self._rt_status: list[str] = []
        self._rs_mic = self._rs_up = self._rs_ref = self._rs_play = None
        self._rs_play_key = None
        self.in_sr = CLIENT_SR
        self.out_sr = CLIENT_SR
        self._wav_out = None
        self._wav_sr = None

    # -- device setup ----------------------------------------------------

    @staticmethod
    def _pick_rate(kind: str, device, prefer: int) -> int:
        check = sd.check_input_settings if kind == "input" else sd.check_output_settings
        try:
            check(device=device, samplerate=prefer, channels=1)
            return prefer
        except Exception:
            return int(sd.query_devices(device, kind)["default_samplerate"])

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            self._start()
        except BaseException:
            # Half-open state: the first stream would keep the device busy
            # and never be closed.
            self.stop()
            raise

    def _start(self) -> None:
        in_block = out_block = FRAME_SAMPLES
        if self.input_file:
            self._reader = threading.Thread(target=self._read_file, daemon=True)
            self._reader.start()
        else:
            require_portaudio()
            self.in_sr = self._pick_rate("input", self.input_device, CLIENT_SR)
            stream = sd.InputStream(
                device=self.input_device, channels=1, samplerate=self.in_sr,
                dtype="float32", blocksize=self._blocksize, callback=self._in_cb)
            self._streams.append(stream)
            in_block = stream.blocksize or FRAME_SAMPLES
        if not self.output_file:
            require_portaudio()
            self.out_sr = self._pick_rate("output", self.output_device, CLIENT_SR)
            stream = sd.OutputStream(
                device=self.output_device, channels=1, samplerate=self.out_sr,
                dtype="float32", blocksize=self._blocksize, callback=self._out_cb)
            self._streams.append(stream)
            out_block = stream.blocksize or FRAME_SAMPLES
        # Rings exist before any callback can fire, and are sized from the
        # block the device actually negotiated.
        self._dsp_block = in_block
        self._mic_ring = SpscRing(_RING_BLOCKS * in_block)
        self._ref_ring = SpscRing(_RING_BLOCKS * out_block, drop_oldest=True)
        self._play_ring = SpscRing(int(self.out_sr * _PLAY_RING_SECONDS))
        for s in self._streams:
            s.start()
        # Stateful resamplers: one filter instance per chain, so chunked
        # blocks stay continuous across boundaries.
        self._rs_mic = Resampler(self.in_sr, self.aec.proc_sr)
        self._rs_up = Resampler(self.aec.proc_sr, CLIENT_SR)
        self._rs_ref = Resampler(self.out_sr, self.aec.proc_sr)
        # Started in file mode too: the playback callback feeds the reference
        # ring, and an undrained ring overflows inside the RT callback.
        self._dsp = threading.Thread(target=self._dsp_loop, daemon=True)
        self._dsp.start()

    def stop(self) -> None:
        self._running = False
        for s in self._streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self._streams.clear()
        if self._wav_out is not None:
            self._wav_out.close()
            self._wav_out = None

    # -- callbacks -------------------------------------------------------

    def _in_cb(self, indata, frames, time_info, status):
        if status:
            # RT callback: never print here, report from the dsp thread.
            self._rt_status.append(f"capture: {status}")
        self._mic_ring.write(indata[:, 0])

    def _out_cb(self, outdata, frames, time_info, status):
        if status:
            self._rt_status.append(f"playback: {status}")
        # Read straight into the device buffer and push that same memory as
        # the reference, so the callback allocates nothing and takes no
        # lock.
        out = outdata[:, 0]
        self._play_ring.read_into(out)
        # The reference must be a continuous timeline: silence is a valid
        # part of it. Skipping silent chunks would shift every later
        # playback against the mic stream and void the delay compensation.
        self._ref_ring.write(out)

    # -- dsp -------------------------------------------------------------

    def _dsp_loop(self) -> None:
        while self._running:
            while self._rt_status:
                print(f"[audio] {self._rt_status.pop(0)}", flush=True)
            if self._mic_ring.available():
                self._drain_reference()
                proc = self._rs_mic.resample(self._mic_ring.read(self._dsp_block))
                cleaned = self.aec.process(proc)
                self._push_upstream(self._rs_up.resample(cleaned))
            else:
                # Nothing to cancel against: a file microphone, or a stalled
                # capture. Keep the reference moving anyway, or the ring
                # fills and the playback callback drops on every call.
                self._drain_reference()
                time.sleep(_DSP_IDLE_S)

    def _drain_reference(self) -> None:
        n = self._ref_ring.available()
        if n:
            self.aec.push_reference(self._rs_ref.resample(self._ref_ring.read(n)))

    def _push_upstream(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        self._leftover = np.concatenate((self._leftover, x))
        while self._leftover.size >= FRAME_SAMPLES:
            frame, self._leftover = (self._leftover[:FRAME_SAMPLES],
                                     self._leftover[FRAME_SAMPLES:])
            self._put_frame(float_to_pcm(frame))

    def _put_frame(self, frame: np.ndarray) -> None:
        try:
            self._up.put_nowait(frame)
        except queue.Full:
            # Network is behind: drop the oldest audio and keep the newest,
            # so the queue never lags further behind real time.
            try:
                self._up.get_nowait()
            except queue.Empty:
                pass
            try:
                self._up.put_nowait(frame)
            except queue.Full:
                pass

    def _read_file(self) -> None:
        """Feed a WAV as the microphone, paced at real time."""
        try:
            x, sr = read_wav_mono(self.input_file)
        except (SystemExit, OSError, wave.Error) as exc:
            print(f"[audio] {exc}; microphone stays silent", flush=True)
            return
        x = resample(x, sr, CLIENT_SR)
        step = FRAME_SAMPLES / CLIENT_SR
        silence = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        off = 0
        while self._running:
            t0 = time.perf_counter()
            if off < x.size:
                frame = x[off : off + FRAME_SAMPLES]
                off += FRAME_SAMPLES
                if frame.size < FRAME_SAMPLES:
                    frame = np.pad(frame, (0, FRAME_SAMPLES - frame.size))
            else:
                # An utterance closes on trailing silence, so the virtual
                # microphone has to keep running after the file is spent.
                frame = silence
            self._put_frame(float_to_pcm(frame))
            time.sleep(max(0.0, step - (time.perf_counter() - t0)))

    # -- public api ------------------------------------------------------

    def upstream_get(self, timeout: float = 0.2) -> np.ndarray | None:
        try:
            return self._up.get(timeout=timeout)
        except queue.Empty:
            return None

    def upstream_clear(self) -> None:
        """Drop queued audio. Called on (re)connect: the new socket is a new
        server session, and stale frames would shift the upstream timeline
        permanently against real time."""
        while True:
            try:
                self._up.get_nowait()
            except queue.Empty:
                return

    def play(self, pcm: np.ndarray, sample_rate: int) -> None:
        """Queue reply audio. pcm is int16 mono at sample_rate."""
        x = pcm_to_float(pcm)
        if self.output_file:
            self._write_wav(pcm, sample_rate)
        else:
            if self._rs_play is None or self._rs_play_key != sample_rate:
                self._rs_play = Resampler(sample_rate, self.out_sr)
                self._rs_play_key = sample_rate
            self._play_ring.write(self._rs_play.resample(x))
        self._play_started = True
        self._last_write = time.perf_counter()

    def _write_wav(self, pcm: np.ndarray, sample_rate: int) -> None:
        if self._wav_out is None:
            self._wav_out = wave.open(self.output_file, "wb")
            self._wav_out.setnchannels(1)
            self._wav_out.setsampwidth(2)
            self._wav_out.setframerate(sample_rate)
            self._wav_sr = sample_rate
        elif sample_rate != self._wav_sr:
            print(f"[audio] reply rate changed {self._wav_sr} -> {sample_rate}; "
                  "resampling into the file", flush=True)
            pcm = float_to_pcm(resample(pcm_to_float(pcm), sample_rate,
                                        self._wav_sr))
        self._wav_out.writeframes(pcm.astype("<i2").tobytes())

    def clear_playback(self) -> None:
        self._play_ring.discard()

    def playback_idle(self) -> bool:
        """True once playback has started and nothing is left to play.

        The ring alone empties while the device is still draining its
        latency buffer, so require a quiet period of play_tail_s first.
        """
        if self.output_file:
            return self._play_started
        return (self._play_started and self._play_ring.available() == 0
                and time.perf_counter() - self._last_write >= self.play_tail_s)

    def reset_playback(self) -> None:
        # Called on audio_start (nothing of the new turn is queued yet, so
        # clearing is safe) and after a turn ends; leftover audio from an
        # abandoned previous turn must not leak into the next one.
        self._play_started = False
        self._play_ring.discard()
