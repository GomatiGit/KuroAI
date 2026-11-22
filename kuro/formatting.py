"""Nachbearbeitung von Kuros Textantworten für Discord.

Manche lokalen Modelle schreiben alles in einen Block. Kuros Stil lebt aber
von Absätzen: Handlung, Rede, Handlung. ``add_paragraphs`` setzt Leerzeilen
vor und nach Handlungen in *Sternchen* und teilt lange Redeblöcke nach
Sätzen – aber nur, wenn das Modell selbst keine Absätze gemacht hat.
"""

from __future__ import annotations

import re

_ACTION = re.compile(r"\*[^*\n]{3,400}\*")
_SENTENCE_END = re.compile(r"[.!?…~)\]]\s*$|[\U0001F000-\U0001FAFF☀-➿]\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÄÖÜ„\"*])")


def _split_long(part: str, max_chars: int) -> list[str]:
    """Langen Redeblock nach Sätzen in Absätze von höchstens ``max_chars`` teilen."""
    if len(part) <= max_chars + 80 or part.startswith("*"):
        return [part]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(part):
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def add_paragraphs(text: str, min_length: int = 220, max_paragraph: int = 260) -> str:
    if not text or len(text) < min_length or "\n" in text.strip() or "```" in text:
        return text

    parts: list[str] = []
    position = 0
    for match in _ACTION.finditer(text):
        before = text[position:match.start()]
        # Nur Handlungen am Satzanfang absetzen; *Betonungen* im Satz bleiben.
        if before.strip() and not _SENTENCE_END.search(before):
            continue
        if before.strip():
            parts.append(before.strip())
        parts.append(match.group(0))
        position = match.end()
    rest = text[position:].strip()
    if rest:
        parts.append(rest)
    parts = [chunk for part in parts for chunk in _split_long(part, max_paragraph)]
    if len(parts) < 2:
        return text

    # Kurze Reste (ein Emoji, "Hmpf.") am vorigen Absatz lassen.
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < 12 and not part.startswith("*"):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return "\n\n".join(merged)
