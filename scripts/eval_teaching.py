#!/usr/bin/env python3
"""Measure whether the tutor follows the plan, and what the plan costs.

Four questions, in the order the design cares about them:

  1. does the tutor teach the words the plan asks for, and does it avoid
     presenting an already-known word as new
  2. how much of a kid-mode reply is English
  3. what the plan block costs in time to first token
  4. whether reasoning leaks into the reply

Only the LLM endpoint has to be running. Nothing is written anywhere unless
--write is given, and learner state goes to a temporary directory so a real
profile is never touched.

    ./venv/bin/python scripts/eval_teaching.py
    ./venv/bin/python scripts/eval_teaching.py --rounds 20
    ./venv/bin/python scripts/eval_teaching.py --write docs/bench/teaching-eval.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import re
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from orchestrator import main as orch  # noqa: E402
from orchestrator.config import load  # noqa: E402

# Child turns a seven-year-old plausibly says, in the order they might.
SCRIPT = [
    "你好", "你叫什么名字", "红色怎么说", "我喜欢猫", "猫用英语怎么说",
    "蓝色呢", "我喜欢狗", "兔子怎么说", "今天天气很好", "我累了",
    "苹果怎么说", "我们聊点别的吧", "再见",
]
# Seeded as already known: the tutor must not present these as new.
KNOWN = ["red", "blue"]
# A one-syllable Chinese turn the recogniser can produce instead of the
# word being drilled: what ASR wrote, the word in the plan, the English a
# tutor must not switch to, and the gloss the tutor used for the drill.
MISHEARINGS = [
    ("爱", "I", "love", "我"),
    ("热的", "red", "hot", "红色"),
    ("海", "hi", "sea", "你好"),
]
# Things children actually said in logged sessions that no unit lists. The
# tutor is expected to take them up rather than steer away from them.
TOPIC_TURNS = [
    "今天是中秋节", "这是我的魔法笔", "我想养一只恐龙", "我今天踢足球了",
    "我家有一只小狗", "我最喜欢吃火锅",
]
# Phrases a tutor uses when it turns a topic down.
REFUSALS = re.compile(r"还没学过|没学过|没有关系|没关系哦|不聊|先不聊|和.{0,6}英语.{0,6}没")
INVITATION = re.compile(r"跟我(?:读|说)")
QUESTION = re.compile(r"[?？]\s*$")
ENGLISH = re.compile(r"[a-zA-Z]")
CJK = re.compile(r"[一-鿿]")
FRAMES = [re.compile(r"英文是\s*([a-z]+)"), re.compile(r"([a-z]+)\s*的英文是")]


def english_share(text: str) -> float:
    """English letters over English letters plus Chinese characters."""
    letters = len(ENGLISH.findall(text))
    hanzi = len(CJK.findall(text))
    return letters / (letters + hanzi) if letters + hanzi else 0.0


def taught_as_new(reply: str, known) -> list[str]:
    """Known words the reply appears to introduce as if they were new."""
    found = []
    for pattern in FRAMES:
        for word in pattern.findall(reply.lower()):
            if word in known and word not in found:
                found.append(word)
    return found


def plain_messages(sess, text: str) -> list[dict]:
    """What the pipeline sent before any of this existed, for comparison."""
    prompt = orch.cfg["modes"][sess.mode]["system_prompt"]
    return ([{"role": "system", "content": prompt}]
            + sess.history[-orch.HISTORY_MESSAGES:]
            + [{"role": "user", "content": text}])


async def run(sess, utterances, with_plan: bool) -> dict:
    """Talk through the script and measure what came back."""
    asked, off_plan, leaked, shares, first, total = 0, [], 0, [], [], []
    for text in utterances:
        messages = (orch.build_messages(sess, text) if with_plan
                    else plain_messages(sess, text))
        plan = sess.plan if with_plan else orch.state_block.Plan()
        targets = list(plan.new) + list(plan.review)
        start = asyncio.get_event_loop().time()
        reply, ttft = "", None
        async for delta in orch.llm.stream_chat(messages):
            if ttft is None:
                ttft = (asyncio.get_event_loop().time() - start) * 1000
            reply += delta
        total.append((asyncio.get_event_loop().time() - start) * 1000)
        if ttft is not None:
            first.append(ttft)
        if orch.THINK_RE.search(reply):
            leaked += 1
        if targets and any(w in reply.lower() for w in targets):
            asked += 1
        off_plan += taught_as_new(reply, KNOWN)
        shares.append(english_share(reply))
        orch.record_turn(sess, text, [reply], interrupted=False)
        sess.history.append({"role": "user", "content": text})
        sess.history.append({"role": "assistant", "content": reply})
    return {
        "asked": asked, "off_plan": off_plan, "leaked": leaked,
        "share": round(statistics.median(shares), 3),
        "ttft": round(statistics.median(first), 1) if first else None,
        "total": round(statistics.median(total), 1) if total else None,
    }


async def run_mishearings(sess) -> dict:
    """Short Chinese turns that are really the drilled word misheard.

    Each case puts the word in the plan, has the tutor drill it, then has
    the child "say" the Chinese the recogniser is liable to write instead.
    A tutor that follows the plan takes it as an attempt at the word; one
    that does not teaches the English for the Chinese it just read.
    """
    hit, trapped, replies = 0, [], []
    for heard, word, trap, gloss in MISHEARINGS:
        block = orch.state_block.render(orch.state_block.Plan(new=[word]))
        messages = [{"role": "system", "content": sess.system_prompt()},
                    {"role": "user", "content": f"{block}\n\n{gloss}怎么说"},
                    {"role": "assistant", "content":
                     f"{gloss}的英文是 {word}。跟我读一遍：{word}！"},
                    {"role": "user", "content": f"{block}\n\n{heard}"}]
        reply = ""
        async for delta in orch.llm.stream_chat(messages):
            reply += delta
        low = reply.lower()
        if re.search(rf"\b{re.escape(word.lower())}\b", low):
            hit += 1
        if re.search(rf"\b{re.escape(trap.lower())}\b", low):
            trapped.append(f"{heard} -> {trap}")
        replies.append((heard, word, reply.strip()))
    return {"hit": hit, "trapped": trapped, "replies": replies}


async def run_topics(sess, plan_turn: bool) -> dict:
    """Topics the child brings up that the curriculum does not cover.

    Each turn starts from the same short opening exchange, so the tutor is
    mid-conversation rather than greeting. Measured: whether it took the
    topic up instead of refusing it, whether it taught exactly one English
    point, whether it ended by handing the turn back (a question or an
    invitation to repeat), and whether that point was recorded as a topic word.
    """
    engaged, one_point, asked_back, kept, woven, replies = 0, 0, 0, 0, 0, []
    opening = [{"role": "user", "content": "你好"},
               {"role": "assistant", "content": "你好呀，今天想聊点什么？"}]
    for text in TOPIC_TURNS:
        sess.history[:] = list(opening)
        sess.turn_count = orch.scheduler.PLAN_EVERY - 1 if plan_turn else 1
        messages = orch.build_messages(sess, text)
        targets = list(sess.plan.new) + list(sess.plan.review)
        reply = ""
        async for delta in orch.llm.stream_chat(messages):
            reply += delta
        reply = orch.THINK_RE.sub("", reply).strip()
        if not REFUSALS.search(reply):
            engaged += 1
        if len(orch.match.teaching_points(reply)) == 1:
            one_point += 1
        if QUESTION.search(reply) or INVITATION.search(reply[-30:]):
            asked_back += 1
        if targets and any(w in reply.lower() for w in targets):
            woven += 1
        before = set(sess.store.topic_words())
        orch.record_turn(sess, text, [reply], interrupted=False)
        if set(sess.store.topic_words()) - before:
            kept += 1
        replies.append((text, reply))
    return {"engaged": engaged, "one_point": one_point,
            "asked_back": asked_back, "kept": kept, "woven": woven,
            "replies": replies}


def report(cfg: dict, rounds: int, planned: dict, baseline: dict,
           mis: dict) -> str:
    profile = cfg["llm"]["profiles"][cfg["llm"]["active"]]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Teaching plan evaluation", "",
        f"- Date: {stamp}",
        f"- Host: {platform.node()} ({platform.machine()})",
        f"- Endpoint: {profile['base_url']}  model: {profile['model']}",
        f"- Rounds: {rounds}",
        f"- History window: {orch.HISTORY_MESSAGES} messages",
        f"- Block caps: new {orch.BLOCK_CAPS.new_items}, "
        f"review {orch.BLOCK_CAPS.review_items}, "
        f"known {orch.BLOCK_CAPS.known_items}",
        "",
        "## Following the plan", "",
        "| Metric | with plan block |",
        "|---|---|",
        f"| taught a planned word | {planned['asked']}/{rounds} |",
        f"| presented a known word as new | {len(planned['off_plan'])} |",
        "| reasoning leaked | " + str(planned["leaked"]) + " |",
        "| English share of reply (median) | " + str(planned["share"]) + " |",
        "",
        "## Misheard one-syllable turns", "",
        "| Metric | with plan block |",
        "|---|---|",
        f"| taken as the drilled word | {mis['hit']}/{len(MISHEARINGS)} |",
        f"| taught the Chinese instead | {len(mis['trapped'])} |",
        "",
        "| ASR wrote | drilled | tutor replied |",
        "|---|---|---|",
    ]
    for heard, word, reply in mis["replies"]:
        lines.append(f"| {heard} | {word} | {' '.join(reply.split())[:120]} |")
    lines += [
        "## Latency", "",
        "| | TTFT median | Total median |",
        "|---|---|---|",
        f"| baseline (no plan block) | {baseline['ttft']} ms | "
        f"{baseline['total']} ms |",
        f"| with plan block | {planned['ttft']} ms | {planned['total']} ms |",
    ]
    if planned["ttft"] is not None and baseline["ttft"] is not None:
        delta = round(planned["ttft"] - baseline["ttft"], 1)
        lines.append(f"| delta | {delta:+} ms | "
                     f"{round(planned['total'] - baseline['total'], 1):+} ms |")
    if planned["off_plan"]:
        lines += ["", "Known words presented as new: "
                  + ", ".join(sorted(set(planned["off_plan"])))]
    return "\n".join(lines) + "\n"


async def main_async(args) -> int:
    tmp = tempfile.mkdtemp(prefix="talknest-eval-")
    os.environ["TALKNEST_DATA_DIR"] = tmp

    utterances = (SCRIPT * (args.rounds // len(SCRIPT) + 1))[: args.rounds]
    planned_session = orch.Session()
    planned_session.open_learner("eval")
    for word in KNOWN:
        planned_session.store.set_box(word, orch.MASTERY_BOX)
    try:
        planned = await run(planned_session, utterances, with_plan=True)
        mis = await run_mishearings(planned_session)
        topics = await run_topics(planned_session, plan_turn=False)
        woven = await run_topics(planned_session, plan_turn=True)
        baseline_session = orch.Session()
        baseline_session.open_learner("eval-baseline")
        baseline = await run(baseline_session, utterances, with_plan=False)
    except Exception as e:
        print(f"no answer from the LLM endpoint: {e}", file=sys.stderr)
        print("start it first, e.g. server/services/start_llm_llamacpp_9b.sh",
              file=sys.stderr)
        return 1

    print(f"taught a planned word:            {planned['asked']}/{len(utterances)}")
    print(f"presented a known word as new:    {len(planned['off_plan'])}")
    print(f"reasoning leaked:                 {planned['leaked']}")
    print(f"English share (median):           {planned['share']}")
    print(f"TTFT baseline / with block:       {baseline['ttft']} / "
          f"{planned['ttft']} ms")
    print(f"total baseline / with block:      {baseline['total']} / "
          f"{planned['total']} ms")
    print(f"misheard taken as drilled:        {mis['hit']}/{len(MISHEARINGS)}")
    print(f"misheard taught as Chinese:       {len(mis['trapped'])}"
          + (": " + ", ".join(mis["trapped"]) if mis["trapped"] else ""))
    for heard, word, reply in mis["replies"]:
        print(f"  [{heard} / {word}] {' '.join(reply.split())[:110]}")

    n = len(TOPIC_TURNS)
    for label, result in (("topic turn", topics), ("plan turn", woven)):
        print(f"{label}: taken up, not refused:   {result['engaged']}/{n}")
        print(f"{label}: exactly one point:       {result['one_point']}/{n}")
        print(f"{label}: turn handed back:        {result['asked_back']}/{n}")
        print(f"{label}: kept as a topic word:    {result['kept']}/{n}")
        print(f"{label}: used a plan word:        {result['woven']}/{n}")
        for text, reply in result["replies"]:
            print(f"  [{text}] {' '.join(reply.split())[:150]}")

    text = report(load(), len(utterances), planned, baseline, mis)
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
        print(f"wrote {args.write}")
    planned_session.close()
    baseline_session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the teaching plan.")
    parser.add_argument("--rounds", type=int, default=len(SCRIPT),
                        help=f"turns to run (default {len(SCRIPT)})")
    parser.add_argument("--write", help="write a markdown report to this path")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
