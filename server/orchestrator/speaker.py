"""Who is talking: child, dad or mom, from a speaker-embedding model.

A CAM++ model (ONNX, CPU) turns a few seconds of speech into a 192-number
voiceprint. Each family member enrolls a few clips; an utterance is then
matched to the nearest enrolled voice. The model is small enough to run on
the CPU, which matters because the GPU is already full.

Only embeddings are stored, never audio.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime

from .config import model_root
from .vad import FRAME, SR, Resampler, SileroVAD

log = logging.getLogger("talknest.speaker")

ROLES = ("child", "dad", "mom")
PARENTS = ("dad", "mom")
LABELS = {"child": "孩子", "dad": "爸爸", "mom": "妈妈"}

DEFAULT_MODEL = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"

# Kaldi fbank settings the model was trained with.
FRAME_LEN = 400          # 25 ms at 16 kHz
FRAME_SHIFT = 160        # 10 ms
FFT_SIZE = 512
N_MELS = 80
LOW_FREQ = 20.0
PREEMPH = 0.97
# Speech kept around each voiced frame when trimming silence, in frames.
TRIM_PAD_FRAMES = 3
MAX_SAMPLES_PER_ROLE = 12


def _mel(freq):
    return 1127.0 * np.log(1.0 + freq / 700.0)


def _mel_banks() -> np.ndarray:
    """Kaldi triangular mel filters, shape (N_MELS, FFT_SIZE // 2 + 1)."""
    n_bins = FFT_SIZE // 2
    bin_hz = SR / FFT_SIZE
    lo, hi = _mel(LOW_FREQ), _mel(SR / 2.0)
    delta = (hi - lo) / (N_MELS + 1)
    freqs = _mel(bin_hz * np.arange(n_bins))
    banks = np.zeros((N_MELS, n_bins + 1), dtype=np.float32)
    for i in range(N_MELS):
        left = lo + i * delta
        center = left + delta
        right = center + delta
        up = (freqs - left) / (center - left)
        down = (right - freqs) / (right - center)
        banks[i, :n_bins] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks()
_WINDOW = (0.5 - 0.5 * np.cos(
    2.0 * np.pi * np.arange(FRAME_LEN) / (FRAME_LEN - 1))) ** 0.85
_EPS = np.finfo(np.float32).eps


def fbank(pcm16k: np.ndarray) -> np.ndarray:
    """80-dim log-mel features of 16 kHz audio, mean-normalized over time.

    Follows Kaldi's fbank (snip edges, DC removal, pre-emphasis, Povey
    window, no dither). Returns (frames, 80); empty when the audio is
    shorter than one frame.
    """
    x = np.asarray(pcm16k, dtype=np.float32)
    if len(x) < FRAME_LEN:
        return np.empty((0, N_MELS), dtype=np.float32)
    n = 1 + (len(x) - FRAME_LEN) // FRAME_SHIFT
    idx = np.arange(FRAME_LEN)[None, :] + FRAME_SHIFT * np.arange(n)[:, None]
    frames = x[idx].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)
    prev = np.concatenate((frames[:, :1], frames[:, :-1]), axis=1)
    frames = (frames - PREEMPH * prev) * _WINDOW
    spec = np.abs(np.fft.rfft(frames, n=FFT_SIZE, axis=1)) ** 2
    mel = np.log(np.maximum(spec @ _BANKS.T.astype(np.float64), _EPS))
    mel -= mel.mean(axis=0, keepdims=True)
    return mel.astype(np.float32)


class Embedder:
    """The ONNX model plus the audio plumbing around it. One per process."""

    def __init__(self, model_file: str = DEFAULT_MODEL, threads: int = 2):
        path = model_root() / "speaker" / model_file
        self.path = path
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        rate = int(meta.get("sample_rate", SR))
        if rate != SR:
            raise RuntimeError(f"{path.name}: expects {rate} Hz, not {SR}")
        self.dim = int(meta.get("output_dim", 192))
        self._input = self.session.get_inputs()[0].name
        self._lock = threading.Lock()

    def embed16(self, pcm16k: np.ndarray) -> np.ndarray | None:
        """Unit-length voiceprint of 16 kHz int16 audio, None if too short."""
        feats = fbank(np.asarray(pcm16k, dtype=np.float32))
        if len(feats) < 10:
            return None
        with self._lock:
            out = self.session.run(None, {self._input: feats[None]})[0]
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None


