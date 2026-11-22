"""Sprachausgabe über einen OpenAI-kompatiblen Endpunkt (``/v1/audio/speech``).

Der Server streamt rohes PCM; so beginnt die Wiedergabe, bevor der Satz fertig
berechnet ist. Stil-/Emotionsanweisungen gehen – falls der Server sie kennt –
im Feld ``instructions`` mit (z. B. Qwen3-TTS über vLLM-Omni).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, AsyncIterator, NamedTuple

import aiohttp

log = logging.getLogger("kuro.voice.tts")


class TTSError(RuntimeError):
    pass


class AudioChunk(NamedTuple):
    pcm: bytes
    rate: int
    channels: int


def parse_wav_header(data: bytes) -> tuple[int, int, int] | None:
    """(Abtastrate, Kanäle, Beginn der Audiodaten) – oder None, wenn unvollständig."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    rate = channels = 0
    position = 12
    while position + 8 <= len(data):
        tag, size = data[position:position + 4], struct.unpack_from("<I", data, position + 4)[0]
        if tag == b"fmt " and position + 16 <= len(data):
            channels, rate = struct.unpack_from("<HI", data, position + 10)
        elif tag == b"data":
            return (rate, channels, position + 8) if rate else None
        position += 8 + size + (size & 1)
    return None


class TTSClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        *,
        api_key: str = "",
        sample_rate: int = 24000,
        channels: int = 1,
        instructions_field: str = "instructions",
        extra_body: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.instructions_field = instructions_field
        self.extra_body = dict(extra_body or {})
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            self._session = aiohttp.ClientSession(headers=headers, timeout=self._timeout)
        return self._session

    def payload(self, text: str, instruction: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
        }
        body.update(self.extra_body)
        if instruction and self.instructions_field:
            body[self.instructions_field] = instruction
        return body

    async def synthesize(self, text: str, instruction: str = "") -> AsyncIterator[AudioChunk]:
        """Liefert PCM-Stücke (s16le), sobald der Server sie schickt."""
        try:
            async with self._get_session().post(
                f"{self.base_url}/audio/speech", json=self.payload(text, instruction)
            ) as resp:
                if resp.status >= 400:
                    raise TTSError(f"HTTP {resp.status} vom TTS-Endpunkt: {(await resp.text())[:300]}")
                rate = int(resp.headers.get("x-audio-sample-rate", self.sample_rate) or self.sample_rate)
                channels = int(resp.headers.get("x-audio-channels", self.channels) or self.channels)
                head = b""
                checked = False
                async for data in resp.content.iter_chunked(4800):
                    if not checked:
                        head += data
                        if len(head) < 4:
                            continue
                        if head[:4] == b"RIFF":  # Server schickt doch WAV
                            parsed = parse_wav_header(head)
                            if parsed is None:
                                if len(head) < 4096:
                                    continue
                                raise TTSError("WAV-Kopf des TTS-Endpunkts nicht lesbar.")
                            rate, channels, start = parsed
                            head = head[start:]
                        checked = True
                        data, head = head, b""
                    if data:
                        yield AudioChunk(data, rate, channels)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TTSError(f"TTS-Endpunkt {self.base_url} nicht erreichbar: {exc}") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
