"""Verwaltet die Sprachsitzungen – eine pro Server, mehrere Server gleichzeitig."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping

import discord

from ..settings import resolve_endpoint
from .discord_io import DiscordPlayback, VoiceUnavailable, load_voice_recv, make_sink
from .session import VoiceHooks, VoiceSession
from .stt import STTClient
from .tts import TTSClient

log = logging.getLogger("kuro.voice.manager")

class VoiceManager:
    def __init__(
        self,
        bot: Any,
        cfg: dict[str, Any],
        env: Mapping[str, str],
        *,
        llm: Any,
        store: Any,
        hooks_factory: Callable[[discord.Guild, Any, Callable[[], Any]], VoiceHooks],
    ) -> None:
        self.bot = bot
        self.cfg = cfg
        self.llm = llm
        self.store = store
        self.hooks_factory = hooks_factory
        self.sessions: dict[int, VoiceSession] = {}
        self.text_channels: dict[int, Any] = {}
        self._alone_tasks: dict[int, asyncio.Task[None]] = {}
        self._optout: dict[int, bool] = {}

        stt = resolve_endpoint(cfg["stt"], env)
        tts = resolve_endpoint(cfg["tts"], env)
        self.stt = STTClient(
            stt["base_url"], stt["model"], language=stt.get("language", "de"),
            api_key=stt["api_key"], prompt=stt.get("prompt", ""),
            timeout=float(stt.get("request_timeout", 30)),
        )
        self.tts = TTSClient(
            tts["base_url"], tts["model"], tts.get("voice", ""), api_key=tts["api_key"],
            sample_rate=int(tts.get("sample_rate", 24000)), channels=int(tts.get("channels", 1)),
            instructions_field=tts.get("instructions_field", "instructions"),
            extra_body=tts.get("extra_body"), timeout=float(tts.get("request_timeout", 60)),
        )

    # -- Abmelden vom Zuhören --------------------------------------------

    def is_ignored(self, user_id: int) -> bool:
        """Wer ``ignoriermich`` gewählt hat, wird im Sprachkanal nicht verarbeitet."""
        if self.store is not None:
            return self.store.is_opted_out(user_id)  # im Speicher zwischengespeichert
        return self._optout.get(user_id, False)

    def set_ignored(self, user_id: int, ignored: bool) -> None:
        if self.store is not None:
            self.store.set_opted_out(user_id, ignored)
        else:
            self._optout[user_id] = ignored

    # -- Beitreten / Verlassen -------------------------------------------

    def any_busy(self) -> bool:
        return any(session.busy for session in self.sessions.values())

    def get(self, guild_id: int) -> VoiceSession | None:
        return self.sessions.get(guild_id)

    async def join(self, channel: Any, text_channel: Any) -> VoiceSession:
        """Betritt einen Sprachkanal. Pro Server ist genau eine Sitzung möglich."""
        voice_recv = load_voice_recv()
        guild = channel.guild
        await self.leave(guild.id)

        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
        loop = asyncio.get_running_loop()

        def start_playback() -> DiscordPlayback:
            return DiscordPlayback(voice_client, loop)

        hooks = self.hooks_factory(guild, text_channel, start_playback)
        hooks.is_ignored = self.is_ignored
        hooks.human_count = lambda: sum(
            1 for member in getattr(voice_client.channel, "members", []) if not member.bot
        )
        session = VoiceSession(
            guild_id=guild.id, cfg=self.cfg, stt=self.stt, tts=self.tts,
            llm=self.llm, hooks=hooks,
        )
        session.start()
        try:
            voice_client.listen(make_sink(voice_recv, loop, session.ingest))
        except Exception:
            await session.close()
            await voice_client.disconnect(force=True)
            raise
        self.sessions[guild.id] = session
        self.text_channels[guild.id] = text_channel
        log.info("Sprachsitzung gestartet: %s / %s", guild.name, channel.name)
        return session

    async def leave(self, guild_id: int) -> bool:
        task = self._alone_tasks.pop(guild_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        session = self.sessions.pop(guild_id, None)
        self.text_channels.pop(guild_id, None)
        if session is not None:
            await session.close()
        guild = self.bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None
        if voice_client is not None:
            try:
                await voice_client.disconnect(force=True)
            except Exception as exc:
                log.warning("Trennen vom Sprachkanal fehlgeschlagen: %s", exc)
        return session is not None or voice_client is not None

    def check_alone(self, guild: Any) -> None:
        """Nach Wechseln im Sprachkanal aufrufen: allein gelassen -> später gehen."""
        if guild.id not in self.sessions:
            return
        voice_client = guild.voice_client
        humans = 0
        if voice_client is not None and voice_client.channel is not None:
            humans = sum(1 for m in voice_client.channel.members if not m.bot)
        task = self._alone_tasks.pop(guild.id, None)
        if task is not None:
            task.cancel()
        if humans == 0:
            delay = float(self.cfg.get("leave_when_alone_seconds", 120))
            self._alone_tasks[guild.id] = asyncio.get_running_loop().create_task(
                self._leave_later(guild.id, delay)
            )

    async def _leave_later(self, guild_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        log.info("Niemand mehr im Sprachkanal – verlasse ihn (Guild %s)", guild_id)
        await self.leave(guild_id)

    async def close(self) -> None:
        for guild_id in list(self.sessions):
            await self.leave(guild_id)
        await self.stt.close()
        await self.tts.close()


__all__ = ["VoiceManager", "VoiceUnavailable"]
