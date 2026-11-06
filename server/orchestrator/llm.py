"""LLM backend client (OpenAI-compatible chat completions, streaming).
Works unchanged with llama.cpp server and vLLM — the active profile in
config.yaml decides which one is behind the endpoint.

A caller may pass OpenAI-style ``tools``. The client then also yields
``ToolCall`` events assembled from the streamed ``tool_calls`` deltas.
Backends that do not implement tools are tolerated: one failed request is
retried without them and the conversation continues with plain text.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

# Only these statuses mean "this endpoint does not understand tools"; a 5xx
# or a timeout is a real outage and must surface like any other failure.
_TOOL_REJECTIONS = {400, 404, 415, 422, 501}


@dataclass
class ToolCall:
    """One function call the model decided to make."""
    name: str
    arguments: dict


class LLMClient:
    # Capability flag. Callers check it so a test double or a future client
    # without tool support is never handed a ``tools`` keyword.
    supports_tools = True

    def __init__(self, base_url: str, model: str, api_key: str, sampling: dict,
                 extra_body: dict | None = None, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.sampling = sampling
        # Backend-specific request fields a profile may need, e.g. vLLM's
        # chat_template_kwargs to switch a reasoning model's thinking off.
        self.extra_body = dict(extra_body or {})
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._tools_supported = True

    @classmethod
    def from_profile(cls, profile: dict, sampling: dict) -> "LLMClient":
        """Build a client from one `llm.profiles` entry of config.yaml."""
        return cls(profile["base_url"], profile["model"],
                   profile.get("api_key", ""), sampling,
                   extra_body=profile.get("extra_body"),
                   timeout=float(profile.get("timeout", 180.0)))

    async def stream_chat(
        self, messages: list[dict], cancel: asyncio.Event | None = None,
        max_tokens: int | None = None, tools: list[dict] | None = None,
    ) -> AsyncIterator[str | ToolCall]:
        """Yield content deltas and, when tools are given, any tool calls.

        With no ``tools`` the behaviour is exactly the original one: only
        content strings are yielded. A backend that rejects a request with
        tools is remembered, so the retry cost is paid once per client.
        """
        if tools and self._tools_supported:
            try:
                async for event in self._stream(messages, cancel, max_tokens,
                                                tools):
                    yield event
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _TOOL_REJECTIONS:
                    raise
                log.warning("LLM backend rejected tools (%s); using plain text",
                            exc.response.status_code)
                self._tools_supported = False
        async for event in self._stream(messages, cancel, max_tokens, None):
            yield event

    async def _stream(
        self, messages: list[dict], cancel: asyncio.Event | None,
        max_tokens: int | None, tools: list[dict] | None,
    ) -> AsyncIterator[str | ToolCall]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens or self.sampling.get("max_tokens", 300),
            "temperature": self.sampling.get("temperature", 0.7),
            "top_p": self.sampling.get("top_p", 0.8),
            "top_k": self.sampling.get("top_k", 20),
            "presence_penalty": self.sampling.get("presence_penalty", 1.5),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(self.extra_body)
        # OpenAI streams a tool call as several deltas, each carrying a
        # fragment of the function name and of the JSON arguments.
        tool_acc: dict[int, dict] = {}
        saw_tool_call = False
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions",
            json=body, headers=self.headers,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if cancel is not None and cancel.is_set():
                    break
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if tools:
                    for call in delta.get("tool_calls") or []:
                        saw_tool_call = True
                        index = int(call.get("index", 0))
                        acc = tool_acc.setdefault(index, {"name": "", "args": ""})
                        fn = call.get("function") or {}
                        acc["name"] += fn.get("name") or ""
                        acc["args"] += fn.get("arguments") or ""
                content = delta.get("content")
                if content and not saw_tool_call:
                    yield content
        if tools:
            for acc in tool_acc.values():
                try:
                    arguments = json.loads(acc["args"]) if acc["args"] else {}
                except json.JSONDecodeError:
                    arguments = {}
                yield ToolCall(acc["name"], arguments)
