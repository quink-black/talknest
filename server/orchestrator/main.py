"""Talknest orchestrator: WebSocket free-talk voice pipeline.

Flow: the client streams microphone PCM continuously, a streaming VAD cuts
it into turns, and each turn runs ASR -> LLM stream -> sentence split ->
TTS -> PCM frames back to the client. Nobody signals the start or end of a
sentence: speaking over the assistant cuts it off (barge-in) and starts the
next turn.

The LLM backend (llama.cpp or vLLM) is whatever llm.active points at in
config.yaml — this file has no backend branches.
"""

import asyncio
import contextlib
import json
import logging
import pathlib
import re
import time
from dataclasses import replace

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from .asr import ASRClient, DEFAULT_LANGUAGES
from .config import data_root, load
from .llm import LLMClient
from .teaching import (curriculum, digest, match, profile, readback,
                       scheduler, state_block)
from .teaching.store import MASTERY_BOX, Store
from .tts import TTSError, TTSClient
from .vad import SileroVAD, StreamVAD

cfg = load()
app = FastAPI(title="talknest-orchestrator")
log = logging.getLogger(__name__)

llm = LLMClient(
    cfg["llm"]["profiles"][cfg["llm"]["active"]]["base_url"],
    cfg["llm"]["profiles"][cfg["llm"]["active"]]["model"],
    cfg["llm"]["profiles"][cfg["llm"]["active"]].get("api_key", ""),
    cfg["llm"].get("sampling", {}),
)
asr = ASRClient(cfg["asr"]["base_url"], cfg["asr"]["model"],
                cfg["asr"].get("languages", DEFAULT_LANGUAGES))
tts = TTSClient(cfg["tts"]["base_url"], cfg["tts"]["model"], cfg["tts"]["voice"],
                stream=bool(cfg["tts"].get("stream", True)))

# Chunks shorter than this merge into the next one: one TTS round trip per
# two-word fragment costs more than it saves. It has to stay low enough
# that an ordinary kid-mode sentence ("兔子的英文是 rabbit。", 14 chars)
# goes out on its own instead of waiting for the sentence after it.
SENTENCE_MIN_CHARS = int(cfg.get("reply", {}).get("chunk_min_chars", 10))
CLIENT_SR = 24000  # browser captures at 24 kHz: Qwen3-ASR's native rate
# (16 kHz input makes Qwen3-ASR fail; 24 kHz matches the Qwen3-Omni
# audio frontend. VAD runs on an internal 16 kHz copy.)
# Strip any reasoning that leaks despite non-thinking mode.
THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)
SENT_END = re.compile(r"[.!?…。！？；;]|(?:\n)")
# Markdown symbols the LLM may emit (bold, italics, code ticks); the TTS
# reads them literally or mispronounces the surrounding word.
MD_SYMBOLS_RE = re.compile(r"[*_`#>~\[\]]+")

VAD_CFG = cfg["vad"]
BARGE_IN_MS = VAD_CFG.get("barge_in_ms", 400)
BARGE_IN_GRACE_MS = VAD_CFG.get("playback_grace_ms", 300)
BARGE_IN_THRESHOLD = VAD_CFG.get("barge_in_threshold", 0.75)
# Interrupting a turn that has produced no audio yet needs a longer voiced
# run than interrupting one that is playing: the assistant is silent, so
# what the mic hears is a real question, but a child who is only waiting
# says things like "喂?" that must not throw the answer away.
PENDING_BARGE_IN_MS = VAD_CFG.get("pending_barge_in_ms", 1200)

# Fallback chain: configured default, else the first configured mode.
DEFAULT_MODE = cfg.get("default_mode", "")
if DEFAULT_MODE not in cfg["modes"]:
    DEFAULT_MODE = next(iter(cfg["modes"]))

TEACHING_CFG = cfg.get("teaching", {})
TEACHING = bool(TEACHING_CFG.get("enabled"))
BLOCK_CAPS = state_block.Caps(**TEACHING_CFG.get("block", {}))
# off until a host has the room for a second call; on_disconnect runs after
# the socket is gone, so it never delays a reply.
DIGEST_MODE = str(TEACHING_CFG.get("digest", "off"))
COMPACT_MESSAGES = int(TEACHING_CFG.get("compact_messages", 8))
NEW_PER_DAY = int(TEACHING_CFG.get("new_per_day", 5))
# Seconds of silence after a request to repeat before the tutor gently
# prompts once. 0 turns the prompt off.
READ_WAIT_S = float(TEACHING_CFG.get("read_wait_seconds", 8))
# Said once when the child has not answered a request to repeat. The word is
# repeated so the child need not remember it; the line is not a new lesson.
NUDGE_LINES = (
    "不着急，准备好了就跟我读：{w}。",
    "慢慢来，我再说一遍：{w}。你来试试？",
)
# Topic words kept from one reply: the tutor is meant to teach one point a
# turn, so more than this is a misparse, not a lesson.
TOPIC_WORDS_PER_TURN = 2
DEFAULT_LEARNER = str(TEACHING_CFG.get("default_learner", "default"))
HISTORY_MESSAGES = int(
    cfg["llm"]["profiles"][cfg["llm"]["active"]].get("history_messages", 20))

