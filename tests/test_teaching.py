"""Unit tests for the teaching state (no services needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator import main
from orchestrator.config import load
from orchestrator.teaching import (curriculum, digest, match, profile,
                                   readback, scheduler, state_block)
from orchestrator.teaching.curriculum import CurriculumError, Unit
from orchestrator.teaching.store import (MASTERY_BOX, Store, due_after,
                                         local_day)


class FakeLLM:
    """Stands in for the tutor model, replying with a fixed string."""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts = []

    async def stream_chat(self, messages, cancel=None, max_tokens=None):
        self.prompts.append(messages)
        yield self.reply

def day(n: int, hour: int = 12) -> str:
    """A timestamp on day n of a fixed month, the same hour every day."""
    return f"2026-10-{n:02d}T{hour:02d}:00:00+00:00"


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
        path.write_text('{"lemma": "cat", "zh": "猫"}\n\n{"lemma": "dog"}\n',
                        encoding="utf-8")
        assert curriculum.load_lexicon(path) == {"cat": "猫", "dog": None}

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

    def test_a_mention_does_not_introduce(self, store):
        store.expose(["red"], 1)
        assert store.boxes()["red"] == 0
        assert store.get("red")["last_seen_at"] is not None

    def test_a_planned_word_is_introduced_when_taught(self, store):
        store.expose(["red", "blue"], 1, introduce=["red"], at=day(1))
        assert store.boxes()["red"] == 1
        assert store.boxes()["blue"] == 0
        assert store.get("red")["due_at"] == due_after(1, day(1))

    def test_the_child_saying_a_word_introduces_it(self, store):
        store.practice(["red"], 1, at=day(1))
        assert store.boxes()["red"] == 1
        assert store.get("red")["practices"] == 1

    def test_practice_the_same_day_does_not_promote(self, store):
        store.practice(["red"], 1, at=day(1, 11))
        store.practice(["red"], 1, at=day(1, 12))
        store.practice(["red"], 2, at=day(1, 13))
        assert store.boxes()["red"] == 1
        assert store.get("red")["practices"] == 3

    def test_promotion_waits_for_the_due_day(self, store):
        store.practice(["red"], 1, at=day(1))       # box 1, due day 2
        store.practice(["red"], 2, at=day(2))       # due: box 2, due day 4
        assert store.boxes()["red"] == 2
        store.practice(["red"], 3, at=day(3))       # not due yet
        assert store.boxes()["red"] == 2
        store.practice(["red"], 4, at=day(4))       # due: box 3
        assert store.boxes()["red"] == 3

    def test_mastery_takes_production_on_several_days(self, store):
        for n, d in enumerate([1, 2, 4, 8], start=1):
            assert store.boxes()["red"] < MASTERY_BOX
            store.practice(["red"], n, at=day(d))
        assert store.boxes()["red"] == MASTERY_BOX

    def test_a_mention_never_promotes(self, store):
        store.practice(["red"], 1, at=day(1))
        for d in (2, 3, 9):
            store.expose(["red"], 2, introduce=["red"], at=day(d))
        assert store.boxes()["red"] == 1

    def test_unknown_word_is_ignored(self, store):
        store.practice(["ghost"], 1)
        assert "ghost" not in store.boxes()

    def test_parent_pinned_box_survives_practice(self, store):
        store.set_box("red", 6)
        store.practice(["red"], 1, at=day(1))
        store.practice(["red"], 2, at=day(9))
        assert store.boxes()["red"] == 6
        assert store.get("red")["practices"] == 2

    def test_release_hands_word_back_to_automatic(self, store):
        store.set_box("red", 6)
        store.release("red")
        assert store.get("red")["manual"] == 0

    def test_demote_stale(self, store):
        store.set_box("red", 3, manual=False)
        store.db.execute("UPDATE words SET due_at = '2020-01-01'")
        assert store.demote_stale() == 1
        assert store.boxes()["red"] == 2
        assert store.get("red")["due_at"] <= local_day().isoformat()
        assert store.demote_stale() == 0       # one step per lapse

    def test_demote_leaves_the_slightly_overdue_alone(self, store):
        store.set_box("red", 3, manual=False)
        store.db.execute("UPDATE words SET due_at = ?",
                         ((local_day() - timedelta(days=2)).isoformat(),))
        assert store.demote_stale() == 0

    def test_demote_skips_pinned_words(self, store):
        store.set_box("red", 3)
        store.db.execute("UPDATE words SET due_at = '2020-01-01'")
        assert store.demote_stale() == 0

    def test_settings_roundtrip(self, store):
        assert store.get_setting("current_unit") is None
        store.set_setting("current_unit", "animals")
        assert store.get_setting("current_unit") == "animals"
        store.set_setting("current_unit", "food")
        assert store.get_setting("current_unit") == "food"

    def test_review_lists_only_words_that_are_due(self, store):
        assert store.review_due(10, day(1)) == []      # nothing introduced yet
        store.practice(["red"], 1, at=day(1))
        assert store.review_due(10, day(1)) == []      # due tomorrow
        assert store.review_due(10, day(2)) == ["red"]

    def test_a_mention_sends_a_word_to_the_back_of_the_queue(self, store):
        store.set_box("red", 2, manual=False)
        store.set_box("blue", 2, manual=False)
        store.db.execute("UPDATE words SET last_seen_at = '2026-01-01T00:00:00+00:00' "
                         "WHERE lemma = 'red'")
        store.db.execute("UPDATE words SET last_seen_at = '2026-01-02T00:00:00+00:00' "
                         "WHERE lemma = 'blue'")
        assert store.review_due(10) == ["red", "blue"]
        store.expose(["red"], 1)
        assert store.review_due(10) == ["blue", "red"]

    def test_a_word_with_no_due_date_is_due(self, store):
        store.set_box("red", 2, manual=False)
        assert store.get("red")["due_at"] is None
        assert store.review_due(10) == ["red"]

    def test_review_is_capped(self, store):
        store.set_box("red", 2, manual=False)
        store.set_box("blue", 2, manual=False)
        assert len(store.review_due(1)) == 1

    def test_pending_words_wait_for_the_child(self, store):
        store.expose(["red", "blue"], 1, introduce=["red", "blue"], at=day(1))
        assert store.pending(5, 3, day(1)) == ["red", "blue"]
        store.practice(["red"], 1, at=day(1))
        assert store.pending(5, 3, day(1)) == ["blue"]
        assert store.pending(5, 3, day(2)) == []        # a new day

    def test_a_word_shown_often_enough_stops_being_pending(self, store):
        for n in range(3):
            store.expose(["red"], 1, introduce=["red"], at=day(1))
        assert store.pending(5, 3, day(1)) == []

    def test_introduced_today_counts_curriculum_words(self, store):
        store.expose(["red", "cat"], 1, introduce=["red", "cat"], at=day(1))
        store.add_topic_word("moon", "中秋节", 1, at=day(1))
        assert store.introduced_today(day(1)) == 2
        assert store.introduced_today(day(2)) == 0

    def test_topic_words_keep_their_context(self, store):
        assert store.add_topic_word("moon", "中秋节", 1, at=day(1)) is True
        assert store.add_topic_word("moon", "别的话题", 1, at=day(1)) is False
        assert store.topic_words() == {"moon": "中秋节"}
        assert store.get("moon")["box"] == 1

    def test_last_topics_come_from_the_last_session_with_talk(self, store):
        first = store.start_session("kid", at=day(1))
        for _ in range(3):
            store.log_turn(first, "x", "y", [], at=day(1))
        store.add_topic_word("moon", "中秋节", first, at=day(1, 13))
        second = store.start_session("kid", at=day(2))
        assert store.last_topics(second) == ["moon(中秋节)"]
        assert store.last_topics(first) == []

    def test_an_old_database_gains_the_new_columns(self, tmp_path):
        import sqlite3
        path = tmp_path / "learners" / "old" / "state.sqlite"
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE words (lemma TEXT PRIMARY KEY, unit TEXT NOT NULL, "
                   "box INTEGER NOT NULL DEFAULT 0, introduced_at TEXT, "
                   "last_seen_at TEXT, last_session INTEGER, "
                   "practices INTEGER NOT NULL DEFAULT 0, "
                   "sessions_seen INTEGER NOT NULL DEFAULT 0, "
                   "manual INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO words(lemma, unit, box) VALUES ('red', 'colors', 3)")
        db.commit()
        db.close()
        old = Store("old", tmp_path)
        assert old.get("red")["due_at"] is None
        assert old.get("red")["exposures"] == 0
        old.close()

    def test_new_and_known_queries(self, store):
        assert store.new_in_unit("colors", 2) == ["red", "blue"]
        store.practice(["red"], 1)
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

    def test_summary_of_the_older_conversation(self):
        plan = state_block.Plan(summary="聊过颜色和猫")
        assert state_block.render(plan) == "【前文: 聊过颜色和猫】"

    def test_summary_is_clipped(self):
        plan = state_block.Plan(summary="x" * 300)
        out = state_block.render(plan, state_block.Caps(summary_chars=20))
        assert out == "【前文: " + "x" * 19 + "…】"

    def test_long_lines_are_clipped(self):
        plan = state_block.Plan(portrait=["x" * 80])
        assert state_block.render(plan, state_block.Caps(line_chars=10)) == \
            "【画像: " + "x" * 9 + "…】"


class TestLessonBlock:
    def test_theme_comes_first_and_hint_last(self):
        plan = state_block.Plan(title="动物", pattern="I like ___.",
                                new=["cat"], hint="聊聊宠物")
        assert state_block.render(plan) == \
            "【单元: 动物 ｜ 句型: I like ___. ｜ 新授: cat ｜ 本轮: 聊聊宠物】"

    def test_topics_are_listed(self):
        plan = state_block.Plan(topics=["彩虹", "画画"])
        assert state_block.render(plan) == "【话题: 彩虹、画画】"

    def test_a_topic_word_carries_its_context(self):
        plan = state_block.Plan(review=["moon", "cat"], notes={"moon": "中秋节"})
        assert state_block.render(plan) == "【复习: moon(中秋节), cat】"


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


class TestTeachingPoints:
    def test_the_invitation_to_repeat_names_the_word(self):
        assert match.teaching_points("月亮的英文是 moon，跟我读一遍：moon！") == ["moon"]

    def test_the_gloss_form_names_the_word(self):
        assert match.teaching_points("兔子的英文是 rabbit，就是兔子。") == ["rabbit"]

    def test_a_phrase_is_not_a_word(self):
        assert match.teaching_points("跟我读一遍：magic pen！") == []

    def test_quotes_and_full_width_punctuation(self):
        assert match.teaching_points("跟我读：“moon”！") == ["moon"]

    def test_english_in_passing_is_not_a_teaching_point(self):
        assert match.teaching_points("Sunny 老师觉得 good 很好。") == []

    def test_single_letters_are_ignored(self):
        assert match.teaching_points("跟我读一遍：I！") == []


class TestInvitation:
    def test_the_requested_english_is_found(self):
        assert match.invitation("月亮的英文是 moon。跟我读一遍：moon！") == "moon"
        assert match.invitation("来，再说一次：hi！") == "hi"
        assert match.invitation("再试一次：red！") == "red"
        assert match.invitation("跟我读：“magic pen”！") == "magic pen"

    def test_english_after_a_colon_in_a_story_is_not_a_request(self):
        assert match.invitation("妈妈说：hello") is None
        assert match.invitation("月亮的英文是 moon。") is None

    def test_the_reply_is_cut_after_the_request(self):
        text = "月亮的英文是 moon，就是“月亮”。跟我读一遍：moon！你吃月饼了吗？"
        assert match.cut_after_invitation(text) == \
            "月亮的英文是 moon，就是“月亮”。跟我读一遍：moon！"

    def test_nothing_is_cut_while_the_word_is_still_arriving(self):
        assert match.cut_after_invitation("跟我读一遍：mo") is None
        assert match.cut_after_invitation("跟我读一遍：moon") is None
        assert match.cut_after_invitation("跟我读一遍：moon！") == "跟我读一遍：moon！"

    def test_the_closing_quote_stays_with_the_request(self):
        assert match.cut_after_invitation("跟我读：“moon”！然后") == "跟我读：“moon”！"

    def test_no_request_no_cut(self):
        assert match.cut_after_invitation("今天天气真好。") is None

    def test_said_matches_every_word_of_a_phrase(self):
        assert match.said("Moon.", "moon")
        assert match.said("this is a magic pen", "magic pen")
        assert not match.said("magic", "magic pen")
        assert match.said("apples", "apple")
        assert not match.said("", "moon")


class TestReadback:
    def test_a_correct_attempt(self):
        v = readback.judge("moon", "Moon.", 0)
        assert (v.kind, v.failed) == ("ok", False)
        assert "具体" in v.hint

    def test_a_wrong_word_is_a_miss_with_a_retry(self):
        v = readback.judge("moon", "Mon.", 0)
        assert (v.kind, v.failed) == ("retry", True)
        assert "Mon" in v.line

    def test_a_short_chinese_turn_is_taken_as_unclear(self):
        v = readback.judge("moon", "木", 0)
        assert (v.kind, v.failed) == ("unclear", True)

    def test_something_else_is_not_a_miss(self):
        v = readback.judge("moon", "我今天去了动物园玩得很开心", 0)
        assert (v.kind, v.failed) == ("other", False)

    def test_the_second_miss_gives_the_word_up(self):
        assert readback.judge("moon", "Mon.", 1).kind == "giveup"
        assert readback.judge("moon", "木", 1).kind == "giveup"

    def test_a_good_attempt_after_a_miss_still_counts(self):
        assert readback.judge("moon", "moon", 1).kind == "ok"


class TestScheduler:
    def test_without_a_pick_the_first_unfinished_unit_wins(self, store):
        assert scheduler.current_unit(store, UNITS).id == "colors"
        store.expose(["red", "blue"], 1, introduce=["red", "blue"])
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_parent_pick_wins(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "animals")
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_parent_pick_survives_a_finished_unit(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "animals")
        store.practice(["cat"], 1)
        assert scheduler.current_unit(store, UNITS).id == "animals"

    def test_unknown_pick_falls_back_to_automatic(self, store):
        store.set_setting(scheduler.CURRENT_UNIT, "ghost")
        assert scheduler.current_unit(store, UNITS).id == "colors"

    def test_a_finished_curriculum_goes_back_to_the_weakest_unit(self, store):
        store.set_box("red", 5, manual=False)
        store.set_box("blue", 5, manual=False)
        store.set_box("cat", 1, manual=False)
        assert scheduler.current_unit(store, UNITS).id == "animals"
        store.set_box("cat", 6, manual=False)
        store.set_box("blue", 1, manual=False)
        assert scheduler.current_unit(store, UNITS).id == "colors"

    def test_plan_respects_the_caps(self, store):
        plan = scheduler.plan(store, UNITS,
                              state_block.Caps(new_items=1, known_items=5))
        assert (plan.unit, plan.new) == ("colors", ["red"])

    def test_plan_carries_review_and_keeps_it_out_of_known(self, store):
        store.set_box("red", 2, manual=False)
        store.set_box("blue", 2, manual=False)
        plan = scheduler.plan(store, UNITS, state_block.Caps(new_items=1))
        assert sorted(plan.review) == ["blue", "red"]
        assert plan.new == ["cat"]
        assert "red" not in plan.known and "blue" not in plan.known

    def test_the_next_word_waits_until_the_child_has_said_one(self, store):
        store.expose(["red"], 1, introduce=["red"], at=day(1))
        again = scheduler.plan(store, UNITS, state_block.Caps(new_items=2),
                               at=day(1))
        assert again.new == ["red"]
        store.practice(["red"], 1, at=day(1))
        moved = scheduler.plan(store, UNITS, state_block.Caps(new_items=2),
                               at=day(1))
        assert moved.new == ["blue"]

    def test_a_word_the_child_never_says_does_not_hold_the_lesson(self, store):
        for _ in range(scheduler.MAX_EXPOSURES):
            store.expose(["red"], 1, introduce=["red"], at=day(1))
        plan = scheduler.plan(store, UNITS, state_block.Caps(new_items=2),
                              at=day(1))
        assert plan.new == ["blue"]

    def test_the_daily_cap_holds(self, store):
        store.expose(["red"], 1, introduce=["red"], at=day(1))
        store.practice(["red"], 1, at=day(1))
        capped = scheduler.plan(store, UNITS, state_block.Caps(new_items=2),
                                new_per_day=1, at=day(1))
        assert capped.new == []
        tomorrow = scheduler.plan(store, UNITS, state_block.Caps(new_items=2),
                                  new_per_day=1, at=day(2))
        assert tomorrow.new == ["blue"]

    def test_the_plan_names_the_lesson(self, store):
        plan = scheduler.plan(store, UNITS, state_block.Caps())
        assert plan.title == "颜色"

    def test_topics_only_open_a_conversation(self, store):
        units = [Unit(id="colors", title="颜色", goal="", words=("red",),
                      patterns=(), after=(), sessions=1, topics=("彩虹", "画画"))]
        store.sync_units(units)
        assert scheduler.plan(store, units, state_block.Caps(),
                              opening=True).topics == ["彩虹", "画画"]
        assert scheduler.plan(store, units, state_block.Caps()).topics == []

    def test_a_pattern_appears_once_the_unit_is_secure(self, store):
        units = [Unit(id="colors", title="颜色", goal="", words=("red", "blue"),
                      patterns=("It is red.",), after=(), sessions=1)]
        assert scheduler.plan(store, units, state_block.Caps()).pattern == ""
        store.set_box("red", 2, manual=False)
        store.set_box("blue", 1, manual=False)
        plan = scheduler.plan(store, units, state_block.Caps())
        assert plan.pattern == "It is red."
        assert "句型" in plan.hint

    def test_the_hint_is_one_suggestion(self):
        assert "red" in scheduler.hint(["red"], ["cat"], "", False)
        assert "cat" in scheduler.hint([], ["cat"], "", False)
        assert "开场" not in scheduler.hint([], [], "", False)
        assert "不要宣布" in scheduler.hint(["red"], [], "", True)

    def test_last_time_reaches_the_opening_plan(self, store):
        first = store.start_session("kid", at=day(1))
        for _ in range(3):
            store.log_turn(first, "x", "y", [], at=day(1))
        store.add_topic_word("moon", "中秋节", first, at=day(1, 13))
        second = store.start_session("kid", at=day(2))
        plan = scheduler.plan(store, UNITS, state_block.Caps(),
                              session_id=second)
        assert plan.digest == ["moon(中秋节)"]

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


class TestRecordTurn:
    def _turn(self, session, child, tutor):
        """One turn the way the orchestrator runs it: plan, reply, record."""
        main.build_messages(session, child)
        main.record_turn(session, child, [tutor], interrupted=False)
        session.history.append({"role": "user", "content": child})

    def _plan_turn(self, session):
        """Advance to the next turn that works the plan's words in."""
        while not scheduler.plan_turn(session.turn_count):
            session.turn_count += 1

    def test_planned_words_are_introduced_and_wait_for_the_child(self, session):
        self._plan_turn(session)
        main.build_messages(session, "你好")
        assert session.plan.new == ["hello", "hi"]
        self._turn(session, "你好", "你好呀。hello 就是你好，跟我读一遍：hello！")
        boxes = session.store.boxes()
        assert boxes["hello"] == 1 and boxes["hi"] == 0
        self._plan_turn(session)
        main.build_messages(session, "嗯")
        assert session.plan.new == ["hello"]          # still waiting on the child
        self._turn(session, "Hello", "没错，就是 hello！")
        self._plan_turn(session)
        main.build_messages(session, "嗯")
        assert "hello" not in session.plan.new

    def test_a_word_mentioned_in_passing_is_not_introduced(self, session):
        self._turn(session, "你好", "你好呀，我们可以聊聊 bus 和 bike 吗？")
        assert session.store.boxes()["bus"] == 0

    def test_function_words_rotate_through_review(self, session):
        """The queue used to stay on the same five function words forever."""
        for lemma in session.vocab:
            session.store.set_box(lemma, 2, manual=False)
        session.store.db.execute(
            "UPDATE words SET last_seen_at = '2026-09-30T00:00:00+00:00'")
        session.store.db.execute(
            "UPDATE words SET last_seen_at = '2026-09-17T00:00:00+00:00' "
            "WHERE lemma IN ('i', 'my', 'what', 'where', 'who')")
        session.store.db.commit()
        self._plan_turn(session)
        main.build_messages(session, "你好")
        first = list(session.plan.review)
        assert set(first) == {"i", "my", "what", "where", "who"}
        main.record_turn(session, "你好", ["I my what where who"], False)
        self._plan_turn(session)
        main.build_messages(session, "你好")
        assert set(session.plan.review).isdisjoint(first)

    def test_english_function_words_from_the_child_count(self, session):
        self._turn(session, "This is my magic pen", "好棒的笔！")
        assert session.store.get("my")["practices"] == 1
        assert session.store.get("my")["box"] == 1

    def test_a_word_taught_for_the_childs_topic_is_kept(self, session):
        self._turn(session, "今天是中秋节",
                   "中秋节要赏月吃月饼。月亮的英文是 moon，跟我读一遍：moon！")
        assert session.store.topic_words() == {"moon": "今天是中秋节"}
        self._turn(session, "moon", "没错！")
        assert session.store.get("moon")["practices"] == 1

    def test_a_curriculum_word_is_not_a_topic_word(self, session):
        self._turn(session, "猫怎么说", "猫的英文是 cat，跟我读一遍：cat！")
        assert session.store.topic_words() == {}

    def test_the_first_turn_gets_topics_and_later_turns_do_not(self, session):
        first = main.build_messages(session, "你好")[-1]["content"]
        assert "话题:" in first and "不要宣布" in first
        session.history.append({"role": "user", "content": "你好"})
        later = main.build_messages(session, "嗯")[-1]["content"]
        assert "话题:" not in later


