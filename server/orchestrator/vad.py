"""Silero VAD (ONNX, CPU) with streaming turn segmentation.

The browser streams 24 kHz PCM continuously. Silero itself runs at 16 kHz,
so the stream is decimated on the fly and fed to the model frame by frame.
StreamVAD turns those per-frame probabilities into utterances: it opens one
after a short voiced run and closes it after a tail of silence, so the
caller never has to signal the start or end of a sentence.
"""

from dataclasses import dataclass

import numpy as np
import onnxruntime

from .config import model_root

MODEL_PATH = str(model_root() / "silero-vad" / "silero_vad.onnx")
FRAME = 512          # samples per VAD frame at 16 kHz
SR = 16000
FRAME_MS = FRAME * 1000.0 / SR


class Resampler:
    """Linear-interpolation rate converter that keeps phase across calls.

    Feeding a stream in chunks must produce the same samples as feeding it
    whole, so the leftover sample and the fractional read position are
    carried over between calls.
    """

    def __init__(self, in_sr: int, out_sr: int):
        self.step = in_sr / out_sr      # input samples per output sample
        self._prev = np.float32(0.0)
        self._frac = 0.0                # output position, in samples past _prev

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = np.concatenate(([self._prev], pcm.astype(np.float32)))
        n = max(0, int((len(pcm) - self._frac) // self.step) + 1)
        if n == 0:
            self._prev = x[-1]
            self._frac -= len(pcm)
            return np.empty(0, dtype=np.float32)
        pos = self._frac + self.step * np.arange(n)
        out = np.interp(pos, np.arange(len(x)), x).astype(np.float32)
        self._frac = float(pos[-1] + self.step - len(pcm))
        self._prev = x[-1]
        return out


class SileroVAD:
    def __init__(self, threshold: float = 0.5, model_path: str = MODEL_PATH):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.threshold = threshold
        self._inputs = {i.name: i.shape for i in self.session.get_inputs()}
        self.reset()

    def reset(self):
        # v4 exports use separate h/c (2,1,64); v5 uses one "state" tensor
        # of shape (2,1,128). Support both.
        if "state" in self._inputs:
            n = self._inputs["state"][2]
            self._state = np.zeros((2, 1, n), dtype=np.float32)
        else:
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)
        # The model consumes 64 context samples + 512 new samples per call
        # (576 wide); the context slides forward after each frame.
        self._context = np.zeros(64, dtype=np.float32)

    def frame_prob(self, frame: np.ndarray) -> float:
        """One 16 kHz frame of int16 samples -> speech probability."""
        # Silero expects float32 input normalized to [-1, 1].
        new = frame.astype(np.float32) / 32768.0
        x = np.concatenate([self._context, new]).reshape(1, -1)
        feed = {"input": x}
        if "state" in self._inputs:
            feed["state"] = self._state
        else:
            feed["h"] = self._h
            feed["c"] = self._c
        if "sr" in self._inputs:
            feed["sr"] = np.array(SR, dtype=np.int64)
        outs = self.session.run(None, feed)
        if "state" in self._inputs:
            self._state = outs[1]
        else:
            self._h, self._c = outs[1], outs[2]
        self._context = x[0, -64:]
        return float(outs[0].squeeze())


@dataclass
class VADEvent:
    kind: str                       # "speech_start" | "utterance"
    pcm24: np.ndarray | None = None  # utterance audio at the client rate
    reason: str = ""                 # "silence" | "max_speech"


class StreamVAD:
    """Turns a continuous PCM stream into utterances.

    Audio is buffered at the client rate (24 kHz) so an utterance can start
    before the first voiced frame: pre_roll_ms of lead-in is kept, which is
    what stops the first syllable from being cut off.
    """

    def __init__(self, vad: SileroVAD, pre_roll_ms: int = 300,
                 silence_ms: int = 700, start_ms: int = 120,
                 min_speech_ms: int = 300, max_speech_ms: int = 20000,
                 client_sr: int = 24000):
        self.vad = vad
        self.client_sr = client_sr
        self.pre_roll_ms = pre_roll_ms
        self.silence_ms = silence_ms
        self.start_ms = start_ms
        self.min_speech_ms = min_speech_ms
        self.max_speech_ms = max_speech_ms
        self.cap = int((pre_roll_ms + max_speech_ms) * client_sr / 1000)
        self.resampler = Resampler(client_sr, SR)
        self._pend16 = np.empty(0, dtype=np.float32)
        self._buf = np.empty(0, dtype=np.int16)
        self._end = 0            # absolute client-rate index of buffer end
        self._consumed16 = 0
        self.reset()

    def reset(self):
        self.vad.reset()
        self.active = False
        self.speech_ms = 0.0      # voiced time in the current utterance
        self.elapsed_ms = 0.0
        self._voiced_run = 0.0
        self._silence_run = 0.0
        self._start = 0

    def feed(self, pcm24: np.ndarray, threshold: float | None = None) -> list[VADEvent]:
        """Consume a chunk of client-rate int16 PCM, emit pending events.

        A higher threshold can be passed while the assistant is speaking,
        so echo leaking into the mic is harder to mistake for a turn.
        """
        thr = self.vad.threshold if threshold is None else threshold
        x = np.asarray(pcm24, dtype=np.int16)
        self._buf = np.concatenate((self._buf, x)) if len(self._buf) else x.copy()
        self._end += len(x)
        if len(self._buf) > self.cap:
            self._buf = self._buf[len(self._buf) - self.cap:]

        self._pend16 = np.concatenate((self._pend16, self.resampler.process(x)))
        n = len(self._pend16) // FRAME
        events = []
        for i in range(n):
            prob = self.vad.frame_prob(self._pend16[i * FRAME : (i + 1) * FRAME])
            self._consumed16 += FRAME
            end = int(self._consumed16 * self.client_sr / SR)
            ev = self._step(prob > thr, end)
            if ev is not None:
                events.append(ev)
        self._pend16 = self._pend16[n * FRAME:]
        return events

    def _step(self, voiced: bool, end: int) -> VADEvent | None:
        if not self.active:
            self._voiced_run = self._voiced_run + FRAME_MS if voiced else 0.0
            if self._voiced_run < self.start_ms:
                return None
            self.active = True
            self.elapsed_ms = 0.0
            self.speech_ms = 0.0
            self._silence_run = 0.0
            self._start = max(0, end - int(self.pre_roll_ms * self.client_sr / 1000))
            return VADEvent("speech_start")

        self.elapsed_ms += FRAME_MS
        if voiced:
            self.speech_ms += FRAME_MS
            self._silence_run = 0.0
        else:
            self._silence_run += FRAME_MS
        if self._silence_run >= self.silence_ms:
            return self._close(end, "silence")
        if self.elapsed_ms >= self.max_speech_ms:
            return self._close(end, "max_speech")
        return None

    def _close(self, end: int, reason: str) -> VADEvent | None:
        too_short = self.speech_ms < self.min_speech_ms
        start = self._start
        self.reset()
        if too_short:
            return None
        lo = max(0, start - (self._end - len(self._buf)))
        hi = min(len(self._buf), end - (self._end - len(self._buf)))
        return VADEvent("utterance", self._buf[max(lo, 0):max(hi, lo)].copy(), reason)