# Appended to a mode's system prompt when it sets english_ratio. A model
# cannot count its own output, so the number is restated as a rule it can
# follow sentence by sentence; the per-turn rules already in the prompt do
# the real work. Modes without the key (adult) get nothing appended.
ENGLISH_RATIO_RULE = (
    "English letters must stay under {pct}% of the reply's English letters "
    "plus Chinese characters. If what you are about to write would go over, "
    "say it in Chinese instead."
)

# How to read the plan block. These rules sit in the system prompt because
# they never change, while the block itself carries the words and is rebuilt
# every turn: llama.cpp can only reuse the cached prefix up to the first
# token that differs, so everything volatile goes into the last message.
TEACHING_RULES = (
    "The text in brackets at the start of the child's message is your "
    "teaching plan and a record of what the child has met. It is not "
    "something the child said, so never repeat it and never refer to it. "
    "Introduce only the words listed under 新授. Words under 已会 are "
    "already known: use them freely, but never present them as new. "
    "Words under 复习 are due for another look: bring them back inside "
    "the conversation instead of drilling them. 单元, 句型 and 话题 name "
    "the lesson's theme, a sentence frame and conversation starters, and "
    "本轮 is the one thing suggested for this turn. 跟读 states how the "
    "child did on the word you just asked them to say back; answer that "
    "first. When the block lists "
    "no new words, there is nothing to introduce: carry on with the "
    "child's own topic or ask what they would like to talk about."
)

web_dir = str(pathlib.Path(__file__).resolve().parent.parent.parent / "web")
# Mounted after the /ws route is registered (see bottom of this file) so
# the catch-all static mount cannot shadow it.


def now_ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


async def send_json(ws: WebSocket, obj: dict):
    if ws.client_state == WebSocketState.CONNECTED:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))


def extract_sentences(text: str, start: int):
    """Split text[start:] into TTS-sized chunks at sentence punctuation.

    A chunk is emitted once it reaches SENTENCE_MIN_CHARS; a run of short
    sentences merges into the following chunk instead of being emitted
    (or, at end of stream, flushed whole by the caller). Returns
    (chunks, new_start).
    """
    out = []
    pos = start
    m = SENT_END.search(text, pos)
    while m:
        if m.end() - start >= SENTENCE_MIN_CHARS:
            s = text[start : m.end()].strip()
            start = m.end()
            if s:
                out.append(s)
        pos = m.end()
        m = SENT_END.search(text, pos)
    return out, start


def barge_in_due(speaking: bool, busy: bool, active: bool, barged: bool,
                 speech_ms: float, played_ms: float) -> bool:
    """Whether voiced speech is a child cutting in rather than echo.

    Assistant audio reaches the mic even with echo cancellation on, so
    speech heard while playing must both outlast the playback lead-in and
    run long enough to be a real interruption.

    A turn that is still being produced makes no sound, so there is no
    echo to explain away -- but there is also no playback clock, and
    throwing the answer away on a short noise costs the whole turn. Such
    an interruption therefore has to be a sustained run of speech.
    """
    if not (active and not barged):
        return False
    if speaking:
        return played_ms >= BARGE_IN_GRACE_MS and speech_ms >= BARGE_IN_MS
    if busy:
        return speech_ms >= PENDING_BARGE_IN_MS
    return False