class TestReadBack:
    def test_a_request_to_repeat_is_remembered(self, session):
        main.note_invitation(session, "月亮的英文是 moon。跟我读一遍：moon！")
        assert session.awaiting == "moon"

    def test_the_next_turn_carries_the_verdict(self, session):
        main.note_invitation(session, "跟我读一遍：moon！")
        block = main.build_messages(session, "Moon.")[-1]["content"]
        assert "跟读: moon → 读对了" in block
        assert "不要只说“真棒”" in block

    def test_a_feedback_turn_sets_the_plan_words_aside(self, session):
        session.turn_count = scheduler.PLAN_EVERY - 1      # a plan turn
        main.note_invitation(session, "跟我读一遍：moon！")
        main.build_messages(session, "Moon.")
        assert session.plan.new == [] and session.plan.review == []

    def test_a_miss_is_counted_and_a_second_request_keeps_the_count(self, session):
        main.note_invitation(session, "跟我读一遍：moon！")
        main.build_messages(session, "Mon.")
        assert session.read_tries == 1
        main.note_invitation(session, "再读一遍：moon！")
        assert session.awaiting == "moon" and session.read_tries == 1
        block = main.build_messages(session, "Mon.")[-1]["content"]
        assert "还没读准" in block

    def test_a_new_word_starts_the_count_again(self, session):
        main.note_invitation(session, "跟我读一遍：moon！")
        main.build_messages(session, "Mon.")
        main.note_invitation(session, "跟我读一遍：sun！")
        assert session.read_tries == 0

    def test_a_reply_without_a_request_closes_the_exchange(self, session):
        main.note_invitation(session, "跟我读一遍：moon！")
        main.build_messages(session, "Moon.")
        main.note_invitation(session, "读得很清楚。你今晚会看月亮吗？")
        assert session.awaiting == ""

    def test_without_a_request_the_block_has_no_verdict(self, session):
        assert "跟读" not in main.build_messages(session, "你好")[-1]["content"]


