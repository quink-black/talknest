"""Speaker recognition: features, voiceprint book, enrollment endpoints."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import numpy as np
import pytest
from fastapi.testclient import TestClient

from orchestrator import main, speaker
from orchestrator.config import model_root

SR24 = 24000


class FakeEmbedder:
    """Maps a clip's loudness to a fixed vector, so a "voice" is a level."""

    def __init__(self, vectors):
        self.vectors = vectors

    def embed16(self, pcm16k):
        level = int(np.abs(pcm16k).mean() // 1000)
        vec = np.asarray(self.vectors[level], dtype=np.float32)
        return vec / np.linalg.norm(vec)


class FakeTrimmer:
    """Keeps everything: the clips in these tests have no silence."""

    def trim(self, pcm24, sr):
        # Same duration, at the model's 16 kHz rate.
        x = np.asarray(pcm24, dtype=np.float32)
        return x[: len(x) * 2 // 3]


def clip(level: int, seconds: float = 4.0) -> np.ndarray:
    """A constant-amplitude clip: its level picks the fake voice."""
    return np.full(int(seconds * SR24), level * 1000 + 500, dtype=np.int16)


# Child, dad, mom as level 1, 2, 3; level 4 sits between dad and mom.
VOICES = {
    1: [1, 0, 0, 0],
    2: [0, 1, 0, 0],
    3: [0, 0, 1, 0],
    4: [0, 1, 1, 0.01],
    5: [0.5, 0.5, 0.5, 0.5],
}
CFG = {"min_score": 0.6, "margin": 0.1, "min_seconds": 1.0,
       "enroll_min_seconds": 3.0, "max_enroll_seconds": 20.0}


def make_book(tmp_path):
    return speaker.SpeakerBook(tmp_path / "speakers.json",
                               FakeEmbedder(VOICES), FakeTrimmer(), CFG)


def enroll_all(book):
    book.enroll("child", clip(1), SR24)
    book.enroll("dad", clip(2), SR24)
    book.enroll("mom", clip(3), SR24)


class TestFbank:
    def test_shape_follows_kaldi_framing(self):
        pcm = np.random.default_rng(0).integers(-3000, 3000, 16000)
        feats = speaker.fbank(pcm)
        assert feats.shape == (1 + (16000 - 400) // 160, 80)

    def test_shorter_than_one_frame_is_empty(self):
        assert speaker.fbank(np.zeros(399)).shape == (0, 80)

    def test_features_are_mean_normalized(self):
        pcm = np.random.default_rng(1).integers(-3000, 3000, 8000)
        assert np.abs(speaker.fbank(pcm).mean(axis=0)).max() < 1e-4


_WAV_DIR = model_root() / "speaker" / "samples"
_MODEL = model_root() / "speaker" / speaker.DEFAULT_MODEL


@pytest.mark.skipif(not (_MODEL.exists() and (_WAV_DIR / "fangjun-sr-1.wav").exists()),
                    reason="speaker model and sample recordings not downloaded")
class TestRealModel:
    """The same person scores far above two different people.

    This is what proves the numpy features match the ones the model was
    trained on: a wrong filterbank still produces vectors, but they stop
    separating speakers.
    """

    @staticmethod
    def vec(embedder, name):
        import soundfile as sf
        wav, rate = sf.read(_WAV_DIR / f"{name}.wav", dtype="float32")
        assert rate == 16000
        return embedder.embed16(wav * 32768.0)

    def test_same_speaker_scores_above_different_speakers(self):
        emb = speaker.Embedder()
        fa1, fa2 = self.vec(emb, "fangjun-sr-1"), self.vec(emb, "fangjun-sr-2")
        le1, le2 = self.vec(emb, "leijun-sr-1"), self.vec(emb, "leijun-sr-2")
        same = [float(fa1 @ fa2), float(le1 @ le2)]
        diff = [float(a @ b) for a in (fa1, fa2) for b in (le1, le2)]
        assert min(same) > 0.6
        assert max(diff) < 0.45
        assert min(same) - max(diff) > 0.3


class TestSpeakerBook:
    def test_matching_waits_for_child_and_a_parent(self, tmp_path):
        book = make_book(tmp_path)
        assert not book.enabled()
        book.enroll("child", clip(1), SR24)
        assert not book.enabled()
        book.enroll("dad", clip(2), SR24)
        assert book.enabled()

    def test_a_voice_is_named_by_its_closest_enrolled_role(self, tmp_path):
        book = make_book(tmp_path)
        enroll_all(book)
        assert book.identify(clip(1), SR24).role == "child"
        assert book.identify(clip(2), SR24).role == "dad"
        assert book.identify(clip(3), SR24).role == "mom"

    def test_a_voice_between_two_roles_is_unknown(self, tmp_path):
        book = make_book(tmp_path)
        enroll_all(book)
        assert book.identify(clip(4), SR24).role is None

    def test_a_voice_close_to_nobody_is_unknown(self, tmp_path):
        book = make_book(tmp_path)
        enroll_all(book)
        assert book.identify(clip(5), SR24).role is None

    def test_too_little_speech_is_not_matched(self, tmp_path):
        book = make_book(tmp_path)
        enroll_all(book)
        assert book.identify(clip(2, seconds=0.8), SR24).role is None

    def test_nothing_is_matched_before_enabling(self, tmp_path):
        book = make_book(tmp_path)
        book.enroll("child", clip(1), SR24)
        assert book.identify(clip(1), SR24).role is None
        assert book.identify(None, SR24).role is None

    def test_enrollment_survives_a_reload(self, tmp_path):
        enroll_all(make_book(tmp_path))
        again = make_book(tmp_path)
        assert again.enabled()
        assert again.identify(clip(3), SR24).role == "mom"

    def test_only_embeddings_are_stored(self, tmp_path):
        book = make_book(tmp_path)
        book.enroll("child", clip(1), SR24)
        saved = json.loads((tmp_path / "speakers.json").read_text())
        assert set(saved["child"][0]) == {"vec", "seconds", "ts"}

    def test_clearing_a_role_disables_matching(self, tmp_path):
        book = make_book(tmp_path)
        enroll_all(book)
        book.clear("child")
        assert not book.enabled()
        assert book.status()["roles"]["child"] == {"samples": 0, "seconds": 0.0}

    def test_short_recordings_are_refused_with_a_reason(self, tmp_path):
        book = make_book(tmp_path)
        with pytest.raises(ValueError, match="至少需要"):
            book.enroll("child", clip(1, seconds=2.0), SR24)

    def test_long_recordings_are_refused(self, tmp_path):
        book = make_book(tmp_path)
        with pytest.raises(ValueError, match="太长"):
            book.enroll("child", clip(1, seconds=21.0), SR24)

    def test_unknown_roles_are_refused(self, tmp_path):
        with pytest.raises(ValueError):
            make_book(tmp_path).enroll("cat", clip(1), SR24)

    def test_an_unreadable_file_starts_empty(self, tmp_path):
        (tmp_path / "speakers.json").write_text("{not json")
        assert make_book(tmp_path).status()["roles"]["child"]["samples"] == 0

    def test_without_a_model_nothing_can_be_enrolled(self, tmp_path):
        book = speaker.SpeakerBook(tmp_path / "s.json", None, None, CFG)
        assert not book.ready
        with pytest.raises(ValueError, match="未加载"):
            book.enroll("child", clip(1), SR24)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main.speaker, "shared_models",
                        lambda cfg: (FakeEmbedder(VOICES), FakeTrimmer()))
    monkeypatch.setattr(main, "SPEAKER_CFG", CFG)
    return TestClient(main.app)


class TestEnrollmentEndpoints:
    def test_status_starts_empty(self, client):
        body = client.get("/learners/tester/speakers").json()
        assert body["ready"] and not body["enabled"]
        assert set(body["roles"]) == {"child", "dad", "mom"}

    def test_enrolling_returns_the_new_status(self, client):
        r = client.post("/learners/tester/speakers/child",
                        content=clip(1).tobytes())
        assert r.status_code == 200
        body = r.json()
        assert body["seconds"] == pytest.approx(4.0, abs=0.1)
        assert body["roles"]["child"]["samples"] == 1

    def test_the_voiceprint_is_kept_per_learner(self, client):
        client.post("/learners/tester/speakers/child", content=clip(1).tobytes())
        other = client.get("/learners/someone-else/speakers").json()
        assert other["roles"]["child"]["samples"] == 0

    def test_unknown_role_is_404(self, client):
        r = client.post("/learners/tester/speakers/cat", content=clip(1).tobytes())
        assert r.status_code == 404
        assert client.delete("/learners/tester/speakers/cat").status_code == 404

    def test_a_clip_with_half_a_sample_is_refused(self, client):
        r = client.post("/learners/tester/speakers/child",
                        content=clip(1).tobytes() + b"\x00")
        assert r.status_code == 400

    def test_an_oversized_upload_is_refused_before_decoding(self, client):
        r = client.post("/learners/tester/speakers/child",
                        content=clip(1, seconds=25.0).tobytes())
        assert r.status_code == 400

    def test_a_short_clip_explains_itself(self, client):
        r = client.post("/learners/tester/speakers/child",
                        content=clip(1, seconds=1.0).tobytes())
        assert r.status_code == 400
        assert "至少需要" in r.json()["error"]

    def test_deleting_clears_the_role(self, client):
        client.post("/learners/tester/speakers/child", content=clip(1).tobytes())
        body = client.delete("/learners/tester/speakers/child").json()
        assert body["roles"]["child"]["samples"] == 0

    def test_open_conversations_see_a_new_enrollment(self, client, tmp_path):
        sess = main.Session()
        sess.open_learner("tester")
        main.session_by_ws[object()] = sess
        try:
            client.post("/learners/tester/speakers/child",
                        content=clip(1).tobytes())
            client.post("/learners/tester/speakers/dad",
                        content=clip(2).tobytes())
            assert sess.speakers.enabled()
        finally:
            main.session_by_ws.clear()
            sess.close()