class SpeechTrimmer:
    """Drops silence so the voiceprint is made of voice only."""

    def __init__(self, threshold: float = 0.5):
        self._vad = SileroVAD(threshold=threshold)
        self._lock = threading.Lock()

    def trim(self, pcm24: np.ndarray, sr: int) -> np.ndarray:
        """16 kHz int16 audio holding only the voiced stretches of pcm24."""
        x = np.asarray(pcm24, dtype=np.int16)
        pcm16 = (x if sr == SR
                 else Resampler(sr, SR).process(x)).astype(np.float32)
        n = len(pcm16) // FRAME
        if n == 0:
            return np.empty(0, dtype=np.float32)
        voiced = np.zeros(n, dtype=bool)
        with self._lock:
            self._vad.reset()
            for i in range(n):
                voiced[i] = (self._vad.frame_prob(
                    pcm16[i * FRAME:(i + 1) * FRAME]) > self._vad.threshold)
        keep = voiced.copy()
        for shift in range(1, TRIM_PAD_FRAMES + 1):
            keep[shift:] |= voiced[:-shift]
            keep[:-shift] |= voiced[shift:]
        if not keep.any():
            return np.empty(0, dtype=np.float32)
        return np.concatenate(
            [pcm16[i * FRAME:(i + 1) * FRAME] for i in np.flatnonzero(keep)])


@dataclass
class Match:
    role: str | None      # None when the voice is not clearly anyone's
    score: float = 0.0


