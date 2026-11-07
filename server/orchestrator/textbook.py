"""Local textbook audio: directory index, unit resolution, progress and PCM.

Audio directories come from the ``textbook`` section of config.yaml. Each
directory is described by a generated index file (unit number, title,
filename, duration); playback progress lives in the learner's store, never
next to the audio. Files are decoded with ffmpeg into the same mono int16
PCM the TTS worker sends, so clients need no new message types to listen.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_VERSION = 1
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
DEFAULT_SAMPLE_RATE = 24000
PCM_CHUNK_MS = 100
# Textbook recordings sit near -29 dBFS RMS with peaks around -5 dBFS, much
# quieter than TTS speech. Normalize to broadcast loudness with a true-peak
# limiter. loudnorm preserves the sample count, so position_ms stays exact.
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# Grace period after the stream ends for the device to report its exact
# position. If no report arrives, the server clears the session itself.
END_FALLBACK_S = 5.0
PROGRESS_KEY = "textbook.progress"
TOOL_NAME = "play_textbook"

_UNIT_RE = re.compile(r"\bunit\s*0*(\d{1,2})\b", re.I)
_PROJECT_RE = re.compile(r"\bproject\s*0*(\d{1,2})\b", re.I)
_LEADING_RE = re.compile(r"^\s*0*(\d{1,2})\b")


def file_id(path: str | Path) -> str:
    """Stable id for one audio file across restarts."""
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:16]


def describe_filename(filename: str) -> dict:
    """Pull the unit number, kind and title out of a filename.

    Three naming schemes occur in the library: ``Unit 3.MP3``,
    ``03 Unit 3 My face.mp3`` and ``13 Project 4 New Year's gifts.mp3``.
    The leading number is kept as ``order`` so a track can still be found
    when it has no ``Unit`` keyword or when the caller counts tracks.
    """
    stem = Path(filename).stem
    leading = _LEADING_RE.match(stem)
    order = int(leading.group(1)) if leading else None
    match = _UNIT_RE.search(stem)
    if match:
        unit, kind = int(match.group(1)), "unit"
        tail = stem[match.end():].strip(" -_.")
    else:
        match = _PROJECT_RE.search(stem)
        if match:
            unit, kind = int(match.group(1)), "project"
            tail = stem[match.end():].strip(" -_.")
        else:
            unit, kind, tail = None, "track", stem.strip()
    return {"unit": unit, "order": order, "kind": kind, "title": tail}


@dataclass
class Entry:
    path: Path
    directory: Path
    filename: str
    unit: int | None
    order: int | None
    kind: str
    title: str
    duration_ms: int
    size: int

    @property
    def id(self) -> str:
        return file_id(self.path)

    @property
    def book(self) -> str:
        return self.directory.name

    def label(self) -> str:
        if self.unit is None:
            return self.title or self.filename
        prefix = "Project" if self.kind == "project" else "Unit"
        return f"{prefix} {self.unit}" + (f" {self.title}" if self.title else "")


def _duration_ms(path: Path) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return max(0, int(float(out.stdout.strip()) * 1000))
    except (OSError, subprocess.SubprocessError, ValueError):
        log.warning("ffprobe could not measure %s", path)
        return 0


def _audio_files(directory: Path) -> list[Path]:
    return sorted(
        (p for p in directory.iterdir()
         if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


def _signature(directory: Path) -> list:
    """Name/size/mtime of the audio files, used to decide when to rebuild."""
    result = []
    for path in _audio_files(directory):
        stat = path.stat()
        result.append([path.name, stat.st_size, int(stat.st_mtime)])
    return result


def _build_index(directory: Path) -> dict:
    files = []
    for path in _audio_files(directory):
        desc = describe_filename(path.name)
        stat = path.stat()
        files.append({
            "file": path.name,
            "unit": desc["unit"],
            "order": desc["order"],
            "kind": desc["kind"],
            "title": desc["title"],
            "duration_ms": _duration_ms(path),
            "size": stat.st_size,
        })
    return {
        "version": INDEX_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "directory": str(directory),
        "book": directory.name,
        "files": files,
    }


def load_index(directory: Path, index_file: str) -> dict:
    """Read the generated index, rebuilding it when the directory changed.

    The index is a cache: a stale or missing one is regenerated. A read-only
    directory still works from memory, so playback never depends on being
    able to write next to the audio.
    """
    path = directory / index_file
    signature = _signature(directory)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("version") == INDEX_VERSION
                    and data.get("signature") == signature):
                return data
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    data = _build_index(directory)
    data["signature"] = signature
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write textbook index %s: %s", path, exc)
    return data


class TextbookLibrary:
    """The configured audio directories and their indexed files."""

    def __init__(self, cfg: dict):
        section = cfg.get("textbook") or {}
        self.dirs = [Path(d).expanduser() for d in (section.get("dirs") or [])]
        self.index_file = str(section.get("index_file", ".talknest-index.json"))
        self.sample_rate = int(section.get("sample_rate", DEFAULT_SAMPLE_RATE))
        if self.sample_rate != DEFAULT_SAMPLE_RATE:
            # The device plays int16 PCM at its fixed I2S rate. Any other
            # rate is silently dropped there, which would make the reported
            # playback position meaningless.
            log.warning("textbook.sample_rate %d is unsupported; using %d",
                        self.sample_rate, DEFAULT_SAMPLE_RATE)
            self.sample_rate = DEFAULT_SAMPLE_RATE
        self.entries: list[Entry] = []
        self.warnings: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.dirs)

    def refresh(self) -> None:
        entries: list[Entry] = []
        warnings: list[str] = []
        for directory in self.dirs:
            if not directory.is_dir():
                warnings.append(f"{directory}: not a directory")
                continue
            try:
                data = load_index(directory, self.index_file)
            except OSError as exc:
                warnings.append(f"{directory}: {exc}")
                continue
            for item in data.get("files", []):
                entries.append(Entry(
                    path=directory / item["file"],
                    directory=directory,
                    filename=str(item["file"]),
                    unit=item.get("unit"),
                    order=item.get("order"),
                    kind=str(item.get("kind", "track")),
                    title=str(item.get("title", "")),
                    duration_ms=int(item.get("duration_ms", 0)),
                    size=int(item.get("size", 0)),
                ))
        self.entries, self.warnings = entries, warnings
        log.info("Textbook library: %d file(s) in %d dir(s)%s", len(entries),
                 len(self.dirs),
                 f"; {'; '.join(warnings)}" if warnings else "")

    def books(self) -> list[str]:
        return [d.name for d in self.dirs if d.is_dir()]

    def units(self) -> list[int]:
        return sorted({e.unit for e in self.entries if e.unit is not None})

    def by_id(self, ident: str) -> Entry | None:
        return next((e for e in self.entries if e.id == ident), None)

    def resolve(self, unit: int | None, book: str | None = None) -> Entry | None:
        """Find the file for a spoken unit number.

        ``unit`` comes from the LLM tool call, so it is already a number.
        Multiple directories may contain the same unit; a ``book`` hint
        narrows the choice, otherwise the first configured directory wins.
        """
        if unit is None:
            return None
        candidates = [e for e in self.entries if e.unit == unit]
        if not candidates:
            candidates = [e for e in self.entries if e.order == unit]
        if book:
            needle = book.strip().lower()
            narrowed = [e for e in candidates
                        if needle in e.book.lower()
                        or needle in str(e.directory).lower()]
            if narrowed:
                candidates = narrowed
        if not candidates:
            return None

        def rank(entry: Entry):
            try:
                dir_rank = self.dirs.index(entry.directory)
            except ValueError:
                dir_rank = len(self.dirs)
            return (dir_rank,
                    entry.order if entry.order is not None else 10 ** 6,
                    entry.filename)

        return sorted(candidates, key=rank)[0]


def progress_load(store) -> dict:
    empty = {"version": 1, "last": None, "files": {}}
    if store is None:
        return empty
    try:
        data = json.loads(store.get_setting(PROGRESS_KEY) or "")
    except (TypeError, ValueError):
        return empty
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data
    return empty


def progress_save(store, data: dict) -> None:
    if store is not None:
        store.set_setting(PROGRESS_KEY, json.dumps(data, ensure_ascii=False))


def progress_position(store, entry: Entry) -> int:
    """Resume point in milliseconds; a finished file starts over."""
    record = progress_load(store).get("files", {}).get(entry.id)
    if not isinstance(record, dict):
        return 0
    position = max(0, int(record.get("position_ms", 0)))
    duration = int(record.get("duration_ms", 0)) or entry.duration_ms
    if duration and position >= duration - 2000:
        return 0
    return position


def progress_record(store, entry: Entry, position_ms: int) -> None:
    data = progress_load(store)
    files = data.setdefault("files", {})
    files[entry.id] = {
        "path": str(entry.path),
        "book": entry.book,
        "unit": entry.unit,
        "title": entry.title,
        "duration_ms": entry.duration_ms,
        "position_ms": max(0, int(position_ms)),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["last"] = entry.id
    progress_save(store, data)


def progress_last(store, library: TextbookLibrary) -> Entry | None:
    ident = progress_load(store).get("last")
    return library.by_id(ident) if ident else None


def tool_definition(books: list[str]) -> dict:
    """The function the LLM calls to express a playback intent.

    Intent is decided by the model from the meaning of the whole utterance,
    not by matching the transcript against keywords. The unit number is a
    parameter the model infers, so "第三单元", "unit three" and "第 3 课"
    all arrive here as ``unit: 3``.
    """
    hint = ""
    if books:
        hint = " 可选教材目录：" + "、".join(books) + "。"
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "播放本地英语教材课文音频。当用户想听某个单元的课文、想继续听上次"
                "没听完的课文、想从头重听某个单元，或想停止播放时调用。"
                "根据整句话的意思判断意图，不要逐字匹配关键词；单元号由你从用户的"
                "自然语言里推断，例如「第三单元」「unit three」「第 3 课」都取 3。"
                + hint
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "resume", "restart", "stop"],
                        "description": (
                            "play=播放指定单元（从开头）；resume=从上次中断处继续播放；"
                            "restart=明确从头重播指定单元；stop=停止当前播放"
                        ),
                    },
                    "unit": {
                        "type": "integer",
                        "description": "用户想听的单元号；resume 与 stop 可省略。",
                    },
                    "book": {
                        "type": "string",
                        "description": "用户提到的教材或目录名，用于选择音频目录。",
                    },
                },
                "required": ["action"],
            },
        },
    }


class TextbookPlayback:
    """One file playing over the existing audio WebSocket messages."""

    def __init__(self, ws, send_json, sess, store, library: TextbookLibrary,
                 entry: Entry, start_ms: int, playback_id: int):
        self.ws = ws
        self._send_json = send_json
        self.sess = sess
        self.store = store
        self.library = library
        self.entry = entry
        self.start_ms = max(0, int(start_ms))
        self.playback_id = playback_id
        # The server pushes at real time, so bytes sent track what was heard
        # to within the device's prebuffer. The device's own report refines it.
        self.position_ms = self.start_ms
        self.finished = False
        self._cancel = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())
        self._cleanup_task = asyncio.create_task(self._clear_stale_session())

    async def run(self) -> None:
        rate = self.library.sample_rate
        bytes_per_second = rate * 2
        chunk_bytes = bytes_per_second * PCM_CHUNK_MS // 1000
        chunk_bytes -= chunk_bytes % 2
        args = ["ffmpeg", "-v", "error"]
        if self.start_ms:
            args += ["-ss", f"{self.start_ms / 1000:.3f}"]
        args += ["-i", str(self.entry.path), "-af", LOUDNORM,
                 "-ar", str(rate), "-ac", "1", "-f", "s16le", "-"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            log.warning("ffmpeg unavailable: %s", exc)
            await self._send_json(self.ws, {"type": "error",
                                            "message": f"cannot decode audio: {exc}"})
            return
        try:
            await self._send_json(self.ws, {"type": "audio_start",
                                            "sample_rate": rate,
                                            "playback_id": self.playback_id})
            await self._send_json(self.ws, {"type": "state", "value": "speaking"})
            self.sess.speaking = True
            self.sess.speaking_since = time.perf_counter()
            sent = 0
            while not self._cancel.is_set():
                chunk = await self._proc.stdout.read(chunk_bytes)
                if not chunk:
                    break
                await self.ws.send_bytes(chunk)
                sent += len(chunk)
                self.position_ms = (self.start_ms
                                    + sent * 1000 // bytes_per_second)
                # Pace to real time: a burst would overflow the device's
                # playback buffer and drop whole seconds of the lesson.
                await asyncio.sleep(len(chunk) / bytes_per_second)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("textbook playback failed: %s", exc)
        finally:
            await self._stop_process()
        if not self._cancel.is_set():
            await self._send_json(self.ws, {"type": "audio_end"})
            self.finished = True
            progress_record(self.store, self.entry,
                            self.entry.duration_ms or self.position_ms)

    async def _stop_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        with contextlib.suppress(Exception):
            await proc.stderr.read()

    async def _clear_stale_session(self) -> None:
        """Return the session to listening if the device never reports back.

        The device's ``playback: ended`` carries the exact heard position, so
        it normally clears the playback first. A lost report must not leave
        ``speaking`` set forever: that gates the uplink and wedges the session.
        """
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await asyncio.sleep(END_FALLBACK_S)
        if self.sess.textbook is not self:
            return
        self.sess.speaking = False
        self.sess.textbook = None
        await self._send_json(self.ws, {"type": "state", "value": "listening"})

    async def stop(self) -> None:
        """Stop the stream and keep the server-side position as a fallback."""
        self._cancel.set()
        await self._stop_process()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if not self.finished:
            progress_record(self.store, self.entry, self.position_ms)

    def refine(self, device_ms: int) -> None:
        """Apply the device's exact position (relative to this stream).

        A position at the end of the file is already treated as finished by
        progress_position, so no separate completion flag is needed.
        """
        absolute = self.start_ms + max(0, int(device_ms))
        limit = self.entry.duration_ms or absolute
        progress_record(self.store, self.entry, min(absolute, limit))
