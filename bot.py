import asyncio
import json
import logging
import os
import re
import random
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from openai import OpenAI

CONFIG_PATH = Path("config.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("discord-gpt-bot")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.json fehlt. Kopiere config.example.json nach config.json.")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("Discord Token fehlt.")
if not OPENAI_API_KEY:
    raise RuntimeError("OpenAI API Key fehlt.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=config.get("prefix", "!"),
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

client = OpenAI(api_key=OPENAI_API_KEY)

MAX_HISTORY_PER_CHANNEL = int(config.get("max_history_per_channel", 100))
conversation_history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=MAX_HISTORY_PER_CHANNEL))
reply_lock = defaultdict(asyncio.Lock)
bot_reply_counter = defaultdict(int)
bot_reply_timeout = {}

log.info("Autorisierte Server: %s", config.get("allowed_guild_ids", []))
# ---------------------------------------------------------------------------
# Multi-Guild helpers
# ---------------------------------------------------------------------------
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


GHETTO_DAY_FILE = Path("ghetto_day.json")


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


def get_active_persona_name() -> str:
    override = load_mode_override()
    if override:
        return override

    now = datetime.now(ZoneInfo("Europe/Berlin"))

    ghetto_day = get_or_create_ghetto_day()
    if now.day == ghetto_day:
        return "ghetto_kuro"

    if now.weekday() == 6:
        return "frech"

    return "standard"


def get_current_mode_name() -> str:
    return get_active_persona_name()


def get_time_of_day() -> str:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    hour = now.hour

    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "day"
    return "evening"

def build_personality_text() -> str:
    active = get_active_persona_name()
    persona = config.get("personas", {}).get(active, {})
    name = persona.get("name", "Bot")
    style = persona.get("style", "freundlich")
    background = persona.get("background", "")
    rules = persona.get("rules", [])
    rules_text = "\n".join(f"- {r}" for r in rules) if rules else "- Sei freundlich und hilfreich."
    return (
        f"Du bist {name}, ein Discord-Bot.\n"
        f"Stil: {style}\n"
        f"Hintergrund: {background}\n"
        "Sprich standardmäßig Deutsch, außer der Nutzer schreibt klar in einer anderen Sprache.\n"
        "Halte Antworten für Discord eher kompakt und gut lesbar.\n"
        f"Regeln:\n{rules_text}"
    )


KNOWN_MEMBERS_FILE = Path("known_members.json")
MODE_OVERRIDE_FILE = Path("mode_override.json")


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


def load_mode_override() -> str | None:
    if not MODE_OVERRIDE_FILE.exists():
        return None

    try:
        data = json.loads(MODE_OVERRIDE_FILE.read_text(encoding="utf-8"))
        mode = data.get("mode")
        if isinstance(mode, str) and mode:
            return mode
    except Exception:
        pass

    return None


