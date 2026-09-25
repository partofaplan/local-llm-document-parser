"""Minimal async client for the Ollama HTTP API."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _same_model(a: str, b: str) -> bool:
    norm = lambda m: m if ":" in m else f"{m}:latest"  # noqa: E731
    return norm(a) == norm(b)


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 900.0, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> list[str]:
        resp = await self._http.get("/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    async def has_model(self, model: str) -> bool:
        return any(_same_model(model, m) for m in await self.list_models())

    async def pull(self, model: str) -> None:
        log.info("Pulling model %s (this can take several minutes)", model)
        last = ""
        async with self._http.stream("POST", "/api/pull", json={"model": model, "stream": True}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise LLMError(f"pull {model}: {event['error']}")
                status = event.get("status", "")
                if status != last:
                    log.info("pull %s: %s", model, status)
                    last = status
        log.info("Model %s ready", model)

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,  # Ollama constrains decoding to this JSON schema
            "stream": False,
            "options": options or {},
        }
        try:
            resp = await self._http.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned HTTP {resp.status_code}: {resp.text[:500]}")
        content = resp.json().get("message", {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid JSON: {content[:500]}") from exc
        if not isinstance(data, dict):
            raise LLMError("LLM returned JSON that is not an object")
        return data
