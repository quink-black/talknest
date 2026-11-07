"""Unit tests for local textbook audio (no services needed).

Run: ./venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import soundfile as sf
from starlette.websockets import WebSocketState

from orchestrator import main, textbook
from orchestrator.llm import LLMClient, ToolCall
from orchestrator.teaching.store import Store


async def _collect(agen):
    return [item async for item in agen]


@pytest.fixture
def fixed_duration(monkeypatch):
    monkeypatch.setattr(textbook, "_duration_ms", lambda path: 300000)


def _touch(directory, *names):
    for name in names:
        (directory / name).write_bytes(b"placeholder")


def test_describe_filename():
    cases = {
        "Unit 3.MP3": (3, None, "unit", ""),
        "unit 12.mp3": (12, None, "unit", ""),
        "03 Unit 3 My face.mp3": (3, 3, "unit", "My face"),
        "13 Project 4 New Year's gifts.mp3": (4, 13, "project",
                                              "New Year's gifts"),
        "01 Unit 1 Hello.mp3": (1, 1, "unit", "Hello"),
        "08 Unit 8 Weather.mp3": (8, 8, "unit", "Weather"),
        "Theme song.mp3": (None, None, "track", "Theme song"),
    }
    for filename, (unit, order, kind, title) in cases.items():
        got = textbook.describe_filename(filename)
        assert (got["unit"], got["order"], got["kind"], got["title"]) == \
            (unit, order, kind, title), filename


def test_tool_definition():
    tool = textbook.tool_definition(["一年级上-网盘", "一年级下"])
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == "play_textbook"
    assert "一年级上-网盘" in fn["description"]
    params = fn["parameters"]["properties"]
    assert set(params) == {"action", "unit", "book"}
    assert params["action"]["enum"] == ["play", "resume", "restart", "stop"]


def test_index_generation_and_reuse(tmp_path, fixed_duration):
    book = tmp_path / "一年级上-网盘"
    book.mkdir()
    _touch(book, "Unit 1.MP3", "Unit 3.MP3", "Unit 6.MP3")
    library = textbook.TextbookLibrary({"textbook": {"dirs": [str(book)]}})
    library.refresh()

    index_path = book / ".talknest-index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["version"] == textbook.INDEX_VERSION
    assert index["book"] == "一年级上-网盘"
    assert [f["unit"] for f in index["files"]] == [1, 3, 6]
    assert all(f["duration_ms"] == 300000 for f in index["files"])

    assert library.units() == [1, 3, 6]
    assert library.resolve(3).filename == "Unit 3.MP3"
    assert library.resolve(3).label() == "Unit 3"
    assert library.resolve(99) is None

    # An unchanged directory reuses the index instead of rewriting it.
    before = index_path.read_bytes()
    library.refresh()
    assert index_path.read_bytes() == before

    # A new file changes the signature and rebuilds the index.
    _touch(book, "Unit 2.MP3")
    library.refresh()
    assert library.resolve(2) is not None


def test_multiple_directories_and_book_hint(tmp_path, fixed_duration):
    first = tmp_path / "一年级上-网盘"
    second = tmp_path / "一年级下"
    first.mkdir()
    second.mkdir()
    _touch(first, "Unit 3.MP3")
    _touch(second, "03 Unit 3 Taste and smell.mp3")
    library = textbook.TextbookLibrary(
        {"textbook": {"dirs": [str(first), str(second)]}})
    library.refresh()

    assert len(library.entries) == 2
    assert library.books() == ["一年级上-网盘", "一年级下"]
    assert library.resolve(3).directory == first      # first configured dir
    assert library.resolve(3, "一年级下").directory == second
    assert library.resolve(3, "网盘").directory == first
    assert library.resolve(3, "missing") is not None  # hint that matches none


def test_progress_round_trip(tmp_path, fixed_duration):
    book = tmp_path / "book"
    book.mkdir()
    _touch(book, "Unit 3.MP3")
    library = textbook.TextbookLibrary({"textbook": {"dirs": [str(book)]}})
    library.refresh()
    entry = library.resolve(3)
    store = Store("kid", tmp_path / "data")

    assert textbook.progress_position(store, entry) == 0
    textbook.progress_record(store, entry, 45000)
    assert textbook.progress_position(store, entry) == 45000
    assert textbook.progress_last(store, library).id == entry.id

    # A finished file starts over on the next resume.
    textbook.progress_record(store, entry, entry.duration_ms)
    assert textbook.progress_position(store, entry) == 0
    store.close()


def test_llm_tool_calls_are_accumulated():
    async def scenario():
        client = LLMClient("http://backend", "model", "", {})
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            chunks = [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {
                        "name": "play_textbook", "arguments": "{\"unit\":"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "3}"}}]}}]},
                {"choices": [{"delta": {}}]},
            ]
            body = "".join("data: " + json.dumps(c) + "\n" for c in chunks)
            return httpx.Response(200, text=body + "data: [DONE]\n")

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        tools = [textbook.tool_definition(["book"])]
        messages = [{"role": "user", "content": "播放课文"}]
        with_tools = await _collect(client.stream_chat(messages, tools=tools))
        plain = await _collect(client.stream_chat(messages))
        return seen, with_tools, plain

    seen, with_tools, plain = asyncio.run(scenario())
    assert "tools" in seen[0]
    calls = [e for e in with_tools if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "play_textbook"
    assert calls[0].arguments == {"unit": 3}
    # With no tools the request and the yielded events stay unchanged.
    assert "tools" not in seen[-1]
    assert plain == []


def test_llm_backend_rejecting_tools_falls_back():
    async def scenario():
        client = LLMClient("http://backend", "model", "", {})

        def handler(request):
            if "tools" in json.loads(request.content):
                return httpx.Response(400, json={"error": "no tools"})
            return httpx.Response(
                200, text="data: " + json.dumps(
                    {"choices": [{"delta": {"content": "你好"}}]})
                + "\ndata: [DONE]\n")

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        events = await _collect(client.stream_chat(
            [{"role": "user", "content": "hi"}],
            tools=[textbook.tool_definition(["book"])]))
        # A second turn must not retry tools again.
        again = await _collect(client.stream_chat(
            [{"role": "user", "content": "hi"}],
            tools=[textbook.tool_definition(["book"])]))
        return events, again, client._tools_supported

    events, again, supported = asyncio.run(scenario())
    assert events == ["你好"]
    assert again == ["你好"]
    assert supported is False


def test_llm_stream_only_uses_tools_when_supported(monkeypatch):
    class PlainDouble:
        supports_tools = False

        async def stream_chat(self, messages, cancel=None, max_tokens=None):
            yield "plain"

    class ToolDouble:
        supports_tools = True

        def __init__(self):
            self.tools = None

        async def stream_chat(self, messages, cancel=None, max_tokens=None,
                              tools=None):
            self.tools = tools
            yield ToolCall("play_textbook", {"action": "play", "unit": 3})

    plain = PlainDouble()
    monkeypatch.setattr(main, "llm_client", lambda: plain)
    assert asyncio.run(_collect(main.llm_stream([], None))) == ["plain"]

    tool = ToolDouble()
    monkeypatch.setattr(main, "llm_client", lambda: tool)
    assert asyncio.run(_collect(main.llm_stream([], None)))[0].name == \
        "play_textbook"
    assert tool.tools == main.TEXTBOOK_TOOLS


class _FakeWS:
    client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []
        self.audio = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))

    async def send_bytes(self, data):
        self.audio.append(data)


def test_dispatch_play_resume_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(textbook, "_duration_ms", lambda path: 300000)
    book = tmp_path / "book"
    book.mkdir()
    # A real 1-second WAV so ffmpeg has something to decode.
    sf.write(str(book / "Unit 3.wav"), np.zeros(24000, dtype=np.int16), 24000)
    library = textbook.TextbookLibrary(
        {"textbook": {"dirs": [str(book)], "sample_rate": 24000}})
    monkeypatch.setattr(main, "TEXTBOOK", library)
    store = Store("kid", tmp_path / "data")

    sess = SimpleNamespace(textbook=None, playback_seq=0, store=store,
                           speaking=False, speaking_since=0.0, busy=False)

    async def dispatch(action, unit=None):
        ws = _FakeWS()
        args = {"action": action}
        if unit is not None:
            args["unit"] = unit
        await main.dispatch_textbook_tool(
            ws, sess, None, None, {},
            ToolCall("play_textbook", args))
        await asyncio.sleep(0.02)   # let audio_start fire
        playback = sess.textbook
        if playback is not None:
            await playback.stop()
        return ws, sess, playback

    ws, sess, playback = asyncio.run(dispatch("play", 3))
    starts = [m for m in ws.messages if m.get("type") == "audio_start"]
    assert starts and starts[0]["sample_rate"] == 24000
    assert starts[0]["playback_id"] == 1
    assert playback.entry.filename == "Unit 3.wav"
    assert playback.start_ms == 0

    # A saved position is where "resume" picks up.
    textbook.progress_record(store, playback.entry, 45000)
    ws, sess, playback = asyncio.run(dispatch("resume"))
    assert playback.start_ms == 45000
    assert playback.playback_id == 2

    # "从头播放" clears the bookmark and starts at zero.
    ws, sess, playback = asyncio.run(dispatch("restart", 3))
    assert playback.start_ms == 0
    assert textbook.progress_position(store, playback.entry) == 0

    # Stopping asks the device to cancel; its playback:ended reply is what
    # clears the object, so here it stays present but inactive.
    ws, sess, playback = asyncio.run(dispatch("stop"))
    assert playback is not None and not playback.active
    assert [m for m in ws.messages if m.get("type") == "stop_playback"]
    assert [m for m in ws.messages if m.get("type") == "reply_done"]
    store.close()


def test_refresh_runs_off_the_event_loop(monkeypatch):
    """A slow index build must not stall the other sessions' streams."""
    library = textbook.TextbookLibrary({"textbook": {"dirs": []}})
    monkeypatch.setattr(library, "refresh", lambda: time.sleep(0.15))
    monkeypatch.setattr(main, "TEXTBOOK", library)

    sess = SimpleNamespace(textbook=None, playback_seq=0, store=None,
                           speaking=False, speaking_since=0.0, busy=False)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        t = asyncio.create_task(ticker())
        ws = _FakeWS()
        await main.dispatch_textbook_tool(
            ws, sess, None, None, {},
            ToolCall("play_textbook", {"action": "stop"}))
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
        return ticks

    # 0.15 s of blocking work on the loop would allow almost no ticks.
    assert asyncio.run(scenario()) >= 5


