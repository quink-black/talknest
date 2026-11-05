"""The two-channel start must be refused unless the server AEC is real.

The design note (docs/design/server-side-aec.md) requires an explicit
refusal -- never a silent half-duplex degradation -- when AEC is disabled
or its models fail to load. These tests drive the /ws endpoint through
the ASGI stack, no real models or services needed.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from orchestrator import main  # noqa: E402


@pytest.fixture
def client():
    return TestClient(main.app)


def _start(channels):
    return {"type": "start", "channels": channels}


def test_disabled_aec_refuses_two_channels(client, monkeypatch):
    monkeypatch.setattr(main, "AEC_ENABLED", False)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_start(2))
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert "server AEC unavailable" in reply["message"]
        assert "disabled" in reply["message"]
        # The stream is refused, not degraded: the server closes.
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_json()


def test_broken_models_refuse_two_channels(client, monkeypatch):
    monkeypatch.setattr(main, "AEC_ENABLED", True)
    monkeypatch.setattr(main, "AEC_MODEL_PREFIX",
                        str(Path(__file__) / "no-such-model"))
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_start(2))
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert "server AEC unavailable" in reply["message"]


def test_unsupported_channel_count_is_refused(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_start(3))
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert "unsupported channels" in reply["message"]


def test_single_channel_starts_without_aec(client, monkeypatch):
    monkeypatch.setattr(main, "AEC_ENABLED", False)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_start(1))
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "stop"})
        # The stop path tears the session down without an AEC present.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