class Session:
    def __init__(self):
        self.mode = DEFAULT_MODE
        self.history: list[dict] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.streaming = False
        # Silero keeps LSTM state, so each session owns its own VAD.
        self.svad = StreamVAD(
            SileroVAD(threshold=VAD_CFG.get("threshold", 0.5)),
            pre_roll_ms=VAD_CFG.get("pre_roll_ms", 300),
            silence_ms=VAD_CFG.get("silence_ms", 700),
            start_ms=VAD_CFG.get("start_ms", 120),
            min_speech_ms=VAD_CFG.get("min_speech_ms", 300),
            max_speech_ms=VAD_CFG.get("max_speech_ms", 20000),
            client_sr=CLIENT_SR,
        )
        self.vad_task: asyncio.Task | None = None
        self.turn_task: asyncio.Task | None = None
        self.cancel: asyncio.Event | None = None
        self.speaking = False      # audio is on its way to the client
        self.speaking_since = 0.0
        # Spans the whole turn, from VAD close to the last audio frame.
        # Unlike `speaking` it covers the silent ASR -> LLM -> TTS stretch,
        # which is the part where an impatient re-ask used to cancel the
        # answer before it had any chance to be heard.
        self.busy = False
        # Both describe the turn the VAD is collecting right now.
        self.turn_echo = False     # opened while the assistant was occupied
        self.turn_barged = False   # confirmed as a real interruption
        self.learner = DEFAULT_LEARNER
        self.store: Store | None = None
        self.units: list = []
        self.vocab: set[str] = set()
        self.tracked: set[str] = set()
        self.plan = state_block.Plan()
        self.portrait = profile.Profile()
        self.summary = ""
        self.compacting = False
        self.session_id: int | None = None
        # Child turns so far in this conversation, which the plan uses to
        # alternate following the child's topic with working in plan words.
        self.turn_count = 0
        # The English the tutor last asked the child to repeat, how many
        # times they have missed it, and whether the silence prompt was
        # already used for it.
        self.awaiting = ""
        self.read_tries = 0
        self.nudged = False
        self.read_target = ""      # what the last judged turn was judged on
        self.read_verdict = ""
        self.nudge_task: asyncio.Task | None = None
        if TEACHING:
            self.open_learner(DEFAULT_LEARNER)

    def open_learner(self, learner: str):
        """Point this session at a learner: their own store, fresh talk."""
        if self.store is not None:
            self.store.close()
        self.learner = learner
        self.history.clear()
        self.store = Store(learner, data_root())
        self.portrait = profile.load(profile.path_for(data_root(), learner))
        self.load_curriculum()

    def load_curriculum(self):
        """Units and vocabulary of the current mode; no-op without one."""
        if self.store is None:
            return
        name = cfg["modes"][self.mode].get("curriculum")
        # Without a lexicon the words are not checked, so a curriculum still
        # loads on a machine that has never run the importer.
        lexicon = curriculum.load_lexicon(
            data_root() / "curriculum" / "lexicon.jsonl")
        self.units = curriculum.load(name, lexicon) if name else []
        self.vocab = {w for u in self.units for w in u.words}
        self.tracked = self.vocab - {w for u in self.units for w in u.untracked}
        self.store.sync_units(self.units)
        # Session start is when words that have not come up for a while drop
        # back a box, so they start turning up in the review queue again.
        self.store.demote_stale()
        self.session_id = self.store.start_session(self.mode)
        self.turn_count = 0
        self.awaiting, self.read_tries, self.nudged = "", 0, False

    def close(self):
        """End the session row and release the store."""
        if self.store is None:
            return
        if self.session_id is not None:
            self.store.end_session(self.session_id)
            self.session_id = None
        self.store.close()
        self.store = None

    def system_prompt(self) -> str:
        m = cfg["modes"][self.mode]
        text = m["system_prompt"]
        ratio = m.get("english_ratio")
        if ratio is not None:
            text += " " + ENGLISH_RATIO_RULE.format(pct=round(float(ratio) * 100))
        if self.units:
            text += " " + TEACHING_RULES
        return " ".join(text.split())


session_by_ws: dict[WebSocket, Session] = {}


def tts_params(sess: Session) -> dict:
    """Per-mode TTS overrides, falling back to the global tts section."""
    m = cfg["modes"][sess.mode]
    return {
        "voice": m.get("tts_voice", cfg["tts"]["voice"]),
        "language": m.get("tts_language", cfg["tts"]["language"]),
        "speed": float(m.get("tts_speed", cfg["tts"]["speed"])),
    }


def clean_for_tts(text: str) -> str:
    """Remove markdown symbols; TTS has no notion of formatting."""
    return MD_SYMBOLS_RE.sub("", text)


def slug(name: str) -> str:
    """Learner names become directory names, so keep them boring."""
    return re.sub(r"[^a-z0-9_-]+", "-",
                  name.strip().lower()).strip("-") or DEFAULT_LEARNER


