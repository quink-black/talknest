"""Render the per-turn teaching block.

The block is prepended to the child's own words in the last user message and
is never written to the conversation history: history has to stay a stable
prefix for llama.cpp to reuse its KV cache slot, and a stack of stale plans
in history would also tell the model to teach words it already taught.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Caps:
    """Per-section item limits; field names match config.yaml teaching.block."""

    new_items: int = 2
    review_items: int = 5
    known_items: int = 20
    portrait_lines: int = 3
    digest_lines: int = 2
    summary_chars: int = 140
    line_chars: int = 30


@dataclass(frozen=True)
class Plan:
    unit: str = ""
    new: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    portrait: list[str] = field(default_factory=list)
    digest: list[str] = field(default_factory=list)
    # What the older part of this conversation was about, once it has been
    # folded out of the window.
    summary: str = ""
    # The lesson's theme, so the tutor knows what the words are for.
    title: str = ""
    pattern: str = ""
    # Conversation starters, only while the conversation has not begun.
    topics: list[str] = field(default_factory=list)
    # Where a word came from, for words that arose from the child's own talk.
    notes: dict[str, str] = field(default_factory=dict)
    # The one thing the tutor is suggested to do this turn.
    hint: str = ""


def render(plan: Plan, caps: Caps = Caps()) -> str:
    """Render the block, or "" when there is nothing worth saying.

    One line on purpose: the block sits inside the child's own message, so
    anything that looks like separate lines reads as something the child
    said.
    """
    parts = []
    if plan.title:
        parts.append("单元: " + plan.title)
    if plan.pattern:
        parts.append("句型: " + plan.pattern)
    if plan.topics:
        parts.append("话题: " + "、".join(plan.topics))
    if plan.review:
        parts.append("复习: " + ", ".join(
            _labelled(w, plan.notes) for w in plan.review[: caps.review_items]))
    if plan.new:
        label = f"新授({plan.unit}): " if plan.unit else "新授: "
        parts.append(label + ", ".join(plan.new[: caps.new_items]))
    if plan.known:
        parts.append("已会: " + ", ".join(plan.known[: caps.known_items]))
    if plan.portrait:
        lines = [_clip(x, caps.line_chars) for x in plan.portrait[: caps.portrait_lines]]
        parts.append("画像: " + "；".join(lines))
    if plan.digest:
        lines = [_clip(x, caps.line_chars) for x in plan.digest[: caps.digest_lines]]
        parts.append("上次: " + "；".join(lines))
    if plan.summary:
        parts.append("前文: " + _clip(plan.summary, caps.summary_chars))
    if plan.hint:
        parts.append("本轮: " + plan.hint)
    if not parts:
        return ""
    return "【" + " ｜ ".join(parts) + "】"


def _labelled(word: str, notes: dict[str, str]) -> str:
    note = notes.get(word)
    return f"{word}({_clip(note, 10)})" if note else word


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