class _FakeWS:
    client_state = main.WebSocketState.CONNECTED

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        pass


class _FakeTTS:
    spoken = []

    async def pcm_chunks(self, text, **kw):
        self.spoken.append(text)
        return
        yield


class _ChunkedLLM:
    def __init__(self, reply, size=3):
        self.chunks = [reply[i:i + size] for i in range(0, len(reply), size)]

    async def stream_chat(self, messages, cancel=None, max_tokens=None):
        for chunk in self.chunks:
            yield chunk


class _FakeASR:
    def __init__(self, text):
        self.text = text

    async def transcribe(self, pcm, sr=None):
        return self.text


class TestFloor:
    def _run(self, session, monkeypatch, reply, heard="你好"):
        ws = _FakeWS()
        tts = _FakeTTS()
        tts.spoken = []
        monkeypatch.setattr(main, "llm", _ChunkedLLM(reply))
        monkeypatch.setattr(main, "asr", _FakeASR(heard))
        monkeypatch.setattr(main, "tts", tts)
        asyncio.run(main.handle_utterance(ws, session, None, asyncio.Event()))
        return ws, tts

    def test_nothing_is_said_after_the_request_to_repeat(self, session, monkeypatch):
        reply = ("月亮的英文是 moon，就是“月亮”。跟我读一遍：moon！"
                 "你今晚会看月亮吗？我们再来聊聊月饼吧。")
        ws, tts = self._run(session, monkeypatch, reply)
        assert "".join(tts.spoken).endswith("moon！")
        assert "月饼" not in "".join(tts.spoken)
        shown = "".join(m["text"] for m in ws.sent if m["type"] == "reply_delta")
        assert shown.endswith("moon！") and "月饼" not in shown
        assert session.history[-1]["content"].endswith("moon！")
        assert session.awaiting == "moon"

    def test_a_reply_without_a_request_is_spoken_whole(self, session, monkeypatch):
        reply = "中秋节真热闹。你们家今晚吃月饼了吗？"
        ws, tts = self._run(session, monkeypatch, reply)
        assert "".join(tts.spoken) == reply
        assert session.awaiting == ""

    def test_the_log_keeps_only_what_was_said(self, session, monkeypatch):
        reply = "猫的英文是 cat。跟我读一遍：cat！然后再聊聊狗 dog。"
        self._run(session, monkeypatch, reply)
        row = session.store.db.execute(
            "SELECT tutor_text FROM turns ORDER BY id DESC LIMIT 1").fetchone()
        assert "dog" not in row["tutor_text"]