async def tts_worker(ws: WebSocket, sess: Session, queue: asyncio.Queue,
                     marks: dict, t0: float, cancel: asyncio.Event):
    """Synthesizes queued sentences in order. None sentinel ends it."""
    first_audio = False
    params = tts_params(sess)
    try:
        while True:
            text = await queue.get()
            if text is None:
                return
            try:
                async for audio, sr in tts.pcm_chunks(clean_for_tts(text), **params):
                    if cancel.is_set():
                        return
                    if not first_audio:
                        sess.speaking = True
                        sess.speaking_since = time.perf_counter()
                        await send_json(ws, {"type": "audio_start", "sample_rate": sr})
                        await send_json(ws, {"type": "state", "value": "speaking"})
                        marks["tts_first_audio"] = now_ms(t0)
                        first_audio = True
                    await ws.send_bytes(audio.tobytes())
            except TTSError as e:
                await send_json(ws, {"type": "tts_error", "message": str(e)[:200]})
    finally:
        # The client reports back when the queue has drained; until then the
        # mic still hears the assistant.
        if first_audio:
            await send_json(ws, {"type": "audio_end"})


async def queue_sentence(ws: WebSocket, queue: asyncio.Queue, marks: dict,
                         t0: float, sentence: str):
    """Hand one chunk to the TTS worker and mark when text became ready.

    The gap between this and the first audio frame is pure TTS time, and
    it is the difference between a conversation and a page that looks
    stuck, so the client gets told as soon as synthesis is in flight.
    """
    if "llm_first_sentence" not in marks:
        marks["llm_first_sentence"] = now_ms(t0)
        await send_json(ws, {"type": "state", "value": "preparing"})
    await send_json(ws, {"type": "sentence", "text": sentence})
    queue.put_nowait(sentence)


def record_turn(sess: Session, learner_text: str, spoken: list[str],
                interrupted: bool):
    """Bookkeeping for one turn, including one the child cut short.

    What counts as taught is what reached the TTS queue: a sentence that
    was cut off after the child heard it still came up.
    """
    if sess.store is None or sess.session_id is None:
        return
    sess.turn_count += 1
    said = " ".join(spoken)
    topic = sess.store.topic_words()
    # Words too common to recognise in free speech are only looked for when
    # the plan asked for them: the tutor was told to teach those.
    planned = {w for w in sess.plan.new + sess.plan.review
               if w in sess.vocab and w not in sess.tracked}
    taught = match.find(said, sess.tracked | set(topic) | planned)
    # Everything the child says in English counts, function words included:
    # the tutor talks mostly in Chinese, so English in the child's turn is
    # the child producing it.
    heard = match.find(learner_text, sess.vocab | set(topic))
    sess.store.expose(taught, sess.session_id, introduce=sess.plan.new)
    sess.store.practice(heard, sess.session_id)
    keep_topic_words(sess, said, learner_text, sess.vocab | set(topic))
    sess.store.log_turn(sess.session_id, learner_text, " ".join(spoken),
                        sorted(set(taught) | set(heard)), interrupted)


def keep_topic_words(sess: Session, said: str, learner_text: str,
                     known: set[str]) -> None:
    """Remember words the tutor taught that the curriculum does not have.

    They arise from what the child was talking about, so they are kept with
    a few words of that talk and come back for review like any other word.
    """
    note = " ".join(learner_text.split())[:10]
    fresh = [w for w in match.teaching_points(said) if w not in known]
    for lemma in fresh[:TOPIC_WORDS_PER_TURN]:
        sess.store.add_topic_word(lemma, note, sess.session_id)


# Folding the oldest turns into a summary rather than dropping them: the
# oldest turns are exactly where "we already did colours" is written down,
# and losing that is how the tutor starts circling back to them.
COMPACT_PROMPT = """下面是英语陪练对话里最早的一段，以及之前对更早期内容的摘要。

请把它们合并成一段不超过 140 字的中文摘要，只保留后面用得上的：聊过的
话题、提到的人和事、教过的英文词、孩子犯过的错。不要写评价，不要写
“摘要”两个字。

已有摘要：
{previous}

最早的一段：
{transcript}
"""


async def compact_history(sess: Session):
    """Fold the oldest turns into the rolling summary.

    Runs off the critical path: the summary is only used from the next turn
    on, so the child never waits for it.
    """
    overflow = len(sess.history) - HISTORY_MESSAGES
    if overflow < COMPACT_MESSAGES:
        return
    oldest = sess.history[: overflow]
    transcript = "\n".join(
        f"{'孩子' if m['role'] == 'user' else '老师'}：{m['content']}"
        for m in oldest)
    prompt = COMPACT_PROMPT.format(previous=sess.summary or "（无）",
                                   transcript=transcript)
    try:
        reply = "".join([d async for d in llm.stream_chat(
            [{"role": "user", "content": prompt}], max_tokens=160)]).strip()
    except Exception as e:
        log.warning("compaction failed: %s", e)
        return
    # No await between the check and the cut, so nothing else can have moved
    # the history in the meantime.
    if not reply or sess.history[: overflow] != oldest:
        return
    sess.summary = THINK_RE.sub("", reply)
    del sess.history[: overflow]