class SpeakerBook:
    """The enrolled voices of one learner's household.

    Lives next to the learner's other state as speakers.json. The household
    is small, so the whole file is rewritten on every change.
    """

    def __init__(self, path: Path, embedder: Embedder | None,
                 trimmer: SpeechTrimmer | None, cfg: dict):
        self.path = Path(path)
        self.embedder = embedder
        self.trimmer = trimmer
        self.min_score = float(cfg.get("min_score", 0.45))
        self.margin = float(cfg.get("margin", 0.05))
        self.min_seconds = float(cfg.get("min_seconds", 1.0))
        self.enroll_min_seconds = float(cfg.get("enroll_min_seconds", 3.0))
        self.max_enroll_seconds = float(cfg.get("max_enroll_seconds", 20.0))
        self._lock = threading.Lock()
        self._last_id = 0
        self._voices: dict[str, list[dict]] = {r: [] for r in ROLES}
        self._centroids: dict[str, np.ndarray] = {}
        self.reload()

    # -- persistence -------------------------------------------------------

    def reload(self) -> None:
        voices: dict[str, list[dict]] = {r: [] for r in ROLES}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for role in ROLES:
                for item in data.get(role, []):
                    voices[role].append({
                        "vec": [float(v) for v in item["vec"]],
                        "seconds": float(item.get("seconds", 0.0)),
                        "id": int(item.get("id", 0)),
                    })
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
        with self._lock:
            self._voices = voices
            self._last_id = max((i["id"] for items in voices.values()
                                 for i in items), default=0)
            if self._assign_missing_ids():
                self._save()
            self._rebuild()

    def _assign_missing_ids(self) -> bool:
        """Give every clip an id of its own; True when any had to change.

        Files written before clips had ids give all of them 0, and deleting
        clip 0 would then delete every one of them. The ids are written back
        at once, because each request reads the file afresh and has to see
        the same ids the page was shown.
        """
        seen, changed = set(), False
        for items in self._voices.values():
            for item in items:
                if item["id"] <= 0 or item["id"] in seen:
                    item["id"] = self._next_id()
                    changed = True
                seen.add(item["id"])
        return changed

    def _next_id(self) -> int:
        """When the clip was taken, in ms, but always above every id in use:
        a clock stepping back must not hand out an id twice."""
        self._last_id = max(int(time.time() * 1000), self._last_id + 1)
        return self._last_id

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._voices), encoding="utf-8")
        os.replace(tmp, self.path)

    def _rebuild(self) -> None:
        self._centroids = {}
        for role, items in self._voices.items():
            if not items:
                continue
            mean = np.mean([np.asarray(i["vec"], dtype=np.float32)
                            for i in items], axis=0)
            norm = float(np.linalg.norm(mean))
            if norm > 0:
                self._centroids[role] = mean / norm

    # -- state -------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """The model is loaded, so audio can be enrolled and matched."""
        return self.embedder is not None

    def enabled(self) -> bool:
        """Matching needs the child and at least one parent enrolled."""
        return (self.ready and "child" in self._centroids
                and any(p in self._centroids for p in PARENTS))

    def status(self) -> dict:
        with self._lock:
            roles = {
                role: {"samples": len(items),
                       "seconds": round(sum(i["seconds"] for i in items), 1),
                       "clips": [{"id": i["id"],
                                  "seconds": round(i["seconds"], 1)}
                                 for i in items]}
                for role, items in self._voices.items()
            }
        return {"ready": self.ready, "enabled": self.enabled(),
                "roles": roles}

    # -- enroll / clear ----------------------------------------------------

    def enroll(self, role: str, pcm24: np.ndarray, sr: int) -> float:
        """Add one recording to a role; returns the seconds of voice used.

        Raises ValueError with a message for the person recording when the
        clip cannot be used.
        """
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if not self.ready:
            raise ValueError("声纹模型未加载")
        if len(pcm24) / sr > self.max_enroll_seconds:
            raise ValueError(f"录音太长，请控制在 {self.max_enroll_seconds:g} 秒内")
        voice = self.trimmer.trim(pcm24, sr)
        seconds = len(voice) / SR
        if seconds < self.enroll_min_seconds:
            raise ValueError(
                f"有效语音只有 {seconds:.1f} 秒，至少需要 "
                f"{self.enroll_min_seconds:g} 秒，请靠近麦克风再录一次")
        vec = self.embedder.embed16(voice)
        if vec is None:
            raise ValueError("无法从这段录音提取声纹")
        with self._lock:
            items = self._voices[role]
            items.append({"vec": [float(v) for v in vec],
                          "seconds": seconds, "id": self._next_id()})
            del items[:-MAX_SAMPLES_PER_ROLE]
            self._rebuild()
            self._save()
        return seconds

    def drop(self, role: str, clip_id: int) -> bool:
        """Forget one recording. False when it is not there."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        with self._lock:
            items = self._voices[role]
            kept = [i for i in items if i["id"] != clip_id]
            if len(kept) == len(items):
                return False
            self._voices[role] = kept
            self._rebuild()
            self._save()
        return True

    def clear(self, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        with self._lock:
            self._voices[role] = []
            self._rebuild()
            self._save()

    # -- matching ----------------------------------------------------------

    def identify(self, pcm24: np.ndarray | None, sr: int = 24000) -> Match:
        """Who spoke this utterance. role is None when it is unclear."""
        if pcm24 is None or not self.enabled():
            return Match(None)
        voice = self.trimmer.trim(pcm24, sr)
        if len(voice) / SR < self.min_seconds:
            return Match(None)
        vec = self.embedder.embed16(voice)
        if vec is None:
            return Match(None)
        with self._lock:
            scores = sorted(
                ((float(vec @ c), role) for role, c in self._centroids.items()),
                reverse=True)
        best_score, best_role = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        if best_score < self.min_score or best_score - second < self.margin:
            return Match(None, best_score)
        return Match(best_role, best_score)


_embedder: Embedder | None = None
_trimmer: SpeechTrimmer | None = None
_load_lock = threading.Lock()
_load_failed = False


def shared_models(cfg: dict) -> tuple[Embedder | None, SpeechTrimmer | None]:
    """Load the model once per process; (None, None) if disabled or missing."""
    global _embedder, _trimmer, _load_failed
    if not cfg.get("enabled", False):
        return None, None
    with _load_lock:
        if _embedder is None and not _load_failed:
            try:
                _embedder = Embedder(cfg.get("model", DEFAULT_MODEL),
                                     int(cfg.get("threads", 2)))
                _trimmer = SpeechTrimmer()
            except Exception as e:  # missing weights must not stop the server
                _load_failed = True
                log.warning("speaker model unavailable: %s", e)
        return _embedder, _trimmer