class TestSilencePrompt:
    def test_prompts_once_when_the_child_stays_silent(self, session, monkeypatch):
        monkeypatch.setattr(main, "READ_WAIT_S", 0.01)
        tts = _FakeTTS()
        tts.spoken = []
        monkeypatch.setattr(main, "tts", tts)
        ws = _FakeWS()
        session.streaming = True
        main.note_invitation(session, "跟我读一遍：moon！")

        async def go():
            main.schedule_nudge(ws, session)
            await asyncio.sleep(0.1)
            main.schedule_nudge(ws, session)      # the prompt's own playback end
            await asyncio.sleep(0.1)

        asyncio.run(go())
        nudges = [m for m in ws.sent if m["type"] == "nudge"]
        assert len(nudges) == 1 and "moon" in nudges[0]["text"]
        assert session.awaiting == "moon"          # still waiting for the child

    def test_no_prompt_when_the_child_is_speaking(self, session, monkeypatch):
        monkeypatch.setattr(main, "READ_WAIT_S", 0.01)
        ws = _FakeWS()
        session.streaming = True
        main.note_invitation(session, "跟我读一遍：moon！")
        session.svad.active = True

        async def go():
            main.schedule_nudge(ws, session)
            await asyncio.sleep(0.1)

        asyncio.run(go())
        assert not [m for m in ws.sent if m["type"] == "nudge"]

    def test_no_prompt_when_nothing_was_asked(self, session, monkeypatch):
        monkeypatch.setattr(main, "READ_WAIT_S", 0.01)
        ws = _FakeWS()
        session.streaming = True

        async def go():
            main.schedule_nudge(ws, session)
            await asyncio.sleep(0.1)

        asyncio.run(go())
        assert not ws.sent

    def test_zero_turns_the_prompt_off(self, session, monkeypatch):
        monkeypatch.setattr(main, "READ_WAIT_S", 0)
        session.streaming = True
        main.note_invitation(session, "跟我读一遍：moon！")

        async def go():
            main.schedule_nudge(_FakeWS(), session)
            return session.nudge_task

        assert asyncio.run(go()) is None


