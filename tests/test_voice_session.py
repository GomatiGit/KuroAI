"""Verhalten der Sprachsitzung mit Attrappen für STT, TTS, LLM und Wiedergabe."""

import asyncio

import numpy as np
import pytest

from conftest import run
from kuro.settings import DEFAULTS, deep_merge
from kuro.voice.audio import FRAME_BYTES
from kuro.voice.session import IDLE, VoiceHooks, VoiceSession
from kuro.voice.tts import AudioChunk

GOMATI, MARBUS, SPURDO = 1, 2, 3
NAMES = {GOMATI: "Gomati", MARBUS: "Marbus", SPURDO: "Spurd0"}


class FakePlayback:
    def __init__(self):
        self.pcm = b""
        self.finished = self.stopped = False
        self._done = asyncio.Event()
        self.played_seconds = 0.0

    def feed(self, pcm):
        self.pcm += pcm

    def finish(self):
        self.finished = True
        self._done.set()

    def stop(self):
        self.stopped = True
        self._done.set()

    async def wait(self):
        await self._done.wait()


class FakeTTS:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    async def synthesize(self, text, instruction=""):
        self.calls.append((text, instruction))
        for _ in range(3):
            await asyncio.sleep(self.delay)
            yield AudioChunk(b"\x10\x00" * 240, 24000, 1)


class FakeLLM:
    def __init__(self, reply, delay=0.0):
        self.reply, self.delay, self.calls = reply, delay, []

    async def stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        for i in range(0, len(self.reply), 6):
            await asyncio.sleep(self.delay)
            yield self.reply[i:i + 6]


class FakeSTT:
    def __init__(self, text):
        self.text = text

    async def transcribe(self, audio):
        return self.text


class Rig:
    def __init__(self, reply="*grinst breit* Na endlich, du Schlingel! Setz dich. Hmpf.", humans=3,
                 llm_delay=0.0, tts_delay=0.0, **cfg):
        self.playbacks, self.stored, self.posted, self.built = [], [], [], []
        self.ignored = set()
        self.llm = FakeLLM(reply, llm_delay)
        self.tts = FakeTTS(tts_delay)

        async def build_messages(user_id, text, heard):
            self.built.append((user_id, text, list(heard)))
            return [{"role": "user", "content": text}]

        async def store_turn(user_id, text, reply, interrupted):
            self.stored.append((user_id, text, reply, interrupted))

        async def post(name, text, reply):
            self.posted.append((name, text, reply))

        def start_playback():
            self.playbacks.append(FakePlayback())
            return self.playbacks[-1]

        hooks = VoiceHooks(build_messages=build_messages, store_turn=store_turn,
                           start_playback=start_playback, member_name=lambda uid: NAMES[uid],
                           human_count=lambda: humans, is_ignored=lambda uid: uid in self.ignored,
                           post_transcript=post)
        voice_cfg = deep_merge(DEFAULTS["voice"], cfg)
        self.session = VoiceSession(guild_id=1, cfg=voice_cfg, stt=FakeSTT("Kuro, hallo!"),
                                    tts=self.tts, llm=self.llm, hooks=hooks)

    async def settle(self, timeout=3.0):
        async def idle():
            while self.session.busy or not self.session._turns.empty() or self.session._tasks:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(idle(), timeout)


def scenario(coro_fn, **rig_kwargs):
    async def wrapper():
        rig = Rig(**rig_kwargs)
        rig.session.start()
        try:
            await coro_fn(rig)
        finally:
            await rig.session.close()
    run(wrapper())


def test_addressed_by_name_she_answers_aloud_and_stores_the_turn():
    async def body(rig):
        await rig.session.handle_text(MARBUS, "Hey Kuro, bin wieder da!")
        await asyncio.sleep(0.05)
        await rig.settle()
        spoken = [text for text, _ in rig.tts.calls]
        assert spoken == ["Na endlich, du Schlingel!", "Setz dich. Hmpf."]
        assert "Neckisch" in rig.tts.calls[0][1], "Regieanweisung *grinst* färbt die Stimme"
        assert len(rig.playbacks) == 1 and rig.playbacks[0].finished and not rig.playbacks[0].stopped
        assert abs(len(rig.playbacks[0].pcm) - 2 * 3 * 240 * 2 * 4) <= 16, "24 kHz mono -> 48 kHz stereo"
        assert rig.stored == [(MARBUS, "Hey Kuro, bin wieder da!",
                               "*grinst breit* Na endlich, du Schlingel! Setz dich. Hmpf.", False)]
        assert rig.posted[0][0] == "Marbus"
        assert rig.llm.calls[0][1]["max_tokens"] == 220
        assert rig.session.state == IDLE

    scenario(body)


def test_chatter_between_others_is_only_overheard():
    async def body(rig):
        await rig.session.handle_text(SPURDO, "Hat jemand meinen Krug gesehen?")
        await asyncio.sleep(0.05)
        assert rig.tts.calls == [] and rig.stored == []
        await rig.session.handle_text(MARBUS, "Kuro, weißt du was Spurd0 sucht?")
        await asyncio.sleep(0.05)
        await rig.settle()
        assert rig.built[0][2] == ["Spurd0: Hat jemand meinen Krug gesehen?"]

    scenario(body)


