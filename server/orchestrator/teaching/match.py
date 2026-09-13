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