class TestTurnAlternation:
    def test_most_turns_follow_the_childs_topic(self, session):
        kinds = []
        for n in range(6):
            session.turn_count = n
            main.build_messages(session, "你好")
            kinds.append(bool(session.plan.new or session.plan.review))
        assert kinds == [False, False, True, False, False, True]

    def test_a_topic_turn_carries_no_plan_words(self, session):
        session.turn_count = 1
        session.history.append({"role": "user", "content": "你好"})
        block = main.build_messages(session, "嗯")[-1]["content"]
        assert "新授" not in block and "复习" not in block and "已会" not in block
        assert "只教话题里的一个简单词" in block

    def test_a_topic_turn_introduces_nothing(self, session):
        session.turn_count = 1
        main.build_messages(session, "你好")
        main.record_turn(session, "你好", ["hello 就是你好，跟我读一遍：hello！"], False)
        assert session.store.boxes()["hello"] == 0

    def test_turns_restart_with_a_new_conversation(self, session):
        session.turn_count = 5
        session.load_curriculum()
        assert session.turn_count == 0


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


class TestCompaction:
    def _session(self, tmp_path, monkeypatch, turns):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(main, "HISTORY_MESSAGES", 4)
        monkeypatch.setattr(main, "COMPACT_MESSAGES", 2)
        session = main.Session()
        session.open_learner("tester")
        session.history.extend(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i}"}
            for i in range(turns))
        return session

    def test_oldest_turns_are_folded_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "llm", FakeLLM("聊过颜色和猫"))
        session = self._session(tmp_path, monkeypatch, 6)
        asyncio.run(main.compact_history(session))
        assert session.summary == "聊过颜色和猫"
        assert len(session.history) == 4
        session.close()

    def test_nothing_happens_below_the_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "llm", FakeLLM("聊过颜色和猫"))
        session = self._session(tmp_path, monkeypatch, 5)
        asyncio.run(main.compact_history(session))
        assert session.summary == ""
        assert len(session.history) == 5
        session.close()

    def test_a_failed_summary_leaves_the_history_alone(self, tmp_path,
                                                       monkeypatch):
        class Broken:
            async def stream_chat(self, messages, cancel=None, max_tokens=None):
                raise RuntimeError("model is down")
                yield ""

        monkeypatch.setattr(main, "llm", Broken())
        session = self._session(tmp_path, monkeypatch, 6)
        asyncio.run(main.compact_history(session))
        assert len(session.history) == 6
        session.close()

    def test_summary_reaches_the_prompt(self, session):
        session.summary = "聊过颜色和猫"
        assert "前文: 聊过颜色和猫" in main.build_messages(session, "你好")[-1]["content"]