def test_cleanup_fallback_without_device_report(monkeypatch, tmp_path):
    """A lost playback:ended must not leave speaking set forever."""
    monkeypatch.setattr(textbook, "END_FALLBACK_S", 0.02)
    monkeypatch.setattr(textbook, "_duration_ms", lambda path: 300000)
    book = tmp_path / "book"
    book.mkdir()
    sf.write(str(book / "Unit 3.wav"), np.zeros(1200, dtype=np.int16), 24000)
    library = textbook.TextbookLibrary(
        {"textbook": {"dirs": [str(book)], "sample_rate": 24000}})
    library.refresh()
    store = Store("kid", tmp_path / "data")
    sess = SimpleNamespace(textbook=None, playback_seq=1, store=store,
                           speaking=False, speaking_since=0.0, busy=False)
    ws = _FakeWS()
    playback = textbook.TextbookPlayback(
        ws, main.send_json, sess, store, library, library.resolve(3), 0, 1)
    sess.textbook = playback

    async def scenario():
        playback.start()
        await playback._cleanup_task

    asyncio.run(scenario())
    assert sess.speaking is False
    assert sess.textbook is None
    assert {"type": "state", "value": "listening"} in ws.messages
    store.close()


def test_refine_treats_device_position_as_milliseconds(tmp_path, fixed_duration):
    book = tmp_path / "book"
    book.mkdir()
    _touch(book, "Unit 3.MP3")
    library = textbook.TextbookLibrary({"textbook": {"dirs": [str(book)]}})
    library.refresh()
    entry = library.resolve(3)
    store = Store("kid", tmp_path / "data")
    playback = textbook.TextbookPlayback(
        _FakeWS(), lambda *args: None, None, store, library, entry, 10000, 1)
    playback.refine(5000)
    # 5000 is milliseconds into this stream, not a sample count.
    assert textbook.progress_position(store, entry) == 15000
    store.close()


