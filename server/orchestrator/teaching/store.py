"""Per-learner progress store (L2) and session log (L4).

One sqlite file per learner, so a learner is a directory that can be copied
or deleted. The default rollback journal is kept on purpose: WAL needs
shared memory that is unreliable on the removable media this checkout can
live on, and the write volume here is a handful of rows per turn.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

MASTERY_BOX = 4    # box at or above this counts as known
MAX_BOX = 6
STALE_DAYS = 14
# Days until a word in this box is due again. A word moves up a box only on
# a day it is due, so reaching MASTERY_BOX takes production on several
# different days, not one busy afternoon.
INTERVAL_DAYS = {1: 1, 2: 2, 3: 4, 4: 7, 5: 14, 6: 30}
# Unit of words the tutor taught because the child's own topic needed them.
TOPIC_UNIT = "topic"

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    lemma TEXT PRIMARY KEY,
    unit TEXT NOT NULL,
    box INTEGER NOT NULL DEFAULT 0,
    introduced_at TEXT,
    last_seen_at TEXT,
    last_session INTEGER,
    practices INTEGER NOT NULL DEFAULT 0,
    sessions_seen INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    exposures INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS learner_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner TEXT NOT NULL,
    mode TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    turns INTEGER NOT NULL DEFAULT 0,
    digest TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL,
    ts TEXT NOT NULL,
    learner_text TEXT,
    tutor_text TEXT,
    items TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0
);
"""


