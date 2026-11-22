"""Eine Sprachsitzung: Kuro hört in einem Sprachkanal zu und antwortet laut.

Ablauf pro Äußerung:

1. ``ingest`` bekommt Audio *je Nutzer* (Discord trennt die Sprecher bereits),
   die VAD schneidet daraus Äußerungen.
2. STT macht Text daraus. Stopp-Befehle unterbrechen Kuro sofort.
3. Ist Kuro gemeint (Name, Anschlussfrage, Zweiergespräch), antwortet sie:
   Das LLM streamt, fertige Sätze gehen sofort an die TTS, das Audio sofort
   in den Kanal. Nicht an sie gerichtete Sätze merkt sie sich nur kurz als
   Gesprächskontext (im Arbeitsspeicher, nicht in der Datenbank).

Die Klasse kennt discord.py nicht; Wiedergabe und Bot-Anbindung kommen über
``VoiceHooks`` herein. Dadurch lässt sich das Verhalten mit Attrappen testen.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..llm import LLMUnavailable
from ..speech import SpeechSegment, SpeechStream
from .audio import StreamResampler, discord_to_mono16k
from .vad import END, START, UtteranceDetector

log = logging.getLogger("kuro.voice.session")

IDLE = "idle"
THINKING = "thinking"
SPEAKING = "speaking"


class Playback(Protocol):
    played_seconds: float

    def feed(self, pcm: bytes) -> None: ...
    def finish(self) -> None: ...
    def stop(self) -> None: ...
    async def wait(self) -> None: ...


@dataclass
class VoiceHooks:
    """Verbindung zum Bot. Alle Funktionen sind asynchron, außer wo vermerkt."""

    # (user_id, text, mitgehörte Zeilen) -> Chat-Nachrichten fürs LLM
    build_messages: Callable[[int, str, list[str]], Awaitable[list[dict[str, Any]]]]
    # (user_id, gesagter Text, Kuros Antwort, unterbrochen?) -> speichern
    store_turn: Callable[[int, str, str, bool], Awaitable[None]]
    # startet die Wiedergabe im Kanal (synchron)
    start_playback: Callable[[], Playback]
    # Anzeigename eines Nutzers (synchron)
    member_name: Callable[[int], str]
    # Anzahl Menschen im Kanal (synchron)
    human_count: Callable[[], int] = lambda: 2
    # Nutzer möchte nicht gehört werden (synchron)
    is_ignored: Callable[[int], bool] = lambda user_id: False
    # optionale Mitschrift in den Textkanal: (Sprecher, gehört, Antwort)
    post_transcript: Callable[[str, str, str], Awaitable[None]] | None = None


def _normalize(text: str) -> str:
    return re.sub(r"[^\wäöüß\- ]+", " ", text.lower()).strip()


def make_matcher(phrases: list[str]) -> re.Pattern[str]:
    cleaned = sorted({_normalize(p) for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not cleaned:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(p) for p in cleaned) + r")(?!\w)")


class VoiceSession:
    def __init__(
        self,
        *,
        guild_id: int,
        cfg: dict[str, Any],
        stt: Any,
        tts: Any,
        llm: Any,
        hooks: VoiceHooks,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.guild_id = guild_id
        self.cfg = cfg
        self.stt = stt
        self.tts = tts
        self.llm = llm
        self.hooks = hooks
        self.clock = clock
        self.state = IDLE
        self.closed = False

        self._names = make_matcher(list(cfg.get("names", ["kuro"])))
        self._stops = make_matcher(list(cfg.get("stop_phrases", ["stopp"])))
        self._detectors: dict[int, UtteranceDetector] = {}
        self._heard: deque[tuple[float, str]] = deque(maxlen=8)
        self._turns: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()
        self._current: asyncio.Task[None] | None = None
        self._playback: Playback | None = None
        self._last_partner: int | None = None
        self._last_reply_end = float("-inf")
        self._tasks: set[asyncio.Task[Any]] = set()
        self._voice_lock = asyncio.Lock()  # immer nur eine Wiedergabe gleichzeitig
        self._worker: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None

    # -- Lebenszyklus ---------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._worker = loop.create_task(self._run_turns())
        self._ticker = loop.create_task(self._tick())

    async def close(self) -> None:
        self.closed = True
        self.interrupt("close")
        for task in (self._worker, self._ticker, *self._tasks):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._worker, self._ticker, *self._tasks) if t is not None),
            return_exceptions=True,
        )

    @property
    def busy(self) -> bool:
        return self.state != IDLE

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- Audio hinein ---------------------------------------------------

    def ingest(self, user_id: int, pcm: bytes) -> None:
        """20-ms-Paket (48 kHz Stereo) eines Nutzers. Im Event-Loop aufrufen."""
        if self.closed or self.hooks.is_ignored(user_id):
            return
        detector = self._detectors.get(user_id)
        if detector is None:
            detector = self._detectors[user_id] = UtteranceDetector(**self.cfg.get("vad", {}))
        self._handle_events(user_id, detector.feed(discord_to_mono16k(pcm), self.clock()))

    def _handle_events(self, user_id: int, events: list[tuple[str, Any]]) -> None:
        for kind, audio in events:
            if kind == START:
                if self.state == SPEAKING and self.cfg.get("interrupt_mode") == "speech":
                    self.interrupt("speech")
            elif kind == END and audio is not None:
                self._spawn(self._on_utterance(user_id, audio))

    async def _tick(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.1)
            now = self.clock()
            for user_id, detector in list(self._detectors.items()):
                self._handle_events(user_id, detector.tick(now))

    # -- Verstehen ------------------------------------------------------

    def is_stop_command(self, text: str) -> bool:
        normalized = _normalize(text)
        return len(normalized.split()) <= 6 and bool(self._stops.search(normalized))

    def is_addressed(self, user_id: int, text: str) -> bool:
        mode = self.cfg.get("respond_mode", "smart")
        if mode == "always":
            return True
        if self._names.search(_normalize(text)):
            return True
        if mode != "smart":
            return False
        if self.hooks.human_count() <= 1:
            return True
        followup = float(self.cfg.get("followup_seconds", 20))
        waiting = self.busy and user_id == self._last_partner
        recent = user_id == self._last_partner and self.clock() - self._last_reply_end <= followup
        return waiting or recent

    def recent_heard(self, max_age: float = 120.0) -> list[str]:
        now = self.clock()
        return [line for at, line in self._heard if now - at <= max_age]

    async def _on_utterance(self, user_id: int, audio: Any) -> None:
        try:
            text = await self.stt.transcribe(audio)
        except Exception as exc:
            log.warning("Spracherkennung fehlgeschlagen: %s", exc)
            return
        if text:
            await self.handle_text(user_id, text)

    async def handle_text(self, user_id: int, text: str) -> None:
        """Verarbeitet eine erkannte Äußerung (auch direkt für Tests nutzbar)."""
        name = self.hooks.member_name(user_id)
        log.info("Gehört [%s]: %s", name, text)

        if self.busy and self.cfg.get("interrupt_mode", "command") != "off" \
                and self.is_stop_command(text):
            self.interrupt("command")
            acks = self.cfg.get("interrupt_ack") or []
            if acks:
                self._spawn(self.say(random.choice(acks)))
            return

        if not self.is_addressed(user_id, text):
            self._heard.append((self.clock(), f"{name}: {text}"))
            return

        while self._turns.qsize() >= 2:  # nicht ewig hinterherhinken
            self._turns.get_nowait()
        self._turns.put_nowait((user_id, name, text))

    # -- Antworten ------------------------------------------------------

    async def _run_turns(self) -> None:
        while not self.closed:
            user_id, name, text = await self._turns.get()
            self._current = asyncio.get_running_loop().create_task(
                self._respond(user_id, name, text)
            )
            try:
                await self._current
            except asyncio.CancelledError:
                if self.closed:
                    raise
            except LLMUnavailable:
                log.warning("Sprachantwort entfällt: Sprachmodell nicht erreichbar")
            except Exception:
                log.exception("Sprachantwort fehlgeschlagen")
            finally:
                self._current = None

    def interrupt(self, reason: str = "command") -> bool:
        """Bricht die laufende Antwort ab. Gibt True zurück, wenn etwas lief."""
        while not self._turns.empty():
            self._turns.get_nowait()
        task = self._current
        if task is None or task.done():
            return False
        log.info("Kuro wird unterbrochen (%s)", reason)
        task.cancel()
        if self._playback is not None:
            self._playback.stop()
        return True

    async def _speak_segments(self, queue: "asyncio.Queue[SpeechSegment | None]",
                              spoken: list[str]) -> None:
        while True:
            segment = await queue.get()
            if segment is None:
                break
            resampler: StreamResampler | None = None
            async for chunk in self.tts.synthesize(segment.text, segment.instruction):
                if resampler is None:
                    resampler = StreamResampler(chunk.rate, chunk.channels)
                if self._playback is None:
                    self._playback = self.hooks.start_playback()
                    self.state = SPEAKING
                self._playback.feed(resampler.feed(chunk.pcm))
            spoken.append(segment.text)
        if self._playback is not None:
            self._playback.finish()
            await self._playback.wait()

    def _speech_stream(self) -> SpeechStream:
        tts_cfg = self.cfg.get("tts", {})
        return SpeechStream(
            pronunciation=self.cfg.get("pronunciation"),
            emotions=self.cfg.get("emotions"),
            base_instruction=tts_cfg.get("base_instruction", ""),
        )

    async def _respond(self, user_id: int, name: str, text: str) -> None:
        self._last_partner = user_id
        async with self._voice_lock:
            await self._respond_locked(user_id, name, text)

    async def _respond_locked(self, user_id: int, name: str, text: str) -> None:
        self.state = THINKING
        self._playback = None
        full: list[str] = []
        spoken: list[str] = []
        interrupted = False
        finished = False
        queue: asyncio.Queue[SpeechSegment | None] = asyncio.Queue()
        speaker = asyncio.get_running_loop().create_task(self._speak_segments(queue, spoken))
        try:
            messages = await self.hooks.build_messages(user_id, text, self.recent_heard())
            stream = self._speech_stream()
            async for delta in self.llm.stream(
                messages, max_tokens=int(self.cfg.get("max_reply_tokens", 220))
            ):
                full.append(delta)
                for segment in stream.feed(delta):
                    queue.put_nowait(segment)
            for segment in stream.flush():
                queue.put_nowait(segment)
            queue.put_nowait(None)
            await speaker
            finished = True
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if not speaker.done():
                speaker.cancel()
            if not finished and self._playback is not None:
                self._playback.stop()  # Abbruch oder Fehler: Kanal sofort freigeben
            self._playback = None
            self.state = IDLE
            self._last_reply_end = self.clock()
            reply = "".join(full).strip()
            if interrupted:
                reply = (" ".join(spoken) + " – (unterbrochen)").strip() if spoken else ""
            if reply:
                # eigener Task, damit ein Abbruch das Speichern nicht verhindert
                self._spawn(self._after_turn(user_id, name, text, reply, interrupted))

    async def _after_turn(self, user_id: int, name: str, text: str, reply: str,
                          interrupted: bool) -> None:
        try:
            await self.hooks.store_turn(user_id, text, reply, interrupted)
            if self.hooks.post_transcript is not None and self.cfg.get("post_transcript", True):
                await self.hooks.post_transcript(name, text, reply)
        except Exception:
            log.exception("Sprachverlauf konnte nicht gespeichert werden")

    async def say(self, text: str) -> None:
        """Spricht einen festen Text (Begrüßung, Bestätigung) ohne LLM."""
        if self.closed:
            return
        stream = self._speech_stream()
        queue: asyncio.Queue[SpeechSegment | None] = asyncio.Queue()
        for segment in stream.feed(text) + stream.flush():
            queue.put_nowait(segment)
        queue.put_nowait(None)
        async with self._voice_lock:
            self.state = SPEAKING
            self._playback = None
            finished = False
            try:
                await self._speak_segments(queue, [])
                finished = True
            except Exception as exc:
                log.warning("Sprachausgabe fehlgeschlagen: %s", exc)
            finally:
                if not finished and self._playback is not None:
                    self._playback.stop()
                self._playback = None
                self.state = IDLE
