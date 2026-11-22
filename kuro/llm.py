"""Schlanker Chat-Client für OpenAI-kompatible Endpunkte.

Funktioniert mit Ollama, llama.cpp-Server, vLLM, LM Studio, KoboldCpp,
TabbyAPI und OpenAI selbst (``/v1/chat/completions``). Bewusst auf aiohttp
aufgebaut: vollständig asynchron, Streaming ist jederzeit abbrechbar – das
braucht die Sprachfunktion, wenn Kuro unterbrochen wird.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp

log = logging.getLogger("kuro.llm")

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """Der Endpunkt war nicht erreichbar oder hat einen Fehler gemeldet."""


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


def strip_think(text: str) -> str:
    """Entfernt <think>-Blöcke, die manche lokalen Modelle mitschicken."""
    text = _THINK_BLOCK.sub("", text or "")
    open_at = text.lower().find("<think>")
    if open_at != -1:  # abgeschnittener Block ohne Ende
        text = text[:open_at]
    return text.strip()


class ThinkFilter:
    """Filtert <think>…</think> aus einem Token-Strom heraus."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, delta: str) -> str:
        self._buf += delta
        out: list[str] = []
        while self._buf:
            lowered = self._buf.lower()
            if self._inside:
                end = lowered.find(self.CLOSE)
                if end == -1:
                    self._buf = self._buf[-(len(self.CLOSE) - 1):]
                    break
                self._buf = self._buf[end + len(self.CLOSE):].lstrip("\n")
                self._inside = False
                continue
            start = lowered.find(self.OPEN)
            if start != -1:
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(self.OPEN):]
                self._inside = True
                continue
            # Möglichen Tag-Anfang am Ende zurückhalten ("<thi").
            keep = 0
            for size in range(min(len(self.OPEN) - 1, len(lowered)), 0, -1):
                if self.OPEN.startswith(lowered[-size:]):
                    keep = size
                    break
            if keep:
                out.append(self._buf[:-keep])
                self._buf = self._buf[-keep:]
            else:
                out.append(self._buf)
                self._buf = ""
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def chat_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Werkzeugdefinition im Chat-Completions-Format."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        params: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 120.0,
        max_concurrency: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.params = {k: v for k, v in (params or {}).items() if v is not None}
        # Ausdrücklich auf null gesetzte Parameter werden nie gesendet – auch nicht
        # als Einzelfall-Vorgabe (OpenAIs Reasoning-Modelle lehnen sie ab).
        self._never_send = {k for k, v in (params or {}).items() if v is None}
        self.extra_body = dict(extra_body or {})
        self._timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "LLMClient":
        return cls(
            cfg["base_url"],
            cfg["model"],
            cfg.get("api_key", ""),
            params={
                "temperature": cfg.get("temperature"),
                "top_p": cfg.get("top_p"),
                "max_tokens": cfg.get("max_tokens"),
            },
            extra_body=cfg.get("extra_body"),
            timeout=float(cfg.get("request_timeout", 120)),
        )

    # -- intern ---------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers, timeout=self._timeout)
        return self._session

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
        stream: bool,
        model: str | None,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model or self.model, "messages": messages}
        payload.update(self.params)
        payload.update(self.extra_body)
        payload.update({
            k: v for k, v in overrides.items()
            if v is not None and k not in self._never_send
        })
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream"] = True
        return payload

    @staticmethod
    async def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        body = (await resp.text())[:500]
        raise LLMError(f"HTTP {resp.status} vom LLM-Endpunkt: {body}")

    # -- öffentlich -----------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        model: str | None = None,
        **overrides: Any,
    ) -> ChatResult:
        """Eine vollständige Antwort (ohne Streaming), optional mit Werkzeugen."""
        payload = self._payload(
            messages, tools=tools, json_mode=json_mode, stream=False,
            model=model, overrides=overrides,
        )
        url = f"{self.base_url}/chat/completions"
        async with self._sem:
            try:
                async with self._get_session().post(url, json=payload) as resp:
                    await self._raise_for_status(resp)
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise LLMError(f"LLM-Endpunkt {self.base_url} nicht erreichbar: {exc}") from exc

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unerwartete Antwort vom LLM-Endpunkt: {str(data)[:300]}") from exc

        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args: Any = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            calls.append({"name": fn.get("name", ""), "arguments": args})

        usage = data.get("usage") or {}
        if usage:
            log.info(
                "LLM Tokens | Modell: %s | Prompt: %s | Antwort: %s | Gesamt: %s",
                data.get("model", payload["model"]),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )
        return ChatResult(
            text=strip_think(message.get("content") or ""),
            tool_calls=calls,
            usage=usage,
            model=data.get("model", payload["model"]),
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        **overrides: Any,
    ) -> AsyncIterator[str]:
        """Liefert die Antwort stückweise. Abbruch beendet auch die Generierung."""
        payload = self._payload(
            messages, tools=None, json_mode=False, stream=True,
            model=model, overrides=overrides,
        )
        url = f"{self.base_url}/chat/completions"
        think = ThinkFilter()
        async with self._sem:
            try:
                async with self._get_session().post(url, json=payload) as resp:
                    await self._raise_for_status(resp)
                    async for raw in resp.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta") or {}
                        except (ValueError, KeyError, IndexError, TypeError):
                            continue
                        piece = think.feed(delta.get("content") or "")
                        if piece:
                            yield piece
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise LLMError(f"LLM-Endpunkt {self.base_url} nicht erreichbar: {exc}") from exc
        rest = think.flush()
        if rest:
            yield rest

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
