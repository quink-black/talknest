"""Deterministic word matching for progress bookkeeping.

No model is involved: a word counts when it appears in what the tutor said
or in what the learner said. That proves the word came up and nothing more.
Whether it is known is the parent's call.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z][a-z']*")


def find(text: str, words) -> list[str]:
    """Curriculum words present in text, in curriculum order."""
    if not text or not words:
        return []
    tokens = {_stem(t) for t in _TOKEN.findall(text.lower())}
    return [w for w in words if _stem(str(w).lower()) in tokens]


# The kid persona teaches a word as "X的英文是 word" and invites the child to
# "跟我读一遍：word". Both are the tutor saying what it means to teach, which
# is a far better signal than any English word that happens to be in a reply.
_POINT = re.compile(
    r"(?:跟我读一遍|跟我读|跟我说|读一遍)\s*[：:]\s*[\"“'‘]?([A-Za-z][A-Za-z' ]*)"
    r"|的英文(?:是|可以说|叫|说)\s*[\"“'‘]?([A-Za-z][A-Za-z' ]*)")


def teaching_points(text: str) -> list[str]:
    """Single English words the tutor taught in text, in order of appearance.

    Phrases are left out: progress is kept per word.
    """
    out = []
    for first, second in _POINT.findall(text or ""):
        phrase = (first or second).strip().lower()
        if " " not in phrase and len(phrase) >= 2 and phrase not in out:
            out.append(phrase)
    return out


def _stem(word: str) -> str:
    """Trim plural and third-person endings.

    ASR adds and drops them freely, and a child saying "apples" has
    produced "apple".
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word
