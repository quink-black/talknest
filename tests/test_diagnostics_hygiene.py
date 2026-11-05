"""Diagnostics must stay bounded, and a malformed mode must not drop the WS."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

pytest.importorskip("onnxruntime")
from orchestrator import main  # noqa: E402
from orchestrator.config import model_root  # noqa: E402
from orchestrator.aec_stream import UpstreamAec  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (model_root() / "silero-vad" / "silero_vad.onnx").exists(),
    reason="Silero VAD weights are machine-local",
)


class FakeAec:
    """Stands in for the real AEC so dump() can be driven directly."""

    def __init__(self):
        self.calls = 0
        self.dumped = []

    def dump(self, prefix, max_wavs=9):
        self.calls += 1
        self.dumped.append((prefix, max_wavs))


def test_mode_without_value_keeps_the_socket_open(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
    client = TestClient(main.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "mode"})  # no value at all
        assert ws.receive_json()["type"] == "mode"
        assert ws.receive_json()["type"] == "free"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_mode_with_non_string_value_keeps_the_socket_open(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
    client = TestClient(main.app)
    with client.websocket_connect("/ws") as ws:
        for value in ([], {}, 7, True):
            ws.send_json({"type": "mode", "value": value})
            assert ws.receive_json()["type"] == "mode"
            assert ws.receive_json()["type"] == "free"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_barge_in_dump_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "AEC_DUMP_ON_BARGE_IN", False)
    fake = FakeAec()

    async def fake_cancel_turn(sess):
        return None

    async def fake_send_json(ws, obj):
        return None

    monkeypatch.setattr(main, "cancel_turn", fake_cancel_turn)
    monkeypatch.setattr(main, "send_json", fake_send_json)
    monkeypatch.setattr(main, "cancel_nudge", lambda s: None)
    sess = main.Session()
    sess.aec = fake
    import asyncio
    asyncio.run(main.cut_off(None, sess))
    assert fake.calls == 0


def test_dump_rotates_and_rejects_symlinks(tmp_path):
    from orchestrator import aec_stream

    aec = UpstreamAec.__new__(UpstreamAec)
    aec._dbg = {"mic": [], "ref": [], "out": []}

    for i in range(5):
        aec.dump(str(tmp_path / f"aec-barge-{i}"), max_wavs=6,
                 rotate_prefix="aec-barge-")
    names = sorted(p.name for p in tmp_path.glob("aec-barge-*.wav"))
    assert len(names) == 6  # bounded, not 15

    # A pre-created path (e.g. a symlink planted by another process) must
    # not be followed or overwritten.
    victim = tmp_path / "aec-barge-victim_mic.wav"
    victim.symlink_to(tmp_path / "elsewhere")
    aec.dump(str(tmp_path / "aec-barge-victim"), max_wavs=99,
             rotate_prefix="aec-barge-victim")
    assert victim.is_symlink()


def test_rotation_survives_a_dangling_symlink(tmp_path):
    from orchestrator import aec_stream

    aec = UpstreamAec.__new__(UpstreamAec)
    aec._dbg = {"mic": [], "ref": [], "out": []}
    dangling = tmp_path / "aec-barge-planted.wav"
    dangling.symlink_to(tmp_path / "nowhere")

    for i in range(10):
        aec.dump(str(tmp_path / f"aec-barge-{i}"), max_wavs=3,
                 rotate_prefix="aec-barge-")
    wavs = list(tmp_path.glob("aec-barge-*.wav"))
    assert len(wavs) <= 3
