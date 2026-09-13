"""Unit tests for the teaching state (no services needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import pytest

from orchestrator.config import load
from orchestrator.teaching import curriculum, state_block
from orchestrator.teaching.curriculum import CurriculumError, Unit
from orchestrator.teaching.store import Store


def _write(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


class TestCurriculum:
    def test_seed_loads_in_dependency_order(self):
        units = curriculum.load("kid")
        ids = [u.id for u in units]
        assert ids[0] == "greetings"
        assert ids.index("colors") < ids.index("animals")
        assert all(u.words for u in units)

    def test_missing_file_is_empty(self, tmp_path):
        assert curriculum.load("nope", directory=tmp_path) == []

    def test_mode_mismatch(self, tmp_path):
        _write(tmp_path, "kid.yaml", "mode: adult\nunits: []\n")
        with pytest.raises(CurriculumError, match="mode"):
            curriculum.load("kid", directory=tmp_path)

    def test_unknown_dependency(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [hi]\n    after: [ghost]\n")
        with pytest.raises(CurriculumError, match="unknown unit"):
            curriculum.load("kid", directory=tmp_path)

    def test_duplicate_ids(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [hi]\n  - id: a\n    words: [bye]\n")
        with pytest.raises(CurriculumError, match="duplicate"):
            curriculum.load("kid", directory=tmp_path)

    def test_cycle(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n"
               "  - id: a\n    words: [hi]\n    after: [b]\n"
               "  - id: b\n    words: [bye]\n    after: [a]\n")
        with pytest.raises(CurriculumError, match="cycle"):
            curriculum.load("kid", directory=tmp_path)

    def test_words_must_be_in_lexicon(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [hi, hallow]\n")
        with pytest.raises(CurriculumError, match="lexicon"):
            curriculum.load("kid", lexicon={"hi"}, directory=tmp_path)

    def test_lexicon_match_passes(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [hi]\n")
        units = curriculum.load("kid", lexicon={"hi"}, directory=tmp_path)
        assert [u.id for u in units] == ["a"]


@pytest.fixture
def store(tmp_path):
    s = Store("tester", tmp_path)
    s.sync_units([
        Unit(id="colors", title="颜色", goal="", words=("red", "blue"),
             patterns=(), after=(), sessions=1),
        Unit(id="animals", title="动物", goal="", words=("cat",),
             patterns=(), after=("colors",), sessions=1),
    ])
    yield s
    s.close()


class TestStore:
    def test_sync_seeds_untaught_words(self, store):
        assert store.boxes() == {"red": 0, "blue": 0, "cat": 0}

    def test_first_touch_introduces(self, store):
        store.touch(["red"], 1, practiced=False)
        assert store.boxes()["red"] == 1

    def test_same_session_does_not_promote(self, store):
        store.touch(["red"], 1, False)
        store.touch(["red"], 1, False)
        assert store.boxes()["red"] == 1

    def test_new_session_promotes(self, store):
        store.touch(["red"], 1, False)
        store.touch(["red"], 2, False)
        assert store.boxes()["red"] == 2

    def test_repeated_production_accelerates(self, store):
        store.touch(["red"], 1, True)
        store.touch(["red"], 2, True)
        assert store.boxes()["red"] == 3

    def test_unknown_word_is_ignored(self, store):
        store.touch(["ghost"], 1, False)
        assert "ghost" not in store.boxes()

    def test_parent_pinned_box_survives_touches(self, store):
        store.set_box("red", 6)
        store.touch(["red"], 1, True)
        store.touch(["red"], 2, True)
        assert store.boxes()["red"] == 6
        assert store.get("red")["practices"] == 2

    def test_release_hands_word_back_to_automatic(self, store):
        store.set_box("red", 6)
        store.release("red")
        assert store.get("red")["manual"] == 0

    def test_demote_stale(self, store):
        store.set_box("red", 3, manual=False)
        store.db.execute("UPDATE words SET last_seen_at = '2020-01-01T00:00:00+00:00'")
        assert store.demote_stale() == 1
        assert store.boxes()["red"] == 2

    def test_demote_skips_pinned_words(self, store):
        store.set_box("red", 3)
        store.db.execute("UPDATE words SET last_seen_at = '2020-01-01T00:00:00+00:00'")
        assert store.demote_stale() == 0

    def test_settings_roundtrip(self, store):
        assert store.get_setting("current_unit") is None
        store.set_setting("current_unit", "animals")
        assert store.get_setting("current_unit") == "animals"
        store.set_setting("current_unit", "food")
        assert store.get_setting("current_unit") == "food"

    def test_new_and_known_queries(self, store):
        assert store.new_in_unit("colors", 2) == ["red", "blue"]
        store.touch(["red"], 1, False)
        assert store.new_in_unit("colors", 2) == ["blue"]
        assert store.known_in_units(["colors"], 10) == ["red"]
        assert store.known_in_units([], 10) == []

    def test_session_log(self, store):
        sid = store.start_session("kid")
        store.log_turn(sid, "兔子怎么说", "兔子的英文是 rabbit。", ["rabbit"])
        assert store.db.execute(
            "SELECT turns FROM sessions WHERE id = ?", (sid,)).fetchone()["turns"] == 1
        turn = store.db.execute(
            "SELECT items FROM turns WHERE session = ?", (sid,)).fetchone()
        assert turn["items"] == '["rabbit"]'


class TestStateBlock:
    def test_empty_plan_renders_nothing(self):
        assert state_block.render(state_block.Plan()) == ""

    def test_sections_in_order(self):
        plan = state_block.Plan(unit="colors", new=["red"], review=["cat"],
                                known=["hi"])
        assert state_block.render(plan) == "【复习: cat ｜ 新授(colors): red ｜ 已会: hi】"

    def test_caps_hold(self):
        plan = state_block.Plan(review=[f"r{i}" for i in range(7)],
                                new=[f"n{i}" for i in range(5)])
        out = state_block.render(plan, state_block.Caps(new_items=2, review_items=5))
        assert "r4" in out and "r5" not in out
        assert "n1" in out and "n2" not in out

    def test_portrait_and_digest_on_one_line(self):
        plan = state_block.Plan(portrait=["喜欢恐龙", "怕黑"], digest=["聊了家里的猫"])
        assert state_block.render(plan) == "【画像: 喜欢恐龙；怕黑 ｜ 上次: 聊了家里的猫】"

    def test_long_lines_are_clipped(self):
        plan = state_block.Plan(portrait=["x" * 80])
        assert state_block.render(plan, state_block.Caps(line_chars=10)) == \
            "【画像: " + "x" * 9 + "…】"


class TestTeachingConfig:
    def test_disabled_until_the_pipeline_is_wired(self):
        assert load()["teaching"]["enabled"] is False

    def test_block_caps_match_the_renderer(self):
        caps = state_block.Caps(**load()["teaching"]["block"])
        assert (caps.new_items, caps.review_items, caps.known_items) == (2, 5, 20)