# Columns added after the first release; databases created before them get
# the columns on open.
_ADDED_COLUMNS = {
    "due_at": "TEXT",
    "exposures": "INTEGER NOT NULL DEFAULT 0",
    "note": "TEXT",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_day(at: str | None = None) -> date:
    """The learner's calendar day: a morning and an evening session on the
    same day are one day of practice, wherever the server clock points."""
    when = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    return when.astimezone().date()


def day_start(at: str | None = None) -> str:
    """Start of the learner's day as a UTC timestamp, comparable with the
    timestamps stored in the database."""
    midnight = datetime.combine(local_day(at), dtime.min).astimezone()
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def due_after(box: int, at: str | None = None) -> str:
    return (local_day(at) + timedelta(days=INTERVAL_DAYS[box])).isoformat()


class Store:
    """Progress of one learner."""

    def __init__(self, learner: str, root: Path):
        self.learner = learner
        self.path = Path(root) / "learners" / learner / "state.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(words)")}
        for name, kind in _ADDED_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE words ADD COLUMN {name} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def sync_units(self, units) -> None:
        """Add words that the curriculum has and this learner does not yet."""
        rows = [(w, u.id) for u in units for w in u.words]
        self.db.executemany(
            "INSERT OR IGNORE INTO words(lemma, unit) VALUES (?, ?)", rows)
        self.db.commit()

    def expose(self, lemmas, session_id: int, introduce=(),
               at: str | None = None) -> None:
        """Record that the tutor said these words.

        Saying a word is not teaching it: only a word the plan asked for
        (introduce) leaves box 0 this way. A word the tutor mentions in
        passing, or one that is already known, only has its record updated.
        """
        at = at or now()
        for lemma in lemmas:
            row = self.get(lemma)
            if row is None:
                continue
            box, due = row["box"], row["due_at"]
            introduced = row["introduced_at"]
            if box == 0 and lemma in introduce and not row["manual"]:
                box, due, introduced = 1, due_after(1, at), at
            self.db.execute(
                "UPDATE words SET box = ?, due_at = ?, introduced_at = ?, "
                "exposures = exposures + 1, last_seen_at = ?, last_session = ?, "
                "sessions_seen = sessions_seen + ? WHERE lemma = ?",
                (box, due, introduced, at, session_id,
                 int(row["last_session"] != session_id), lemma))
        self.db.commit()

    def practice(self, lemmas, session_id: int,
                 at: str | None = None) -> None:
        """Record that the child said these words.

        A word the child produces for the first time is introduced. After
        that it moves up one box, but only on a day it is due; saying it
        again the same day counts as practice without advancing it.
        Parent-pinned words keep their box: only the record moves.
        """
        at = at or now()
        today = local_day(at).isoformat()
        for lemma in lemmas:
            row = self.get(lemma)
            if row is None:
                continue
            box, due = row["box"], row["due_at"]
            introduced = row["introduced_at"] or at
            if not row["manual"]:
                if box == 0:
                    box, due = 1, due_after(1, at)
                elif due is None or due <= today:
                    box = min(MAX_BOX, box + 1)
                    due = due_after(box, at)
            self.db.execute(
                "UPDATE words SET box = ?, due_at = ?, introduced_at = ?, "
                "practices = practices + 1, last_seen_at = ?, last_session = ?, "
                "sessions_seen = sessions_seen + ? WHERE lemma = ?",
                (box, due, introduced, at, session_id,
                 int(row["last_session"] != session_id), lemma))
        self.db.commit()

    def add_topic_word(self, lemma: str, note: str, session_id: int,
                       at: str | None = None) -> bool:
        """Keep a word the tutor taught for the child's own topic.

        The note says what the child was talking about, so the word can
        come back later with its context. A word the store already has is
        left alone.
        """
        at = at or now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO words(lemma, unit, box, introduced_at, "
            "last_seen_at, last_session, sessions_seen, due_at, exposures, "
            "note) VALUES (?, ?, 1, ?, ?, ?, 1, ?, 1, ?)",
            (lemma, TOPIC_UNIT, at, at, session_id, due_after(1, at), note))
        self.db.commit()
        return cur.rowcount > 0

    def demote_stale(self, at: str | None = None,
                     days: int = STALE_DAYS) -> int:
        """Drop one box for words overdue by a long while, so they come back.

        The word is due again the same day. Words only a little overdue are
        left alone: being due already puts them in the review queue.
        """
        cutoff = (local_day(at) - timedelta(days=days)).isoformat()
        cur = self.db.execute(
            "UPDATE words SET box = MAX(1, box - 1), due_at = ? "
            "WHERE manual = 0 AND box > 1 AND due_at IS NOT NULL "
            "AND due_at < ?", (local_day(at).isoformat(), cutoff))
        self.db.commit()
        return cur.rowcount

    def set_box(self, lemma: str, box: int, manual: bool = True) -> bool:
        """Parent verdict on a word; auto bookkeeping stops touching it."""
        cur = self.db.execute(
            "UPDATE words SET box = ?, manual = ? WHERE lemma = ?",
            (box, int(manual), lemma))
        self.db.commit()
        return cur.rowcount > 0

    def release(self, lemma: str) -> None:
        """Hand a word back to automatic bookkeeping."""
        self.db.execute("UPDATE words SET manual = 0 WHERE lemma = ?", (lemma,))
        self.db.commit()

    def get(self, lemma: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM words WHERE lemma = ?", (lemma,)).fetchone()
        return dict(row) if row else None

    def boxes(self) -> dict[str, int]:
        rows = self.db.execute("SELECT lemma, box FROM words").fetchall()
        return {r["lemma"]: r["box"] for r in rows}

    def new_in_unit(self, unit: str, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT lemma FROM words WHERE unit = ? AND box = 0 "
            "ORDER BY rowid LIMIT ?", (unit, limit)).fetchall()
        return [r["lemma"] for r in rows]

    def review_due(self, limit: int, at: str | None = None) -> list[str]:
        """Words that are due, the one left alone the longest first.

        Ordering by the last time a word came up, not by box, is what keeps
        the queue moving: every mention pushes a word to the back, so a few
        stubborn words cannot occupy the front of the queue turn after turn.
        """
        if limit <= 0:
            return []
        rows = self.db.execute(
            "SELECT lemma FROM words WHERE box >= 1 "
            "AND (due_at IS NULL OR due_at <= ?) "
            "ORDER BY last_seen_at, box, rowid LIMIT ?",
            (local_day(at).isoformat(), limit)).fetchall()
        return [r["lemma"] for r in rows]

    def pending(self, limit: int, max_exposures: int,
                at: str | None = None) -> list[str]:
        """Words introduced today that the child has not yet said.

        The tutor keeps offering them until the child says one or they have
        been shown max_exposures times; a word the recogniser cannot hear
        must not hold the whole lesson.
        """
        rows = self.db.execute(
            "SELECT lemma FROM words WHERE unit != ? AND introduced_at >= ? "
            "AND practices = 0 AND exposures < ? ORDER BY introduced_at, rowid "
            "LIMIT ?", (TOPIC_UNIT, day_start(at), max_exposures, limit)
        ).fetchall()
        return [r["lemma"] for r in rows]

    def introduced_today(self, at: str | None = None) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM words WHERE unit != ? "
            "AND introduced_at >= ?", (TOPIC_UNIT, day_start(at))).fetchone()
        return row["n"]

    def topic_words(self) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT lemma, note FROM words WHERE unit = ?",
            (TOPIC_UNIT,)).fetchall()
        return {r["lemma"]: r["note"] or "" for r in rows}

    def last_topics(self, session_id: int, limit: int = 3) -> list[str]:
        """Topic words from the last earlier session that had real talk.

        Read straight from the database: the opening line should not cost a
        model call.
        """
        now_row = self.db.execute(
            "SELECT started_at FROM sessions WHERE id = ?",
            (session_id,)).fetchone()
        prev = self.db.execute(
            "SELECT started_at FROM sessions WHERE id < ? AND turns >= 3 "
            "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if now_row is None or prev is None:
            return []
        rows = self.db.execute(
            "SELECT lemma, note FROM words WHERE unit = ? "
            "AND introduced_at >= ? AND introduced_at < ? "
            "ORDER BY introduced_at DESC LIMIT ?",
            (TOPIC_UNIT, prev["started_at"], now_row["started_at"], limit)
        ).fetchall()
        return [f"{r['lemma']}({r['note']})" if r["note"] else r["lemma"]
                for r in rows]

    def known_in_units(self, units, limit: int) -> list[str]:
        if not units or limit <= 0:
            return []
        marks = ",".join("?" * len(units))
        rows = self.db.execute(
            f"SELECT lemma FROM words WHERE unit IN ({marks}) AND box >= 1 "
            "ORDER BY box, rowid LIMIT ?", (*units, limit)).fetchall()
        return [r["lemma"] for r in rows]

    def get_setting(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM learner_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO learner_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self.db.commit()

    def start_session(self, mode: str, at: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO sessions(learner, mode, started_at) VALUES (?, ?, ?)",
            (self.learner, mode, at or now()))
        self.db.commit()
        return cur.lastrowid

    def end_session(self, session_id: int, digest: str | None = None,
                    at: str | None = None) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = ?, digest = ? WHERE id = ?",
            (at or now(), digest, session_id))
        self.db.commit()

    def session_turns(self, session_id: int) -> list[dict]:
        """Turns of one session, for the portrait written at its end."""
        rows = self.db.execute(
            "SELECT learner_text, tutor_text, items, interrupted FROM turns "
            "WHERE session = ? ORDER BY id", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def log_turn(self, session_id: int, learner_text: str, tutor_text: str,
                 items, interrupted: bool = False, at: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO turns(session, ts, learner_text, tutor_text, items, "
            "interrupted) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, at or now(), learner_text, tutor_text,
             json.dumps(items, ensure_ascii=False), int(interrupted)))
        self.db.execute(
            "UPDATE sessions SET turns = turns + 1 WHERE id = ?", (session_id,))
        self.db.commit()
