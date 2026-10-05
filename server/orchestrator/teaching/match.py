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


# "跟我读一遍：moon！" -- the tutor asking the child to say something back.
# The English after the colon is what the child is expected to say; it may
# be a phrase. The lead-in is required so that an English word after "说："
# in a story ("妈妈说：hello") is not taken for a request.
_INVITE = re.compile(
    r"(?:(?:跟着?我|你来|再|一起|来|试着|请|试试)(?:再)?(?:读|说|念)"
    r"(?:一遍|一下|一次)?|再试一次|试一次)"
    r"\s*[：:]\s*[\"“'‘]?([A-Za-z][A-Za-z' ]*)")
_CLOSERS = re.compile(r"[\"”'’)）]*[！!。.？?]?")


def invitation(text: str) -> str | None:
    """The English the tutor asked the child to repeat, lowercase."""
    m = _INVITE.search(text or "")
    return m.group(1).strip().lower() if m else None


def cut_after_invitation(text: str) -> str | None:
    """text up to the end of the sentence that asks the child to repeat.

    None while there is nothing to cut: no invitation yet, or the invited
    phrase has not finished arriving. A request to repeat is where the tutor
    has to stop talking so the child can answer, so whatever the model wrote
    after it is dropped.
    """
    m = _INVITE.search(text or "")
    if m is None or m.end() >= len(text):
        return None
    return text[: _CLOSERS.match(text, m.end()).end()]


def said(heard: str, phrase: str) -> bool:
    """Whether every word of phrase is in heard (plurals and case ignored)."""
    want = [_stem(t) for t in _TOKEN.findall((phrase or "").lower())]
    have = {_stem(t) for t in _TOKEN.findall((heard or "").lower())}
    return bool(want) and all(w in have for w in want)


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