def test_followup_without_name_counts_only_for_same_speaker_and_in_time():
    async def body(rig):
        await rig.session.handle_text(MARBUS, "Kuro, einen Met bitte.")
        await asyncio.sleep(0.05)
        await rig.settle()
        assert rig.session.is_addressed(MARBUS, "Und einen Keks dazu.")
        assert not rig.session.is_addressed(SPURDO, "Und einen Keks dazu.")
        rig.session._last_reply_end -= 60
        assert not rig.session.is_addressed(MARBUS, "Und einen Keks dazu.")
        assert rig.session.is_addressed(MARBUS, "kuro-chan, noch was")

    scenario(body)


def test_alone_with_one_person_everything_is_for_her():
    async def body(rig):
        assert rig.session.is_addressed(GOMATI, "Was gibt es heute zu essen?")

    scenario(body, humans=1)

    async def name_mode(rig):
        assert not rig.session.is_addressed(GOMATI, "Was gibt es heute zu essen?")

    scenario(name_mode, humans=1, respond_mode="name")


def test_stop_command_interrupts_playback_and_is_recorded():
    async def body(rig):
        await rig.session.handle_text(MARBUS, "Kuro, erzähl mir was.")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if rig.playbacks and rig.playbacks[0].pcm:
                break
        assert rig.session.busy
        await rig.session.handle_text(GOMATI, "Kuro, stopp!")
        await asyncio.sleep(0.05)
        await rig.settle()
        assert rig.playbacks[0].stopped, "Wiedergabe wurde sofort beendet"
        said = [text for text, _ in rig.tts.calls]
        assert said[-1] in DEFAULTS["voice"]["interrupt_ack"], "kurze Bestätigung nach dem Stopp"
        assert len(said) < 12, "die restlichen Sätze wurden nicht mehr gesprochen"
        assert all(entry[3] for entry in rig.stored), "als unterbrochen vermerkt"
        assert all("(unterbrochen)" in entry[2] for entry in rig.stored)

    scenario(body, reply="Also gut. " + "Das ist ein langer Satz für die Geschichte. " * 10,
             llm_delay=0.002, tts_delay=0.02)


def test_stop_words_only_count_as_short_command():
    async def body(rig):
        assert rig.session.is_stop_command("Kuro, sei still!")
        assert rig.session.is_stop_command("HALT")
        assert not rig.session.is_stop_command(
            "Ich musste gestern an der Ampel halt machen weil der Bus stoppte und so weiter")
        assert not rig.session.is_stop_command("Haltestelle")
        await rig.session.handle_text(MARBUS, "stopp")   # nichts läuft -> kein Befehl, nur mitgehört
        await asyncio.sleep(0.05)
        assert rig.tts.calls == []

    scenario(body)


def test_speech_mode_barge_in_stops_her_when_someone_starts_talking():
    async def body(rig):
        await rig.session.handle_text(MARBUS, "Kuro, erzähl mir was.")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if rig.playbacks and rig.playbacks[0].pcm:
                break
        t = np.arange(960) / 48000
        loud = np.repeat((9000 * np.sin(2 * np.pi * 220 * t)).astype("<i2"), 2).tobytes()
        assert len(loud) == FRAME_BYTES
        for _ in range(20):                      # 400 ms Sprache von Gomati
            rig.session.ingest(GOMATI, loud)
        await asyncio.sleep(0.05)
        assert rig.playbacks[0].stopped

    scenario(body, reply="Erster Satz der Geschichte. " * 12, llm_delay=0.002, tts_delay=0.02,
             interrupt_mode="speech")


def test_opted_out_users_are_never_processed():
    async def body(rig):
        rig.ignored.add(SPURDO)
        t = np.arange(960) / 48000
        loud = np.repeat((9000 * np.sin(2 * np.pi * 220 * t)).astype("<i2"), 2).tobytes()
        for _ in range(30):
            rig.session.ingest(SPURDO, loud)
        assert SPURDO not in rig.session._detectors

    scenario(body)


def test_audio_path_runs_from_packets_to_reply():
    async def body(rig):
        t = np.arange(960) / 48000
        loud = np.repeat((9000 * np.sin(2 * np.pi * 220 * t)).astype("<i2"), 2).tobytes()
        for _ in range(40):                       # 0,8 s Sprache, dann keine Pakete mehr
            rig.session.ingest(MARBUS, loud)
            await asyncio.sleep(0.001)
        await asyncio.sleep(1.2)                  # Ticker schließt die Äußerung, STT -> "Kuro, hallo!"
        await rig.settle()
        assert rig.built and rig.built[0][:2] == (MARBUS, "Kuro, hallo!")
        assert rig.playbacks and rig.playbacks[0].finished

    scenario(body)


def test_tts_failure_frees_the_channel_and_session_stays_usable():
    class BrokenTTS(FakeTTS):
        async def synthesize(self, text, instruction=""):
            self.calls.append((text, instruction))
            yield AudioChunk(b"\x10\x00" * 240, 24000, 1)
            raise RuntimeError("TTS weg")

    async def body(rig):
        rig.session.tts = BrokenTTS()
        await rig.session.handle_text(MARBUS, "Kuro?")
        await asyncio.sleep(0.05)
        await rig.settle()
        assert rig.playbacks[0].stopped and rig.session.state == IDLE
        rig.session.tts = rig.tts
        await rig.session.handle_text(MARBUS, "Kuro, noch da?")
        await asyncio.sleep(0.05)
        await rig.settle()
        assert rig.playbacks[-1].finished

    scenario(body)