def test_sample_rate_is_clamped_to_device_rate():
    library = textbook.TextbookLibrary(
        {"textbook": {"sample_rate": 48000}})
    assert library.sample_rate == textbook.DEFAULT_SAMPLE_RATE == 24000


def test_system_prompt_repeats_playback_rule_at_both_ends():
    sess = SimpleNamespace(system_prompt=lambda: "tutor rules")
    prompt = main.system_prompt(sess)
    assert prompt.startswith(main.TEXTBOOK_RULE)
    assert prompt.endswith(main.TEXTBOOK_RULE)
    assert "家长" in main.TEXTBOOK_RULE
    assert main.PARENT_PREFIX not in main.TEXTBOOK_RULE


def test_playback_claim_detects_dropped_tool_replies():
    assert main.playback_claim("好的，正在为您播放第一单元的课文。")
    assert main.playback_claim("已经停止播放了。")
    assert main.playback_claim("我们从头再听一遍吧")


def test_playback_claim_ignores_ordinary_replies():
    assert not main.playback_claim("Apple 是苹果，跟我读一次好吗？")
    assert not main.playback_claim("好呀，我们聊聊小狗吧。")


class _IntentDouble:
    supports_tools = True

    def __init__(self, result=None, raises=False):
        self.result = result
        self.raises = raises
        self.calls = 0

    async def decide_tool(self, messages, tools, name, max_tokens=64):
        self.calls += 1
        if self.raises:
            raise RuntimeError("backend down")
        return self.result