def save_mode_override(mode: str) -> None:
    MODE_OVERRIDE_FILE.write_text(
        json.dumps({"mode": mode}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def clear_mode_override() -> None:
    if MODE_OVERRIDE_FILE.exists():
        MODE_OVERRIDE_FILE.unlink()
        

def get_keyword_reply(message: discord.Message) -> str | None:
    message_content = message.content
    keyword_rules = config.get("keyword_rules", [])
    active = get_active_persona_name()

    bot_name = config.get("personas", {}).get(
        active, {}
    ).get("name", bot.user.display_name if bot.user else "Bot")

    user_name = message.author.display_name

    for rule in keyword_rules:
        pattern = rule.get("pattern")
        response = rule.get("response", "")
        if not pattern:
            continue

        match = re.search(pattern, message_content, re.IGNORECASE)
        if match:
            text = response.replace("{bot_name}", bot_name)
            text = text.replace("{user}", user_name)

            for i, group in enumerate(match.groups(), start=1):
                text = text.replace(f"{{group{i}}}", group.strip())

            return text

    return None


def should_reply_to_message(message: discord.Message) -> bool:
    if message.author.bot:
        return False

    if not is_channel_allowed(message):
        return False

    mention_required = config.get("mention_required", True)
    if mention_required:
        return (bot.user in message.mentions if bot.user else False) or is_reply_to_bot(message)

    return True


def sanitize_user_input(message: discord.Message) -> str:
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return content.strip()


def make_openai_input(channel_id: int, user_name: str, user_text: str, image_urls: list[str] | None = None):
    messages = [{"role": "developer", "content": build_personality_text()}]
    messages.extend(list(conversation_history[channel_id]))

    content = [{"type": "input_text", "text": f"{user_name}: {user_text}"}]

    if image_urls:
        for image_url in image_urls:
            content.append({
                "type": "input_image",
                "image_url": image_url
            })

    messages.append({"role": "user", "content": content})
    return messages


def call_openai(channel_id: int, user_name: str, user_text: str, image_urls: list[str] | None = None) -> str:
    response = client.responses.create(
        model=config.get("model", "gpt-5"),
        reasoning={"effort": config.get("reasoning_effort", "low")},
        input=make_openai_input(channel_id, user_name, user_text, image_urls=image_urls),
    )
    return (response.output_text or "").strip()


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
    marker_file = Path(".avatar_applied")
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
        sum(1 for member in guild.members if not member.bot)
        for guild in bot.guilds
    )

    persona = get_active_persona_name()

    if persona == "ghetto_kuro":
        status_text = f"Hat heute miese Laune und ignoriert {guest_count} Wesen"
    elif persona == "frech":
        status_text = f"Hat heute frei und hilft {guest_count} Abenteurern"
    else:
        status_text = f"Bedient {guest_count} Gäste in der Taverne"

    try:
        await bot.change_presence(
            activity=discord.CustomActivity(name=status_text)
        )
    except discord.HTTPException as e:
        log.warning("Status konnte nicht gesetzt werden: %s", e)


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

    returning = has_member_been_here_before(member.guild.id, member.id)
    time_of_day = get_time_of_day()

    if returning:
        messages = get_guild_messages(member.guild, "welcome_messages_returning")
    else:
        if time_of_day == "morning":
            messages = get_guild_messages(member.guild, "welcome_messages_new_morning")
        elif time_of_day == "day":
            messages = get_guild_messages(member.guild, "welcome_messages_new_day")
        else:
            messages = get_guild_messages(member.guild, "welcome_messages_new_evening")

    if not messages:
        return

    msg = random.choice(messages).replace("{user}", member.mention)

    try:
        await channel.send(msg)
    except discord.Forbidden:
        log.warning("Keine Rechte für Welcome-Nachricht in Guild %s", member.guild.id)
        await send_log_message(member.guild, f"Keine Rechte für Welcome-Nachricht in Channel {channel_id}")

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
        await message.channel.send(keyword_reply)
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
                answer = await asyncio.to_thread(
                    call_openai,
                    message.channel.id,
                    message.author.display_name,
                    user_text,
                    image_urls,
                )
            except Exception as e:
                log.exception("OpenAI Fehler")
                await send_log_message(
                    message.guild,
                    f"OpenAI-Fehler in Channel {message.channel.id} von {message.author} ({message.author.id}): {e}"
                )
                await message.reply(
                    "Nya... gerade ist etwas schiefgelaufen. Ich habe meinem Meister einen Hinweis hinterlassen. Versuch es gleich nochmal.",
                    mention_author=False
                )
                return

        if not answer:
            answer = "Dazu habe ich gerade keine gute Antwort."

        conversation_history[message.channel.id].append({
            "role": "user",
            "content": f"{message.author.display_name}: {user_text}"
        })
        conversation_history[message.channel.id].append({
            "role": "assistant",
            "content": answer
        })

        for chunk in split_message(answer):
            await message.reply(chunk, mention_author=False)


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong.")


@bot.command(name="hilfe")
async def hilfe(ctx):
    prefix = config.get("prefix", "!")
    await ctx.send(
        f"**Befehle**\n"
        f"`{prefix}ping`\n"
        f"`{prefix}hilfe`\n"
        f"`{prefix}modus`\n"
        f"`{prefix}setmodus <modus|auto>` (Admin)\n"
        f"`{prefix}reset`\n"
        f"`{prefix}keywords`\n"
        f"`{prefix}ask` per Slash-Command"
    )


@bot.command(name="modus")
async def modus(ctx):
    active = get_current_mode_name()

    if active == "standard":
        text = "ich arbeite ganz normal in der Taverne."
    elif active == "frech":
        text = "ich habe heute frei und entsprechend Laune 😼"
    elif active == "ghetto_kuro":
        text = "heute solltest du mich besser nicht provozieren."
    else:
        text = active

    await ctx.send(f"Nya~ Heute bin ich im Modus: **{text}**")


@bot.command(name="setmodus")
@commands.has_permissions(administrator=True)
async def setmodus(ctx, mode: str):
    valid_modes = set(config.get("personas", {}).keys())

    if mode.lower() == "auto":
        clear_mode_override()
        await update_server_status()
        await ctx.send("Nya~ Der Modus steht wieder auf **Automatik**.")
        return

    if mode not in valid_modes:
        modes_text = ", ".join(sorted(valid_modes))
        await ctx.send(f"Unbekannter Modus. Erlaubt sind: {modes_text} oder `auto`.")
        return

    save_mode_override(mode)
    await update_server_status()
    await ctx.send(f"Nya~ Modus manuell auf **{mode}** gesetzt.")


@bot.command(name="reset")
async def reset(ctx):
    conversation_history.pop(ctx.channel.id, None)
    await ctx.send("Kontext für diesen Channel gelöscht.")


@bot.command(name="keywords")
async def keywords(ctx):
    rules = config.get("keyword_rules", [])
    if not rules:
        await ctx.send("Keine Keyword-Regeln aktiv.")
        return
    lines = ["**Aktive Keyword-Regeln**"]
    for i, rule in enumerate(rules, start=1):
        lines.append(f"{i}. `{rule.get('pattern', '')}` → `{rule.get('response', '')}`")
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
            answer = await asyncio.to_thread(
                call_openai,
                interaction.channel_id,
                interaction.user.display_name,
                frage,
            )
        except Exception as e:
            log.exception("OpenAI Fehler")
            await send_log_message(
                interaction.guild,
                f"OpenAI-Fehler bei /ask in Channel {interaction.channel_id} von {interaction.user} ({interaction.user.id}): {e}"
            )
            await interaction.followup.send("Nya... gerade ist etwas schiefgelaufen. Versuch es gleich nochmal.")
            return

    conversation_history[interaction.channel_id].append({"role": "user", "content": f"{interaction.user.display_name}: {frage}"})
    conversation_history[interaction.channel_id].append({"role": "assistant", "content": answer})

    for chunk in split_message(answer or "Keine Antwort erhalten."):
        await interaction.followup.send(chunk)


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


bot.run(DISCORD_TOKEN)
