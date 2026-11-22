"""Spracherkennung über einen OpenAI-kompatiblen Endpunkt.

Passt zu Speaches / faster-whisper-server, whisper.cpp-Server, vLLM und
allen anderen Diensten mit ``POST /v1/audio/transcriptions``.
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
import numpy as np

from .audio import STT_RATE, to_wav_bytes

log = logging.getLogger("kuro.voice.stt")

# Typische Whisper-Halluzinationen bei Stille oder Rauschen.
_JUNK = re.compile(
    r"untertitel(ung)?\b.*(amara|zdf|ard|funk|community|auftrag)|vielen dank f(ü|ue)rs zuschauen"
    r"|bis zum n(ä|ae)chsten mal|copyright|www\.|^\W*(musik|applaus|lachen)\W*$",
    re.IGNORECASE,
)


class STTError(RuntimeError):
    pass


def clean_transcript(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text or _JUNK.search(text) or not re.search(r"\w", text):
        return ""
    return text


class STTClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        language: str = "de",
        api_key: str = "",
        prompt: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.prompt = prompt
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            self._session = aiohttp.ClientSession(headers=headers, timeout=self._timeout)
        return self._session

    async def transcribe(self, samples: np.ndarray, rate: int = STT_RATE) -> str:
        form = aiohttp.FormData()
        form.add_field("file", to_wav_bytes(samples, rate), filename="audio.wav",
                       content_type="audio/wav")
        form.add_field("model", self.model)
        form.add_field("response_format", "json")
        form.add_field("temperature", "0")
        if self.language:
            form.add_field("language", self.language)
        if self.prompt:
            form.add_field("prompt", self.prompt)
        try:
            async with self._get_session().post(
                f"{self.base_url}/audio/transcriptions", data=form
            ) as resp:
                if resp.status >= 400:
                    raise STTError(f"HTTP {resp.status} vom STT-Endpunkt: {(await resp.text())[:300]}")
                if "json" in resp.headers.get("Content-Type", ""):
                    text = (await resp.json()).get("text", "")
                else:
                    text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise STTError(f"STT-Endpunkt {self.base_url} nicht erreichbar: {exc}") from exc
        return clean_transcript(text)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