async def write_portrait(sess: Session):
    """Update a learner's portrait from the session that just ended.

    The socket is already closed when this runs, so a slow or a broken
    summary cannot cost the child a reply.
    """
    try:
        await digest.write(llm, sess.store,
                           profile.path_for(data_root(), sess.learner),
                           sess.session_id)
    except Exception as e:
        log.warning("portrait update failed for %s: %s", sess.learner, e)


def judge_reading(sess: Session, plan: state_block.Plan,
                  heard: str) -> state_block.Plan:
    """Turn the child's answer to "跟我读" into feedback for this reply.

    The turn is a feedback turn: the plan's new and review words are set
    aside so the reply answers the attempt instead of starting something
    else.
    """
    verdict = readback.judge(sess.awaiting, heard, sess.read_tries)
    sess.read_tries = sess.read_tries + 1 if verdict.failed else 0
    sess.read_target = sess.awaiting
    sess.read_verdict = verdict.kind
    return replace(plan, new=[], review=[], pattern="", topics=[],
                   reading=verdict.line, hint=verdict.hint)


def note_invitation(sess: Session, reply: str) -> None:
    """Remember what the reply asked the child to say, if anything.

    Called once a reply is complete. A request to repeat stays open until
    the child's next turn is judged against it.
    """
    phrase = match.invitation(reply) or ""
    if phrase != sess.read_target or sess.read_verdict in ("ok", "other"):
        sess.read_tries = 0
    sess.awaiting = phrase
    sess.nudged = False
    sess.read_target, sess.read_verdict = "", ""


def build_messages(sess: Session, text: str) -> list[dict]:
    """Prompt for one turn: stable rules, then history, then the plan.

    The plan rides along with the child's own words and is never written to
    history: history has to stay byte-identical between turns for llama.cpp
    to reuse the cached prefix, and a stack of stale plans would tell the
    tutor to re-teach what it just taught.
    """
    plan = state_block.Plan()
    if sess.store is not None and sess.units:
        plan = scheduler.plan(sess.store, sess.units, BLOCK_CAPS, sess.portrait,
                              session_id=sess.session_id,
                              opening=not sess.history,
                              new_per_day=NEW_PER_DAY, turn=sess.turn_count)
    if sess.summary:
        plan = replace(plan, summary=sess.summary)
    if sess.awaiting:
        plan = judge_reading(sess, plan, text)
    sess.plan = plan   # the turn's bookkeeping credits what was asked for
    block = state_block.render(plan, BLOCK_CAPS)
    messages = [{"role": "system", "content": sess.system_prompt()}]
    messages += sess.history[-HISTORY_MESSAGES:]
    messages.append(
        {"role": "user", "content": f"{block}\n\n{text}" if block else text})
    return messages


