"""Capture, playback, resampling and the upstream DSP chain.

Three threads plus the asyncio loop:

  PortAudio callbacks   only move samples in and out of queues
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


class PcmFifo:
    """Sample stream that pads reads with silence instead of underrunning."""

    def __init__(self):
        self._chunks = []
        self._offset = 0
        self._size = 0
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(np.asarray(data, dtype=np.float32))
            self._size += len(data)

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            filled = 0
            while filled < n and self._chunks:
                head = self._chunks[0]
                take = min(n - filled, len(head) - self._offset)
                out[filled : filled + take] = head[self._offset : self._offset + take]
                filled += take
                self._offset += take
                self._size -= take
                if self._offset >= len(head):
                    self._chunks.pop(0)
                    self._offset = 0
        return out

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._offset = 0
            self._size = 0

    def available(self) -> int:
        with self._lock:
            return self._size


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
        # has drained too; the software FIFO empties while the last samples
        # are still in the device's latency buffer.
        self.play_tail_s = play_tail_s
        self._up: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=512)
        self._mic_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=512)
        self._ref_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=512)
        self._play = PcmFifo()
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
        if self.input_file:
            self._reader = threading.Thread(target=self._read_file, daemon=True)
            self._reader.start()
        else:
            require_portaudio()
            self.in_sr = self._pick_rate("input", self.input_device, CLIENT_SR)
            self._streams.append(sd.InputStream(
                device=self.input_device, channels=1, samplerate=self.in_sr,
                dtype="float32", blocksize=self._blocksize, callback=self._in_cb))
        if not self.output_file:
            require_portaudio()
            self.out_sr = self._pick_rate("output", self.output_device, CLIENT_SR)
            self._streams.append(sd.OutputStream(
                device=self.output_device, channels=1, samplerate=self.out_sr,
                dtype="float32", blocksize=self._blocksize, callback=self._out_cb))
        for s in self._streams:
            s.start()
        # Stateful resamplers: one filter instance per chain, so chunked
        # blocks stay continuous across boundaries.
        self._rs_mic = Resampler(self.in_sr, self.aec.proc_sr)
        self._rs_up = Resampler(self.aec.proc_sr, CLIENT_SR)
        self._rs_ref = Resampler(self.out_sr, self.aec.proc_sr)
        # Started in file mode too: the playback callback feeds the reference
        # queue, and an undrained queue overflows inside the RT callback.
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
        try:
            self._mic_q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # never raise inside an RT callback; drop instead

    def _out_cb(self, outdata, frames, time_info, status):
        if status:
            self._rt_status.append(f"playback: {status}")
        chunk = self._play.read(frames)
        outdata[:, 0] = chunk
        # The reference must be a continuous timeline: silence is a valid
        # part of it. Skipping silent chunks would shift every later
        # playback against the mic stream and void the delay compensation.
        try:
            self._ref_q.put_nowait(chunk.copy())
        except queue.Full:
            # dsp thread is behind; keep the newest end of the timeline
            try:
                self._ref_q.get_nowait()
                self._ref_q.put_nowait(chunk.copy())
            except (queue.Empty, queue.Full):
                pass

    # -- dsp -------------------------------------------------------------

    def _dsp_loop(self) -> None:
        while self._running:
            while self._rt_status:
                print(f"[audio] {self._rt_status.pop(0)}", flush=True)
            try:
                block = self._mic_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._drain_reference()
            proc = self._rs_mic.resample(block)
            cleaned = self.aec.process(proc)
            self._push_upstream(self._rs_up.resample(cleaned))

    def _drain_reference(self) -> None:
        while True:
            try:
                block = self._ref_q.get_nowait()
            except queue.Empty:
                return
            self.aec.push_reference(self._rs_ref.resample(block))

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
        except SystemExit as exc:
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
            self._play.write(self._rs_play.resample(x))
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
        self._play.clear()

    def playback_idle(self) -> bool:
        """True once playback has started and nothing is left to play.

        The FIFO alone empties while the device is still draining its
        latency buffer, so require a quiet period of play_tail_s first.
        """
        if self.output_file:
            return self._play_started
        return (self._play_started and self._play.available() == 0
                and time.perf_counter() - self._last_write >= self.play_tail_s)

    def reset_playback(self) -> None:
        # Called on audio_start (nothing of the new turn is queued yet, so
        # clearing is safe) and after a turn ends; leftover audio from an
        # abandoned previous turn must not leak into the next one.
        self._play_started = False
        self._play.clear()
