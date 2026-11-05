"""Wiener echo-path estimation for the stereo upstream.

The ESP32 terminal's speaker-to-mic path is fixed hardware geometry, but
it is strongly nonlinear at high playback volume: the best possible
linear filter on captured sessions removes only ~13 dB. That linear part
is still worth taking, because it converges once and then cancels
instantly at every far-end onset -- exactly the window where the DTLN
neural canceller re-converges too slowly and leaks speech-shaped residual
that trips the VAD (measured: self-interrupts within 0.7 s of every
playback onset, both here and in the old CLI bench).

Instead of tracking the path sample by sample (LMS diverges on the
autocorrelated speech reference), the Wiener-Hopf equations are solved
over a sliding window every refresh: FFT correlations give the Toeplitz
system, conjugate gradient solves it with diagonal loading for stability,
and a validation gate keeps the incumbent filter unless the new one
cancels better on a hold-out tail that the solve never saw -- a
least-squares fit is optimal on its own samples, so gating on the same
window would adopt every double-talk-polluted estimate. numpy only --
the server venv has no scipy.
"""

import numpy as np

BLOCK = 128  # samples per call, matches the DTLN step


def _fft_xcorr_lags(a, b, max_lag):
    """c[k] = sum_n a[n]*b[n-k] for k in 0..max_lag (a, b same length)."""
    n = 1
    while n < len(a) + max_lag:
        n *= 2
    fa = np.fft.rfft(a, n)
    fb = np.fft.rfft(b, n)
    c = np.fft.irfft(fa * np.conj(fb), n)
    return c[: max_lag + 1]


def _toeplitz_matvec(c, x):
    """(T x)[i] = sum_j c[|i-j|] x[j], as one full convolution."""
    n = len(x)
    k = np.concatenate([c[:0:-1], c])  # c[L-1..1, c0, c1..L-1]
    conv = np.convolve(x, k)
    return conv[len(c) - 1: len(c) - 1 + n]


def _toeplitz_solve(c, p, iters=64, tol=1e-10):
    """Conjugate gradient on the diagonally loaded, hence SPD, system."""
    n = len(p)
    x = np.zeros(n)
    r = p.astype(np.float64).copy()
    d = r.copy()
    rr = float(np.dot(r, r))
    if rr == 0.0:
        return x
    for _ in range(iters):
        Ad = _toeplitz_matvec(c, d)
        denom = float(np.dot(d, Ad))
        if denom <= 0.0:
            break
        a = rr / denom
        x += a * d
        r -= a * Ad
        rr_new = float(np.dot(r, r))
        if rr_new <= tol * tol * max(float(np.dot(p, p)), 1e-30):
            break
        d = r + (rr_new / rr) * d
        rr = rr_new
    return x


class WienerAec:
    """Cancels the linear part of a static echo path at 16 kHz."""

    def __init__(self, taps=2048, window_s=2.0, refresh_s=1.0, sr=16000):
        self.L = taps
        self.window = int(window_s * sr)
        self.refresh = int(refresh_s * sr)
        # The validation tail must never overlap the solve window: a
        # least-squares fit is optimal on its own data, so gating on the
        # same samples would adopt every double-talk-polluted estimate.
        self.validate = max(int(0.25 * sr), self.L)
        self.g = np.zeros(taps, dtype=np.float64)
        self.hist = np.zeros(taps - 1, dtype=np.float64)
        self._ref = np.zeros(0, dtype=np.float64)
        self._mic = np.zeros(0, dtype=np.float64)
        self._since = 0
        self.solves = 0
        self.adopted = 0
        self.rejected = 0

    def _solve_path(self, ref, mic):
        c = _fft_xcorr_lags(ref, ref, self.L - 1)
        p = _fft_xcorr_lags(mic, ref, self.L - 1)
        # Diagonal loading against the band-limited, near-singular
        # autocorrelation of speech.
        c[0] *= 1.001
        return _toeplitz_solve(c, p)

    def process(self, mic, ref):
        """One block of mic and reference; returns the linear residual."""
        self._ref = np.concatenate([self._ref, ref])[-self.window:]
        self._mic = np.concatenate([self._mic, mic])[-self.window:]
        self._since += len(mic)

        out = np.zeros_like(mic)
        for i in range(0, len(mic), BLOCK):
            m = mic[i:i + BLOCK]
            x = np.concatenate([self.hist, ref[i:i + BLOCK]])
            y = np.convolve(x, self.g, mode="valid")[-len(m):]
            out[i:i + len(m)] = m - y
            self.hist = x[-(self.L - 1):]

        if self._since >= self.refresh:
            self._since = 0
            W = len(self._ref)
            # Solve only when the window holds real far-end energy;
            # silence carries no path information.
            if W >= self.L + self.validate and \
                    float(np.dot(self._ref, self._ref)) > 1e-6 * W:
                pad = np.concatenate([np.zeros(self.L - 1), self._ref])
                y_old = np.convolve(pad, self.g, mode="valid")[:W]
                mic_pow = float(np.dot(self._mic, self._mic))
                echo_pow = float(np.dot(y_old, y_old))
                # Double-talk guard. A hold-out tail alone cannot stop a
                # polluted solve: with enough taps the fit tracks the
                # near-end statistics, which hold on the validation tail
                # too. What it cannot fake is total energy -- near-end
                # speech adds power the echo path does not explain. When
                # the mic carries well beyond the modeled echo, freeze
                # the incumbent; before any convergence there is nothing
                # to protect, so always solve.
                if echo_pow < 1e-9 * W or mic_pow < 4.0 * echo_pow:
                    # Fit on the head of the window, validate on the
                    # disjoint tail, so a solve is judged on samples it
                    # never saw.
                    fit = W - self.validate
                    g_new = self._solve_path(self._ref[:fit], self._mic[:fit])
                    self.solves += 1
                    if np.isfinite(g_new).all():
                        y_new = np.convolve(pad, g_new, mode="valid")[:W][fit:]
                        y_old_v = y_old[fit:]
                        e_new = float(np.sum((self._mic[fit:] - y_new) ** 2))
                        e_old = float(np.sum((self._mic[fit:] - y_old_v) ** 2))
                        if e_new < e_old:
                            self.g = g_new
                            self.adopted += 1
                        else:
                            self.rejected += 1
                else:
                    self.rejected += 1
        return out