async def handle_utterance(ws: WebSocket, sess: Session, pcm24: np.ndarray,
                           cancel: asyncio.Event):
    t0 = time.perf_counter()
    marks: dict = {}
    queue: asyncio.Queue = asyncio.Queue()
    spoken: list[str] = []
    user_text = ""
    worker = asyncio.create_task(tts_worker(ws, sess, queue, marks, t0, cancel))
    try:
        text = await asr.transcribe(pcm24, sr=CLIENT_SR)
        marks["asr_done"] = now_ms(t0)
        if not text:
            await send_json(ws, {"type": "error", "message": "empty transcription"})
            return
        user_text = text
        await send_json(ws, {"type": "user_text", "text": text})

        messages = build_messages(sess, text)

        await send_json(ws, {"type": "state", "value": "thinking"})
        reply_parts, sent_start = [], 0
        stopped_at = None   # reply text up to the request to repeat

        async for delta in llm.stream_chat(messages, cancel=cancel):
            if cancel.is_set():
                break
            reply_parts.append(delta)
            cleaned = THINK_RE.sub("", "".join(reply_parts))
            # A request to repeat is the end of the turn: the child has to
            # get the floor, so nothing after it is shown or spoken.
            stopped_at = match.cut_after_invitation(cleaned)
            if stopped_at is not None:
                extra = len(cleaned) - len(stopped_at)
                delta = delta[: max(0, len(delta) - extra)]
                cleaned = stopped_at
            if delta:
                await send_json(ws, {"type": "reply_delta", "text": delta})
            # Emit every complete sentence (>= SENTENCE_MIN_CHARS) for TTS.
            emitted, sent_start = extract_sentences(cleaned, sent_start)
            for sentence in emitted:
                spoken.append(sentence)
                await queue_sentence(ws, queue, marks, t0, sentence)
            if stopped_at is not None:
                break
        if cancel.is_set():
            sess.awaiting = ""
            await send_json(ws, {"type": "interrupted"})
            return

        reply = (stopped_at if stopped_at is not None
                 else THINK_RE.sub("", "".join(reply_parts))).strip()
        rest = reply[sent_start:].strip() if sent_start < len(reply) else ""
        if rest:
            spoken.append(rest)
            await queue_sentence(ws, queue, marks, t0, rest)
        queue.put_nowait(None)  # sentinel: drain then stop
        await worker

        if reply:
            sess.history.append({"role": "user", "content": text})
            sess.history.append({"role": "assistant", "content": reply})
            note_invitation(sess, reply)
        await send_json(ws, {"type": "reply_done", "marks": marks})
    except Exception as e:  # keep the socket alive; surface the error
        queue.put_nowait(None)
        await send_json(ws, {"type": "error", "message": f"{type(e).__name__}: {e}"[:300]})
        # The turn is over either way. Without this the client keeps the
        # reply bubble open and its status frozen on a reply that died.
        await send_json(ws, {"type": "reply_done", "marks": marks})
    finally:
        worker.cancel()
        if user_text:
            record_turn(sess, user_text, spoken, cancel.is_set())
            if (TEACHING and not sess.compacting
                    and len(sess.history) - HISTORY_MESSAGES >= COMPACT_MESSAGES):
                sess.compacting = True
                task = asyncio.create_task(compact_history(sess))
                task.add_done_callback(
                    lambda _: setattr(sess, "compacting", False))
        if not cancel.is_set():
            await send_json(ws, {"type": "state", "value": "listening"})


def cancel_nudge(sess: Session) -> None:
    task, sess.nudge_task = sess.nudge_task, None
    if task is not None and not task.done():
        task.cancel()


def schedule_nudge(ws: WebSocket, sess: Session) -> None:
    """After a request to repeat, prompt once if the child stays silent."""
    cancel_nudge(sess)
    if READ_WAIT_S <= 0 or not sess.awaiting or sess.nudged:
        return
    sess.nudge_task = asyncio.create_task(nudge_after_wait(ws, sess, sess.awaiting))


async def nudge_after_wait(ws: WebSocket, sess: Session, phrase: str):
    await asyncio.sleep(READ_WAIT_S)
    # Anything that has happened since means the child is not silent: they
    # are speaking, a reply is on its way, or the request was answered.
    if (not sess.streaming or sess.awaiting != phrase or sess.nudged
            or sess.busy or sess.speaking or sess.svad.active):
        return
    sess.nudged = True
    sess.cancel = asyncio.Event()
    sess.busy = True
    task = asyncio.create_task(nudge_turn(ws, sess, phrase, sess.cancel))
    sess.turn_task = task
    task.add_done_callback(lambda _: setattr(sess, "busy", False))


async def nudge_turn(ws: WebSocket, sess: Session, phrase: str,
                     cancel: asyncio.Event):
    """Speak the one-off prompt; it is not recorded as a turn."""
    t0 = time.perf_counter()
    queue: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(tts_worker(ws, sess, queue, {}, t0, cancel))
    line = NUDGE_LINES[len(phrase) % len(NUDGE_LINES)].format(w=phrase)
    try:
        await send_json(ws, {"type": "nudge", "text": line})
        queue.put_nowait(line)
        queue.put_nowait(None)
        await worker
    finally:
        worker.cancel()
        if not cancel.is_set():
            await send_json(ws, {"type": "state", "value": "listening"})


async def cancel_turn(sess: Session):
    """Stop the running turn and clear the speaking flag.

    The cancel event is dropped here and rebuilt per turn: reusing one
    across turns would make the next turn see a stale set flag and abort
    as soon as it starts.
    """
    sess.speaking = False
    sess.busy = False
    if sess.cancel:
        sess.cancel.set()
    task = sess.turn_task
    sess.turn_task = None
    sess.cancel = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def start_turn(ws: WebSocket, sess: Session, pcm24: np.ndarray):
    cancel_nudge(sess)
    await cancel_turn(sess)
    sess.cancel = asyncio.Event()
    sess.busy = True
    task = asyncio.create_task(
        handle_utterance(ws, sess, pcm24, sess.cancel)
    )
    sess.turn_task = task
    # Cleared on completion and on cancellation alike, so a turn that was
    # killed never leaves the session stuck thinking it is busy.
    task.add_done_callback(lambda _: setattr(sess, "busy", False))


