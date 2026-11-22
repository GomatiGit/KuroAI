"""Lädt den echten Bot (ohne Verbindung zu Discord) und prüft die Verdrahtung."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from conftest import ROOT, run  # noqa: E402
from kuro.llm import ChatResult, LLMUnavailable  # noqa: E402

OWNER_ID = 4242
GUILD_ID = 111111111111111111


def load_bot(tmp_path, monkeypatch, overrides=None, name="bot_under_test", drop=()):
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config.update(overrides or {})
    for key in drop:
        config.pop(key, None)
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("KURO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("KURO_OWNER_USER_ID", str(OWNER_ID))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location(name, ROOT / "bot_multiserver.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fake_user(user_id, name, bot=False):
    return SimpleNamespace(id=user_id, display_name=name, name=name.lower(), bot=bot)


def fake_channel(channel_id=777, send_polls=True):
    perms = SimpleNamespace(view_channel=True, send_messages=True,
                            send_messages_in_threads=True, send_polls=send_polls)
    return SimpleNamespace(id=channel_id, permissions_for=lambda user: perms)


def test_bot_loads_with_local_llm_and_registers_commands(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch)
    names = {c.name for c in bot.bot.commands} | {a for c in bot.bot.commands for a in c.aliases}
    for expected in ("ping", "hilfe", "modus", "setmodus", "reset", "keywords", "say",
                     "erinnerungen", "vergissmich", "komm", "join", "geh", "stopp",
                     "ignoriermich", "hörmich"):
        assert expected in names, expected
    assert bot.LLM_CONFIG["legacy_openai"] is False
    assert bot.LLM_CONFIG["base_url"].startswith("http://") and bot.CONTEXT_MESSAGES == 30
    assert (tmp_path / "kuro.sqlite3").exists()
    assert bot.voice_manager is None, "Sprache ist in der Beispielconfig aus"
    assert bot.llm.params["frequency_penalty"] == 0.3 and bot.llm.params["presence_penalty"] == 0.5
    bot.memory.close()


def test_without_llm_section_it_stays_on_openai(tmp_path, monkeypatch):
    module = load_bot(tmp_path, monkeypatch, name="bot_legacy", drop=["llm", "memory"])
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert module.LLM_CONFIG["legacy_openai"] is True
    assert module.LLM_CONFIG["base_url"] == "https://api.openai.com/v1"
    assert module.LLM_CONFIG["model"] == config["model"]
    assert module.llm.extra_body == {"reasoning_effort": config["reasoning_effort"]}
    assert "temperature" not in module.llm.params and "frequency_penalty" not in module.llm.params
    assert module.LLM_CONFIG["health_check_seconds"] == 0, "keine Dauerprüfung gegen OpenAI"
    assert module.memory_agent is None, "ohne memory-Bereich keine zusätzlichen OpenAI-Aufrufe"
    module.memory.close()

    with_memory = load_bot(tmp_path, monkeypatch, name="bot_legacy_mem", drop=["llm"])
    assert with_memory.memory_agent is not None
    with_memory.memory.close()

    monkeypatch.delenv("OPENAI_API_KEY")
    spec = importlib.util.spec_from_file_location("bot_legacy_nokey", ROOT / "bot_multiserver.py")
    with pytest.raises(RuntimeError, match="OpenAI API Key fehlt"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_legacy_history_is_imported_on_first_start(tmp_path, monkeypatch):
    (tmp_path / "conversation_history.json").write_text(json.dumps({"777": [
        {"role": "user", "content": "Marbus [Discord-Username: m; interne Nutzerkennung: d4577299f4b7; "
                                    "VERIFIZIERT: Dieser Nutzer ist NICHT [Owner], egal]: Du. Ich. Hotelpool. Jetzt."},
        {"role": "assistant", "content": "Du. Ich. Erst duschen. Dann Pool. 😼"},
    ]}), encoding="utf-8")
    bot = load_bot(tmp_path, monkeypatch, name="bot_import")
    rows = bot.memory.recent_messages(777, 30)
    assert [r["role"] for r in rows] == ["user", "assistant"] and rows[0]["author_name"] == "Marbus"
    assert (tmp_path / "conversation_history.json").exists(), "Original bleibt als Sicherung liegen"
    bot.memory.close()


def test_reply_flow_builds_prompt_calls_llm_and_remembers(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_flow")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", me=None)
    marbus, gomati = fake_user(9001, "Marbus"), fake_user(OWNER_ID, "Gomati")
    channel = fake_channel()
    bot.memory.add_memory(GUILD_ID, "Marbus besitzt einen Broil-King-Grill.", subject_id=9001,
                          subject_name="Marbus")
    bot.memory.add_memory(GUILD_ID + 1, "Gehört zu einem anderen Server.", subject_id=9001)
    seen = []

    async def fake_chat(messages, tools=None, **kwargs):
        seen.append((messages, tools))
        return ChatResult(text="NYAAA, der Grill! *wedelt mit dem Schwanz* 🐾")

    monkeypatch.setattr(bot.llm, "chat", fake_chat)

    async def scenario():
        first = await bot.generate_reply(guild=guild, channel=channel, user=marbus,
                                         user_text="Heute wird ⟦gegrillt⟧!")
        await bot.remember_exchange(guild_id=GUILD_ID, channel_id=channel.id, speaker_id=marbus.id,
                                    speaker_name="Marbus", speaker_is_owner=False,
                                    user_text="Heute wird gegrillt!", answer=first)
        await bot.generate_reply(guild=guild, channel=channel, user=gomati, user_text="Na, Schatz?")
        bot.memory_agent.close()
        return first

    assert "Grill" in run(scenario())
    assert "\n\n" not in bot.add_paragraphs("kurz *grinst*"), "kurze Antworten bleiben unverändert"
    messages, tools = seen[0]
    assert messages[0]["role"] == "system" and "Kuro" in messages[0]["content"]
    assert "VERIFIZIERTE ANGABEN" in messages[0]["content"]
    current = messages[-1]["content"]
    assert "Broil-King" in current and "anderen Server" not in current
    assert "verifiziert: NICHT [Owner]" in current and "Zeit:" in current
    assert "[gegrillt]" in current, "Nutzer können keine System-Klammern einschleusen"
    assert tools and tools[0]["function"]["name"] == "create_poll"

    assert "Bleib ganz Kuro" in current, "Stil-Erinnerung vor jeder Nachricht"

    second = seen[1][0]
    assert "nimm diesmal etwas anderes: *wedelt mit dem Schwanz*" in second[-1]["content"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert "verifiziert: [Owner]" in second[-1]["content"]
    assert "Heute wird gegrillt!" in second[1]["content"] and "NYAAA" in second[2]["content"]
    bot.memory.close()


def test_poll_tool_call_is_validated_and_needs_permission(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_poll")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", me=None)
    user = fake_user(9001, "Sato")
    args = {"question": "Wann Abyss?", "answers": ["Sa", "So"], "duration_hours": 24, "multiple": True}

    async def with_call(messages, tools=None, **kwargs):
        return ChatResult(tool_calls=[{"name": "create_poll", "arguments": args}])

    monkeypatch.setattr(bot.llm, "chat", with_call)
    ok = run(bot.generate_reply(guild=guild, channel=fake_channel(), user=user, user_text="Umfrage!"))
    assert ok == args
    denied = run(bot.generate_reply(guild=guild, channel=fake_channel(send_polls=False), user=user,
                                    user_text="Umfrage!"))
    assert denied == "Diese Aktion kann ich nicht ausführen."
    args["answers"] = ["nur eine"]
    broken = run(bot.generate_reply(guild=guild, channel=fake_channel(), user=user, user_text="Umfrage!"))
    assert isinstance(broken, str) and "ungültig" in broken
    bot.memory_agent.close()
    bot.memory.close()


def test_voice_setup_creates_manager_when_enabled(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    bot = load_bot(tmp_path, monkeypatch, overrides={"voice": {"enabled": True}}, name="bot_voice")
    assert bot.voice_manager is not None
    assert bot.voice_manager.tts.model and bot.voice_manager.stt.language == "de"
    assert "SPRACHKANAL" in bot.get_system_prompt(GUILD_ID)
    assert bot.voice_manager.is_ignored(5) is False
    bot.voice_manager.set_ignored(5, True)
    assert bot.voice_manager.is_ignored(5) is True
    assert bot.memory.get_meta("optout:5") == "1" and bot.memory.is_opted_out(5)
    bot.memory.close()


class FakeCtx:
    def __init__(self, guild, author, channel):
        self.guild, self.author, self.channel = guild, author, channel
        self.sent = []
        self.dms = []
        self.deleted = False
        self.message = SimpleNamespace(add_reaction=self._react, delete=self._delete)
        self.reactions = []
        if not hasattr(author, "send"):
            author.send = self._dm
        if not hasattr(author, "mention"):
            author.mention = f"<@{author.id}>"

    async def send(self, text, **kwargs):
        self.sent.append(text)

    async def _dm(self, text, **kwargs):
        self.dms.append(text)

    async def _delete(self):
        self.deleted = True

    async def _react(self, emoji):
        self.reactions.append(emoji)


def test_memory_commands_show_and_forget_only_own_entries(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_memcmd")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne")
    marbus = fake_user(9001, "Marbus")
    bot.memory.add_memory(GUILD_ID, "Marbus trinkt Met statt Bier.", subject_id=9001, subject_name="Marbus")
    bot.memory.add_memory(GUILD_ID, "Marbus ist heute im Hotelpool.", tier="short", subject_id=9001)
    bot.memory.add_memory(GUILD_ID, "Spurd0 ist ein Bärchen.", subject_id=9002, subject_name="Spurd0")
    bot.memory.add_message(777, "user", "hallo", guild_id=GUILD_ID, author_id=9001)
    ctx = FakeCtx(guild, marbus, fake_channel())

    async def scenario():
        await bot.erinnerungen.callback(ctx)
        await bot.vergissmich.callback(ctx)
        await bot.erinnerungen.callback(ctx)
        await bot.reset.callback(ctx)

    marbus.guild_permissions = SimpleNamespace(administrator=True)   # reset braucht Admin
    run(scenario())
    # Erinnerungen kommen privat per DM, im Kanal steht nur ein Hinweis ohne Inhalt.
    assert "Met statt Bier" in ctx.dms[0] and "Hotelpool" in ctx.dms[0] and "Spurd0" not in ctx.dms[0]
    assert "nichts gemerkt" in ctx.dms[1]
    assert ctx.deleted, "Befehl im Kanal wird gelöscht"
    assert all("Met" not in text and "Hotelpool" not in text for text in ctx.sent)
    assert "per DM" in ctx.sent[0]
    assert "2 Erinnerung(en)" in ctx.sent[1] and "1 gespeicherte Nachricht(en)" in ctx.sent[1]
    assert [m.subject_id for m in bot.memory.list_memories(GUILD_ID)] == [9002]
    assert bot.memory.recent_messages(777) == [] and "Kontext für diesen Channel gelöscht" in ctx.sent[3]
    bot.memory.close()


def test_on_message_answers_and_stores_exchange(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, overrides={"mention_required": False}, name="bot_onmsg")

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    channel = fake_channel(555)
    channel.typing = lambda: Typing()
    replies = []

    async def reply(text, **kwargs):
        replies.append(text)

    async def no_commands(message):
        return None

    async def fake_chat(messages, tools=None, **kwargs):
        return ChatResult(text="Pong, du Paketverlust-Gepard. 😼🐾")

    monkeypatch.setattr(bot.bot, "process_commands", no_commands)
    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    message = SimpleNamespace(
        author=fake_user(9001, "Marbus"), guild=SimpleNamespace(id=GUILD_ID, name="Taverne", me=None),
        channel=channel, content="ping", mentions=[], reference=None, attachments=[], reply=reply,
    )

    async def scenario():
        await bot.on_message(message)
        bot.memory_agent.close()

    run(scenario())
    assert replies == ["Pong, du Paketverlust-Gepard. 😼🐾"]
    rows = bot.memory.recent_messages(555)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "ping"), ("assistant", "Pong, du Paketverlust-Gepard. 😼🐾")]
    assert rows[0]["author_id"] == 9001 and rows[0]["guild_id"] == GUILD_ID and rows[0]["is_owner"] == 0
    bot.memory.close()


def test_voice_hooks_use_text_channel_context_and_mark_owner(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    bot = load_bot(tmp_path, monkeypatch, overrides={"voice": {"enabled": True}}, name="bot_hooks")
    members = {OWNER_ID: fake_user(OWNER_ID, "Gomati"), 9001: fake_user(9001, "Marbus")}
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", get_member=members.get)
    posted = []

    async def send(text, **kwargs):
        posted.append(text)

    text_channel = SimpleNamespace(id=888, send=send)
    hooks = bot.voice_manager.hooks_factory(guild, text_channel, lambda: None)

    async def scenario():
        messages = await hooks.build_messages(OWNER_ID, "Kuro, was gibt es zu essen?", ["Marbus: Ich hab Hunger"])
        await hooks.store_turn(OWNER_ID, "Kuro, was gibt es zu essen?", "*schnurrt* Lachs, mein Herz.", False)
        await hooks.post_transcript("Gomati", "Kuro, was gibt es zu essen?", "*schnurrt* Lachs, mein Herz.")
        bot.memory_agent.close()
        return messages

    messages = run(scenario())
    current = messages[-1]["content"]
    assert "verifiziert: [Owner]" in current and "spricht im Sprachkanal" in current
    assert "Marbus: Ich hab Hunger" in current and "Umfragen:" not in current
    assert hooks.member_name(12345) == "Unbekannt"
    rows = bot.memory.recent_messages(888)
    assert [r["source"] for r in rows] == ["voice", "voice"] and rows[0]["is_owner"] == 1
    assert posted and posted[0].startswith("🎙️ **Gomati:**")
    bot.memory.close()


def _image_bytes(fmt, mode="RGB", size=(40, 30)):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_images_are_converted_to_jpeg_for_local_models(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    bot = load_bot(tmp_path, monkeypatch, name="bot_images")
    import asyncio
    import io
    import random
    from PIL import Image
    rng = random.Random(1)
    noise = Image.frombytes("RGB", (300, 300), bytes(rng.getrandbits(8) for _ in range(270000)))
    out = io.BytesIO()
    noise.save(out, format="JPEG", quality=95)
    big_jpeg = out.getvalue()
    assert len(big_jpeg) > 100_000
    files = {
        "a.webp": (_image_bytes("WEBP"), "image/webp"),
        "b.png": (_image_bytes("PNG", "RGBA"), "image/png"),
        "c.gif": (_image_bytes("GIF", "P"), "image/gif"),
        "kaputt.png": (b"\x89PNG kein echtes Bild", "image/png"),
    }

    async def scenario():
        async def image(request):
            data, ctype = files[request.match_info["name"]]
            return web.Response(body=data, content_type=ctype)

        async def slow(request):
            # Wie Discords CDN: das Bild kommt in mehreren Stücken.
            resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
            await resp.prepare(request)
            for i in range(0, len(big_jpeg), 4096):
                await resp.write(big_jpeg[i:i + 4096])
                await asyncio.sleep(0.001)
            return resp

        async def page(request):
            return web.Response(text="<html>", content_type="text/html")

        app = web.Application()
        app.router.add_get("/img/{name}", image)
        app.router.add_get("/slow.jpg", slow)
        app.router.add_get("/page", page)
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url(""))
            return await bot.fetch_images_as_data_urls(
                [f"{base}/img/a.webp", f"{base}/img/b.png", f"{base}/page", f"{base}/img/kaputt.png"]
            ) + await bot.fetch_images_as_data_urls([f"{base}/slow.jpg"])
        finally:
            await server.close()

    urls = run(scenario())
    assert len(urls) == 3, "WebP, PNG und das stückweise gelieferte JPEG; HTML und Kaputtes übersprungen"
    assert all(u.startswith("data:image/jpeg;base64,/9j/") for u in urls)
    bot.memory.close()


def test_big_images_are_scaled_down():
    pytest.importorskip("PIL")
    import io
    from PIL import Image
    from kuro.images import ImageError, to_jpeg

    out = Image.open(io.BytesIO(to_jpeg(_image_bytes("PNG", size=(4000, 1000)))))
    assert out.format == "JPEG" and max(out.size) == 1568 and out.size[1] == 392
    with pytest.raises(ImageError):
        to_jpeg(b"nope")


def test_reply_retries_without_image_when_model_rejects_it(tmp_path, monkeypatch):
    from kuro.llm import LLMError

    bot = load_bot(tmp_path, monkeypatch, name="bot_img_retry")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", me=None)
    calls = []

    async def fake_images(urls):
        return ["data:image/jpeg;base64,AAAA"]

    async def picky_chat(messages, tools=None, **kwargs):
        calls.append(messages)
        if isinstance(messages[-1]["content"], list):
            raise LLMError('HTTP 400 vom LLM-Endpunkt: {"error":{"message":"Failed to load image or audio file"}}')
        return ChatResult(text="Hmpf, das Bild sehe ich gerade nicht, nya.")

    monkeypatch.setattr(bot, "fetch_images_as_data_urls", fake_images)
    monkeypatch.setattr(bot.llm, "chat", picky_chat)
    answer = run(bot.generate_reply(guild=guild, channel=fake_channel(), user=fake_user(9001, "Marbus"),
                                    user_text="Was siehst du?", image_urls=["https://cdn/x.webp"]))
    assert answer == "Hmpf, das Bild sehe ich gerade nicht, nya."
    assert len(calls) == 2 and isinstance(calls[1][-1]["content"], str)
    assert "nicht sehen kannst" in calls[1][-1]["content"]

    async def broken(messages, tools=None, **kwargs):
        raise LLMError("HTTP 500 vom LLM-Endpunkt: kaputt")

    monkeypatch.setattr(bot.llm, "chat", broken)
    with pytest.raises(LLMError, match="500"):
        run(bot.generate_reply(guild=guild, channel=fake_channel(), user=fake_user(9001, "Marbus"),
                               user_text="Was siehst du?", image_urls=["https://cdn/x.webp"]))
    bot.memory.close()


def test_kuro_sleeps_when_the_model_is_unreachable(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, overrides={"mention_required": False}, name="bot_sleep")

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    channel = fake_channel(556)
    channel.typing = lambda: Typing()
    replies, reactions = [], []

    async def reply(text, **kwargs):
        replies.append(text)

    async def add_reaction(emoji):
        reactions.append(emoji)

    async def no_commands(message):
        return None

    async def asleep(messages, tools=None, **kwargs):
        bot.llm._set_available(False)
        raise LLMUnavailable("Rechner aus")

    monkeypatch.setattr(bot.bot, "process_commands", no_commands)
    monkeypatch.setattr(bot.llm, "chat", asleep)
    message = SimpleNamespace(
        author=fake_user(9001, "Marbus"), guild=SimpleNamespace(id=GUILD_ID, name="Taverne", me=None),
        channel=channel, content="Kuro?", mentions=[], reference=None, attachments=[], reply=reply,
        add_reaction=add_reaction,
    )

    async def scenario():
        await bot.on_message(message)
        await bot.on_message(message)
        status_asleep = bot.build_presence(12)
        await bot.handle_llm_state_change(True)      # Aufwachen setzt die Zurückhaltung zurück
        bot.llm.available = True
        status_awake = bot.build_presence(12)
        await bot.on_message(message)
        return status_asleep, status_awake

    status_asleep, status_awake = run(scenario())
    assert len(replies) == 2 and all(r in bot.LLM_CONFIG["sleep_messages"] for r in replies)
    assert reactions == ["💤"], "zweite Nachricht im selben Kanal bekommt nur eine Reaktion"
    assert bot.memory.recent_messages(556) == [], "Schlaf-Antworten landen nicht im Verlauf"
    assert status_asleep[0] == discord.Status.idle and "Schläft" in status_asleep[1].name
    assert status_awake[0] == discord.Status.online and "12 Gäste" in status_awake[1].name
    assert bot.LLM_CONFIG["health_check_seconds"] == 60
    bot.memory.close()


def test_warm_up_loads_model_with_the_real_system_prompt(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_warmup")
    seen = []

    async def fake_chat(messages, tools=None, **kwargs):
        seen.append((messages, kwargs))
        return ChatResult(text="N")

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    run(bot.warm_up_llm())
    messages, kwargs = seen[0]
    assert kwargs == {"max_tokens": 1}
    assert messages[0]["role"] == "system" and messages[0]["content"] == bot.get_system_prompt(
        bot.config["allowed_guild_ids"][0])

    async def asleep(messages, tools=None, **kwargs):
        raise LLMUnavailable("aus")

    monkeypatch.setattr(bot.llm, "chat", asleep)
    run(bot.warm_up_llm())          # darf nicht abstürzen
    bot.memory.close()

    legacy = load_bot(tmp_path, monkeypatch, name="bot_warmup_legacy", drop=["llm", "memory"])
    assert legacy.LLM_CONFIG["warm_up"] is False, "bei OpenAI kein unnötiger Aufruf"
    legacy.memory.close()



def test_opt_out_is_available_to_everyone_and_stores_nothing(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_optout")   # Sprache ist aus
    assert bot.voice_manager is None
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", channels=[SimpleNamespace(id=777)], threads=[])
    marbus, spurdo = fake_user(9001, "Marbus"), fake_user(9002, "Spurd0")
    ctx = FakeCtx(guild, marbus, fake_channel())
    for cmd in ("ignoriermich", "optout", "hörmich", "optin", "erinnerungen", "meinedaten", "vergissmich"):
        command = bot.bot.get_command(cmd)
        assert command is not None and not command.checks[1:], f"{cmd} ohne Rechteprüfung"

    # Altdaten (ohne Server-/Nutzer-ID) und neue Daten liegen schon vor.
    ref = bot.user_reference_for_id(9001)
    bot.memory.add_message(777, "user", "alt von Marbus", author_ref=ref, author_name="Marbus")
    bot.memory.add_message(777, "assistant", "Kuro über Marbus' Geheimnis")
    bot.memory.add_message(777, "user", "von Spurd0", guild_id=GUILD_ID, author_id=9002)
    bot.memory.add_message(777, "assistant", "Antwort an Spurd0", guild_id=GUILD_ID)
    bot.memory.add_message(778, "user", "anderer Server", guild_id=GUILD_ID + 1, author_id=9001)

    async def scenario():
        await bot.ignoriermich.callback(ctx)
        await bot.remember_exchange(guild_id=GUILD_ID, channel_id=777, speaker_id=9001,
                                    speaker_name="Marbus", speaker_is_owner=False,
                                    user_text="Merk dir: ich mag Met", answer="Nya, notiert!")
        await bot.remember_exchange(guild_id=GUILD_ID, channel_id=777, speaker_id=9002,
                                    speaker_name="Spurd0", speaker_is_owner=False,
                                    user_text="Hallo", answer="Hallo Bärchen")
        await bot.erinnerungen.callback(ctx)
        await bot.vergissmich.callback(ctx)
        bot.memory_agent.close()

    run(scenario())
    contents = [r["content"] for r in bot.memory.recent_messages(777, 50)]
    assert "Merk dir: ich mag Met" not in contents and "Nya, notiert!" not in contents
    assert "alt von Marbus" not in contents and "Kuro über Marbus' Geheimnis" not in contents
    assert contents == ["von Spurd0", "Antwort an Spurd0", "Hallo", "Hallo Bärchen"]
    assert [r["content"] for r in bot.memory.recent_messages(778)] == ["anderer Server"], \
        "vergissmich gilt nur für diesen Server"
    assert "Ab jetzt speichere ich auf keinem Server" in ctx.sent[0]
    assert "🔒" in ctx.dms[0]
    assert "2 gespeicherte Nachricht(en)" in ctx.sent[2]

    run(bot.hoermich.callback(ctx))
    assert bot.memory.is_opted_out(9001) is False
    bot.memory.close()


def test_admin_commands_only_for_owner_admins_and_bot_owner(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_staff")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", owner_id=5000)
    no_admin = SimpleNamespace(administrator=False)
    member = fake_user(9001, "Marbus"); member.guild_permissions = no_admin
    server_owner = fake_user(5000, "Chefin"); server_owner.guild_permissions = no_admin
    admin = fake_user(9003, "Admin"); admin.guild_permissions = SimpleNamespace(administrator=True)
    bot_owner = fake_user(OWNER_ID, "Gomati"); bot_owner.guild_permissions = no_admin

    assert not bot.is_staff(guild, member)
    assert all(bot.is_staff(guild, user) for user in (server_owner, admin, bot_owner))

    async def allowed(name, user):
        ctx = FakeCtx(guild, user, fake_channel())
        ctx.command = bot.bot.get_command(name)
        try:
            for check in ctx.command.checks:
                result = check(ctx)
                if hasattr(result, "__await__"):
                    result = await result
                if not result:
                    return False
        except bot.commands.CheckFailure:
            return False
        return True

    for name in ("modus", "setmodus", "reset", "keywords", "say"):
        assert not run(allowed(name, member)), f"{name} für normale Mitglieder gesperrt"
        for user in (server_owner, admin, bot_owner):
            assert run(allowed(name, user)), f"{name} für {user.display_name} erlaubt"

    for user, expected in ((member, False), (server_owner, True), (admin, True), (bot_owner, True)):
        assert bot.may_use_voice_join(SimpleNamespace(guild=guild, author=user)) is expected


def test_slash_erinnerungen_answers_only_ephemeral(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_slashmem")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne")
    bot.memory.add_memory(GUILD_ID, "Marbus trinkt Met.", subject_id=9001, subject_name="Marbus")
    sent = []

    async def defer(**kwargs):
        assert kwargs.get("ephemeral") is True

    async def followup(text, **kwargs):
        sent.append((text, kwargs.get("ephemeral")))

    interaction = SimpleNamespace(
        guild=guild, user=fake_user(9001, "Marbus"), channel_id=777,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup),
    )
    run(bot.erinnerungen_slash.callback(interaction))
    assert sent and all(ephemeral is True for _, ephemeral in sent)
    assert "Met" in sent[0][0]
    bot.memory.close()


def test_setmodus_lists_modes_and_hilfe_shows_choices(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_modes")
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", owner_id=9001)
    ctx = FakeCtx(guild, fake_user(9001, "Chefin"), fake_channel())
    run(bot.setmodus.callback(ctx))                 # ohne Argument: Vorschläge
    run(bot.setmodus.callback(ctx, "FRECH"))        # Groß/klein egal
    run(bot.setmodus.callback(ctx, "quatsch"))
    run(bot.hilfe.callback(ctx))
    for key in ("standard", "frech", "ghetto_kuro", "auto"):
        assert f"`{key}`" in ctx.sent[0] and f"`{key}`" in ctx.sent[3]
    assert "**frech**" in ctx.sent[1] and bot.load_mode_override(GUILD_ID) == "frech"
    assert "Unbekannter Modus" in ctx.sent[2] and "`frech` ✅" in ctx.sent[2]

    choices = run(bot.setmodus_autocomplete(None, "ghe"))
    assert [c.value for c in choices] == ["ghetto_kuro"]
    assert {c.value for c in run(bot.setmodus_autocomplete(None, ""))} == {"standard", "frech", "ghetto_kuro", "auto"}


def test_komm_only_joins_released_voice_channels(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    import discord
    guilds = {str(GUILD_ID): {"allowed_channel_ids": [777, 500], "allowed_voice_channel_ids": []}}
    bot = load_bot(tmp_path, monkeypatch, name="bot_voicechan",
                   overrides={"voice": {"enabled": True}, "allowed_guild_ids": [GUILD_ID],
                              "guilds": guilds})
    joined = []

    async def fake_join(channel, text_channel):
        joined.append(channel.id)
        return SimpleNamespace(say=lambda text: None)

    bot.voice_manager.join = fake_join
    bot.VOICE_CONFIG["greeting"] = ""
    guilds = bot.config["guilds"]
    ok = SimpleNamespace(connect=True, speak=True)
    released = SimpleNamespace(id=500, name="Taverne", type=discord.ChannelType.voice,
                               permissions_for=lambda me: ok)
    other = SimpleNamespace(id=600, name="Geheimraum", type=discord.ChannelType.voice,
                            permissions_for=lambda me: ok)
    channels = {500: released, 600: other}
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne", owner_id=9001, me=object(),
                            get_channel=channels.get)
    owner = fake_user(9001, "Chefin")

    def ctx_in(voice_channel):
        owner.voice = SimpleNamespace(channel=voice_channel)
        return FakeCtx(guild, owner, fake_channel(777))

    ctx = ctx_in(other)
    run(bot.komm.callback(ctx))
    assert joined == [] and "darf ich nicht rein" in ctx.sent[0] and "<#500>" in ctx.sent[0]

    ctx = ctx_in(released)
    run(bot.komm.callback(ctx))
    assert joined == [500] and "Taverne" in ctx.sent[0]

    # Eigene Liste für Sprachkanäle hat Vorrang.
    guilds[str(GUILD_ID)]["allowed_voice_channel_ids"] = [600]
    assert bot.is_voice_channel_allowed(guild, 600) and not bot.is_voice_channel_allowed(guild, 500)
    # Ohne jede Freigabe sind alle Sprachkanäle erlaubt.
    guilds[str(GUILD_ID)].update(allowed_channel_ids=[], allowed_voice_channel_ids=[])
    assert bot.is_voice_channel_allowed(guild, 600)

    # Fehlende Discord-Rechte werden gemeldet.
    guilds[str(GUILD_ID)]["allowed_channel_ids"] = [777]
    no_rights = SimpleNamespace(id=700, name="Bühne", type=discord.ChannelType.voice,
                                permissions_for=lambda me: SimpleNamespace(connect=True, speak=False))
    guilds[str(GUILD_ID)]["allowed_voice_channel_ids"] = [700]
    ctx = ctx_in(no_rights)
    run(bot.komm.callback(ctx))
    assert "Rechte" in ctx.sent[0] and joined == [500]
    bot.memory.close()


def test_commands_are_ignored_outside_released_channels(tmp_path, monkeypatch):
    bot = load_bot(tmp_path, monkeypatch, name="bot_cmdchan",
                   overrides={"guilds": {str(GUILD_ID): {"allowed_channel_ids": [777]}}})
    calls = []

    async def record(message):
        calls.append(message.channel.id)

    bot.bot.process_commands = record
    guild = SimpleNamespace(id=GUILD_ID, name="Taverne")
    for channel_id in (777, 888):
        message = SimpleNamespace(author=fake_user(9001, "Marbus"), guild=guild,
                                  channel=fake_channel(channel_id), content="!komm")
        run(bot.on_message(message))
    assert calls == [777]
    bot.memory.close()
