"""Portrait update written when a session ends.

A short call to the same model, made after the socket is gone, so a slow or
a failed summary costs the conversation nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import profile as profile_mod

log = logging.getLogger(__name__)

MAX_LINES = 5
MAX_TOKENS = 120
RECENT_TAG = "本次"
MAX_NOTES = 12

PROMPT = """下面是一段儿童英语陪练对话的记录，中文为主、夹英文。

用中文写不超过 {max_lines} 行，每行不超过 30 字，每行以标签开头：

兴趣: 孩子喜欢或反复提到的事物
错误: 孩子反复犯的错
已会: 孩子已经能说的
本次: 这次聊了什么（1-2 行）

只写对话里确实出现的；没有就只写一行“本次: 无”。
不要解释，不要编号，不要引号。

对话：
{transcript}
"""


def parse(text: str) -> tuple[list[str], list[str]]:
    """Split a reply into portrait notes and lines about this session.

    A line that does not carry a tag is kept as a note rather than dropped:
    the parent reads these, and an oddly phrased line is still something
    worth knowing about the child.
    """
    notes: list[str] = []
    recent: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line or line.startswith("#"):
            continue
        tag, sep, body = line.partition(":")
        if not sep:
            # No tag, but still something the model noticed.
            tag, body = "", line
        body = body.strip()
        if not body or body.startswith("无"):
            continue
        (recent if tag.strip() == RECENT_TAG else notes).append(body)
    return notes, recent


async def write(llm, store, path: Path, session_id: int) -> bool:
    """Summarise one session into the learner's portrait."""
    turns = store.session_turns(session_id)
    transcript = "\n".join(
        f"孩子：{t['learner_text'] or '（没听清）'}\n老师：{t['tutor_text'] or ''}"
        for t in turns)
    if not transcript.strip():
        return False

    messages = [{"role": "user", "content": PROMPT.format(
        max_lines=MAX_LINES, transcript=transcript)}]
    reply = "".join([d async for d in
                     llm.stream_chat(messages, max_tokens=MAX_TOKENS)])
    notes, recent = parse(reply)
    if not notes and not recent:
        log.info("digest: nothing worth recording for session %s", session_id)
        return False

    portrait = profile_mod.load(path)
    for note in notes:
        if note not in portrait.notes:
            portrait.notes.append(note)
    portrait.notes = portrait.notes[-MAX_NOTES:]
    if recent:
        portrait.recent = recent[-2:]
    profile_mod.save(path, portrait)
    return True