async def cut_off(ws: WebSocket, sess: Session):
    sess.turn_barged = True
    await send_json(ws, {"type": "stop_playback", "reason": "barge_in"})
    await cancel_turn(sess)
    await send_json(ws, {"type": "state", "value": "hearing"})


async def vad_task(ws: WebSocket, sess: Session):
    while True:
        raw = await sess.q.get()
        if len(raw) % 2:
            # Not int16 PCM. Parsing it would raise out of this task and
            # leave the session streaming into a queue nobody reads.
            continue
        pcm = np.frombuffer(raw, dtype="<i2")
        # While the assistant plays, its own voice is in the mic: demand a
        # higher probability before believing a frame is the child.
        thr = BARGE_IN_THRESHOLD if sess.speaking else None
        for ev in sess.svad.feed(pcm, thr):
            if ev.kind == "speech_start":
                # An utterance that opens while the assistant is playing or
                # still working on an answer is not a turn of its own until
                # it proves itself by outlasting the barge-in rules.
                sess.turn_echo = sess.speaking or sess.busy
                sess.turn_barged = False
                # Only when nothing is in flight: overwriting the
                # thinking/preparing state with "hearing" would tell the
                # child the answer was given up on while it is still coming.
                if not sess.turn_echo:
                    await send_json(ws, {"type": "state", "value": "hearing"})
            elif sess.turn_echo and not sess.turn_barged:
                continue  # echo or impatience, not a turn of its own
            else:
                await start_turn(ws, sess, ev.pcm24)
        if barge_in_due(sess.speaking, sess.busy, sess.svad.active,
                        sess.turn_barged, sess.svad.speech_ms,
                        (time.perf_counter() - sess.speaking_since) * 1000):
            await cut_off(ws, sess)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sess = Session()
    session_by_ws[ws] = sess
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if (text := msg.get("text")) is not None:
                data = json.loads(text)
                mtype = data.get("type")
                if mtype == "mode":
                    cancel_nudge(sess)
                    sess.mode = data["value"] if data["value"] in cfg["modes"] else DEFAULT_MODE
                    # Mode and conversation are the same thing here: a reply
                    # in the wrong register is worse than a fresh start.
                    sess.history.clear()
                    sess.load_curriculum()
                    await send_json(ws, {"type": "mode", "value": sess.mode})
                elif mtype == "start":
                    if not sess.streaming:
                        sess.streaming = True
                        sess.vad_task = asyncio.create_task(vad_task(ws, sess))
                        await send_json(ws, {"type": "state", "value": "listening"})
                elif mtype == "stop":
                    sess.streaming = False
                    await cancel_turn(sess)
                    if sess.vad_task:
                        sess.vad_task.cancel()
                        sess.vad_task = None
                elif mtype == "interrupt":
                    sess.turn_barged = True
                    await send_json(ws, {"type": "stop_playback", "reason": "manual"})
                    await cancel_turn(sess)
                    await send_json(ws, {"type": "state", "value": "listening"})
                elif mtype == "learner":
                    value = str(data.get("value", "")).strip() or DEFAULT_LEARNER
                    sess.open_learner(slug(value))
                    await send_json(ws, {"type": "learner", "value": sess.learner})
                elif mtype == "unit":
                    # Empty means "pick for me"; see scheduler.current_unit.
                    value = str(data.get("value", "")).strip()
                    if sess.store is not None:
                        sess.store.set_setting(scheduler.CURRENT_UNIT, value)
                    await send_json(ws, {"type": "unit", "value": value})
                elif mtype == "playback":
                    if data.get("value") == "ended":
                        sess.speaking = False
                        schedule_nudge(ws, sess)
                elif mtype == "ping":
                    await send_json(ws, {"type": "pong"})
            elif (data := msg.get("bytes")) is not None and sess.streaming:
                sess.q.put_nowait(data)
    except WebSocketDisconnect:
        pass
    finally:
        cancel_nudge(sess)
        await cancel_turn(sess)
        if sess.vad_task:
            sess.vad_task.cancel()
        if DIGEST_MODE == "on_disconnect" and sess.session_id is not None:
            await write_portrait(sess)
        sess.close()
        session_by_ws.pop(ws, None)


