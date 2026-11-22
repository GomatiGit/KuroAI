"""Macht aus Kuros geschriebener Antwort gesprochene Sprache.

Kuro schreibt Rollenspiel: ``*streckt sich* Nya~, na endlich! 😼``. Für die
Stimme heißt das:

* Handlungen in *Sternchen* werden nicht vorgelesen, bestimmen aber den
  Tonfall des folgenden Satzes (schnurrend, schmollend, aufgeregt …).
* Betonungen mitten im Satz (``ein *kleeeines* bisschen``) werden gesprochen.
* Emojis, Markdown, Links und Erwähnungen verschwinden; Emojis zählen aber
  als Hinweis auf die Stimmung.
* Katzenlaute und GESCHREI werden so umgeschrieben, dass eine deutsche
  TTS-Stimme sie natürlich ausspricht (``NYAAA`` -> ``Njaaa``).

``SpeechStream`` verarbeitet einen Token-Strom und gibt fertige Sätze so früh
wie möglich weiter, damit Kuro schon spricht, während das Modell noch schreibt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

DEFAULT_EMOTION = "verspielt"

# Reihenfolge = Vorrang. Geprüft wird kleingeschriebener Rohtext inkl. Emojis.
EMOTION_CUES: tuple[tuple[str, str], ...] = (
    ("empört", r"fauch|empör|entrüst|plustert|knurr|zeigt die krallen|😾|💢|🙀"),
    ("schmollend", r"hmpf|schmoll|verschränk|beleidigt|schnaub|schnippisch|😤"),
    ("aufgeregt", r"ny+a{3,}|\?!|!\?|!!|springt|hüpft|quietsch|begeister|schießen hoch|🤩|🎉|😳"),
    ("schläfrig", r"gähn|schläfrig|döst|💤|😴"),
    ("besorgt", r"mitfühlend|besorgt|tröst|fürsorglich|😿|🥺"),
    ("zärtlich", r"schnurr|kuschel|schmieg|sanft|streichel|krault|kuss|küss|zärtlich|deckt|decke|🖤|💛|💕|💖|🥹|❤"),
    ("neckisch", r"grins|kicher|neck|zwinker|schelmisch|frech|stups|😼|😏|😹|😸|😜"),
    ("nachdenklich", r"überleg|nachdenklich|grübel|mustert|🧐|🤔"),
)

EMOTION_INSTRUCTIONS: dict[str, str] = {
    "verspielt": "Verspielt und gut gelaunt, mit einem Lächeln in der Stimme.",
    "aufgeregt": "Aufgeregt und laut, mit hoher Energie, fast quietschend vor Begeisterung.",
    "empört": "Empört und fauchend, scharf betont, aber niedlich statt bedrohlich.",
    "schmollend": "Schmollend und beleidigt, leicht schnippisch, mit einem kleinen Schnauben.",
    "neckisch": "Neckisch und frech, mit hörbarem Grinsen und leicht gedehnten Wörtern.",
    "zärtlich": "Sanft, warm und leise, fast schnurrend, liebevoll.",
    "schläfrig": "Schläfrig und langsam, weich und gähnend.",
    "besorgt": "Mitfühlend und fürsorglich, ruhig und weich.",
    "nachdenklich": "Nachdenklich und etwas langsamer, überlegend.",
    "neutral": "",
}

# Abkürzungen, die als Großbuchstaben stehen bleiben.
ACRONYMS = frozenset(
    "KI AI API URL HTTP HTTPS JSON YAML SQL CPU GPU RAM SSD USB PC TV MRT EKG "
    "DND NPC RPG PVP PVE LARP IRL AFK GG WSL TTS STT LLM VRAM NAS DNS VPN SSE LB".split()
)
SHOUTED_SHORT = frozenset(
    "DU ICH WAS NEIN JA NIE NYA NUR UND DAS DER DIE SO IST BIST MEIN DEIN WER WIE WO".split()
)

_PRONUNCIATION: tuple[tuple[str, str], ...] = (
    (r"\b([Nn])[Yy](?=[Aa])", r"\1j"),       # Nya -> Nja
    (r"\b[Oo][Ww][Oo]\b", "Oh-wo"),
    (r"\b[Uu][Ww][Uu]\b", "Uh-wu"),
    (r"\b[Mm]rr+p?\b", "Mrrr"),
    (r"(?<!\w)[:;]-?[3DPpx)(]+(?!\w)", " "),   # :3 ;) :D
    (r"(?<!\w)[xX]D+(?!\w)", " "),
    (r"\s*→\s*", ", "),
    (r"\s&\s", " und "),
    (r"(?<![\w.])\+(\d)", r"plus \1"),
    (r"(\d)\s?[x×](?=\s|$|[.,!?])", r"\1 mal"),
)

_EMOJI_CLASS = (
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B00-\u2BFF"
    "\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D\u20E3]"
)
_EMOJI = re.compile(_EMOJI_CLASS + "+")
_CODE_BLOCK = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_URL = re.compile(r"https?://\S+")
_DISCORD_TOKEN = re.compile(r"\\?<(?:@[!&]?|#|a?:\w+:)\d+>")
_LINE_PREFIX = re.compile(r"(?m)^[ \t]*(?:[-•]|\*(?=\s)|\d+[.)]|#{1,6}|>)[ \t]+")
_ASTERISK_SPAN = re.compile(r"\*([^*\n]{1,400})\*")
# Satzende: Satzzeichen, evtl. Anführungszeichen und Emojis, dann Leerraum.
_PUNCT_BOUNDARY = re.compile(
    r"([.!?…]+)[\"“”')\]]*(?:[ \t]*" + _EMOJI_CLASS + r"+)*([ \t]+)"
)
_CAPS_WORD = re.compile(r"\b[A-ZÄÖÜ]{2,}\b")
_STRETCH = re.compile(r"(\w)\1{3,}")
_HAS_WORD = re.compile(r"\w")


@dataclass(frozen=True)
class SpeechSegment:
    text: str
    emotion: str = DEFAULT_EMOTION
    instruction: str = ""


def detect_emotion(raw: str) -> str | None:
    """Stimmung aus Regieanweisung, Emojis oder Ausrufen ablesen."""
    lowered = raw.lower()
    for emotion, pattern in EMOTION_CUES:
        if re.search(pattern, lowered):
            return emotion
    return None


def _is_shouting(raw: str) -> bool:
    words = [w for w in _CAPS_WORD.findall(raw) if w not in ACRONYMS and len(w) >= 3]
    return len(words) >= 2


def _normalize_caps(text: str) -> str:
    """GESCHREI in normale Schreibung bringen, damit die Stimme nicht buchstabiert."""
    shouting = _is_shouting(text)

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        if word in ACRONYMS:
            return word
        if len(word) >= 4 or word in SHOUTED_SHORT or (shouting and len(word) == 3):
            return word.capitalize()
        return word

    return _CAPS_WORD.sub(fix, text)


def spoken_form(text: str, pronunciation: Mapping[str, str] | None = None) -> str:
    """Bereinigt einen Textabschnitt für die Sprachausgabe."""
    text = _EMOJI.sub(" ", text)
    text = _normalize_caps(text)
    for pattern, replacement in _PRONUNCIATION:
        text = re.sub(pattern, replacement, text)
    # Tilde = gedehnter Laut: "Nja~" -> "Njaa"
    text = re.sub(r"([aeiouäöüAEIOUÄÖÜ])~+", r"\1\1", text)
    text = text.replace("~", "")
    text = _STRETCH.sub(r"\1\1\1", text)
    for pattern, replacement in (pronunciation or {}).items():
        try:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        except re.error:
            continue
    text = re.sub(r"[`_|]+", " ", text)
    text = re.sub(r"\s+([,.!?…])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip(" -–—")


def strip_markup(text: str) -> str:
    text = _CODE_BLOCK.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _DISCORD_TOKEN.sub(" ", text)
    text = text.replace("**", "").replace("__", "")
    return _LINE_PREFIX.sub("", text)


def split_script(text: str) -> list[tuple[str, str]]:
    """Zerlegt Text in ``("say", …)`` und ``("act", …)``.

    Ein *Sternchen-Abschnitt* gilt als Handlung, wenn er an einem Satzanfang
    steht (Textanfang, neue Zeile, nach Satzzeichen oder nach einer anderen
    Handlung). Mitten im Satz ist es eine Betonung und wird gesprochen.
    """
    parts: list[tuple[str, str]] = []
    position = 0
    for match in _ASTERISK_SPAN.finditer(text):
        before = text[position:match.start()]
        if before:
            parts.append(("say", before))
        context = _EMOJI.sub("", text[:match.start()]).rstrip(" \t")
        at_start = (
            not context
            or context[-1] in ".!?…:\n\"“”"
            or (parts and parts[-1][0] == "act" and not before.strip())
        )
        parts.append(("act" if at_start else "say", match.group(1)))
        position = match.end()
    if position < len(text):
        parts.append(("say", text[position:]))

    merged: list[tuple[str, str]] = []
    for kind, value in parts:
        if merged and merged[-1][0] == kind == "say":
            merged[-1] = ("say", merged[-1][1] + value)
        else:
            merged.append((kind, value))
    return merged


def _line_boundaries(line: str) -> list[tuple[int, int]]:
    """(Ende des Satzes, Anfang des nächsten) für jede Satzgrenze in einer Zeile."""
    found: list[tuple[int, int]] = []
    for match in _PUNCT_BOUNDARY.finditer(line):
        following = match.end()
        if following >= len(line):
            break
        run = match.group(1)
        if "…" in run or run.count(".") >= 3:
            # Auslassungspunkte trennen nur, wenn danach wirklich ein neuer Satz beginnt.
            nxt = line[following]
            if not (nxt.isupper() or nxt in "\"„“*"):
                continue
        found.append((match.start(2), following))
    return found


def split_sentences(text: str, max_chars: int = 240, min_chars: int = 14) -> list[str]:
    pieces: list[str] = []
    for line in text.split("\n"):
        start = 0
        for end, following in _line_boundaries(line):
            pieces.append(line[start:end])
            start = following
        pieces.append(line[start:])
    pieces = [p.strip() for p in pieces if p.strip()]

    sentences: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            cut = max(piece.rfind(", ", 0, max_chars), piece.rfind(" – ", 0, max_chars))
            if cut <= 0:
                cut = piece.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            sentences.append(piece[:cut + 1].strip())
            piece = piece[cut + 1:].strip()
        if piece:
            sentences.append(piece)
    # Sehr kurze Stücke ("Hmpf.") an den Nachbarn hängen – klingt flüssiger.
    merged: list[str] = []
    for sentence in sentences:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    if len(merged) > 1 and len(merged[-1]) < min_chars:
        tail = merged.pop()
        merged[-1] = f"{merged[-1]} {tail}"
    return merged


class SpeechStream:
    """Wandelt einen wachsenden Antworttext in sprechbare Abschnitte um."""

    def __init__(
        self,
        *,
        pronunciation: Mapping[str, str] | None = None,
        emotions: Mapping[str, str] | None = None,
        base_instruction: str = "",
        min_chunk_chars: int = 12,
    ) -> None:
        self.pronunciation = dict(pronunciation or {})
        self.instructions = {**EMOTION_INSTRUCTIONS, **(emotions or {})}
        self.base_instruction = base_instruction.strip()
        self.min_chunk_chars = min_chunk_chars
        self._buf = ""
        self._carry = DEFAULT_EMOTION
        self._pending: str | None = None

    def _instruction(self, emotion: str) -> str:
        return " ".join(p for p in (self.base_instruction, self.instructions.get(emotion, "")) if p)

    def _convert(self, chunk: str) -> list[SpeechSegment]:
        segments: list[SpeechSegment] = []
        for kind, value in split_script(strip_markup(chunk)):
            if kind == "act":
                self._pending = detect_emotion(value) or self._pending
                continue
            for index, sentence in enumerate(split_sentences(value)):
                own = detect_emotion(sentence)
                if _is_shouting(sentence) and own != "empört":
                    own = "aufgeregt"  # GESCHREI schlägt schwächere Hinweise
                if index == 0 and self._pending:
                    emotion = self._pending
                else:
                    emotion = own or self._carry
                self._pending = None
                spoken = spoken_form(sentence, self.pronunciation)
                if not _HAS_WORD.search(spoken):
                    if own:  # reine Emoji-Zeile: Stimmung mitnehmen
                        self._carry = own
                    continue
                self._carry = emotion
                segments.append(SpeechSegment(spoken, emotion, self._instruction(emotion)))
        return segments

    def _safe_cut(self) -> int:
        """Letzte Satzgrenze, an der keine Handlung/kein Codeblock offen ist.

        Geschnitten wird erst, wenn das nächste Zeichen bekannt ist – so bleibt
        ein Emoji am Satzende bei seinem Satz und färbt dessen Tonfall.
        """
        candidates: list[int] = []
        offset = 0
        for line in self._buf.split("\n"):
            candidates.extend(offset + following for _, following in _line_boundaries(line))
            offset += len(line) + 1
            if offset <= len(self._buf):
                candidates.append(offset)  # Zeilenende
        best = 0
        for cut in sorted(candidates):
            if cut >= len(self._buf) or _EMOJI.match(self._buf, cut) or self._buf[cut] in " \t\n":
                continue
            head = self._buf[:cut]
            if head.count("```") % 2 or head.replace("**", "").count("*") % 2:
                continue
            # Winzige Sätze ("Kein.") nicht abtrennen, sondern mit dem nächsten sprechen.
            if len(self._buf[best:cut].strip()) >= self.min_chunk_chars:
                best = cut
        return best

    def feed(self, delta: str) -> list[SpeechSegment]:
        self._buf += delta
        cut = self._safe_cut()
        if not cut:
            return []
        chunk, self._buf = self._buf[:cut], self._buf[cut:]
        return self._convert(chunk)

    def flush(self) -> list[SpeechSegment]:
        chunk, self._buf = self._buf, ""
        return self._convert(chunk) if chunk.strip() else []


def to_speech_segments(text: str, **kwargs: object) -> list[SpeechSegment]:
    """Kompletten Text auf einmal umwandeln."""
    stream = SpeechStream(**kwargs)  # type: ignore[arg-type]
    return stream.feed(text) + stream.flush()


def spoken_text(segments: Iterable[SpeechSegment]) -> str:
    return " ".join(segment.text for segment in segments)
