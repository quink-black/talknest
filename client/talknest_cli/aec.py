"""Echo cancellation backends.

A backend takes microphone audio and the loudspeaker reference at its own
processing rate and returns audio with the echo removed. The reference is
pushed out of band (by the playback path) because it is produced at a
different time than it is consumed.

Alignment is by sample count, not by wall clock: capture and playback run on
the same device clock, so a fixed pipeline delay turns into a fixed number of
leading zero reference samples.
"""

import numpy as np

BLOCK_SHIFT = 128  # 8 ms at 16 kHz, the DTLN-aec frame step
BLOCK_LEN = 512


class EchoCanceller:
    """Echo canceller running at :attr:`proc_sr`."""

    name = "none"
    proc_sr = 24000

    def reset(self, delay_samples: int = 0) -> None:
        """Start a new stream, discarding buffered state."""

    def push_reference(self, ref: np.ndarray) -> None:
        """Append loudspeaker audio at proc_sr (float32, mono)."""

    def process(self, mic: np.ndarray) -> np.ndarray:
        """Remove echo from mic audio at proc_sr; returns <= len(mic) samples."""
        return mic

    def close(self) -> None:
        pass


class NullAec(EchoCanceller):
    """No cancellation. Half duplex upstream gating covers for it."""

    name = "none"
    proc_sr = 24000

    def process(self, mic: np.ndarray) -> np.ndarray:
        return mic


class DtlnAec(EchoCanceller):
    """DTLN-aec: dual-signal transformation LSTM, AEC-Challenge 3rd place.

    Runs two TFLite graphs per 8 ms frame -- a spectral mask net and a
    time-domain refinement net -- carrying one LSTM state each. Inputs are
    16 kHz mono float32, which is why this backend resamples the stream.
    """

    name = "dtln"
    proc_sr = 16000

    def __init__(self, model_prefix: str):
        from ai_edge_litert.interpreter import Interpreter

        self._interp = [
            Interpreter(model_path=f"{model_prefix}_{i}.tflite") for i in (1, 2)
        ]
        for it in self._interp:
            it.allocate_tensors()
        self.reset()

    def reset(self, delay_samples: int = 0) -> None:
        self._mic_buf = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._ref_buf = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._out_buf = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._pending_ref = np.zeros(0, dtype=np.float32)
        self._pending_mic = np.zeros(0, dtype=np.float32)
        # The overlap-add buffer needs BLOCK_LEN - BLOCK_SHIFT samples before
        # its output is complete. Padding both streams with that much silence
        # reproduces the reference implementation's startup handling; the
        # cost is the same amount of constant pipeline latency.
        warmup = np.zeros(BLOCK_LEN - BLOCK_SHIFT, dtype=np.float32)
        self._pending_mic = warmup
        # The echo of a reference sample arrives delay_samples later, so
        # shifting the reference right by that much aligns the two.
        self._pending_ref = warmup
        if delay_samples > 0:
            self._pending_ref = np.concatenate(
                (self._pending_ref, np.zeros(delay_samples, dtype=np.float32)))
        self._states = [
            np.zeros(self._interp[i].get_input_details()[1]["shape"],
                     dtype=np.float32) for i in (0, 1)
        ]

    def push_reference(self, ref: np.ndarray) -> None:
        self._pending_ref = np.concatenate((self._pending_ref, ref))
        # Bound the backlog: if the consumer stalls (e.g. file input mode,
        # where nothing ever calls process()), older reference is useless
        # for cancellation anyway.
        cap = self.proc_sr * 2
        if len(self._pending_ref) > cap:
            self._pending_ref = self._pending_ref[-cap:]

    def process(self, mic: np.ndarray) -> np.ndarray:
        self._pending_mic = np.concatenate((self._pending_mic, mic))
        n_blocks = len(self._pending_mic) // BLOCK_SHIFT
        if n_blocks == 0:
            return np.zeros(0, dtype=np.float32)
        n = n_blocks * BLOCK_SHIFT
        blocks = self._pending_mic[:n].reshape(n_blocks, BLOCK_SHIFT)
        self._pending_mic = self._pending_mic[n:]
        out = [self._step(b, self._take_ref(BLOCK_SHIFT)) for b in blocks]
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    def _take_ref(self, n: int) -> np.ndarray:
        if len(self._pending_ref) >= n:
            out = self._pending_ref[:n]
            self._pending_ref = self._pending_ref[n:]
            return out
        out = np.zeros(n, dtype=np.float32)
        have = len(self._pending_ref)
        if have:
            out[:have] = self._pending_ref
            self._pending_ref = np.zeros(0, dtype=np.float32)
        return out

    def _step(self, mic_block: np.ndarray, ref_block: np.ndarray) -> np.ndarray:
        self._mic_buf[:-BLOCK_SHIFT] = self._mic_buf[BLOCK_SHIFT:]
        self._mic_buf[-BLOCK_SHIFT:] = mic_block
        self._ref_buf[:-BLOCK_SHIFT] = self._ref_buf[BLOCK_SHIFT:]
        self._ref_buf[-BLOCK_SHIFT:] = ref_block

        mic_fft = np.fft.rfft(self._mic_buf)
        mic_mag = np.abs(mic_fft).reshape(1, 1, -1).astype(np.float32)
        ref_mag = np.abs(np.fft.rfft(self._ref_buf)).reshape(1, 1, -1).astype(np.float32)

        net1, net2 = self._interp
        net1.set_tensor(net1.get_input_details()[0]["index"], mic_mag)
        net1.set_tensor(net1.get_input_details()[1]["index"], self._states[0])
        net1.set_tensor(net1.get_input_details()[2]["index"], ref_mag)
        net1.invoke()
        mask = np.squeeze(net1.get_tensor(net1.get_output_details()[0]["index"]))
        self._states[0] = net1.get_tensor(net1.get_output_details()[1]["index"])

        estimated = np.fft.irfft(mic_fft * mask, n=BLOCK_LEN).reshape(1, 1, -1)
        estimated = estimated.astype(np.float32)

        net2.set_tensor(net2.get_input_details()[0]["index"], estimated)
        net2.set_tensor(net2.get_input_details()[1]["index"], self._states[1])
        net2.set_tensor(net2.get_input_details()[2]["index"],
                        self._ref_buf.reshape(1, 1, -1))
        net2.invoke()
        block = np.squeeze(net2.get_tensor(net2.get_output_details()[0]["index"]))
        self._states[1] = net2.get_tensor(net2.get_output_details()[1]["index"])

        self._out_buf[:-BLOCK_SHIFT] = self._out_buf[BLOCK_SHIFT:]
        self._out_buf[-BLOCK_SHIFT:] = 0.0
        self._out_buf += block
        return self._out_buf[:BLOCK_SHIFT].copy()


def erle_db(mic: np.ndarray, residual: np.ndarray) -> float:
    """Echo return loss enhancement over a far-end single-talk segment."""
    num = float(np.mean(mic.astype(np.float64) ** 2))
    den = float(np.mean(residual.astype(np.float64) ** 2))
    if den <= 1e-12:
        return float("inf")
    return 10.0 * np.log10(num / den)
