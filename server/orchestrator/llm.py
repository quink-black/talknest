"""LLM backend client (OpenAI-compatible chat completions, streaming).
Works unchanged with llama.cpp server and vLLM — the active profile in
config.yaml decides which one is behind the endpoint."""

import asyncio
import json
from typing import AsyncIterator

import httpx


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str, sampling: dict):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.sampling = sampling
        self._client = httpx.AsyncClient(timeout=180.0, trust_env=False)

    async def stream_chat(
        self, messages: list[dict], cancel: asyncio.Event | None = None
    ) -> AsyncIterator[str]:
        """Yield content deltas; stops early if `cancel` is set."""
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.sampling.get("max_tokens", 300),
            "temperature": self.sampling.get("temperature", 0.7),
            "top_p": self.sampling.get("top_p", 0.8),
            "top_k": self.sampling.get("top_k", 20),
            "presence_penalty": self.sampling.get("presence_penalty", 1.5),
        }
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
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                content = delta.get("content")
                if content:
                    yield content
