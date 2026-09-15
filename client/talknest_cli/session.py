"""WebSocket session and the client half of the talknest protocol.

Two rules dominate this file:

  1. `playback ended` must be sent exactly once per turn, after the queue has
     really drained. The server clears `Session.speaking` there and on turn
     cancellation; missing it makes every later utterance look like the
     assistant's own echo until the next cancellation.
  2. Half duplex gates the upstream at the client, it does not send `stop`.
     `stop` cancels the running turn on the server, which would cut the reply
     off after its first audio chunk.
"""

import asyncio
import json
import ssl
import time

import numpy as np
import websockets

from . import CLIENT_SR


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    # Self-signed dev deployments (certs/dev.pem) are the norm here, so
    # verification is off unless --tls-ca names a bundle to trust.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Session:
    def __init__(self, url: str, engine, mode: str,
                 half_duplex: bool = False, reconnect_delay: float = 3.0,
                 ca_file: str | None = None):
        self.url = url
        self.engine = engine
        self.mode = mode
        self.half_duplex = half_duplex
        self.reconnect_delay = reconnect_delay
        self.ca_file = ca_file
        self.ws = None
        self.play_sr = CLIENT_SR
        self._stop = False
        self._gate_open = True   # False while the assistant owns the room
        self._playback_active = False
        self._audio_end = False
        self._state = ""
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not self._stop:
            try:
                await self._serve_once()
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                print(f"[ws] {exc}; retrying in {self.reconnect_delay}s", flush=True)
            except websockets.WebSocketException as exc:
                print(f"[ws] {exc}; retrying in {self.reconnect_delay}s", flush=True)
            if self._stop:
                break
            await asyncio.sleep(self.reconnect_delay)

    async def _serve_once(self) -> None:
        kwargs = {"max_size": 8 * 1024 * 1024, "ping_interval": 20,
                  "ping_timeout": 20, "proxy": None}
        if self.url.startswith("wss://"):
            kwargs["ssl"] = _ssl_context(self.ca_file)
        async with websockets.connect(self.url, **kwargs) as ws:
            self.ws = ws
            self._gate_open = True
            # The new socket is a new server session. Stale upstream frames
            # would be sent at 1x and lag behind real time forever, and
            # leftover reply audio belongs to the dead connection.
            self.engine.upstream_clear()
            self.engine.reset_playback()
            self._playback_active = False
            self._audio_end = False
            await self._send({"type": "mode", "value": self.mode})
            await self._send({"type": "start"})
            print(f"[ws] connected to {self.url}", flush=True)
            tasks = [asyncio.create_task(t) for t in (
                self._sender(), self._receiver(), self._watcher(), self._pinger())]
            try:
                await asyncio.gather(*tasks)
            finally:
                # Awaited, not just cancelled: a pending _receiver would
                # otherwise outlive the socket and play reply audio into
                # the next connection.
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop = True
        loop, ws = self._loop, self.ws
        if loop is not None and ws is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.close()))

    async def _send(self, obj: dict) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    # -- tasks -----------------------------------------------------------

    async def _sender(self) -> None:
        t0 = time.perf_counter()
        sent = 0
        while True:
            frame = await asyncio.to_thread(self.engine.upstream_get, 0.2)
            if frame is None:
                continue
            if not self._gate_open:
                continue
            await self.ws.send(frame.tobytes())
            sent += len(frame)
            # The server queue is unbounded and applies no backpressure, so
            # pacing is the client's job.
            delay = t0 + sent / CLIENT_SR - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

    async def _receiver(self) -> None:
        async for msg in self.ws:
            try:
                if isinstance(msg, (bytes, bytearray)):
                    self.engine.play(np.frombuffer(msg, dtype="<i2"), self.play_sr)
                else:
                    self._on_text(json.loads(msg))
            except ValueError:
                # A frame that is neither int16 PCM nor JSON: drop it. Without
                # this a single malformed frame escapes run()'s OSError /
                # WebSocketException handlers and takes the client down.
                continue

    async def _watcher(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            if (self._playback_active and self._audio_end
                    and self.engine.playback_idle()):
                await self._finish_playback()

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self._send({"type": "ping"})

    # -- inbound ---------------------------------------------------------

    def _on_text(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "state":
            value = data.get("value", "")
            if value != self._state:
                self._state = value
                print(f"[{value}]", flush=True)
        elif kind == "user_text":
            print(f"you: {data.get('text', '')}", flush=True)
        elif kind == "reply_delta":
            print(data.get("text", ""), end="", flush=True)
        elif kind == "sentence":
            pass
        elif kind == "audio_start":
            self.play_sr = int(data.get("sample_rate") or CLIENT_SR)
            self._playback_active = True
            self._audio_end = False
            self.engine.reset_playback()
            if self.half_duplex:
                self._gate_open = False
        elif kind == "audio_end":
            self._audio_end = True
        elif kind == "stop_playback":
            print(f"\n[stop_playback reason={data.get('reason')}]", flush=True)
            # Clear the flags here, not inside a task: a new audio_start for
            # the next turn could otherwise race ahead of it and have its
            # playback state wiped before any audio arrives.
            self._playback_active = False
            self._audio_end = False
            self.engine.clear_playback()
            self.engine.reset_playback()
            if self.half_duplex:
                self._gate_open = True
            asyncio.create_task(self._send({"type": "playback", "value": "ended"}))
        elif kind == "reply_done":
            marks = data.get("marks") or {}
            if marks:
                print(f"\n[marks] {marks}", flush=True)
            else:
                print(flush=True)
        elif kind in ("error", "tts_error"):
            print(f"\n[{kind}] {data.get('message', '')}", flush=True)
        elif kind == "interrupted":
            print("\n[interrupted]", flush=True)
        elif kind == "mode":
            self.mode = data.get("value", self.mode)
        elif kind == "pong":
            pass

    async def _finish_playback(self) -> None:
        if not self._playback_active:
            return
        self._playback_active = False
        self._audio_end = False
        self.engine.reset_playback()
        await self._send({"type": "playback", "value": "ended"})
        if self.half_duplex:
            self._gate_open = True

    # -- user actions ----------------------------------------------------

    async def interrupt(self) -> None:
        if self.ws is None:
            return
        await self._send({"type": "interrupt"})
