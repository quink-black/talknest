#!/usr/bin/env python3
"""Recompute a learner's word progress from what the child actually said.

Older versions credited a word whenever the tutor mentioned it, so boxes
climbed without the child ever saying the word. This replays the logged
turns through the current rules: only the child's own English counts, and a
word moves up a box only on a day it is due. Words the child never said go
back to untaught.

Dry run by default. With --apply the database is copied next to itself
first, then rewritten. Words a parent pinned (manual=1) are left alone.

    ./venv/bin/python scripts/rebuild_progress.py
    ./venv/bin/python scripts/rebuild_progress.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from orchestrator.config import data_root, load  # noqa: E402
from orchestrator.teaching import curriculum, match  # noqa: E402
from orchestrator.teaching.store import Store  # noqa: E402

RESET = {
    "box": 0, "introduced_at": None, "last_seen_at": None,
    "last_session": None, "practices": 0, "sessions_seen": 0,
    "due_at": None, "exposures": 0,
}
COPIED = ("box", "introduced_at", "last_seen_at", "last_session", "practices",
          "sessions_seen", "due_at")


def replay(units, turns, vocab) -> Store:
    """A scratch store after the logged turns, played through the rules."""
    scratch = Store("scratch", Path(tempfile.mkdtemp(prefix="talknest-replay-")))
    scratch.sync_units(units)
    for turn in turns:
        heard = match.find(turn["learner_text"] or "", vocab)
        if heard:
            scratch.practice(heard, turn["session"], at=turn["ts"])
    return scratch


def distribution(store: Store) -> dict[int, int]:
    return dict(sorted(Counter(store.boxes().values()).items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--learner", default=None,
                        help="learner directory (default: teaching.default_learner)")
    parser.add_argument("--mode", default=None, help="mode whose curriculum applies")
    parser.add_argument("--apply", action="store_true",
                        help="write the result; without it nothing changes")
    args = parser.parse_args()

    cfg = load()
    learner = args.learner or cfg.get("teaching", {}).get("default_learner", "default")
    mode = args.mode or cfg.get("default_mode", "kid")
    name = cfg["modes"][mode].get("curriculum")
    lexicon = curriculum.load_lexicon(data_root() / "curriculum" / "lexicon.jsonl")
    units = curriculum.load(name, lexicon) if name else []
    if not units:
        print(f"mode {mode!r} has no curriculum", file=sys.stderr)
        return 1
    vocab = {w for u in units for w in u.words}

    real = Store(learner, data_root())
    real.sync_units(units)
    turns = [dict(r) for r in real.db.execute(
        "SELECT session, ts, learner_text FROM turns ORDER BY id")]
    scratch = replay(units, turns, vocab)

    pinned = {r["lemma"] for r in real.db.execute(
        "SELECT lemma FROM words WHERE manual = 1")}
    print(f"learner {learner}: {len(turns)} turns, {len(pinned)} pinned words")
    print("boxes before:", distribution(real))
    print("boxes after: ", distribution(scratch))

    if not args.apply:
        print("dry run; pass --apply to write")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = real.path.with_name(f"{real.path.name}.bak-{stamp}")
    real.db.commit()
    shutil.copy2(real.path, backup)
    for lemma in vocab - pinned:
        row = scratch.get(lemma)
        values = ({k: row[k] for k in COPIED} | {"exposures": 0}
                  if row and row["box"] > 0 else RESET)
        sets = ", ".join(f"{k} = ?" for k in values)
        real.db.execute(f"UPDATE words SET {sets} WHERE lemma = ?",
                        (*values.values(), lemma))
    real.db.commit()
    print(f"rewritten; previous database kept as {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