@app.get("/modes")
async def get_modes():
    """Mode list for the client dropdown; modes are free-form in
    config.yaml, so an age group is just another entry."""
    return {
        "default": DEFAULT_MODE,
        "modes": [
            {"name": name, "label": m.get("label", name)}
            for name, m in cfg["modes"].items()
        ],
    }


@app.get("/learners")
async def get_learners():
    """Learner profiles on this machine, for the client's dropdown."""
    root = data_root() / "learners"
    names = sorted(p.name for p in root.glob("*") if (p / "state.sqlite").exists())
    if DEFAULT_LEARNER not in names:
        names.insert(0, DEFAULT_LEARNER)
    return {"default": DEFAULT_LEARNER, "learners": names}


@app.get("/learners/{learner}/progress")
async def get_progress(learner: str, mode: str = "", unit: str = ""):
    """Words of one unit with what the learner has done with each.

    This is what the parent judges: not a score, but how often a word has
    come up and whether it has ever been produced.
    """
    mode = mode if mode in cfg["modes"] else DEFAULT_MODE
    name = cfg["modes"][mode].get("curriculum")
    lexicon = curriculum.load_lexicon(
        data_root() / "curriculum" / "lexicon.jsonl")
    units = curriculum.load(name, lexicon) if name else []
    chosen = next((u for u in units if u.id == unit), None)
    store = Store(slug(learner), data_root())
    try:
        store.sync_units(units)
        rows = []
        for word in (chosen.words if chosen else ()):
            row = store.get(word) or {}
            rows.append({
                "lemma": word,
                "zh": (lexicon or {}).get(word),
                "box": row.get("box", 0),
                "practices": row.get("practices", 0),
                "sessions_seen": row.get("sessions_seen", 0),
                "manual": bool(row.get("manual", 0)),
                "state": _word_state(row.get("box", 0)),
            })
    finally:
        store.close()
    return {"learner": slug(learner), "mode": mode,
            "unit": chosen.id if chosen else "", "words": rows}


def _word_state(box: int) -> str:
    if box <= 0:
        return "未学"
    return "已会" if box >= MASTERY_BOX else "学中"


@app.post("/learners/{learner}/words/{lemma}")
async def set_word_state(learner: str, lemma: str, body: dict,
                          mode: str = ""):
    """A parent's verdict on one word.

    Setting a box pins the word: automatic bookkeeping stops moving it, so
    "knows this" survives the word not coming up again, and "teach this
    again" is not undone by the next session touching it.
    """
    mode = mode if mode in cfg["modes"] else DEFAULT_MODE
    name = cfg["modes"][mode].get("curriculum")
    lexicon = curriculum.load_lexicon(
        data_root() / "curriculum" / "lexicon.jsonl")
    store = Store(slug(learner), data_root())
    try:
        # The word has to exist before it can be judged, and it only exists
        # once the curriculum that owns it has been synced.
        store.sync_units(curriculum.load(name, lexicon) if name else [])
        if store.get(lemma) is None:
            return JSONResponse({"error": "unknown word"}, status_code=404)
        if body.get("release"):
            store.release(lemma)
        else:
            # Pinning is the point: a verdict survives the word not coming
            # up again, which is the whole reason the parent sets it.
            store.set_box(lemma, MASTERY_BOX if body.get("known") else 0)
        return store.get(lemma)
    finally:
        store.close()


@app.get("/learners/{learner}/units")
async def get_units(learner: str, mode: str = ""):
    """Units of one mode, each with the words left to teach.

    The parent's unit picker is built from this: a unit is done when it has
    no untaught words left, which is the same test the scheduler uses.
    """
    mode = mode if mode in cfg["modes"] else DEFAULT_MODE
    name = cfg["modes"][mode].get("curriculum")
    lexicon = curriculum.load_lexicon(
        data_root() / "curriculum" / "lexicon.jsonl")
    units = curriculum.load(name, lexicon) if name else []
    store = Store(slug(learner), data_root())
    try:
        store.sync_units(units)
        left = {u.id: len(store.new_in_unit(u.id, len(u.words))) for u in units}
        current = store.get_setting(scheduler.CURRENT_UNIT) or ""
    finally:
        store.close()
    return {
        "mode": mode,
        "current": current,
        "units": [{"id": u.id, "title": u.title, "words": len(u.words),
                   "left": left[u.id]} for u in units],
    }


# Catch-all static mount must come last: routes registered before it win.
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
