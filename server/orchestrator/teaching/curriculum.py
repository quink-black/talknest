"""Curriculum loading (L0).

Units are authored in server/curriculum/<mode>.yaml. Loading validates the
unit graph and returns the units in topological order, so "the first unit
that still has untaught words" is also the first unit whose prerequisites
are satisfied.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CURRICULUM_DIR = Path(__file__).resolve().parents[2] / "curriculum"


class CurriculumError(Exception):
    """Raised when a curriculum file is unusable."""


@dataclass(frozen=True)
class Unit:
    id: str
    title: str
    goal: str
    words: tuple[str, ...]
    patterns: tuple[str, ...]
    after: tuple[str, ...]
    sessions: int
    # Taught, but not counted automatically. Function words such as "I" turn
    # up in almost any English sentence, so matching them would report the
    # child as having produced words they never said.
    untracked: tuple[str, ...] = ()


def load(mode: str, lexicon: set[str] | None = None,
         directory: Path = CURRICULUM_DIR) -> list[Unit]:
    """Load and validate the curriculum of one mode.

    lexicon is the set of known lemmas from the imported word list. Words
    outside it are rejected so a typo in the YAML cannot silently create an
    item that no utterance will ever match. The check is skipped when no
    lexicon is present: importing one is optional.
    """
    path = directory / f"{mode}.yaml"
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    declared = doc.get("mode")
    if declared and declared != mode:
        raise CurriculumError(f"{path}: mode is {declared!r}, expected {mode!r}")

    units = [_unit(raw, path) for raw in doc.get("units") or []]
    _validate(units, path, lexicon)
    return _topological(units, path)


def load_lexicon(path: Path) -> set[str] | None:
    """Lemmas of the imported word list, or None when there is none.

    None means the importer has not run, which skips validation rather than
    rejecting every word in every curriculum.
    """
    if not path.exists():
        return None
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            words.add(json.loads(line)["lemma"])
    return words or None


def _unit(raw: dict, path: Path) -> Unit:
    if not raw.get("id"):
        raise CurriculumError(f"{path}: unit without id")
    if not raw.get("words"):
        raise CurriculumError(f"{path}: unit {raw['id']!r} has no words")
    return Unit(
        id=str(raw["id"]),
        title=str(raw.get("title", raw["id"])),
        goal=str(raw.get("goal", "")),
        words=_words(raw, "words", path),
        patterns=tuple(str(p) for p in raw.get("patterns") or ()),
        after=tuple(str(a) for a in raw.get("after") or ()),
        sessions=int(raw.get("sessions", 1)),
        untracked=_words(raw, "untracked", path),
    )


def _words(raw: dict, key: str, path: Path) -> tuple[str, ...]:
    """Word list, rejecting anything YAML read as a different type.

    A bare on/yes/no is a boolean to YAML, and str()-ing it would quietly
    put "true" into the vocabulary instead of the word.
    """
    out = []
    for value in raw.get(key) or ():
        if not isinstance(value, str):
            raise CurriculumError(
                f"{path}: {key} has {value!r}, which YAML read as a "
                f"{type(value).__name__}; quote it")
        out.append(value.strip().lower())
    return tuple(out)


def _validate(units: list[Unit], path: Path, lexicon: set[str] | None) -> None:
    ids = [u.id for u in units]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CurriculumError(f"{path}: duplicate unit ids {dupes}")
    known = set(ids)
    for unit in units:
        for dep in unit.after:
            if dep not in known:
                raise CurriculumError(
                    f"{path}: unit {unit.id!r} depends on unknown unit {dep!r}")
    if lexicon is None:
        log.warning("%s: no lexicon imported, skipping word validation", path)
        return
    missing = sorted({w for u in units for w in u.words if w not in lexicon})
    if missing:
        raise CurriculumError(f"{path}: words missing from lexicon: {missing}")


def _topological(units: list[Unit], path: Path) -> list[Unit]:
    """Kahn's algorithm; ties keep file order so unit order is predictable."""
    remaining = {u.id: u for u in units}
    done: set[str] = set()
    out: list[Unit] = []
    while remaining:
        ready = [u for u in remaining.values() if set(u.after) <= done]
        if not ready:
            raise CurriculumError(f"{path}: cycle among units {sorted(remaining)}")
        for unit in ready:
            out.append(unit)
            done.add(unit.id)
            del remaining[unit.id]
    return out