def test_recover_playback_call_returns_a_play_textbook_call(monkeypatch):
    monkeypatch.setattr(main, "TEXTBOOK", SimpleNamespace(enabled=True))
    double = _IntentDouble(
        ToolCall("play_intent", {"action": "play", "unit": 1}))
    monkeypatch.setattr(main, "llm_client", lambda: double)
    call = asyncio.run(main.recover_playback_call(
        "播放第一单元课文", None, "好的，正在为您播放第一单元的课文。"))
    assert call.name == "play_textbook"
    assert call.arguments == {"action": "play", "unit": 1}
    assert double.calls == 1


def test_recover_playback_call_keeps_ordinary_turns(monkeypatch):
    monkeypatch.setattr(main, "TEXTBOOK", SimpleNamespace(enabled=True))
    double = _IntentDouble(
        ToolCall("play_intent", {"action": "play", "unit": 1}))
    monkeypatch.setattr(main, "llm_client", lambda: double)
    assert asyncio.run(main.recover_playback_call(
        "我们聊聊小狗吧", None, "好呀，小狗很可爱。")) is None
    assert double.calls == 0


def test_recover_playback_call_ignores_none_decision(monkeypatch):
    monkeypatch.setattr(main, "TEXTBOOK", SimpleNamespace(enabled=True))
    double = _IntentDouble(ToolCall("play_intent", {"action": "none"}))
    monkeypatch.setattr(main, "llm_client", lambda: double)
    assert asyncio.run(main.recover_playback_call(
        "再听一遍刚才的话", None, "好，我们再听一遍这个单元。")) is None


def test_recover_playback_call_survives_backend_failure(monkeypatch):
    monkeypatch.setattr(main, "TEXTBOOK", SimpleNamespace(enabled=True))
    double = _IntentDouble(raises=True)
    monkeypatch.setattr(main, "llm_client", lambda: double)
    assert asyncio.run(main.recover_playback_call(
        "播放第一单元课文", None, "正在播放课文。")) is None
