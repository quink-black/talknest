"""Per-learner portrait (L3): a markdown file a parent can read and edit.

Two lists: what is worth remembering about the child, and what the last
sessions were about. Markdown rather than a database row so a parent can
open the file and correct what the tutor got wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

NOTES_HEADING = "## 画像"
RECENT_HEADING = "## 最近"


@dataclass
class Profile:
    notes: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)


def path_for(root: Path, learner: str) -> Path:
    return Path(root) / "learners" / learner / "profile.md"


def load(path: Path) -> Profile:
    """Read a portrait, tolerating hand edits and a missing file."""
    if not path.exists():
        return Profile()
    notes: list[str] = []
    recent: list[str] = []
    target = notes
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            target = recent if "最近" in stripped else notes
            continue
        if not stripped or stripped.startswith(("#", ">")):
            continue
        item = stripped.lstrip("-*").strip()
        if item:
            target.append(item)
    return Profile(notes=notes, recent=recent)


def save(path: Path, profile: Profile) -> None:
    """Write the portrait back, keeping the file readable."""
    lines = [
        "# 学习者画像", "",
        "> 家长可以直接改这个文件。画像每轮带进提示词；最近是上次会话的内容。",
        "", NOTES_HEADING, "",
    ]
    lines += [f"- {n}" for n in profile.notes] or ["- （还没有记录）"]
    lines += ["", RECENT_HEADING, ""]
    lines += [f"- {r}" for r in profile.recent] or ["- （还没有记录）"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
