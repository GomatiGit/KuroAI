"""Anbindung der Sprachsitzung an discord.py.

Zuhören braucht die Erweiterung ``discord-ext-voice-recv`` in einer Fassung,
die Discords Ende-zu-Ende-Verschlüsselung (DAVE) beim Empfang beherrscht –
siehe docs/LOKALER_BETRIEB.md. Ohne sie kann Kuro im Sprachkanal sprechen,
aber nichts hören.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import discord

from .audio import PCMBuffer

log = logging.getLogger("kuro.voice.discord")


class VoiceUnavailable(RuntimeError):
    pass


def load_voice_recv() -> Any:
    try:
        from discord.ext import voice_recv  # type: ignore[attr-defined]
    except ImportError as exc:
        raise VoiceUnavailable(
            "discord-ext-voice-recv ist nicht installiert. "
            "Siehe requirements-voice.txt und docs/LOKALER_BETRIEB.md."
        ) from exc
    return voice_recv


def make_sink(voice_recv: Any, loop: asyncio.AbstractEventLoop,
              callback: Callable[[int, bytes], None]) -> Any:
    """Senke, die jedes dekodierte Paket samt Nutzer-ID in den Event-Loop reicht."""

    class KuroSink(voice_recv.AudioSink):  # type: ignore[misc, name-defined]
        def wants_opus(self) -> bool:
            return False

        def write(self, user: Any, data: Any) -> None:
            if user is None or getattr(user, "bot", False):
                return
            pcm = getattr(data, "pcm", None)
            if pcm:
                loop.call_soon_threadsafe(callback, user.id, pcm)

        def cleanup(self) -> None:
            pass

    return KuroSink()


class BufferSource(discord.AudioSource):
    def __init__(self, buffer: PCMBuffer) -> None:
        self.buffer = buffer

    def read(self) -> bytes:
        return self.buffer.read_frame()

    def is_opus(self) -> bool:
        return False


class DiscordPlayback:
    """Spielt einen wachsenden PCM-Puffer im Sprachkanal ab."""

    def __init__(self, voice_client: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._voice_client = voice_client
        self._buffer = PCMBuffer()
        self._done = asyncio.Event()
        if voice_client.is_playing():
            voice_client.stop()

        def after(error: Exception | None) -> None:
            if error:
                log.warning("Wiedergabefehler: %s", error)
            loop.call_soon_threadsafe(self._done.set)

        voice_client.play(BufferSource(self._buffer), after=after)

    @property
    def played_seconds(self) -> float:
        return self._buffer.played_seconds

    def feed(self, pcm: bytes) -> None:
        self._buffer.feed(pcm)

    def finish(self) -> None:
        self._buffer.finish()

    def stop(self) -> None:
        self._buffer.clear()
        self._buffer.finish()
        if self._voice_client.is_playing():
            self._voice_client.stop()

    async def wait(self) -> None:
        await self._done.wait()
