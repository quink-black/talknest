"""Unit tests for the headless CLI client (no sound card needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import asyncio
import json
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

from talknest_cli import CLIENT_SR, FRAME_SAMPLES  # noqa: E402
from talknest_cli.audio import AudioEngine  # noqa: E402
from talknest_cli.session import Session  # noqa: E402


class FakeAec:
    """Records what the dsp thread feeds into a canceller."""

    name = "fake"
    proc_sr = 16000

    def __init__(self):
        self.ref = 0

    def reset(self, delay_samples=0):
        pass

    def close(self):
        pass

    def push_reference(self, ref):
        self.ref += len(ref)

    def process(self, mic):
        return mic


class FakeEngine:
    def __init__(self):
        self.played = []

    def play(self, pcm, sample_rate):
        self.played.append((pcm, sample_rate))


class FakeWs:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        async def gen():
            for m in self._messages:
                yield m
        return gen()

    async def send(self, msg):
        self.sent.append(msg)


def write_silence(path, seconds=0.5, sr=CLIENT_SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.zeros(int(seconds * sr), dtype="<i2").tobytes())


class TestReceiverRobustness:
    def _run_receiver(self, session, messages):
        session.ws = FakeWs(messages)
        asyncio.run(session._receiver())

    def test_odd_length_audio_frame_is_dropped(self):
        engine = FakeEngine()
        session = Session("ws://x", engine, mode="kid")
        good = np.zeros(8, dtype="<i2").tobytes()
        self._run_receiver(session, [b"\x01\x02\x03", good])
        assert len(engine.played) == 1, "the well-formed frame must survive"

    def test_non_json_text_frame_is_dropped(self):
        engine = FakeEngine()
        session = Session("ws://x", engine, mode="kid")
        later = json.dumps({"type": "state", "value": "listening"})
        self._run_receiver(session, ["not json", later])
        assert engine.played == []
        assert session._state == "listening"


class TestDspLoop:
    def test_reference_drains_without_a_microphone(self, tmp_path):
        """File microphone: nothing feeds _mic_q, so the reference queue
        must still be emptied or the playback callback drops on every call."""
        aec = FakeAec()
        engine = AudioEngine(aec, input_file=str(tmp_path / "in.wav"),
                             output_file=str(tmp_path / "out.wav"))
        engine.start()
        try:
            for _ in range(4):
                engine._ref_q.put_nowait(np.zeros(1024, dtype=np.float32))
            deadline = time.time() + 3.0
            while time.time() < deadline and engine._ref_q.qsize():
                time.sleep(0.05)
            assert engine._ref_q.qsize() == 0
            assert aec.ref > 0, "the reference must reach the canceller"
        finally:
            engine.stop()

    def test_upstream_is_framed_to_frame_samples(self):
        engine = AudioEngine(FakeAec())
        engine._push_upstream(np.zeros(FRAME_SAMPLES * 2 + 10, dtype=np.float32))
        assert engine._up.qsize() == 2
        assert len(engine.upstream_get(0.1)) == FRAME_SAMPLES


class TestStartCleanup:
    def test_failed_start_closes_opened_streams(self, monkeypatch):
        """The second stream failing must not leave the first one open."""
        opened = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.closed = False
                opened.append(self)

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                self.closed = True

        class FakeSd:
            @staticmethod
            def check_input_settings(**kwargs):
                pass

            @staticmethod
            def check_output_settings(**kwargs):
                pass

            @staticmethod
            def InputStream(**kwargs):
                return FakeStream()

            @staticmethod
            def OutputStream(**kwargs):
                raise RuntimeError("device busy")

        monkeypatch.setattr("talknest_cli.audio.sd", FakeSd)
        engine = AudioEngine(FakeAec())
        with pytest.raises(RuntimeError):
            engine.start()
        assert opened and all(s.closed for s in opened)
        assert engine._streams == []
        assert engine._running is False


class TestFileMicrophone:
    def test_unreadable_input_file_does_not_raise(self, tmp_path, capsys):
        engine = AudioEngine(FakeAec(),
                             input_file=str(tmp_path / "missing.wav"),
                             output_file=str(tmp_path / "out.wav"))
        engine._running = True
        engine._read_file()
        assert "microphone stays silent" in capsys.readouterr().out

    def test_file_microphone_feeds_upstream_at_real_time(self, tmp_path):
        mic = tmp_path / "in.wav"
        write_silence(mic, seconds=0.5)
        engine = AudioEngine(FakeAec(), input_file=str(mic),
                             output_file=str(tmp_path / "out.wav"))
        engine.start()
        try:
            deadline = time.time() + 3.0
            got = 0
            while time.time() < deadline and got < 10:
                if engine.upstream_get(0.1) is not None:
                    got += 1
            assert got >= 10
        finally:
            engine.stop()
