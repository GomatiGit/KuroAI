import asyncio
import json
import logging
import os
import re
import random
import hashlib
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp
import discord
from discord.ext import commands

from kuro import prompts as kuro_prompts
from kuro.images import ImageError, to_data_url
from kuro.llm import LLMClient, LLMError, LLMUnavailable, chat_tool
from kuro.memory import MemoryStore
from kuro.memory_agent import MemoryAgent
from kuro.settings import resolve_llm, section

DATA_DIR = Path(os.getenv("KURO_DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

MODE_OVERRIDE_DIR = DATA_DIR / "mode_overrides"
CONFIG_PATH = DATA_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("discord-gpt-bot")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} fehlt. "
            "Kopiere config.example.json als config.json in das Datenverzeichnis."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()


DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# Sprachmodell: lokal (Ollama, llama.cpp, vLLM …) oder wie bisher OpenAI.
# Ohne "llm"-Bereich in der config.json bleibt es beim bisherigen OpenAI-Betrieb.
LLM_CONFIG = resolve_llm(config, os.environ)
MEMORY_CONFIG = section(config, "memory")
VOICE_CONFIG = section(config, "voice")
OWNER_USER_ID_RAW = os.getenv("KURO_OWNER_USER_ID", "").strip()

try:
    OWNER_USER_ID = int(OWNER_USER_ID_RAW)
except ValueError:
    OWNER_USER_ID = 0
    
def is_owner(user: discord.abc.User) -> bool:
    return OWNER_USER_ID != 0 and user.id == OWNER_USER_ID

if not DISCORD_TOKEN:
    raise RuntimeError("Discord Token fehlt.")
if LLM_CONFIG["needs_api_key"] and not LLM_CONFIG["api_key"]:
    raise RuntimeError(
        "OpenAI API Key fehlt. Für lokalen Betrieb den Bereich 'llm' "
        "in der config.json eintragen (siehe docs/LOKALER_BETRIEB.md)."
    )
if OWNER_USER_ID == 0:
    raise RuntimeError("KURO_OWNER_USER_ID fehlt oder ist ungültig.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=config.get("prefix", "!"),
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

SAFE_ALLOWED_MENTIONS = discord.AllowedMentions(
    users=True,
    roles=False,
    everyone=False,
    replied_user=False
)

llm = LLMClient.from_config(LLM_CONFIG)

log.info("Autorisierte Server: %s", config.get("allowed_guild_ids", []))
log.info("Sprachmodell: %s über %s", LLM_CONFIG["model"], LLM_CONFIG["base_url"])
# ---------------------------------------------------------------------------
# Multi-Guild helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Gedächtnis (SQLite): Chatkontext, Kurzzeit- und Langzeitgedächtnis
# ---------------------------------------------------------------------------
CONTEXT_MESSAGES = int(MEMORY_CONFIG["context_messages"])
HISTORY_FILE = DATA_DIR / "conversation_history.json"

memory = MemoryStore(
    DATA_DIR / MEMORY_CONFIG["db_file"],
    short_term_hours=MEMORY_CONFIG["short_term_hours"],
    max_messages_per_channel=MEMORY_CONFIG["max_messages_per_channel"],
    max_long_term_per_guild=MEMORY_CONFIG["max_long_term_per_guild"],
)
# Einmalige Übernahme des bisherigen JSON-Verlaufs (die Datei bleibt liegen).
memory.import_legacy_history(HISTORY_FILE)

voice_manager = None  # wird in setup_voice() gesetzt, falls Sprache aktiv ist
background_tasks: set[asyncio.Task] = set()

# Der Gedächtnis-Agent kostet zusätzliche Modellaufrufe. Im bisherigen
# OpenAI-Betrieb läuft er deshalb nur, wenn "memory" ausdrücklich konfiguriert ist.
MEMORY_AGENT_ENABLED = bool(MEMORY_CONFIG["enabled"]) and (
    not LLM_CONFIG["legacy_openai"] or isinstance(config.get("memory"), dict)
)

memory_agent: MemoryAgent | None = None
if MEMORY_AGENT_ENABLED:
    memory_agent = MemoryAgent(
        memory,
        llm,
        min_new_messages=MEMORY_CONFIG["extract_after_messages"],
        idle_seconds=MEMORY_CONFIG["extract_idle_seconds"],
        model=LLM_CONFIG.get("memory_model") or None,
        is_busy=lambda: voice_manager is not None and voice_manager.any_busy(),
    )


def load_style_examples() -> list[dict[str, str]]:
    """Stilbeispiele (echte Kuro-Antworten) als Vorbild für lokale Modelle."""
    path = DATA_DIR / str(LLM_CONFIG.get("style_examples_file") or "")
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Stilbeispiele konnten nicht geladen werden: %s", exc)
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


STYLE_EXAMPLES = load_style_examples()

# ---------------------------------------------------------------------------
# Kuro schläft, wenn der Rechner mit dem Sprachmodell nicht erreichbar ist
# ---------------------------------------------------------------------------
sleep_reply_times: dict[int, float] = {}


def pick_sleep_message() -> str:
    messages = [m for m in LLM_CONFIG.get("sleep_messages", []) if isinstance(m, str) and m.strip()]
    return random.choice(messages) if messages else "Zzz… 💤"


def should_send_sleep_text(channel_id: int) -> bool:
    """Schlaf-Text nur ab und zu je Kanal; dazwischen genügt eine 💤-Reaktion."""
    cooldown = float(LLM_CONFIG.get("sleep_reply_cooldown_minutes", 10)) * 60
    now = time.monotonic()
    last = sleep_reply_times.get(channel_id)

    if last is not None and now - last < cooldown:
        return False

    sleep_reply_times[channel_id] = now
    return True


def build_presence(guest_count: int) -> tuple[discord.Status, discord.CustomActivity]:
    if llm.available:
        return (
            discord.Status.online,
            discord.CustomActivity(name=f"Bedient {guest_count} Gäste in der Taverne"),
        )

    return (
        discord.Status.idle,
        discord.CustomActivity(name=str(LLM_CONFIG.get("sleep_status") or "Schläft 💤")),
    )


async def handle_llm_state_change(available: bool) -> None:
    """Status anpassen und den Wechsel einmal im Log-Channel vermerken."""
    if available:
        sleep_reply_times.clear()
    elif voice_manager is not None:
        # Eine schlafende Kuro bleibt nicht lauschend im Sprachkanal sitzen.
        for guild_id in list(voice_manager.sessions):
            text_channel = voice_manager.text_channels.get(guild_id)
            await voice_manager.leave(guild_id)
            if text_channel is not None:
                try:
                    await text_channel.send(pick_sleep_message())
                except discord.HTTPException:
                    pass

    await update_server_status()

    text = (
        "☀️ Das Sprachmodell ist wieder erreichbar – Kuro ist wach."
        if available
        else "😴 Das Sprachmodell ist nicht erreichbar – Kuro schläft, bis es wieder da ist."
    )
    for guild in list(bot.guilds):
        if is_allowed_guild(guild):
            await send_log_message(guild, text)


def on_llm_state_change(available: bool) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = loop.create_task(handle_llm_state_change(available))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


llm.on_state_change = on_llm_state_change


async def llm_watchdog() -> None:
    """Prüft regelmäßig, ob das Sprachmodell erreichbar ist (Aufwachen/Einschlafen)."""
    interval = float(LLM_CONFIG.get("health_check_seconds", 0) or 0)
    if interval <= 0:
        return

    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await llm.ping()
        except Exception:
            log.exception("Erreichbarkeitsprüfung des Sprachmodells fehlgeschlagen")
        await asyncio.sleep(interval)

reply_lock = defaultdict(asyncio.Lock)
bot_reply_counter = defaultdict(int)
bot_reply_timeout = {}

def is_allowed_guild(guild: discord.Guild | None) -> bool:
    if guild is None:
        return False

    allowed = set(config.get("allowed_guild_ids", []))
    return guild.id in allowed
    
def should_reply_to_limited_bot(message: discord.Message) -> bool:
    if not message.author.bot:
        bot_reply_counter.clear()
        bot_reply_timeout.clear()
        return True

    guild_cfg = get_guild_config(message.guild)
    limits = guild_cfg.get("bot_reply_limits", config.get("bot_reply_limits", {}))
    bot_limit = limits.get(str(message.author.id))

    if not bot_limit:
        return False

    now = datetime.now(ZoneInfo("Europe/Berlin"))

    timeout = bot_reply_timeout.get(message.author.id)
    if timeout and now >= timeout:
        bot_reply_counter[message.author.id] = 0
        del bot_reply_timeout[message.author.id]

    max_replies = int(bot_limit.get("max_replies", 3))
    cooldown_minutes = int(bot_limit.get("cooldown_minutes", 30))

    if bot_reply_counter[message.author.id] >= max_replies:
        return False

    bot_reply_counter[message.author.id] += 1

    if bot_reply_counter[message.author.id] >= max_replies:
        bot_reply_timeout[message.author.id] = now + timedelta(minutes=cooldown_minutes)

    return True

def get_guild_config(guild: discord.Guild | None) -> dict[str, Any]:
    if guild is None:
        return {}

    guilds = config.get("guilds", {})
    return guilds.get(str(guild.id), {})


def get_channel_id_for_guild(guild: discord.Guild | None, key: str) -> int | None:
    guild_cfg = get_guild_config(guild)

    value = guild_cfg.get(key)
    if isinstance(value, int):
        return value

    # Backward compatible fallback for old single-server configs
    value = config.get(key)
    if isinstance(value, int):
        return value

    return None


def get_allowed_channel_ids(guild: discord.Guild | None) -> set[int]:
    guild_cfg = get_guild_config(guild)

    if "allowed_channel_ids" in guild_cfg:
        return set(guild_cfg.get("allowed_channel_ids", []))

    # Backward compatible fallback for old single-server configs
    return set(config.get("allowed_channel_ids", []))


def is_channel_allowed(message: discord.Message) -> bool:
    allowed_channel_ids = get_allowed_channel_ids(message.guild)

    if allowed_channel_ids and message.channel.id not in allowed_channel_ids:
        return False

    return True


def get_guild_messages(guild: discord.Guild | None, key: str) -> list[str]:
    guild_cfg = get_guild_config(guild)

    if key in guild_cfg:
        return guild_cfg.get(key, [])

    # Backward compatible fallback for old single-server configs
    return config.get(key, [])


def is_reply_to_bot(message: discord.Message) -> bool:
    if not bot.user:
        return False

    if not message.reference:
        return False

    resolved = message.reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot.user.id

    return False


async def send_log_message(guild: discord.Guild | None, text: str) -> None:
    channel_id = get_channel_id_for_guild(guild, "log_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        await channel.send(text)
    except discord.Forbidden:
        log.warning("Keine Rechte für Log-Channel %s", channel_id)
    except Exception as e:
        log.warning("Log-Nachricht konnte nicht gesendet werden: %s", e)


GHETTO_DAY_FILE = DATA_DIR / "ghetto_day.json"


def get_or_create_ghetto_day() -> int:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    month_key = now.strftime("%Y-%m")

    if GHETTO_DAY_FILE.exists():
        try:
            data = json.loads(GHETTO_DAY_FILE.read_text(encoding="utf-8"))
            if data.get("month") == month_key:
                return data.get("day")
        except Exception:
            pass

    # neuen Tag generieren (1–28 safe)
    day = random.randint(1, 28)

    GHETTO_DAY_FILE.write_text(
        json.dumps({"month": month_key, "day": day}, indent=2),
        encoding="utf-8"
    )

    log.info("Neuer Ghetto-Kuro Tag für %s: %s", month_key, day)
    return day


def get_active_persona_name(guild_id: int | None = None) -> str:
    if guild_id is not None:
        override = load_mode_override(guild_id)

        if override:
            return override

    now = datetime.now(ZoneInfo("Europe/Berlin"))

    ghetto_day = get_or_create_ghetto_day()

    if now.day == ghetto_day:
        return "ghetto_kuro"

    if now.weekday() == 6:
        return "frech"

    return "standard"


def get_current_mode_name(guild_id: int) -> str:
    return get_active_persona_name(guild_id)


def get_time_of_day() -> str:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    hour = now.hour

    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "day"
    return "evening"

def build_personality_text(guild_id: int | None = None) -> str:
    return kuro_prompts.personality_from_config(
        config,
        get_active_persona_name(guild_id),
        guild_id,
    )


KNOWN_MEMBERS_FILE = DATA_DIR / "known_members.json"
MODE_OVERRIDE_FILE = DATA_DIR / "mode_overrides"


def load_known_members() -> dict[str, list[int]]:
    if not KNOWN_MEMBERS_FILE.exists():
        return {}

    try:
        with KNOWN_MEMBERS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_known_members(data: dict[str, list[int]]) -> None:
    with KNOWN_MEMBERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def has_member_been_here_before(guild_id: int, member_id: int) -> bool:
    data = load_known_members()
    guild_key = str(guild_id)
    member_list = data.get(guild_key, [])
    return member_id in member_list
   

def remember_member(guild_id: int, member_id: int) -> None:
    data = load_known_members()
    guild_key = str(guild_id)

    if guild_key not in data:
        data[guild_key] = []

    if member_id not in data[guild_key]:
        data[guild_key].append(member_id)
        save_known_members(data)


def get_mode_override_path(guild_id: int) -> Path:
    if type(guild_id) is not int or guild_id <= 0:
        raise ValueError("Eine gültige Guild-ID ist erforderlich.")

    return MODE_OVERRIDE_DIR / f"{guild_id}.json"


def load_mode_override(guild_id: int) -> str | None:
    path = get_mode_override_path(guild_id)

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Die Modusdatei muss ein JSON-Objekt enthalten.")

        mode = data.get("mode")

        if isinstance(mode, str) and mode in config.get("personas", {}):
            return mode

        log.warning(
            "Ungültige oder entfernte Persona für Guild %s; "
            "verwende Automatik.",
            guild_id,
        )

    except (OSError, ValueError):
        log.exception(
            "Persona-Auswahl für Guild %s konnte nicht geladen werden.",
            guild_id,
        )

    return None


def save_mode_override(guild_id: int, mode: str) -> None:
    if mode not in config.get("personas", {}):
        raise ValueError("Unbekannte Persona.")

    path = get_mode_override_path(guild_id)
    MODE_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp")

    temporary_path.write_text(
        json.dumps(
            {"mode": mode},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(path)


def clear_mode_override(guild_id: int) -> None:
    path = get_mode_override_path(guild_id)
    path.unlink(missing_ok=True)
        

def get_keyword_reply(message: discord.Message) -> str | None:
    message_content = message.content
    keyword_rules = config.get("keyword_rules", [])
    active = get_active_persona_name(
        message.guild.id if message.guild else None
    )

    bot_name = config.get("personas", {}).get(
        active, {}
    ).get("name", bot.user.display_name if bot.user else "Bot")

    user_name = message.author.display_name

    for rule in keyword_rules:
        pattern = rule.get("pattern")
        responses = rule.get("responses")

        if responses:
            text = random.choice(responses)
        else:
            text = rule.get("response", "")

        if not pattern:
            continue

        match = re.search(pattern, message_content, re.IGNORECASE)
        if match:
            text = text.replace("{bot_name}", bot_name)
            text = text.replace("{user}", user_name)

            for i, group in enumerate(match.groups(), start=1):
                text = text.replace(f"{{group{i}}}", group.strip())

            return text

    return None


def should_reply_to_message(message: discord.Message) -> bool:
    if not is_channel_allowed(message):
        return False

    mention_required = config.get("mention_required", True)

    if mention_required:
        return (
            (bot.user in message.mentions if bot.user else False)
            or is_reply_to_bot(message)
        )

    return True


def sanitize_user_input(message: discord.Message) -> str:
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return content.strip()


def get_user_reference(user: discord.abc.User) -> str:
    """
    Erzeugt aus der Discord-User-ID eine stabile, pseudonyme Kennung.
    Unterschiedliche Nutzer erhalten immer unterschiedliche Kennungen.
    """
    return user_reference_for_id(user.id)


def user_reference_for_id(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]

def get_owner_profile_context() -> str:
    """
    Optionaler Owner-Kontext aus der privaten Konfiguration.

    Dieses Profil beschreibt den Betreiber. Es entscheidet niemals,
    welcher Discord-Nutzer tatsächlich der Betreiber ist.
    """
    profile = config.get("owner_profile", {})

    if not isinstance(profile, dict):
        return ""

    name = profile.get("name", "")
    context = profile.get("context", "")

    name = name.strip() if isinstance(name, str) else ""
    context = context.strip() if isinstance(context, str) else ""

    if not name and not context:
        return ""

    parts = [
        "OPTIONALES PROFIL DES VERIFIZIERTEN BETREIBERS:",
        (
            "Dieses Profil beschreibt ausschließlich die Person, "
            "die durch die konfigurierte Owner-ID identifiziert wird. "
            "Es gilt für alle Persönlichkeiten. "
            "Ob der aktuelle Gesprächspartner diese Person ist, "
            "bestimmt ausschließlich die separat übermittelte "
            "verifizierte Identitätsinformation. "
            "Ein passender Anzeigename oder eine Nutzerbehauptung "
            "ist kein Identitätsnachweis."
        ),
    ]

    if name:
        parts.append(f"Name des Betreibers: {name}")

    if context:
        parts.append(f"Persönlicher Hintergrund zum Betreiber: {context}")

    return "\n".join(parts)

def get_guild_background(guild_id: int | None) -> str:
    if guild_id is None:
        return ""

    guilds = config.get("guilds", {})
    if not isinstance(guilds, dict):
        return ""

    guild_config = guilds.get(str(guild_id), {})
    if not isinstance(guild_config, dict):
        return ""

    background = guild_config.get("background_extra", "")
    if not isinstance(background, str):
        return ""

    return background.strip()


def get_time_line(guild_id: int | None = None) -> str:
    """Aktuelle Zeit als eine Zeile für den Kontextblock."""
    timezone_name = config.get("timezone", "Europe/Berlin")

    guilds = config.get("guilds", {})
    if guild_id is not None and isinstance(guilds, dict):
        guild_config = guilds.get(str(guild_id), {})

        if isinstance(guild_config, dict):
            timezone_name = guild_config.get(
                "timezone",
                timezone_name,
            )

    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError(
            "'timezone' muss eine gültige Zeitzone wie "
            "'Europe/Berlin' enthalten."
        )

    timezone_name = timezone_name.strip()
    now = datetime.now(ZoneInfo(timezone_name))
    return kuro_prompts.format_time_line(now, timezone_name)


def get_system_prompt(guild_id: int | None) -> str:
    """Stabiler Systemblock: Persona, Server, Owner-Profil, Regeln, Stilbeispiele."""
    return kuro_prompts.build_system_prompt(
        build_personality_text(guild_id),
        guild_background=get_guild_background(guild_id),
        owner_profile=get_owner_profile_context(),
        tools_available=bool(LLM_CONFIG["tools"]),
        voice_capable=bool(VOICE_CONFIG["enabled"]),
        style_examples=STYLE_EXAMPLES,
        max_style_examples=int(LLM_CONFIG.get("max_style_examples", 6)),
    )


MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024


async def fetch_images_as_data_urls(urls: list[str]) -> list[str]:
    """Lädt Bildanhänge und bettet sie ein – lokale Modelle können keine URLs abrufen."""
    data_urls: list[str] = []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in urls[:MAX_IMAGES]:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    content_type = resp.headers.get("Content-Type", "image/png").split(";")[0]
                    data = await resp.content.read(MAX_IMAGE_BYTES + 1)
                    if not content_type.startswith("image/") or len(data) > MAX_IMAGE_BYTES:
                        continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Bild konnte nicht geladen werden: %s", exc)
                continue
            # Einheitlich als JPEG: lokale Server lesen nicht jedes Format (z. B. WebP, GIF).
            try:
                data_urls.append(await asyncio.to_thread(to_data_url, data))
            except ImageError as exc:
                log.warning("Bild konnte nicht umgewandelt werden (%s): %s", content_type, exc)
    return data_urls


async def build_chat_messages(
    *,
    guild_id: int,
    channel_id: int,
    speaker_id: int,
    speaker_name: str,
    speaker_is_owner: bool,
    user_text: str,
    image_urls: list[str] | None = None,
    poll_allowed: bool | None = None,
    via_voice: bool = False,
    heard: list[str] | None = None,
    skip_images: bool = False,
) -> list[dict[str, Any]]:
    """Baut die Anfrage: Systemblock, Chatkontext, Erinnerungen, aktuelle Nachricht."""
    history = await asyncio.to_thread(memory.recent_messages, channel_id, CONTEXT_MESSAGES)

    memories: list[str] = []
    if MEMORY_AGENT_ENABLED:
        # Erinnerungen werden als Strom aus der Datenbank gelesen und einsortiert.
        async for item in memory.stream_search(
            guild_id,
            user_text,
            subject_ids=[speaker_id],
            limit=int(MEMORY_CONFIG["max_memories_in_prompt"]),
        ):
            memories.append(item.as_prompt_line())

    notes: list[str] = []
    image_data_urls: list[str] = []
    if image_urls:
        if LLM_CONFIG["vision"] and not skip_images:
            image_data_urls = await fetch_images_as_data_urls(image_urls)
        if not image_data_urls:
            notes.append(
                "Hinweis: Der Nutzer hat ein Bild angehängt, das du gerade nicht sehen kannst. "
                "Sag das offen, statt den Inhalt zu erraten."
            )

    context_block = kuro_prompts.build_context_block(
        time_line=get_time_line(guild_id),
        memories=memories,
        poll_allowed=poll_allowed,
        heard=heard or (),
        notes=notes,
    )

    return kuro_prompts.build_messages(
        get_system_prompt(guild_id),
        history,
        speaker_name=speaker_name,
        speaker_ref=user_reference_for_id(speaker_id),
        is_owner=speaker_is_owner,
        user_text=user_text,
        context_block=context_block,
        via_voice=via_voice,
        image_data_urls=image_data_urls,
    )


async def remember_exchange(
    *,
    guild_id: int,
    channel_id: int,
    speaker_id: int,
    speaker_name: str,
    speaker_is_owner: bool,
    user_text: str,
    answer: str,
    source: str = "text",
) -> None:
    """Speichert Frage und Antwort und stößt den Gedächtnis-Agenten an."""
    def write() -> None:
        memory.add_message(
            channel_id, "user", user_text, guild_id=guild_id, author_id=speaker_id,
            author_ref=user_reference_for_id(speaker_id),
            author_name=speaker_name, is_owner=speaker_is_owner, source=source,
        )
        memory.add_message(channel_id, "assistant", answer, guild_id=guild_id, source=source)

    try:
        await asyncio.to_thread(write)
    except Exception as e:
        log.warning("Konversationsverlauf konnte nicht gespeichert werden: %s", e)
        return
    if memory_agent is not None:
        memory_agent.notify(channel_id, guild_id)


POLL_TOOL = chat_tool(
    "create_poll",
    "Eine ausdrücklich gewünschte Discord-Umfrage im aktuellen Kanal erstellen.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
            },
            "answers": {
                "type": "array",
                "minItems": 2,
                "maxItems": 10,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 55,
                },
            },
            "duration_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 768,
            },
            "multiple": {
                "type": "boolean",
            },
        },
        "required": [
            "question",
            "answers",
            "duration_hours",
            "multiple",
        ],
    },
)


