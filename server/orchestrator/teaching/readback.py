"""Feedback on a word the tutor asked the child to say back.

The tutor invites ("跟我读一遍：moon！") and then has to stop and listen. What
the child does next is compared with the invited English here, without a
model, and the result goes into the next plan block so the reply gives
feedback that fits: praise for a good attempt, a demonstration for a
miss, a graceful exit after repeated misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import match

# After this many misses on one phrase the tutor stops asking for it again.
MAX_TRIES = 2
_ENGLISH = re.compile(r"[A-Za-z]{2,}")
_HANZI = re.compile(r"[一-鿿]")
# One or two Chinese syllables with no English is how the recogniser writes
# an English word it could not place.
SHORT_HANZI = 4


@dataclass(frozen=True)
class Verdict:
    kind: str      # ok | retry | unclear | giveup | other
    line: str      # what the plan block states as fact
    hint: str      # what the tutor is suggested to do
    failed: bool   # counts toward MAX_TRIES


def judge(expected: str, heard: str, tries: int) -> Verdict:
    """Compare what the child said with the phrase they were asked to say.

    tries is how many times this phrase was already missed.
    """
    heard = (heard or "").strip()
    if match.said(heard, expected):
        return Verdict(
            "ok", f"{expected} → 读对了",
            f"孩子读对了 {expected}：用一句具体的话认可，说出他读得好的地方，"
            "不要只说“真棒”；再用这个词问一个孩子答得出的小问题，或者接着"
            "聊话题。这一轮不要再请孩子重读，也不要教新词", False)
    english = _ENGLISH.search(heard)
    chinese = len(_HANZI.findall(heard))
    if not english and chinese > SHORT_HANZI:
        return Verdict(
            "other", f"{expected} → 孩子没有读，说了别的",
            f"孩子没有读 {expected}，而是说了别的：先回应他说的内容，不要责备，"
            "也不要重复邀请；这一轮不教新词", False)
    if tries + 1 >= MAX_TRIES:
        return Verdict(
            "giveup", f"{expected} → 读了 {tries + 1} 次还没读准",
            f"孩子已经试了几次 {expected}：夸他肯坚持，说这个词以后还会再练，"
            "不要再请他重读，接着聊话题", True)
    if english:
        return Verdict(
            "retry", f"{expected} → 孩子读的是：{heard[:20]}",
            f"孩子读的和 {expected} 不一样：先肯定他愿意开口，再示范一遍 "
            f"{expected} 并说出差在哪，最后请他再读一遍，让这句请求作为回复的"
            "最后一句", True)
    return Verdict(
        "unclear", f"{expected} → 没听清",
        f"没听清孩子说了什么，可能是读得太轻：说一句“我没听清”，示范一遍 "
        f"{expected}，再请他读一遍，让这句请求作为回复的最后一句", True)
