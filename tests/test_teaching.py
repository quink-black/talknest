"""Unit tests for the teaching state (no services needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import asyncio

import pytest
from fastapi.testclient import TestClient

from orchestrator import main
from orchestrator.config import load
from orchestrator.teaching import (curriculum, digest, match, profile,
                                   scheduler, state_block)
from orchestrator.teaching.curriculum import CurriculumError, Unit
from orchestrator.teaching.store import Store


class FakeLLM:
    """Stands in for the tutor model, replying with a fixed string."""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts = []

    async def stream_chat(self, messages, cancel=None, max_tokens=None):
        self.prompts.append(messages)
        yield self.reply

UNITS = [
    Unit(id="colors", title="颜色", goal="", words=("red", "blue"),
         patterns=(), after=(), sessions=1),
    Unit(id="animals", title="动物", goal="", words=("cat",),
         patterns=(), after=("colors",), sessions=1),
]


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

    def test_yaml_booleans_are_rejected(self, tmp_path):
        # A bare on/yes/no is a boolean to YAML and would otherwise reach the
        # vocabulary as "true".
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [in, on, up]\n")
        with pytest.raises(CurriculumError, match="quote it"):
            curriculum.load("kid", directory=tmp_path)

    def test_untracked_words_parse(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [i, cat]\n"
               "    untracked: [i]\n")
        assert curriculum.load("kid", directory=tmp_path)[0].untracked == ("i",)

    def test_load_lexicon_is_none_without_a_file(self, tmp_path):
        assert curriculum.load_lexicon(tmp_path / "lexicon.jsonl") is None

    def test_load_lexicon_reads_lemmas(self, tmp_path):
        path = tmp_path / "lexicon.jsonl"
        path.write_text('{"lemma": "cat"}\n\n{"lemma": "dog"}\n',
                        encoding="utf-8")
        assert curriculum.load_lexicon(path) == {"cat", "dog"}

    def test_curricula_validate_against_the_imported_lexicon(self):
        lexicon = curriculum.load_lexicon(
            main.data_root() / "curriculum" / "lexicon.jsonl")
        if lexicon is None:
            pytest.skip("no word list imported: run scripts/import_curriculum.py")
        for mode in ("kid", "kid_upper"):
            assert curriculum.load(mode, lexicon), mode

    def test_lexicon_match_passes(self, tmp_path):
        _write(tmp_path, "kid.yaml",
               "mode: kid\nunits:\n  - id: a\n    words: [hi]\n")
        units = curriculum.load("kid", lexicon={"hi"}, directory=tmp_path)
        assert [u.id for u in units] == ["a"]


@pytest.fixture
def store(tmp_path):
    s = Store("tester", tmp_path)
    s.sync_units(UNITS)
    yield s
    s.close()


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
    s = main.Session()
    s.open_learner("tester")
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

    def test_review_skips_untaught_and_mastered(self, store):
        assert store.review_due(10) == []          # nothing introduced yet
        store.touch(["red"], 1, False)             # box 1
        assert store.review_due(10) == ["red"]
        store.set_box("red", 4, manual=False)      # mastered
        assert store.review_due(10) == []

    def test_review_takes_the_weakest_first(self, store):
        store.set_box("red", 3, manual=False)
        store.set_box("blue", 1, manual=False)
        assert store.review_due(10) == ["blue", "red"]

    def test_review_is_capped(self, store):
        store.touch(["red", "blue"], 1, False)
        assert len(store.review_due(1)) == 1

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


class TestProfile:
    def test_missing_file_is_an_empty_portrait(self, tmp_path):
        assert profile.load(tmp_path / "profile.md") == profile.Profile()

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "profile.md"
        profile.save(path, profile.Profile(notes=["喜欢恐龙"],
                                           recent=["聊了家里的猫"]))
        assert profile.load(path) == profile.Profile(notes=["喜欢恐龙"],
                                                     recent=["聊了家里的猫"])

    def test_hand_written_file_parses(self, tmp_path):
        path = tmp_path / "profile.md"
        path.write_text("# 学习者画像\n\n## 画像\n\n- 喜欢恐龙\n- 怕黑\n"
                        "\n## 最近\n\n- 聊了猫\n", encoding="utf-8")
        loaded = profile.load(path)
        assert loaded.notes == ["喜欢恐龙", "怕黑"]
        assert loaded.recent == ["聊了猫"]

    def test_path_for(self, tmp_path):
        assert profile.path_for(tmp_path, "xiaoming") == \
            tmp_path / "learners" / "xiaoming" / "profile.md"


class TestDigest:
    def test_tagged_lines_are_sorted(self):
        notes, recent = digest.parse(
            "兴趣: 喜欢恐龙\n错误: three 和 tree 分不清\n本次: 聊了家里的猫")
        assert notes == ["喜欢恐龙", "three 和 tree 分不清"]
        assert recent == ["聊了家里的猫"]

    def test_untagged_line_is_kept_as_a_note(self):
        assert digest.parse("喜欢画画") == (["喜欢画画"], [])

    def test_nothing_to_record_is_dropped(self):
        assert digest.parse("本次: 无") == ([], [])

    def test_markdown_bullets_are_ignored(self):
        assert digest.parse("- 兴趣: 喜欢恐龙") == (["喜欢恐龙"], [])

    def test_write_appends_to_the_portrait(self, tmp_path):
        store = Store("tester", tmp_path)
        sid = store.start_session("kid")
        store.log_turn(sid, "我喜欢猫", "Cat 就是猫。", ["cat"])
        path = profile.path_for(tmp_path, "tester")
        written = asyncio.run(digest.write(
            FakeLLM("兴趣: 喜欢猫\n本次: 聊了猫"), store, path, sid))
        assert written is True
        saved = profile.load(path)
        assert "喜欢猫" in saved.notes
        assert saved.recent == ["聊了猫"]
        store.close()

    def test_write_without_turns_does_nothing(self, tmp_path):
        store = Store("tester", tmp_path)
        sid = store.start_session("kid")
        assert asyncio.run(digest.write(
            FakeLLM("兴趣: 喜欢猫"), store,
            profile.path_for(tmp_path, "tester"), sid)) is False
        store.close()

    def test_portrait_reaches_the_plan(self, store):
        portrait = profile.Profile(notes=["喜欢恐龙", "怕黑"], recent=["聊了猫"])
        plan = scheduler.plan(store, UNITS,
                              state_block.Caps(portrait_lines=1), portrait)
        assert plan.portrait == ["怕黑"]      # the newest line
        assert plan.digest == ["聊了猫"]


class TestMatch:
    def test_finds_curriculum_words(self):
        assert match.find("The apple is red", ["red", "apple", "cat"]) == \
            ["red", "apple"]

    def test_plurals_match(self):
        assert match.find("I like apples", ["apple"]) == ["apple"]
        assert match.find("two boxes", ["box"]) == ["box"]

    def test_chinese_alone_matches_nothing(self):
        assert match.find("兔子怎么说", ["rabbit"]) == []

    def test_order_follows_the_curriculum(self):
        assert match.find("cat and apple", ["apple", "cat"]) == ["apple", "cat"]

    def test_words_that_end_in_s_are_left_alone(self):
        assert match.find("this is a bus", ["this", "bus"]) == ["this", "bus"]

    def test_empty_inputs(self):
        assert match.find("", ["cat"]) == []
        assert match.find("cat", []) == []


class TestScheduler:
    def test_without_a_pick_the_first_unfinished_unit_wins(self, store):
        assert scheduler.current_unit(store, UNITS).id == "colors"
        store.touch(["red", "blue"], 1, False)
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_parent_pick_wins(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "animals")
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_parent_pick_survives_a_finished_unit(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "animals")
        store.touch(["cat"], 1, False)
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_unknown_pick_falls_back_to_automatic(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "ghost")
        assert scheduler.current_unit(store, UNITS).id == "colors"

    def test_plan_respects_the_caps(self, store):
        plan = scheduler.plan(store, UNITS,
                              state_block.Caps(new_items=1, known_items=5))
        assert (plan.unit, plan.new) == ("colors", ["red"])

    def test_plan_carries_review_and_keeps_it_out_of_known(self, store):
        store.touch(["red", "blue"], 1, False)
        plan = scheduler.plan(store, UNITS, state_block.Caps(new_items=1))
        assert plan.review == ["red", "blue"]
        assert plan.new == ["cat"]
        assert "red" not in plan.known and "blue" not in plan.known

    def test_window_is_the_unit_and_two_before_it(self):
        assert scheduler.window(UNITS, "animals") == ["colors", "animals"]


class TestTurnPrompt:
    def test_plan_block_never_enters_history(self, session):
        messages = main.build_messages(session, "你好")
        assert "【" in messages[-1]["content"]
        assert messages[-1]["content"].endswith("你好")

        session.history.append({"role": "user", "content": "你好"})
        again = main.build_messages(session, "兔子怎么说")
        assert again[-2]["content"] == "你好"
        assert "【" not in again[-2]["content"]

    def test_history_window_comes_from_the_profile(self, session):
        session.history.extend(
            {"role": "user", "content": f"u{i}"} for i in range(40))
        assert len(main.build_messages(session, "now")) == \
            main.HISTORY_MESSAGES + 2

    def test_function_words_are_taught_but_not_counted(self, session):
        assert "i" in session.vocab
        assert "i" not in session.tracked

    def test_teaching_rules_join_the_system_prompt(self, session):
        assert "新授" in session.system_prompt()

    def test_mode_switch_clears_the_conversation(self, session):
        session.history.append({"role": "user", "content": "你好"})
        session.mode = "kid"
        session.history.clear()
        session.load_curriculum()
        assert session.history == []
        assert session.units


class TestEndpoints:
    def test_learners_lists_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        data = TestClient(main.app).get("/learners").json()
        assert data["default"] == main.DEFAULT_LEARNER
        assert main.DEFAULT_LEARNER in data["learners"]

    def test_units_report_words_left(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        data = TestClient(main.app).get("/learners/tester/units?mode=kid").json()
        assert data["units"][0]["id"] == "greetings"
        assert data["units"][0]["left"] == data["units"][0]["words"]
        assert data["current"] == ""

    def test_learner_names_cannot_escape_the_data_dir(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        TestClient(main.app).get("/learners/..escaped/units?mode=kid")
        assert not (tmp_path.parent / "escaped").exists()
        assert (tmp_path / "learners" / "escaped").is_dir()


class TestTeachingConfig:
    def test_enabled(self):
        assert load()["teaching"]["enabled"] is True

    def test_block_caps_match_the_renderer(self):
        caps = state_block.Caps(**load()["teaching"]["block"])
        assert (caps.new_items, caps.review_items, caps.known_items) == (2, 5, 20)