def validate_poll(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Ungültige Umfragedaten.")

    question = data.get("question")
    answers = data.get("answers")
    hours = data.get("duration_hours")
    multiple = data.get("multiple")

    if (
        not isinstance(question, str)
        or not 1 <= len(question.strip()) <= 300
    ):
        raise ValueError(
            "Die Frage muss 1 bis 300 Zeichen enthalten."
        )

    if not isinstance(answers, list) or not 2 <= len(answers) <= 10:
        raise ValueError(
            "Eine Umfrage braucht 2 bis 10 Antworten."
        )

    if any(
        not isinstance(answer, str)
        or not 1 <= len(answer.strip()) <= 55
        for answer in answers
    ):
        raise ValueError(
            "Jede Antwort muss 1 bis 55 Zeichen enthalten."
        )

    answers = [answer.strip() for answer in answers]

    if len({answer.casefold() for answer in answers}) != len(answers):
        raise ValueError(
            "Die Antworten müssen unterschiedlich sein."
        )

    if type(hours) is not int or not 1 <= hours <= 768:
        raise ValueError(
            "Die Dauer muss zwischen 1 und 768 ganzen Stunden liegen."
        )

    if type(multiple) is not bool:
        raise ValueError("Ungültige Einstellung für Mehrfachauswahl.")

    return {
        "question": question.strip(),
        "answers": answers,
        "duration_hours": hours,
        "multiple": multiple,
    }


def can_create_poll(guild, channel, user) -> bool:
    if guild is None or channel is None or user.bot:
        return False

    if not is_allowed_guild(guild):
        return False

    allowed_channels = get_allowed_channel_ids(guild)
    if allowed_channels and channel.id not in allowed_channels:
        return False

    permissions = channel.permissions_for(user)

    if isinstance(channel, discord.Thread):
        can_send = permissions.send_messages_in_threads
    else:
        can_send = permissions.send_messages

    return (
        permissions.view_channel
        and can_send
        and (is_owner(user) or permissions.send_polls)
    )


async def execute_poll(data, guild, channel, user) -> str:
    if not can_create_poll(guild, channel, user):
        return (
            "Du darfst in diesem Kanal keine Umfrage über meine Verwendung erstellen."
        )

    if guild.me is None:
        return "Ich konnte meine eigenen Serverrechte nicht prüfen."

    permissions = channel.permissions_for(guild.me)

    if isinstance(channel, discord.Thread):
        can_send = permissions.send_messages_in_threads
    else:
        can_send = permissions.send_messages

    if (
        not permissions.view_channel
        or not can_send
        or not permissions.send_polls
    ):
        return (
            "Mir fehlen in diesem Kanal die Rechte zum Senden von Nachrichten oder Umfragen."
        )

    try:
        data = validate_poll(data)
    except ValueError as exc:
        return f"Die Umfrage wurde nicht erstellt: {exc}"

    poll = discord.Poll(
        question=data["question"],
        duration=timedelta(hours=data["duration_hours"]),
        multiple=data["multiple"],
    )

    for answer in data["answers"]:
        poll.add_answer(text=answer)

    try:
        sent = await channel.send(
            poll=poll,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        return (
            "Discord hat die Umfrage abgelehnt. "
            "Bitte prüfe meine Kanalrechte."
        )
    except discord.HTTPException:
        log.exception("Discord-Umfrage konnte nicht gesendet werden")
        return (
            "Discord konnte die Umfrage nicht bestätigen. "
            "Bitte prüfe den Kanal, bevor du es erneut versuchst."
        )

    return f"Die Umfrage ist erstellt: {sent.jump_url}"


async def generate_reply(
    *,
    guild: discord.Guild,
    channel: Any,
    user: discord.abc.User,
    user_text: str,
    image_urls: list[str] | None = None,
) -> str | dict[str, Any]:
    """Fragt das Sprachmodell. Gibt Text zurück oder geprüfte Umfragedaten."""
    allow_poll = bool(LLM_CONFIG["tools"]) and can_create_poll(guild, channel, user)

    async def build(skip_images: bool = False) -> list[dict[str, Any]]:
        return await build_chat_messages(
            guild_id=guild.id,
            channel_id=channel.id,
            speaker_id=user.id,
            speaker_name=user.display_name,
            speaker_is_owner=is_owner(user),
            user_text=user_text,
            image_urls=image_urls,
            poll_allowed=allow_poll if LLM_CONFIG["tools"] else None,
            skip_images=skip_images,
        )

    tools = [POLL_TOOL] if allow_poll else None

    try:
        result = await llm.chat(await build(), tools=tools)
    except LLMUnavailable:
        raise
    except LLMError as exc:
        # Kann das Modell mit dem Bild nichts anfangen, antwortet Kuro ohne Bild
        # und sagt das offen – statt mit einer Fehlermeldung abzubrechen.
        if not image_urls or "HTTP 400" not in str(exc):
            raise
        log.warning("Bild vom Sprachmodell abgelehnt, antworte ohne Bild: %s", exc)
        result = await llm.chat(await build(skip_images=True), tools=tools)

    if result.tool_calls:
        calls = result.tool_calls
        if (
            not allow_poll
            or len(calls) != 1
            or calls[0]["name"] != "create_poll"
        ):
            return "Diese Aktion kann ich nicht ausführen."

        try:
            return validate_poll(calls[0]["arguments"])
        except (ValueError, TypeError):
            return (
                "Die Umfrage ist ungültig. "
                "Bitte nenne eine Frage, 2–10 unterschiedliche Antworten "
                "und eine Dauer von 1–768 ganzen Stunden."
            )

    return result.text


def split_message(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    out = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        out.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return out


async def maybe_set_avatar_once():
    avatar_path = config.get("avatar_path", "")
    if not avatar_path:
        return
    marker_file = DATA_DIR / ".avatar_applied"
    if marker_file.exists():
        return
    path = Path(avatar_path)
    if not path.exists():
        log.warning("Avatar-Datei nicht gefunden: %s", avatar_path)
        return
    try:
        await bot.user.edit(avatar=path.read_bytes())
        marker_file.write_text("done", encoding="utf-8")
        log.info("Avatar gesetzt.")
    except discord.HTTPException as e:
        log.warning("Avatar konnte nicht gesetzt werden: %s", e)


async def update_server_status():
    if not bot.guilds or not bot.user:
        return

    guest_count = sum(
        1
        for guild in bot.guilds
        if is_allowed_guild(guild)
        for member in guild.members
        if not member.bot
    )

    status, activity = build_presence(guest_count)

    try:
        await bot.change_presence(
            status=status,
            activity=activity,
        )
    except discord.HTTPException as exc:
        log.warning("Status konnte nicht gesetzt werden: %s", exc)


async def midnight_status_updater():
    await bot.wait_until_ready()

    tz = ZoneInfo("Europe/Berlin")

    while not bot.is_closed():
        now = datetime.now(tz)

        # nächste Mitternacht berechnen
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (next_midnight - now).total_seconds()

        await asyncio.sleep(wait_seconds)

        await update_server_status()
        log.info("Status automatisch um Mitternacht aktualisiert.")

@bot.event
async def on_ready():
    ghetto_day = get_or_create_ghetto_day()
    log.info("Ghetto-Kuro aktiv am Tag: %s", ghetto_day)
    log.info("Bot online: %s (%s)", bot.user, bot.user.id)
    for guild in list(bot.guilds):
      if not is_allowed_guild(guild):
          log.warning(
              "Nicht autorisierter Server: %s (%s) - verlasse Server.",
              guild.name,
              guild.id
          )
          await guild.leave()

    try:
        synced = await bot.tree.sync()
        log.info("Slash-Commands synchronisiert: %s", len(synced))
    except Exception as e:
        log.warning("Slash-Sync fehlgeschlagen: %s", e)

    await maybe_set_avatar_once()
    await update_server_status()

    if not hasattr(bot, "midnight_task"):
        bot.midnight_task = asyncio.create_task(midnight_status_updater())

    if not hasattr(bot, "llm_watchdog_task"):
        bot.llm_watchdog_task = asyncio.create_task(llm_watchdog())

@bot.event
async def on_guild_join(guild: discord.Guild):
    if is_allowed_guild(guild):
        log.info("Server autorisiert: %s (%s)", guild.name, guild.id)
        return

    log.warning(
        "Nicht autorisierter Server beigetreten: %s (%s)",
        guild.name,
        guild.id
    )

    if guild.system_channel:
        try:
            await guild.system_channel.send(
                "🐾 Danke für die Einladung!\n"
                "Dieser Bot ist derzeit nur auf freigegebenen Servern verfügbar.\n"
                "Falls dies ein Versehen ist, kontaktiere bitte den Entwickler."
            )
        except discord.Forbidden:
            pass

    await guild.leave()  
  
@bot.event
async def on_member_join(member: discord.Member):
    await update_server_status()

    channel_id = get_channel_id_for_guild(member.guild, "welcome_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    is_guest = member.flags.guest
    returning = has_member_been_here_before(
        member.guild.id,
        member.id,
    )
    time_of_day = get_time_of_day()

    if is_guest:
        messages = get_guild_messages(
            member.guild,
            "welcome_messages_guest",
        )

        # Gast-Begrüßung auch ohne eigene Konfiguration.
        if not messages:
            messages = [
                "Willkommen zu deinem Besuch, {user}! "
                "Schön, dass du vorbeischaust."
            ]

    elif returning:
        messages = get_guild_messages(
            member.guild,
            "welcome_messages_returning",
        )

    elif time_of_day == "morning":
        messages = get_guild_messages(
            member.guild,
            "welcome_messages_new_morning",
        )

    elif time_of_day == "day":
        messages = get_guild_messages(
            member.guild,
            "welcome_messages_new_day",
        )

    else:
        messages = get_guild_messages(
            member.guild,
            "welcome_messages_new_evening",
        )

    if not messages:
        return

    msg = random.choice(messages).replace("{user}", member.mention)

    try:
        await channel.send(msg)
    except discord.Forbidden:
        log.warning("Keine Rechte für Welcome-Nachricht in Guild %s", member.guild.id)
        await send_log_message(member.guild, f"Keine Rechte für Welcome-Nachricht in Channel {channel_id}")

    if not is_guest:
        remember_member(member.guild.id, member.id)


@bot.event
async def on_member_remove(member: discord.Member):
    await update_server_status()

    channel_id = get_channel_id_for_guild(member.guild, "welcome_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    messages = get_guild_messages(member.guild, "goodbye_messages")
    if not messages:
        return

    msg = random.choice(messages).replace("{user}", member.name)

    try:
        await channel.send(msg)
    except discord.Forbidden:
        log.warning("Keine Rechte für Goodbye-Nachricht in Guild %s", member.guild.id)
        await send_log_message(member.guild, f"Keine Rechte für Goodbye-Nachricht in Channel {channel_id}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        if not should_reply_to_limited_bot(message):
            return
    else:
        bot_reply_counter.clear()
        bot_reply_timeout.clear()

    # DMs blockieren (spart Tokens)
    if message.guild is None:
        return

    await bot.process_commands(message)

    prefix = config.get("prefix", "!")
    if message.content.startswith(prefix):
        return

    if not is_channel_allowed(message):
        return

    # Keyword Replies
    keyword_reply = get_keyword_reply(message)
    if keyword_reply:
        await message.channel.send(
            keyword_reply,
            allowed_mentions=SAFE_ALLOWED_MENTIONS
        )
        return

    # Bild von Kuro (ohne OpenAI)
    if re.search(r"\b(wie siehst du aus|zeig dich|bild von dir|wie siehst du)\b", message.content, re.IGNORECASE):
        image_path = Path("assets/kuro_summer.png")

        if image_path.exists():
            responses = [
                "Nya~ Das bin ich in meinem Sommeroutfit. Schau nicht zu lange hin 😼",
                "Hmpf... na gut, ein Blick ist erlaubt.",
                "Nur kurz schauen, verstanden?"
            ]

            await message.reply(
                random.choice(responses),
                file=discord.File(image_path),
                mention_author=False
            )
        else:
            await message.reply("Hmpf... mein Bild ist gerade nicht da.", mention_author=False)

        return

    # KI nur wenn erlaubt
    if not should_reply_to_message(message):
        return

    user_text = sanitize_user_input(message)

    image_urls = []
    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/"):
            image_urls.append(attachment.url)

    if not user_text and not image_urls:
        return

    if not user_text and image_urls:
        if message.reference:
            user_text = "Der Nutzer hat als Antwort ein Bild geschickt. Reagiere passend auf das Bild und den Gesprächskontext."
        else:
            user_text = "Bitte reagiere auf das hochgeladene Bild."

    async with reply_lock[message.channel.id]:
        async with message.channel.typing():
            try:
                answer = await generate_reply(
                    guild=message.guild,
                    channel=message.channel,
                    user=message.author,
                    user_text=user_text,
                    image_urls=image_urls,
                )

                if isinstance(answer, dict):
                    answer = await execute_poll(
                        answer,
                        message.guild,
                        message.channel,
                        message.author,
                    )
            except LLMUnavailable:
                # Rechner mit dem Sprachmodell ist aus: Kuro schläft.
                if should_send_sleep_text(message.channel.id):
                    await message.reply(pick_sleep_message(), mention_author=False)
                else:
                    try:
                        await message.add_reaction("💤")
                    except discord.HTTPException:
                        pass
                return
            except Exception as e:
                log.exception("KI-Fehler")
                await send_log_message(
                    message.guild,
                    f"KI-Fehler in Channel {message.channel.id} von {message.author} ({message.author.id}): {e}"
                )
                await message.reply(
                    "Nya... gerade ist etwas schiefgelaufen. Ich habe in der Log einen Hinweis hinterlassen. Versuch es später nochmal.",
                    mention_author=False
                )
                return

        if not answer:
            answer = "Dazu habe ich gerade keine gute Antwort."

        await remember_exchange(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            speaker_id=message.author.id,
            speaker_name=message.author.display_name,
            speaker_is_owner=is_owner(message.author),
            user_text=user_text,
            answer=answer,
        )
        for chunk in split_message(answer):
            await message.reply(
                chunk,
                mention_author=False,
                allowed_mentions=SAFE_ALLOWED_MENTIONS
            )


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong.")


@bot.command(name="hilfe")
async def hilfe(ctx):
    prefix = config.get("prefix", "!")
    await ctx.send(
        f"**Befehle**\n"
        f"`{prefix}ping`\n"
        f"`{prefix}hilfe` zeigt das hier an\n"
        f"`{prefix}modus`\n"
        f"`{prefix}setmodus <modus|auto>` (Admin)\n"
        f"`{prefix}reset` (Admin)\n" 
        f"`{prefix}keywords` (Admin)\n"
        f"`{prefix}erinnerungen` zeigt, was ich mir über dich gemerkt habe\n"
        f"`{prefix}vergissmich` löscht meine Erinnerungen an dich\n"
        f"`{prefix}komm` / `{prefix}geh` Sprachkanal betreten/verlassen\n"
        f"`{prefix}stopp` unterbricht mich beim Sprechen\n"
        f"`{prefix}ignoriermich` / `{prefix}hörmich` Zuhören für dich aus/an\n"
        f"`/ask` per Slash-Command"
    )


@bot.command(name="modus")
@commands.guild_only()
async def modus(ctx):
    if not is_allowed_guild(ctx.guild):
        return

    active = get_current_mode_name(ctx.guild.id)
    manual_mode = load_mode_override(ctx.guild.id)

    selection = "manuell" if manual_mode else "automatisch"

    await ctx.send(
        f"Auf diesem Server ist die Persona "
        f"**{active}** aktiv ({selection}).",
        allowed_mentions=SAFE_ALLOWED_MENTIONS,
    )


@bot.command(name="setmodus")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def setmodus(ctx, mode: str):
    if not is_allowed_guild(ctx.guild):
        return

    requested_mode = mode.strip()
    valid_modes = set(config.get("personas", {}).keys())

    if requested_mode.lower() == "auto":
        try:
            clear_mode_override(ctx.guild.id)
        except OSError:
            log.exception(
                "Persona-Auswahl für Guild %s konnte nicht "
                "zurückgesetzt werden.",
                ctx.guild.id,
            )
            await ctx.send(
                "Die Einstellung konnte nicht gespeichert werden. "
                "Bitte prüfe die Logs."
            )
            return

        await ctx.send(
            "Die Persona steht auf diesem Server wieder auf "
            "**Automatik**. Andere Server bleiben unverändert."
        )
        return

    if requested_mode not in valid_modes:
        modes_text = ", ".join(sorted(valid_modes))

        await ctx.send(
            f"Unbekannte Persona. Erlaubt sind: "
            f"{modes_text} oder `auto`.",
            allowed_mentions=SAFE_ALLOWED_MENTIONS,
        )
        return

    try:
        save_mode_override(ctx.guild.id, requested_mode)
    except OSError:
        log.exception(
            "Persona-Auswahl für Guild %s konnte nicht gespeichert werden.",
            ctx.guild.id,
        )
        await ctx.send(
            "Die Einstellung konnte nicht gespeichert werden. "
            "Bitte prüfe die Logs."
        )
        return

    await ctx.send(
        f"Die Persona wurde für diesen Server auf "
        f"**{requested_mode}** gesetzt. "
        "Andere Server bleiben unverändert.",
        allowed_mentions=SAFE_ALLOWED_MENTIONS,
    )


@bot.command(name="reset")
@commands.has_permissions(administrator=True)
async def reset(ctx):
    await asyncio.to_thread(memory.clear_channel, ctx.channel.id)
    await ctx.send(
        "Kontext für diesen Channel gelöscht. "
        "Meine Erinnerungen an euch behalte ich – die löscht `vergissmich`."
    )


@bot.command(name="keywords")
@commands.has_permissions(administrator=True)
async def keywords(ctx):
    rules = config.get("keyword_rules", [])
    if not rules:
        await ctx.send("Keine Keyword-Regeln aktiv.")
        return

    lines = ["**Aktive Keyword-Regeln**"]

    for i, rule in enumerate(rules, start=1):
        if "responses" in rule:
            response_text = f"{len(rule['responses'])} Antworten"
        else:
            response_text = rule.get("response", "")

        lines.append(
            f"{i}. `{rule.get('pattern', '')}` → {response_text}"
        )

    await ctx.send("\n".join(lines[:25]))


@bot.command(name="say")
@commands.has_permissions(administrator=True)
async def say(ctx, *, text: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(text)


@bot.tree.command(name="ask", description="Fragt die KI direkt.")
async def ask(interaction: discord.Interaction, frage: str):
    await interaction.response.defer(thinking=True)

    if interaction.guild is None:
        await interaction.followup.send("Nya~ Benutz mich in einem Server, nicht im Direktchat.")
        return

    if interaction.channel_id is None:
        await interaction.followup.send("Dieser Befehl braucht einen gültigen Channel.")
        return

    allowed_channel_ids = get_allowed_channel_ids(interaction.guild)
    if allowed_channel_ids and interaction.channel_id not in allowed_channel_ids:
        await interaction.followup.send("Dieser Channel ist für KuroAI nicht freigegeben.")
        return

    async with reply_lock[interaction.channel_id]:
        try:
            answer = await generate_reply(
                guild=interaction.guild,
                channel=interaction.channel,
                user=interaction.user,
                user_text=frage,
            )

            if isinstance(answer, dict):
                answer = await execute_poll(
                    answer,
                    interaction.guild,
                    interaction.channel,
                    interaction.user,
                )
        except LLMUnavailable:
            await interaction.followup.send(pick_sleep_message())
            return
        except Exception as e:
            log.exception("KI-Fehler")
            await send_log_message(
                interaction.guild,
                f"KI-Fehler bei /ask in Channel {interaction.channel_id} von {interaction.user} ({interaction.user.id}): {e}"
            )
            await interaction.followup.send("Nya... gerade ist etwas schiefgelaufen. Versuch es gleich nochmal.")
            return

        if not answer:
            answer = "Dazu habe ich gerade keine gute Antwort."

        await remember_exchange(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            speaker_id=interaction.user.id,
            speaker_name=interaction.user.display_name,
            speaker_is_owner=is_owner(interaction.user),
            user_text=frage,
            answer=answer,
        )

    for chunk in split_message(answer):
        await interaction.followup.send(
            chunk,
            allowed_mentions=SAFE_ALLOWED_MENTIONS
        )


@say.error
async def say_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Dafür brauchst du Administrator-Rechte.")
    else:
        await ctx.send(f"Fehler: {error}")


@setmodus.error
async def setmodus_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Dafür brauchst du Administrator-Rechte.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Nutze `!setmodus <modus>` oder `!setmodus auto`.")
    else:
        await ctx.send(f"Fehler: {error}")

@reset.error
async def reset_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Dafür brauchst du Administrator-Rechte.")
    else:
        log.exception(
            "Fehler beim Befehl reset",
            exc_info=error
        )
        await ctx.send("Beim Zurücksetzen ist ein Fehler aufgetreten.")


@keywords.error
async def keywords_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Dafür brauchst du Administrator-Rechte.")
    else:
        log.exception(
            "Fehler beim Befehl keywords",
            exc_info=error
        )
        await ctx.send("Beim Anzeigen der Keywords ist ein Fehler aufgetreten.")

# ---------------------------------------------------------------------------
# Gedächtnis-Befehle
# ---------------------------------------------------------------------------
@bot.command(name="erinnerungen", aliases=["gedächtnis", "gedaechtnis"])
@commands.guild_only()
async def erinnerungen(ctx):
    if not is_allowed_guild(ctx.guild):
        return

    items = await asyncio.to_thread(
        memory.list_memories, ctx.guild.id, subject_ids=[ctx.author.id], limit=25
    )
    if not items:
        await ctx.send("Über dich habe ich mir hier noch nichts gemerkt, nya.")
        return

    lines = ["**Das habe ich mir über dich gemerkt:**"]
    for item in items:
        marker = "🕒" if item.tier == "short" else "📌"
        lines.append(f"{marker} {item.content}")
    lines.append("-# 📌 dauerhaft · 🕒 nur vorübergehend · `vergissmich` löscht alles")

    for chunk in split_message("\n".join(lines)):
        await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="vergissmich")
@commands.guild_only()
async def vergissmich(ctx):
    if not is_allowed_guild(ctx.guild):
        return

    removed = await asyncio.to_thread(memory.forget_user, ctx.author.id, ctx.guild.id)
    await ctx.send(
        f"Erledigt – {removed} Erinnerung(en) an dich auf diesem Server sind gelöscht.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------------------------------------------------------------------
# Sprache: Kuro im Sprachkanal
# ---------------------------------------------------------------------------
def setup_voice() -> None:
    """Richtet die Sprachfunktion ein, falls sie in der config.json aktiv ist."""
    global voice_manager

    if not VOICE_CONFIG["enabled"]:
        return

    try:
        from kuro.voice.manager import VoiceManager
        from kuro.voice.session import VoiceHooks
    except ImportError as exc:
        log.warning(
            "Sprachfunktion ist aktiviert, aber nicht ladbar (%s). "
            "Bitte requirements-voice.txt installieren.",
            exc,
        )
        return

    def hooks_factory(guild, text_channel, start_playback):
        def member_name(user_id: int) -> str:
            member = guild.get_member(user_id)
            return member.display_name if member else "Unbekannt"

        def speaker_is_owner(user_id: int) -> bool:
            return OWNER_USER_ID != 0 and user_id == OWNER_USER_ID

        async def build_messages(user_id, text, heard):
            return await build_chat_messages(
                guild_id=guild.id,
                channel_id=text_channel.id,
                speaker_id=user_id,
                speaker_name=member_name(user_id),
                speaker_is_owner=speaker_is_owner(user_id),
                user_text=text,
                via_voice=True,
                heard=heard,
            )

        async def store_turn(user_id, text, reply, interrupted):
            await remember_exchange(
                guild_id=guild.id,
                channel_id=text_channel.id,
                speaker_id=user_id,
                speaker_name=member_name(user_id),
                speaker_is_owner=speaker_is_owner(user_id),
                user_text=text,
                answer=reply,
                source="voice",
            )

        async def post_transcript(name, text, reply):
            body = f"🎙️ **{name}:** {text}\n{reply}"
            for chunk in split_message(body):
                await text_channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        return VoiceHooks(
            build_messages=build_messages,
            store_turn=store_turn,
            start_playback=start_playback,
            member_name=member_name,
            post_transcript=post_transcript,
        )

    voice_manager = VoiceManager(
        bot,
        VOICE_CONFIG,
        os.environ,
        llm=llm,
        store=memory,
        hooks_factory=hooks_factory,
    )
    log.info("Sprachfunktion aktiv (STT: %s, TTS: %s)",
             VOICE_CONFIG["stt"]["model"], VOICE_CONFIG["tts"]["model"])


setup_voice()


def may_use_voice_join(ctx) -> bool:
    permission = VOICE_CONFIG.get("join_permission", "admin")

    if is_owner(ctx.author):
        return True
    if permission == "everyone":
        return True
    if permission == "admin":
        return ctx.author.guild_permissions.administrator
    return False


@bot.command(name="komm", aliases=["join"])
@commands.guild_only()
async def komm(ctx):
    if not is_allowed_guild(ctx.guild):
        return

    if voice_manager is None:
        await ctx.send("Die Sprachfunktion ist nicht eingerichtet.")
        return

    if not may_use_voice_join(ctx):
        await ctx.send("Dafür fehlt dir die Berechtigung.")
        return

    if not llm.available:
        await ctx.send(pick_sleep_message())
        return

    voice_state = ctx.author.voice
    if voice_state is None or voice_state.channel is None:
        await ctx.send("Geh erst selbst in einen Sprachkanal, dann komme ich nach, nya.")
        return

    try:
        session = await voice_manager.join(voice_state.channel, ctx.channel)
    except Exception as exc:
        log.exception("Sprachkanal konnte nicht betreten werden")
        await ctx.send(f"Hmpf… ich komme nicht in den Sprachkanal: {exc}")
        return

    prefix = config.get("prefix", "!")
    await ctx.send(
        f"🎙️ Ich bin in **{voice_state.channel.name}** und höre zu. "
        "Sprecht mich mit meinem Namen an. "
        "Was ich höre, wird nur in Text umgewandelt – Tonaufnahmen speichere ich nicht. "
        f"`{prefix}ignoriermich` schaltet mein Zuhören für dich ab, "
        f"`{prefix}stopp` unterbricht mich, `{prefix}geh` schickt mich raus.",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    greeting = VOICE_CONFIG.get("greeting", "")
    if greeting:
        task = asyncio.create_task(session.say(greeting))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)


@bot.command(name="geh", aliases=["leave"])
@commands.guild_only()
async def geh(ctx):
    if voice_manager is None or not is_allowed_guild(ctx.guild):
        return

    if not may_use_voice_join(ctx):
        await ctx.send("Dafür fehlt dir die Berechtigung.")
        return

    if await voice_manager.leave(ctx.guild.id):
        await ctx.send("Bin wieder hinterm Tresen. 🐾")
    else:
        await ctx.send("Ich bin doch gar nicht im Sprachkanal, du Nuss.")


@bot.command(name="stopp", aliases=["stop"])
@commands.guild_only()
async def stopp(ctx):
    if voice_manager is None:
        return

    session = voice_manager.get(ctx.guild.id)
    if session is not None and session.interrupt("command"):
        await ctx.message.add_reaction("🤐")


@bot.command(name="ignoriermich")
@commands.guild_only()
async def ignoriermich(ctx):
    if voice_manager is None:
        return

    await asyncio.to_thread(voice_manager.set_ignored, ctx.author.id, True)
    await ctx.send(
        "Verstanden – ich höre dir im Sprachkanal nicht mehr zu.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="hörmich", aliases=["hoermich"])
@commands.guild_only()
async def hoermich(ctx):
    if voice_manager is None:
        return

    await asyncio.to_thread(voice_manager.set_ignored, ctx.author.id, False)
    await ctx.send(
        "Alles klar, ich höre dir wieder zu, nya.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.event
async def on_voice_state_update(member, before, after):
    if voice_manager is not None and not member.bot:
        voice_manager.check_alone(member.guild)


if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        memory.close()
