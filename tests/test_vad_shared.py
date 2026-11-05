"""The VAD model must not load per connection.

Each WebSocket session used to build its own onnxruntime InferenceSession
on the event loop, stalling every other session for the load time. The
model is now a shared singleton; the per-session LSTM state lives in
numpy arrays, so two VADs must run fully independent turns on one model.
"""

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

# orchestrator.vad imports onnxruntime at module level, so this skip has
# to run before that import chain starts.
pytest.importorskip("onnxruntime")
from orchestrator.config import model_root  # noqa: E402
from orchestrator.vad import SileroVAD, onnx_session  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (model_root() / "silero-vad" / "silero_vad.onnx").exists(),
    reason="Silero VAD weights are machine-local",
)


def test_sessions_share_one_model():
    a = SileroVAD()
    b = SileroVAD()
    assert a.session is b.session
    assert a.session is onnx_session()


def test_states_stay_independent():
    a, b = SileroVAD(), SileroVAD()
    speech = (np.random.default_rng(7).standard_normal(512 * 20) * 0.3
              ).astype(np.float32)
    for i in range(20):
        a.frame_prob(speech[i * 512:(i + 1) * 512])  # a hears a long utterance
    if hasattr(a, "_state"):
        assert not np.array_equal(a._state, b._state)
    else:
        assert not np.array_equal(a._h, b._h)


def test_reset_clears_state():
    a = SileroVAD()
    speech = (np.random.default_rng(9).standard_normal(512 * 20) * 0.3
              ).astype(np.float32)
    for i in range(20):
        a.frame_prob(speech[i * 512:(i + 1) * 512])
    a.reset()
    if hasattr(a, "_state"):
        assert not a._state.any()
    else:
        assert not a._h.any() and not a._c.any()
