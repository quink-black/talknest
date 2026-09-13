"""Unit tests for talknest orchestrator pure logic (no services needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import numpy as np
import pytest

from orchestrator.config import load
from orchestrator.main import (SENTENCE_MIN_CHARS, THINK_RE, barge_in_due,
                               clean_for_tts, extract_sentences)
from orchestrator.tts import time_stretch
from orchestrator.vad import FRAME_MS, Resampler, StreamVAD


@pytest.fixture(scope="module")
def config():
    return load()


class TestConfig:
    def test_required_sections(self, config):
        for key in ("server", "llm", "asr", "tts", "vad", "modes"):
            assert key in config
        assert config["llm"]["active"] in config["llm"]["profiles"]

    def test_modes_have_prompts(self, config):
        for name, m in config["modes"].items():
            assert len(m["system_prompt"]) > 50, name

    def test_default_mode_valid(self, config):
        default = config.get("default_mode", next(iter(config["modes"])))
        assert default in config["modes"]


class TestThinkStripping:
    def test_complete_block_removed(self):
        text = "<think>internal reasoning</think>Hello there."
        assert THINK_RE.sub("", text) == "Hello there."

    def test_unterminated_block_removed(self):
        text = "<think>still reasoning when cut off"
        assert THINK_RE.sub("", text) == ""

    def test_plain_text_untouched(self):
        assert THINK_RE.sub("", "Normal reply.") == "Normal reply."


class TestExtractSentences:
    def test_emits_only_full_sentences(self):
        text = "One two three four five six seven. Short tail"
        out, start = extract_sentences(text, 0)
        assert out == ["One two three four five six seven."]
        assert text[start:] == " Short tail"

    def test_short_first_sentence_held_back(self):
        # A short first sentence merges into the following chunk until the
        # accumulated text reaches SENTENCE_MIN_CHARS.
        text = "Hi. One two three four five six seven eight nine ten."
        out, start = extract_sentences(text, 0)
        assert len(out) == 1
        assert out[0].startswith("Hi.")
        assert text[start:].strip() == ""

    def test_short_final_sentence_held_back(self):
        # A short tail chunk is not emitted; the caller flushes it at
        # end of stream.
        text = "First sentence goes here plainly. OK."
        out, start = extract_sentences(text, 0)
        assert out == ["First sentence goes here plainly."]
        assert text[start:].strip() == "OK."

    def test_first_sentence_goes_out_on_its_own(self):
        # The point of a low threshold: synthesis starts on the first
        # sentence. Holding it back for the next one is what delayed the
        # first audio by most of the reply on short kid-mode answers.
        text = "兔子的英文是 rabbit。"
        out, start = extract_sentences(text, 0)
        assert out == ["兔子的英文是 rabbit。"]
        assert start == len(text)

    def test_offset_continues_across_calls(self):
        # One call emits every currently-complete chunk, then continues
        # from that offset on the next call.
        text = "First sentence goes here plainly. Second one follows right now."
        out1, start1 = extract_sentences(text, 0)
        assert out1 == ["First sentence goes here plainly.",
                        "Second one follows right now."]
        assert start1 == len(text)
        out2, start2 = extract_sentences(text, start1)
        assert out2 == []
        assert start2 == start1

    def test_streaming_growth(self):
        text = "One two three four five six seven. Fine."
        out1, start1 = extract_sentences(text, 0)
        assert out1 == ["One two three four five six seven."]
        out2, start2 = extract_sentences(text, start1)
        assert out2 == []
        # More text arrives; the short tail merges into the new chunk.
        text += " Eleven twelve thirteen fourteen fifteen."
        out3, start3 = extract_sentences(text, start2)
        assert out3 == ["Fine. Eleven twelve thirteen fourteen fifteen."]
        assert start3 == len(text)

    def test_min_chars_respected(self):
        assert SENTENCE_MIN_CHARS >= 8


class TestCleanForTts:
    def test_bold_markers_removed(self):
        assert clean_for_tts("洗澡的英文是 **take a shower**。") == \
            "洗澡的英文是 take a shower。"

    def test_single_asterisk_and_backticks(self):
        assert clean_for_tts("It is *very* `good`") == "It is very good"

    def test_plain_text_untouched(self):
        text = "兔子的英文是 rabbit。跟我读一遍：rabbit！"
        assert clean_for_tts(text) == text


class TestTimeStretch:
    SR = 24000

    def sine(self, freq=440.0, seconds=1.0):
        t = np.arange(int(self.SR * seconds)) / self.SR
        return (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)

    def dominant_freq(self, x):
        spec = np.abs(np.fft.rfft(x.astype(np.float64)))
        return float(np.argmax(spec) * self.SR / len(x))

    def test_identity_at_speed_one(self):
        x = self.sine()
        assert np.array_equal(time_stretch(x, 1.0, self.SR), x)

    def test_duration_ratio(self):
        x = self.sine()
        y = time_stretch(x, 0.8, self.SR)
        ratio = len(y) / len(x)
        assert 1.2 < ratio < 1.3   # 1/0.8 = 1.25

    def test_pitch_preserved(self):
        x = self.sine(freq=440.0, seconds=2.0)
        y = time_stretch(x, 0.8, self.SR)
        assert abs(self.dominant_freq(y) - 440) <= 5

    def test_speed_up_shrinks(self):
        x = self.sine()
        assert len(time_stretch(x, 1.2, self.SR)) < len(x)


class TestResampler:
    SR_IN, SR_OUT = 24000, 16000

    def signal(self, seconds=1.0):
        t = np.arange(int(self.SR_IN * seconds)) / self.SR_IN
        return (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)

    def test_length_ratio(self):
        x = self.signal()
        out = Resampler(self.SR_IN, self.SR_OUT).process(x)
        assert abs(len(out) / len(x) - self.SR_OUT / self.SR_IN) < 0.01

    def test_chunked_matches_whole(self):
        x = self.signal()
        whole = Resampler(self.SR_IN, self.SR_OUT).process(x)
        r = Resampler(self.SR_IN, self.SR_OUT)
        parts = [r.process(x[i:i + 1024]) for i in range(0, len(x), 1024)]
        assert np.array_equal(np.concatenate(parts), whole)

    def test_uneven_chunks_match_whole(self):
        # Chunk boundaries must not drop or repeat samples.
        x = self.signal()
        whole = Resampler(self.SR_IN, self.SR_OUT).process(x)
        r = Resampler(self.SR_IN, self.SR_OUT)
        parts, i = [], 0
        for n in (1, 3, 7, 128, 511, 1024):
            parts.append(r.process(x[i:i + n]))
            i += n
        parts.append(r.process(x[i:]))
        assert np.array_equal(np.concatenate(parts), whole)


class FakeVAD:
    """Stands in for SileroVAD: replays a scripted probability per frame."""

    def __init__(self, probs):
        self.probs = list(probs)
        self.threshold = 0.5
        self.resets = 0

    def reset(self):
        self.resets += 1

    def frame_prob(self, frame):
        return self.probs.pop(0) if self.probs else 0.0


def scripted(*runs):
    """(duration_ms, probability) runs -> per-frame probabilities."""
    probs = []
    for ms, prob in runs:
        probs += [prob] * int(round(ms / FRAME_MS))
    return probs


def stream_vad(probs, **over):
    params = dict(pre_roll_ms=300, silence_ms=700, start_ms=120,
                  min_speech_ms=300, max_speech_ms=20000)
    params.update(over)
    return StreamVAD(FakeVAD(probs), client_sr=24000, **params)


def pump(sv, seconds, chunk=1024):
    """Feed zeros at 24 kHz for `seconds`, as the browser would."""
    events, n = [], int(seconds * 24000)
    for i in range(0, n, chunk):
        events += sv.feed(np.zeros(min(chunk, n - i), dtype=np.int16))
    return events


class TestStreamVAD:
    def test_one_turn_with_lead_in_and_tail(self):
        sv = stream_vad(scripted((500, 0.0), (1000, 0.9), (2000, 0.0)))
        events = pump(sv, 3.5)
        assert [e.kind for e in events] == ["speech_start", "utterance"]
        dur_ms = len(events[1].pcm24) / 24
        # 1 s of speech, 0.3 s of lead-in, 0.7 s of trailing silence.
        assert 1750 <= dur_ms <= 2000
        assert events[1].reason == "silence"

    def test_short_burst_is_dropped(self):
        sv = stream_vad(scripted((200, 0.9), (2000, 0.0)))
        assert [e.kind for e in pump(sv, 2.5)] == ["speech_start"]

    def test_max_speech_closes_the_turn(self):
        sv = stream_vad(scripted((5000, 0.9)), max_speech_ms=1000)
        events = pump(sv, 2.0)
        assert [e.kind for e in events[:2]] == ["speech_start", "utterance"]
        assert events[1].reason == "max_speech"

    def test_silence_never_opens_a_turn(self):
        sv = stream_vad(scripted((3000, 0.0)))
        assert pump(sv, 3.0) == []

    def test_higher_threshold_rejects_weak_speech(self):
        # Used while the assistant plays: echo must not open a turn.
        sv = stream_vad(scripted((1000, 0.6), (1000, 0.0)))
        assert pump(sv, 2.0) != []
        sv = stream_vad(scripted((1000, 0.6), (1000, 0.0)))
        events = []
        for _ in range(int(2.0 * 24000 / 1024)):
            events += sv.feed(np.zeros(1024, dtype=np.int16), 0.75)
        assert events == []

    def test_state_resets_between_turns(self):
        sv = stream_vad(scripted((500, 0.0), (1000, 0.9), (1500, 0.0),
                                 (1000, 0.9), (1500, 0.0)))
        resets = sv.vad.resets
        assert [e.kind for e in pump(sv, 5.0)] == ["speech_start", "utterance"] * 2
        assert sv.active is False
        assert sv.speech_ms == 0.0
        assert sv.vad.resets == resets + 2  # Silero state restarts per turn


class TestBargeInDue:
    # barge_in_due(speaking, busy, active, barged, speech_ms, played_ms)
    def test_needs_something_in_flight(self):
        # Nothing playing and nothing pending: there is no turn to cut off.
        assert not barge_in_due(False, False, True, False, 2000, 2000)

    def test_needs_active(self):
        assert not barge_in_due(True, True, False, False, 2000, 2000)

    def test_needs_long_speech_while_playing(self):
        assert not barge_in_due(True, True, True, False, 100, 2000)

    def test_needs_playback_past_the_lead_in(self):
        assert not barge_in_due(True, True, True, False, 2000, 100)

    def test_fires_once_conditions_hold(self):
        assert barge_in_due(True, True, True, False, 2000, 2000)

    def test_does_not_fire_twice(self):
        assert not barge_in_due(True, True, True, True, 2000, 2000)

    def test_pending_turn_ignores_impatience(self):
        # A turn that has produced no audio yet is silent, but a child
        # waiting for an answer says things like "喂?" -- losing the
        # reply to that is worse than making them wait it out.
        assert not barge_in_due(False, True, True, False, 300, 0)

    def test_pending_turn_yields_to_a_real_question(self):
        assert barge_in_due(False, True, True, False, 3000, 0)
