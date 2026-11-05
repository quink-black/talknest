"""Quantitative tests for the Wiener pre-stage's validation gate.

Run: ./venv/bin/python -m pytest tests/test_wiener_aec.py -v

Synthetic far-end speech over a fixed random echo path, no services or
models needed. The double-talk regression test is the one that exposes
the old same-window gate: a least-squares solve gated on its own samples
adopts near-end-polluted filters, and the measured ERLE collapses.
"""

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from orchestrator.wiener_aec import WienerAec  # noqa: E402

SR = 16000
# Small enough for the conjugate-gradient solve to stay fast, large
# enough that a 512-tap path is identifiable from the window.
TAPS = 512
WINDOW_S = 1.0
REFRESH_S = 0.5


def colored_noise(n, seed, cutoff=0.05):
    """Band-limited noise: speech-like autocorrelation, deterministic."""
    rng = np.random.default_rng(seed)
    w = rng.standard_normal(n)
    freq = np.fft.rfftfreq(n)
    shape = 1.0 / (1.0 + (freq / cutoff) ** 3)
    y = np.fft.irfft(np.fft.rfft(w) * shape, n)
    return y / np.std(y)


def echo_path(taps, seed):
    rng = np.random.default_rng(seed)
    h = rng.standard_normal(taps) * np.exp(-np.arange(taps) / (taps / 5.0))
    return h / np.sqrt(np.sum(h**2))


def erle_db(mic, out):
    return 10.0 * np.log10(np.sum(mic**2) / np.sum(out**2))


def run(mic, ref, **kwargs):
    aec = WienerAec(taps=TAPS, window_s=WINDOW_S, refresh_s=REFRESH_S,
                    sr=SR, **kwargs)
    out = np.zeros_like(mic)
    step = 1024
    for i in range(0, len(mic), step):
        out[i:i + step] = aec.process(mic[i:i + step], ref[i:i + step])
    return aec, out


def feed(aec, mic, ref):
    out = np.zeros_like(mic)
    step = 1024
    for i in range(0, len(mic), step):
        out[i:i + step] = aec.process(mic[i:i + step], ref[i:i + step])
    return out


def make_signal(double_talk_from=None, double_talk_until=None, n_s=6.0):
    """Far-end echo; near-end speech between the two marks, if given."""
    n = int(n_s * SR)
    ref = colored_noise(n, seed=11)
    h = echo_path(TAPS, seed=23)
    echo = np.convolve(ref, h)[:n]
    mic = echo + 0.01 * colored_noise(n, seed=37)
    if double_talk_from is not None:
        start = int(double_talk_from * SR)
        end = int(double_talk_until * SR) if double_talk_until else n
        near = 2.0 * colored_noise(end - start, seed=41)  # ~+6 dB over echo
        mic[start:end] += near
    return mic, ref


def test_converges_on_clean_far_end():
    mic, ref = make_signal(n_s=4.0)
    aec, out = run(mic, ref)
    tail = slice(int(3.0 * SR), None)  # last second, filter long settled
    assert erle_db(mic[tail], out[tail]) > 15.0


def test_double_talk_does_not_break_the_filter():
    # Old same-window gate: a double-talk-polluted solve is adopted and
    # the ERLE stays collapsed after the near end goes quiet.
    mic, ref = make_signal(double_talk_from=3.0, double_talk_until=5.0,
                           n_s=7.0)
    aec, out = run(mic, ref)
    after = slice(int(5.5 * SR), None)  # clean far end, near end gone
    assert erle_db(mic[after], out[after]) > 15.0


def test_double_talk_freezes_the_filter():
    mic, ref = make_signal(double_talk_from=3.0, double_talk_until=6.0,
                           n_s=6.0)
    aec = WienerAec(taps=TAPS, window_s=WINDOW_S, refresh_s=REFRESH_S,
                    sr=SR)
    step = 1024
    feed(aec, mic[:int(3.0 * SR)], ref[:int(3.0 * SR)])
    assert aec.adopted >= 1  # the clean phase did converge
    g_before = aec.g.copy()
    feed(aec, mic[int(3.0 * SR):], ref[int(3.0 * SR):])
    # While the child talks over the assistant, the echo path must stay
    # exactly as it was: neither a double-talk-fitted solve nor a noisy
    # one may replace a converged filter.
    assert np.array_equal(g_before, aec.g)


def test_no_solve_on_silent_far_end():
    n = int(3.0 * SR)
    mic = 0.5 * colored_noise(n, seed=53)  # near-end only
    aec, _ = run(mic, np.zeros(n))
    assert aec.solves == 0