class TestProgress:
    def test_words_of_a_unit_start_untaught(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        data = TestClient(main.app).get(
            "/learners/tester/progress?mode=kid&unit=greetings").json()
        assert data["unit"] == "greetings"
        assert [w["lemma"] for w in data["words"]][:2] == ["hello", "hi"]
        assert all(w["state"] == "未学" for w in data["words"])

    def test_progress_shows_the_chinese_gloss(self, tmp_path, monkeypatch):
        source = main.data_root() / "curriculum" / "lexicon.jsonl"
        if not source.exists():
            pytest.skip("no word list imported: run scripts/import_curriculum.py")
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        (tmp_path / "curriculum").mkdir()
        (tmp_path / "curriculum" / "lexicon.jsonl").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8")
        data = TestClient(main.app).get(
            "/learners/tester/progress?mode=kid&unit=colors").json()
        assert data["words"][0]["zh"] == "红色"

    def test_a_verdict_pins_the_word(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        client = TestClient(main.app)
        row = client.post("/learners/tester/words/hello?mode=kid",
                          json={"known": True}).json()
        assert row["box"] >= 4 and row["manual"] == 1
        row = client.post("/learners/tester/words/hello?mode=kid",
                          json={"release": True}).json()
        assert row["manual"] == 0

    def test_a_verdict_can_send_a_word_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        client = TestClient(main.app)
        client.post("/learners/tester/words/hello?mode=kid", json={"known": True})
        row = client.post("/learners/tester/words/hello?mode=kid",
                          json={"known": False}).json()
        assert row["box"] == 0 and row["manual"] == 1

    def test_a_word_outside_the_curriculum_is_rejected(self, tmp_path,
                                                       monkeypatch):
        monkeypatch.setenv("TALKNEST_DATA_DIR", str(tmp_path))
        r = TestClient(main.app).post("/learners/tester/words/ghost?mode=kid",
                                      json={"known": True})
        assert r.status_code == 404


class TestTeachingConfig:
    def test_enabled(self):
        assert load()["teaching"]["enabled"] is True

    def test_block_caps_match_the_renderer(self):
        caps = state_block.Caps(**load()["teaching"]["block"])
        assert (caps.new_items, caps.review_items, caps.known_items) == (2, 5, 10)
