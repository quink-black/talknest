#!/usr/bin/env python3
"""Import the word list behind the curricula (L1).

Downloads CC-CEDICT, matches it against every word the curricula teach and
writes data/curriculum/lexicon.jsonl: one line per word, with its Chinese
gloss and pinyin. Everything lands in data/, which is not in the
repository, so nothing downloaded here leaves the machine.

    ./venv/bin/python scripts/import_curriculum.py
    ./venv/bin/python scripts/import_curriculum.py --force   # download again
    ./venv/bin/python scripts/import_curriculum.py --pin     # print hashes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from orchestrator.config import data_root  # noqa: E402

CURRICULUM = REPO / "server" / "curriculum"
SOURCES = CURRICULUM / "sources.yaml"

# 苹果 苹果 [ping2 guo3] /apple/
ENTRY = re.compile(r"^\S+\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/\s*$")
PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def read_words() -> list[str]:
    """Every word the curricula teach, first-seen order."""
    words: list[str] = []
    for path in sorted(CURRICULUM.glob("*.yaml")):
        if path.name == SOURCES.name:
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for unit in doc.get("units") or []:
            for word in unit.get("words") or []:
                words.append(str(word).strip().lower())
    return list(dict.fromkeys(words))


def fetch(source: dict, cache: Path, force: bool) -> tuple[Path, str]:
    """Download a source once into the cache and hash it."""
    target = cache / (source["name"] + Path(source["url"]).suffix)
    if force or not target.exists():
        request = urllib.request.Request(
            source["url"], headers={"User-Agent": "talknest/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(payload)
        part.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    pinned = source.get("sha256")
    if pinned and digest != pinned:
        raise SystemExit(
            f"{source['name']}: sha256 {digest} does not match the pinned "
            f"{pinned}. Delete {target} and re-run if the change is expected.")
    return target, digest


def read_member(path: Path, source: dict) -> str:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        member = source.get("member") or names[0]
        if member not in names:
            raise SystemExit(f"{path}: no member {member}, found {names}")
        return archive.read(member).decode("utf-8")


def norm(gloss: str) -> str:
    """Strip the annotations CC-CEDICT wraps around its English glosses."""
    text = gloss.split(";")[0]
    text = PARENS.sub(" ", text)
    text = re.sub(r"[^a-z' -]", " ", text.lower())
    text = text.strip()
    if text.startswith("to "):
        text = text[3:]
    return " ".join(text.split())


def index_cedict(text: str):
    """Map English glosses to entries.

    Two indexes: the whole gloss for exact words such as "apple", and the
    first word of a gloss so "run" also finds "run away". Several entries
    can share a gloss, so each key keeps them all and lookup picks one.
    """
    by_gloss: dict[str, list] = {}
    by_first: dict[str, list] = {}
    pinyin_of: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        matched = ENTRY.match(line)
        if not matched:
            continue
        simplified, pinyin, glosses = matched.groups()
        pinyin_of.setdefault(simplified, pinyin.strip().lower())
        parts = [g for g in glosses.split("/") if g.strip()]
        for raw in parts:
            key = norm(raw)
            if not key:
                continue
            entry = (simplified, pinyin.strip().lower(),
                     raw.strip(), len(parts))
            by_gloss.setdefault(key, []).append(entry)
            by_first.setdefault(key.split()[0], []).append(entry)
    return by_gloss, by_first, pinyin_of


def quality(entry: tuple) -> tuple:
    """Lower is better.

    A bare lowercase gloss beats a qualified one: "apple" is the fruit,
    while "Apple (American tech company)" is not. Fewer senses in the entry
    and a shorter Chinese form break what is left.
    """
    raw = entry[2]
    qualified = 1 if ("(" in raw or any(c.isupper() for c in raw)) else 0
    return (qualified, entry[3], len(entry[0]))


def lookup(word: str, by_gloss: dict, by_first: dict) -> tuple | None:
    candidates = by_gloss.get(word) or by_first.get(word)
    return min(candidates, key=quality) if candidates else None


def read_overrides() -> dict:
    """Hand corrections for glosses the dictionary gets wrong.

    The dictionary has the right word and the wrong sense often enough
    (a brand, a rare synonym, the wrong kind of grandma) that a few picks
    have to be named. They live in the repository so they can be reviewed.
    """
    path = CURRICULUM / "glosses.yaml"
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fixes = {}
    for lemma, fix in doc.items():
        key = str(lemma).strip().lower()
        if isinstance(fix, str):
            fixes[key] = {"zh": fix}
        elif isinstance(fix, dict):
            fixes[key] = {k: v for k, v in fix.items()
                          if k in ("zh", "pinyin", "gloss")}
    return fixes


def write_licenses(out_dir: Path, sources: list, digests: dict) -> None:
    lines = ["# Word list sources", "",
             "Written by scripts/import_curriculum.py. These files live in",
             "data/, which is not in the repository, so nothing listed here",
             "is redistributed with the code. Personal study use only.", ""]
    for source in sources:
        lines += [
            f"## {source['name']}", "",
            f"- Publisher: {source.get('publisher', '')}",
            f"- URL: {source['url']}",
            f"- Licence: {source['license']} ({source['license_url']})",
            f"- Used for: {source['use']}",
            f"- sha256: {digests.get(source['name'], '')}", "",
        ]
    (out_dir / "LICENSES.md").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the word list (L1).")
    parser.add_argument("--force", action="store_true",
                        help="download again even if a copy is cached")
    parser.add_argument("--pin", action="store_true",
                        help="print the hash of what was downloaded")
    args = parser.parse_args()

    sources = (yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
               or {}).get("sources") or []
    if not sources:
        raise SystemExit(f"{SOURCES}: no sources listed")

    words = read_words()
    if not words:
        raise SystemExit("no curriculum words found")

    out_dir = data_root() / "curriculum"
    cache = out_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    digests, by_gloss, by_first, pinyin_of = {}, {}, {}, {}
    for source in sources:
        path, digest = fetch(source, cache, args.force)
        digests[source["name"]] = digest
        by_gloss, by_first, pinyin_of = index_cedict(read_member(path, source))

    overrides = read_overrides()
    rows, unmatched = [], []
    for word in words:
        found = lookup(word, by_gloss, by_first)
        row = {
            "lemma": word,
            "zh": found[0] if found else None,
            "pinyin": found[1] if found else None,
            "gloss": found[2] if found else None,
            "in_dictionary": found is not None,
        }
        # A dictionary picks the right word and the wrong sense often enough
        # -- "apple" is a phone company before it is a fruit -- so the
        # glosses the parent actually reads are written by hand.
        if word in overrides:
            row.update(overrides[word])
            # Pinyin belongs to the Chinese form that was settled on, not to
            # whatever sense the dictionary matched the English word to.
            row["pinyin"] = pinyin_of.get(row["zh"], row.get("pinyin"))
        rows.append(row)
        if not row["zh"]:
            unmatched.append(word)

    lexicon = out_dir / "lexicon.jsonl"
    with lexicon.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_licenses(out_dir, sources, digests)

    print(f"{lexicon}: {len(rows) - len(unmatched)}/{len(rows)} words glossed")
    if unmatched:
        print("no gloss yet for: " + ", ".join(unmatched))
        print(f"add them to {CURRICULUM / 'glosses.yaml'} and re-run")
    if args.pin:
        for name, digest in digests.items():
            print(f"{name} sha256: {digest}")


if __name__ == "__main__":
    main()
