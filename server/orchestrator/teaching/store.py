"""Per-learner progress store (L2) and session log (L4).

One sqlite file per learner, so a learner is a directory that can be copied
or deleted. The default rollback journal is kept on purpose: WAL needs
shared memory that is unreliable on the removable media this checkout can
live on, and the write volume here is a handful of rows per turn.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

MASTERY_BOX = 4    # box at or above this counts as known
MAX_BOX = 6
STALE_DAYS = 14

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
    manual INTEGER NOT NULL DEFAULT 0
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Progress of one learner."""

    def __init__(self, learner: str, root: Path):
        self.learner = learner
        self.path = Path(root) / "learners" / learner / "state.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def sync_units(self, units) -> None:
        """Add words that the curriculum has and this learner does not yet."""
        rows = [(w, u.id) for u in units for w in u.words]
        self.db.executemany(
            "INSERT OR IGNORE INTO words(lemma, unit) VALUES (?, ?)", rows)
        self.db.commit()

    def touch(self, lemmas, session_id: int, practiced: bool,
              at: str | None = None) -> None:
        """Record that lemmas came up in a turn.

        A word met in a session it has not been counted in yet moves up one
        Leitner box, or two when the learner has produced it repeatedly.
        Parent-pinned words keep their box: only the record moves.
        """
        at = at or now()
        for lemma in lemmas:
            row = self.db.execute(
                "SELECT * FROM words WHERE lemma = ?", (lemma,)).fetchone()
            if row is None:
                continue
            practices = row["practices"] + (1 if practiced else 0)
            new_session = row["last_session"] != session_id
            sessions = row["sessions_seen"] + (1 if new_session else 0)
            box = row["box"]
            if not row["manual"] and new_session:
                if box == 0:
                    box = 1
                else:
                    box = min(MAX_BOX, box + 1 + (1 if practices >= 2 else 0))
            self.db.execute(
                "UPDATE words SET box = ?, practices = ?, sessions_seen = ?, "
                "last_seen_at = ?, last_session = ?, introduced_at = ? "
                "WHERE lemma = ?",
                (box, practices, sessions, at, session_id,
                 row["introduced_at"] or at, lemma))
        self.db.commit()

    def demote_stale(self, at: str | None = None,
                     days: int = STALE_DAYS) -> int:
        """Drop one box for words not seen for a while, so they come back."""
        cutoff = (datetime.fromisoformat(at) if at else datetime.now(timezone.utc))
        cutoff = (cutoff - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.db.execute(
            "UPDATE words SET box = MAX(1, box - 1) WHERE manual = 0 AND box > 1 "
            "AND last_seen_at IS NOT NULL AND last_seen_at < ?", (cutoff,))
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

    def review_due(self, limit: int) -> list[str]:
        """Words met but not yet mastered, weakest first.

        Oldest first inside a box, so the word that has not come up for the
        longest is the one brought back. Mastered words are left alone: the
        point of mastering one is that it stops coming back.
        """
        if limit <= 0:
            return []
        rows = self.db.execute(
            "SELECT lemma FROM words WHERE box >= 1 AND box < ? "
            "ORDER BY box, last_seen_at, rowid LIMIT ?",
            (MASTERY_BOX, limit)).fetchall()
        return [r["lemma"] for r in rows]

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
