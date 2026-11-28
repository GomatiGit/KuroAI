"""Wecker: „Kuro, erinnere mich in 30 Minuten …“.

Das Sprachmodell ruft das Werkzeug ``create_reminder`` auf. Hier wird der
Aufruf geprüft, die Fälligkeit berechnet und der Text für Bestätigung und
Zustellung gebaut. Gespeichert wird im :class:`kuro.memory.MemoryStore`.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kuro.llm import chat_tool

MAX_PER_USER = 3
MAX_DAYS = 7
MAX_TEXT = 200
#: Wie spät eine Erinnerung zugestellt werden darf, bevor sie als „verspätet“ gilt.
LATE_AFTER_SECONDS = 5 * 60

REMINDER_TOOL = chat_tool(
    "create_reminder",
    "Eine ausdrücklich gewünschte Erinnerung stellen. Kuro schreibt der Person "
    "zur gewünschten Zeit eine private Nachricht (DM).",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_TEXT,
                "description": "Woran erinnert werden soll, kurz, z. B. „Spülmaschine ausräumen“.",
            },
            "minutes_from_now": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DAYS * 24 * 60,
                "description": "Für relative Angaben wie „in 30 Minuten“ oder „in 2 Stunden“.",
            },
            "at": {
                "type": "string",
                "description": "Für feste Zeitpunkte wie „um 18 Uhr“ oder „morgen früh“: "
                               "Ortszeit im Format JJJJ-MM-TT HH:MM.",
            },
        },
        "required": ["text"],
    },
)


@dataclass
class ReminderRequest:
    """Vom Modell gewünschte Erinnerung, noch nicht gespeichert."""

    text: str
    minutes_from_now: int | None = None
    at: str | None = None


_AT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_request(data: Any) -> ReminderRequest:
    if not isinstance(data, dict):
        raise ValueError("Ungültige Angaben für die Erinnerung.")
    text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
    if not text:
        raise ValueError("Woran soll ich dich erinnern?")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Der Text ist zu lang (höchstens {MAX_TEXT} Zeichen).")
    minutes = data.get("minutes_from_now")
    at = data.get("at")
    if minutes in ("", None):
        minutes = None
    if at in ("", None):
        at = None
    if (minutes is None) == (at is None):
        raise ValueError("Ich brauche genau eine Zeitangabe: in wie vielen Minuten oder um wann.")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or int(minutes) != minutes:
            raise ValueError("Die Minuten müssen eine ganze Zahl sein.")
        minutes = int(minutes)
    return ReminderRequest(text=text, minutes_from_now=minutes, at=str(at) if at else None)


def resolve_due(request: ReminderRequest, now: datetime) -> datetime:
    """Fälligkeit als zeitzonenbewusster Zeitpunkt in der Zeitzone von ``now``."""
    if request.minutes_from_now is not None:
        due = now + timedelta(minutes=request.minutes_from_now)
    else:
        due = None
        for fmt in _AT_FORMATS:
            try:
                due = datetime.strptime(request.at.strip(), fmt).replace(tzinfo=now.tzinfo)
                break
            except ValueError:
                continue
        if due is None:
            raise ValueError("Die Uhrzeit verstehe ich nicht.")
    if due <= now:
        raise ValueError("Der Zeitpunkt liegt schon in der Vergangenheit.")
    if due - now > timedelta(days=MAX_DAYS):
        raise ValueError(f"So weit kann ich nicht vorausplanen, höchstens {MAX_DAYS} Tage.")
    return due


def format_when(due: datetime, now: datetime) -> str:
    """„in 30 Minuten (um 12:22 Uhr)“, „morgen um 08:00 Uhr“, „am 30.11. um 09:00 Uhr“."""
    clock = f"{due:%H:%M} Uhr"
    minutes = round((due - now).total_seconds() / 60)
    if minutes < 60:
        return f"in {minutes} Minute{'n' if minutes != 1 else ''} (um {clock})"
    days = (due.date() - now.date()).days
    if days == 0:
        return f"heute um {clock}"
    if days == 1:
        return f"morgen um {clock}"
    if days == 2:
        return f"übermorgen um {clock}"
    return f"am {due:%d.%m.} um {clock}"


_CONFIRMATIONS = (
    "*kritzelt es mit der Kralle auf einen Zettel* Notiert, nya~ Ich stupse dich {when} per DM an: „{text}“.",
    "*zieht einen Zettel hinters Ohr* Abgemacht, {when} kriegst du von mir eine DM: „{text}“. Hmpf, auf mich ist Verlass.",
    "*nickt und tippt sich an die Schläfe* Gemerkt! {when_cap} schreib ich dir per DM: „{text}“. Miau.",
)

_DELIVERIES = (
    "{mention} Nya~ Erinnerung! ⏰ **{text}** *stupst dich an* Los, los!",
    "{mention} *zupft dich am Ärmel* Hey du, du wolltest: **{text}** ⏰ Ich hab's nicht vergessen, hmpf.",
    "{mention} Miau! ⏰ Es ist so weit: **{text}** *schaut dich erwartungsvoll an*",
)


def confirmation_text(text: str, when: str) -> str:
    template = random.choice(_CONFIRMATIONS)
    return template.format(text=text, when=when, when_cap=when[:1].upper() + when[1:])


def delivery_text(mention: str, text: str, late_seconds: float = 0) -> str:
    message = random.choice(_DELIVERIES).format(mention=mention, text=text)
    if late_seconds > LATE_AFTER_SECONDS:
        message += "\n-# Etwas verspätet, ich war kurz nicht da. Entschuldige, nya."
    return message
