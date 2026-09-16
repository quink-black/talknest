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
from talknest_cli.audio import AudioEngine, SpscRing  # noqa: E402
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


class TestSpscRing:
    def test_round_trip_keeps_order(self):
        ring = SpscRing(8)
        x = np.arange(8, dtype=np.float32)
        assert ring.write(x) == 8
        assert ring.available() == 8
        assert np.array_equal(ring.read(8), x)

    def test_wrap_around(self):
        """Many small writes and reads must not lose or reorder samples."""
        ring = SpscRing(8)
        sent, got = [], []
        for i in range(40):
            x = np.full(3, i, dtype=np.float32)
            # The ring is small on purpose: the surplus is dropped, so only
            # what write() took may be expected back.
            sent.extend(x.tolist()[: ring.write(x)])
            got.extend(ring.read(2).tolist())
        got.extend(ring.read(ring.available()).tolist())
        assert got == sent[: len(got)], "order must survive wrapping"

    def test_read_pads_with_silence(self):
        ring = SpscRing(8)
        ring.write(np.ones(3, dtype=np.float32))
        out = np.full(6, -1.0, dtype=np.float32)
        assert ring.read_into(out) == 3
        assert np.array_equal(out, [1, 1, 1, 0, 0, 0])

    def test_full_ring_drops_the_new_samples(self):
        ring = SpscRing(4)
        ring.write(np.ones(4, dtype=np.float32))
        assert ring.write(np.full(2, 9.0, dtype=np.float32)) == 0
        assert np.array_equal(ring.read(4), np.ones(4))

    def test_full_ring_can_drop_the_oldest_instead(self):
        ring = SpscRing(4, drop_oldest=True)
        ring.write(np.zeros(4, dtype=np.float32))
        ring.write(np.full(2, 7.0, dtype=np.float32))
        assert np.array_equal(ring.read(4), [0, 0, 7, 7])

    def test_write_larger_than_the_ring_keeps_the_tail(self):
        ring = SpscRing(4, drop_oldest=True)
        assert ring.write(np.arange(10, dtype=np.float32)) == 4
        assert np.array_equal(ring.read(4), [6, 7, 8, 9])

    def test_write_after_discard_reuses_the_whole_ring(self):
        ring = SpscRing(8)
        ring.write(np.ones(8, dtype=np.float32))
        ring.discard()
        # The discarded samples are already gone even though the reader has
        # not run, so their space must count as free.
        assert ring.write(np.full(8, 2.0, dtype=np.float32)) == 8
        assert np.array_equal(ring.read(8), np.full(8, 2.0))

    def test_concurrent_write_and_read_keep_the_stream_intact(self):
        """A ramp pushed from one thread must come out whole on the other."""
        ring = SpscRing(4096)
        total = 200_000
        got: list[float] = []
        done = threading.Event()

        def produce():
            sent = 0
            chunk = np.zeros(64, dtype=np.float32)
            while sent < total:
                n = min(64, total - sent, ring.capacity - ring.available())
                if n:
                    chunk[:n] = np.arange(sent, sent + n, dtype=np.float32)
                    ring.write(chunk[:n])
                    sent += n
            done.set()

        def consume():
            out = np.zeros(64, dtype=np.float32)
            while not (done.is_set() and ring.available() == 0):
                n = ring.read_into(out)
                if n:
                    got.extend(out[:n].tolist())

        writer = threading.Thread(target=produce)
        reader = threading.Thread(target=consume)
        writer.start()
        reader.start()
        writer.join(timeout=30)
        reader.join(timeout=30)
        assert got == list(range(total))

    def test_discard_from_the_producer_side(self):
        ring = SpscRing(8)
        ring.write(np.ones(5, dtype=np.float32))
        ring.discard()
        assert ring.available() == 0
        ring.write(np.full(2, 3.0, dtype=np.float32))
        assert np.array_equal(ring.read(2), [3, 3])


class TestPlaybackCallback:
    def test_out_cb_fills_the_device_buffer_and_the_reference(self):
        """The callback must not allocate a copy on its way to the
        reference: it reads into the device buffer and pushes that."""
        engine = AudioEngine(FakeAec())
        engine._play_ring = SpscRing(1024)
        engine._ref_ring = SpscRing(1024, drop_oldest=True)
        engine._play_ring.write(np.array([0.1, 0.2, 0.3], dtype=np.float32))
        outdata = np.full((6, 1), -1.0, dtype=np.float32)
        engine._out_cb(outdata, 6, None, None)
        assert np.allclose(outdata[:, 0], [0.1, 0.2, 0.3, 0, 0, 0])
        assert np.allclose(engine._ref_ring.read(6), [0.1, 0.2, 0.3, 0, 0, 0])

    def test_discarded_playback_is_not_played(self):
        engine = AudioEngine(FakeAec())
        engine._play_ring = SpscRing(1024)
        engine._play_ring.write(np.ones(100, dtype=np.float32))
        engine.clear_playback()
        assert engine._play_ring.available() == 0
        out = np.full((8, 1), -1.0, dtype=np.float32)
        engine._out_cb(out, 8, None, None)
        assert np.array_equal(out[:, 0], np.zeros(8))


class TestDspLoop:
    def test_reference_drains_without_a_microphone(self, tmp_path):
        """File microphone: nothing feeds the capture ring, so the reference
        ring must still be emptied or the playback callback drops."""
        aec = FakeAec()
        engine = AudioEngine(aec, input_file=str(tmp_path / "in.wav"),
                             output_file=str(tmp_path / "out.wav"))
        engine.start()
        try:
            engine._ref_ring.write(np.zeros(4096, dtype=np.float32))
            deadline = time.time() + 3.0
            while time.time() < deadline and engine._ref_ring.available():
                time.sleep(0.05)
            assert engine._ref_ring.available() == 0
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
            blocksize = 512

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
