"""Kuros Gedächtnis in einer SQLite-Datei.

Drei Ebenen:

* **Chatkontext** – Tabelle ``messages``: der Verlauf je Kanal. Die letzten
  N Nachrichten (Standard 30) gehen ins Prompt.
* **Kurzzeitgedächtnis** – ``memories`` mit ``tier='short'`` und Ablaufzeit:
  was gerade los ist (jemand ist erkältet, heute wird gegrillt).
* **Langzeitgedächtnis** – ``memories`` mit ``tier='long'``: Dauerhaftes über
  Personen, Beziehungen, Running Gags, Versprechen.

Erinnerungen sind an den Server gebunden, auf dem sie entstanden sind. Was
gespeichert wird, entscheidet der Gedächtnis-Agent (``kuro.memory_agent``).

Die Klasse ist synchron und thread-sicher; aus asynchronem Code ruft man sie
über ``asyncio.to_thread`` auf oder nutzt ``stream_search``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Iterator, Sequence

log = logging.getLogger("kuro.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    channel_id  INTEGER NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    author_id   INTEGER,
    author_ref  TEXT,
    author_name TEXT,
    is_owner    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'text',
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages (channel_id, processed, id);

CREATE TABLE IF NOT EXISTS memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    subject_id        INTEGER,
    subject_name      TEXT,
    tier              TEXT NOT NULL CHECK (tier IN ('short', 'long')),
    kind              TEXT NOT NULL DEFAULT 'fact',
    content           TEXT NOT NULL,
    importance        INTEGER NOT NULL DEFAULT 3,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    expires_at        REAL,
    last_used_at      REAL,
    use_count         INTEGER NOT NULL DEFAULT 0,
    source_channel_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memories_guild ON memories (guild_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_memories_expiry ON memories (expires_at);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER,
    channel_id  INTEGER,
    text        TEXT NOT NULL,
    due_at      REAL NOT NULL,
    created_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, subject_name, tokenize = 'unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, content, subject_name)
    VALUES (new.id, new.content, coalesce(new.subject_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE OF content, subject_name ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
    INSERT INTO memories_fts (rowid, content, subject_name)
    VALUES (new.id, new.content, coalesce(new.subject_name, ''));
END;
"""

KINDS = ("fact", "preference", "relationship", "event", "running_gag", "promise")

STOPWORDS = frozenset(
    "aber alle als also auch auf aus bei bin bis bist bitte dann das dass dein deine "
    "dem den der des dich die dir doch dort du ein eine einem einen einer eines er es "
    "euch für hab habe haben hast hat hier ich ihr ihm ihn im in ist ja jetzt kann "
    "kannst kein keine mal man mein meine mich mir mit nach nicht noch nur oder schon "
    "sehr sein seine sich sie sind so und uns vom von vor war was weil wenn wer wie "
    "wir wird wo zu zum zur the and you for kuro mal bitte danke".split()
)

_WORD = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]{3,}")
_REF = re.compile(r"interne Nutzerkennung: ([0-9a-f]{6,16})")


@dataclass(frozen=True)
class Memory:
    id: int
    guild_id: int
    subject_id: int | None
    subject_name: str | None
    tier: str
    kind: str
    content: str
    importance: int
    created_at: float
    updated_at: float
    expires_at: float | None

    def as_prompt_line(self) -> str:
        about = f"über {self.subject_name}" if self.subject_name else "allgemein"
        marker = "aktuell, " if self.tier == "short" else ""
        return f"({marker}{about}) {self.content}"


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in STOPWORDS}


def fts_query(text: str, limit: int = 12) -> str:
    """Baut aus Freitext eine sichere FTS5-Anfrage (Präfixsuche, ODER-verknüpft)."""
    seen: list[str] = []
    for token in _WORD.findall(text.lower()):
        if token not in STOPWORDS and token not in seen:
            seen.append(token)
    return " OR ".join(f'"{token}"*' for token in seen[:limit])


def parse_legacy_user_line(content: str) -> dict[str, Any]:
    """Zerlegt eine Nutzerzeile aus der alten conversation_history.json.

    Formate: ``Name: Text`` oder ``Name [… interne Nutzerkennung: x; …]: Text``.
    """
    name, ref, is_owner, text = "Unbekannt", "", False, content
    bracket = content.find(" [")
    colon = content.find(": ")
    if bracket != -1 and (colon == -1 or bracket < colon):
        end = content.find("]: ", bracket)
        if end != -1:
            name = content[:bracket].strip() or name
            meta = content[bracket + 2:end]
            text = content[end + 3:]
            match = _REF.search(meta)
            ref = match.group(1) if match else ""
            if "NICHT" not in meta and "normaler Nutzer" not in meta:
                is_owner = any(
                    marker in meta
                    for marker in ("ist [Owner]", "ist Gomati", "verifizierter Besitzer")
                )
    elif 0 < colon <= 64:
        name, text = content[:colon].strip(), content[colon + 2:]
    return {"name": name, "ref": ref, "is_owner": is_owner, "text": text}


class MemoryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        short_term_hours: float = 48,
        max_messages_per_channel: int = 2000,
        max_long_term_per_guild: int = 1500,
    ) -> None:
        self.path = str(path)
        self.short_term_seconds = float(short_term_hours) * 3600
        self.max_messages_per_channel = int(max_messages_per_channel)
        self.max_long_term_per_guild = int(max_long_term_per_guild)
        self._lock = threading.RLock()
        self._optout_cache: dict[int, bool] = {}
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(SCHEMA)
        try:
            self._db.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError as exc:  # SQLite ohne FTS5
            log.warning("FTS5 nicht verfügbar, nutze einfache Suche: %s", exc)
            self.fts = False

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- Meta -----------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- Chatkontext ----------------------------------------------------

    def add_message(
        self,
        channel_id: int,
        role: str,
        content: str,
        *,
        guild_id: int | None = None,
        author_id: int | None = None,
        author_ref: str = "",
        author_name: str = "",
        is_owner: bool = False,
        source: str = "text",
        created_at: float | None = None,
        processed: bool = False,
    ) -> int:
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO messages (guild_id, channel_id, role, author_id, author_ref, "
                "author_name, is_owner, source, content, created_at, processed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id, channel_id, role, author_id, author_ref or None,
                    author_name or None, int(is_owner), source, content,
                    created_at if created_at is not None else time.time(), int(processed),
                ),
            )
            message_id = int(cur.lastrowid)
            db.execute(
                "DELETE FROM messages WHERE channel_id = ? AND id <= ("
                "  SELECT id FROM messages WHERE channel_id = ? "
                "  ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (channel_id, channel_id, self.max_messages_per_channel),
            )
        return message_id

    def recent_messages(self, channel_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """Die letzten ``limit`` Nachrichten des Kanals, älteste zuerst."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE channel_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (channel_id, max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_channel(self, channel_id: int) -> int:
        with self._tx() as db:
            return db.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,)).rowcount

    def unprocessed_messages(self, channel_id: int, limit: int = 24) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE channel_id = ? AND processed = 0 "
                "ORDER BY id ASC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_unprocessed(self, channel_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT count(*) AS n FROM messages WHERE channel_id = ? AND processed = 0",
                (channel_id,),
            ).fetchone()
        return int(row["n"])

    def mark_processed(self, message_ids: Iterable[int]) -> None:
        ids = [int(i) for i in message_ids]
        if not ids:
            return
        with self._tx() as db:
            db.executemany("UPDATE messages SET processed = 1 WHERE id = ?", [(i,) for i in ids])

    def import_legacy_history(self, json_path: str | Path) -> int:
        """Übernimmt einmalig die alte conversation_history.json. Idempotent."""
        path = Path(json_path)
        if self.get_meta("legacy_history_imported") or not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Alte Konversationsverläufe nicht lesbar: %s", exc)
            return 0

        count = 0
        base = time.time() - 1.0
        with self._tx() as db:
            for channel_key, messages in data.items():
                try:
                    channel_id = int(channel_key)
                except (TypeError, ValueError):
                    continue
                for entry in messages if isinstance(messages, list) else []:
                    role = entry.get("role")
                    content = entry.get("content")
                    if role not in ("user", "assistant") or not isinstance(content, str):
                        continue
                    info = {"name": None, "ref": None, "is_owner": False, "text": content}
                    if role == "user":
                        info = parse_legacy_user_line(content)
                    db.execute(
                        "INSERT INTO messages (guild_id, channel_id, role, author_id, "
                        "author_ref, author_name, is_owner, source, content, created_at, "
                        "processed) VALUES (NULL, ?, ?, NULL, ?, ?, ?, 'legacy', ?, ?, 1)",
                        (
                            channel_id, role, info["ref"] or None, info["name"],
                            int(info["is_owner"]), info["text"], base + count * 1e-3,
                        ),
                    )
                    count += 1
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('legacy_history_imported', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(int(time.time())),),
            )
        log.info("Alte Konversationsverläufe übernommen: %s Nachrichten", count)
        return count

    # -- Erinnerungen ---------------------------------------------------

    @staticmethod
    def _to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], guild_id=row["guild_id"], subject_id=row["subject_id"],
            subject_name=row["subject_name"], tier=row["tier"], kind=row["kind"],
            content=row["content"], importance=row["importance"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    def add_memory(
        self,
        guild_id: int,
        content: str,
        *,
        tier: str = "long",
        subject_id: int | None = None,
        subject_name: str | None = None,
        kind: str = "fact",
        importance: int = 3,
        ttl_hours: float | None = None,
        source_channel_id: int | None = None,
        now: float | None = None,
    ) -> int:
        """Speichert eine Erinnerung. Fast gleiche Einträge werden aufgefrischt."""
        content = " ".join(content.split())[:400]
        if not content:
            raise ValueError("Leere Erinnerung.")
        if tier not in ("short", "long"):
            raise ValueError("tier muss 'short' oder 'long' sein.")
        kind = kind if kind in KINDS else "fact"
        importance = max(1, min(5, int(importance)))
        now = time.time() if now is None else now
        expires_at = None
        if tier == "short":
            seconds = float(ttl_hours) * 3600 if ttl_hours else self.short_term_seconds
            expires_at = now + max(600.0, min(seconds, 30 * 86400.0))

        new_tokens = _tokens(content)
        with self._tx() as db:
            if subject_id is None:
                rows = db.execute(
                    "SELECT * FROM memories WHERE guild_id = ? AND subject_id IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?)", (guild_id, now),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM memories WHERE guild_id = ? AND subject_id = ? "
                    "AND (expires_at IS NULL OR expires_at > ?)", (guild_id, subject_id, now),
                ).fetchall()
            for row in rows:
                old_tokens = _tokens(row["content"])
                union = new_tokens | old_tokens
                if union and len(new_tokens & old_tokens) / len(union) >= 0.75:
                    keep_long = row["tier"] == "long" or tier == "long"
                    db.execute(
                        "UPDATE memories SET content = ?, importance = ?, updated_at = ?, "
                        "tier = ?, expires_at = ?, subject_name = coalesce(?, subject_name) "
                        "WHERE id = ?",
                        (
                            content, max(importance, row["importance"]), now,
                            "long" if keep_long else "short",
                            None if keep_long else max(expires_at or 0, row["expires_at"] or 0),
                            subject_name, row["id"],
                        ),
                    )
                    return int(row["id"])

            cur = db.execute(
                "INSERT INTO memories (guild_id, subject_id, subject_name, tier, kind, content, "
                "importance, created_at, updated_at, expires_at, source_channel_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, subject_id, subject_name, tier, kind, content, importance,
                 now, now, expires_at, source_channel_id),
            )
            memory_id = int(cur.lastrowid)
            if tier == "long":
                db.execute(
                    "DELETE FROM memories WHERE id IN (SELECT id FROM memories "
                    "WHERE guild_id = ? AND tier = 'long' "
                    "ORDER BY importance DESC, updated_at DESC LIMIT -1 OFFSET ?)",
                    (guild_id, self.max_long_term_per_guild),
                )
        return memory_id

    def get_memory(self, memory_id: int) -> Memory | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._to_memory(row) if row else None

    def update_memory(self, memory_id: int, guild_id: int, *, content: str | None = None,
                      importance: int | None = None, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        sets, args = ["updated_at = ?"], [now]
        if content:
            sets.append("content = ?")
            args.append(" ".join(content.split())[:400])
        if importance is not None:
            sets.append("importance = ?")
            args.append(max(1, min(5, int(importance))))
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE memories SET {', '.join(sets)} WHERE id = ? AND guild_id = ?",
                (*args, memory_id, guild_id),
            )
            return cur.rowcount > 0

    def promote_memory(self, memory_id: int, guild_id: int, now: float | None = None) -> bool:
        """Kurzzeit -> Langzeit."""
        now = time.time() if now is None else now
        with self._tx() as db:
            cur = db.execute(
                "UPDATE memories SET tier = 'long', expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND guild_id = ?", (now, memory_id, guild_id),
            )
            return cur.rowcount > 0

    def delete_memory(self, memory_id: int, guild_id: int) -> bool:
        with self._tx() as db:
            cur = db.execute(
                "DELETE FROM memories WHERE id = ? AND guild_id = ?", (memory_id, guild_id)
            )
            return cur.rowcount > 0

    def forget_user(self, user_id: int, guild_id: int | None = None) -> int:
        """Löscht alle Erinnerungen über eine Person (optional nur auf einem Server)."""
        with self._tx() as db:
            if guild_id is None:
                cur = db.execute("DELETE FROM memories WHERE subject_id = ?", (user_id,))
            else:
                cur = db.execute(
                    "DELETE FROM memories WHERE subject_id = ? AND guild_id = ?",
                    (user_id, guild_id),
                )
            return cur.rowcount

    def forget_user_messages(
        self,
        user_id: int,
        guild_id: int,
        *,
        author_ref: str = "",
        channel_ids: Sequence[int] = (),
    ) -> int:
        """Löscht die Nachrichten einer Person auf einem Server samt Kuros Antwort darauf.

        Übernommene Altdaten tragen keine Server- und Nutzer-ID, nur die Kennung
        (``author_ref``) und den Kanal – deshalb die beiden Zusatzangaben.
        """
        marks = ",".join("?" for _ in channel_ids)
        legacy = f" OR (guild_id IS NULL AND channel_id IN ({marks}))" if channel_ids else ""
        with self._tx() as db:
            rows = db.execute(
                "SELECT id, channel_id FROM messages WHERE role = 'user' "
                "AND (author_id = ? OR (author_ref IS NOT NULL AND author_ref = ?)) "
                f"AND (guild_id = ?{legacy})",
                (user_id, author_ref or "\0", guild_id, *channel_ids),
            ).fetchall()
            removed = 0
            for row in rows:
                removed += db.execute("DELETE FROM messages WHERE id = ?", (row["id"],)).rowcount
                # Kuros Antwort direkt danach erzählt oft nach, was die Person sagte.
                removed += db.execute(
                    "DELETE FROM messages WHERE id = (SELECT min(id) FROM messages "
                    "WHERE channel_id = ? AND id > ?) AND role = 'assistant'",
                    (row["channel_id"], row["id"]),
                ).rowcount
            return removed

    # -- Wecker (Erinnerungen per DM) ----------------------------------------

    def add_reminder(self, user_id: int, guild_id: int | None, channel_id: int | None,
                     text: str, due_at: float, *, limit: int | None = None,
                     now: float | None = None) -> int | None:
        """Speichert eine Erinnerung. ``None``, wenn das Limit pro Person erreicht ist."""
        now = time.time() if now is None else now
        with self._tx() as db:
            if limit is not None:
                count = db.execute("SELECT count(*) FROM reminders WHERE user_id = ?",
                                   (user_id,)).fetchone()[0]
                if count >= limit:
                    return None
            cur = db.execute(
                "INSERT INTO reminders (user_id, guild_id, channel_id, text, due_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, guild_id, channel_id, text, due_at, now),
            )
            return int(cur.lastrowid)

    def list_reminders(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE user_id = ? ORDER BY due_at, id", (user_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def due_reminders(self, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE due_at <= ? ORDER BY due_at, id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_reminder(self, reminder_id: int, user_id: int | None = None) -> bool:
        with self._tx() as db:
            if user_id is None:
                cur = db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            else:
                cur = db.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?",
                                 (reminder_id, user_id))
            return cur.rowcount > 0

    def bump_reminder_attempts(self, reminder_id: int) -> int:
        with self._tx() as db:
            db.execute("UPDATE reminders SET attempts = attempts + 1 WHERE id = ?", (reminder_id,))
            row = db.execute("SELECT attempts FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return int(row[0]) if row else 0

    def forget_reminders(self, user_id: int, guild_id: int | None = None) -> int:
        """Löscht die Erinnerungen einer Person (optional nur die von einem Server)."""
        with self._tx() as db:
            if guild_id is None:
                cur = db.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
            else:
                cur = db.execute("DELETE FROM reminders WHERE user_id = ? AND guild_id = ?",
                                 (user_id, guild_id))
            return cur.rowcount

    # -- Opt-out ----------------------------------------------------------

    def is_opted_out(self, user_id: int) -> bool:
        """Hat die Person ``ignoriermich`` gewählt? (gilt auf allen Servern)"""
        cached = self._optout_cache.get(user_id)
        if cached is None:
            cached = bool(self.get_meta(f"optout:{user_id}"))
            self._optout_cache[user_id] = cached
        return cached

    def set_opted_out(self, user_id: int, opted_out: bool) -> None:
        self.set_meta(f"optout:{user_id}", "1" if opted_out else "")
        self._optout_cache[user_id] = opted_out

    def purge_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._tx() as db:
            cur = db.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            return cur.rowcount

    def list_memories(
        self,
        guild_id: int,
        *,
        subject_ids: Sequence[int] | None = None,
        include_general: bool = False,
        tier: str | None = None,
        limit: int = 50,
        now: float | None = None,
    ) -> list[Memory]:
        now = time.time() if now is None else now
        where = ["guild_id = ?", "(expires_at IS NULL OR expires_at > ?)"]
        args: list[Any] = [guild_id, now]
        if subject_ids is not None:
            marks = ",".join("?" for _ in subject_ids) or "NULL"
            clause = f"subject_id IN ({marks})"
            if include_general:
                clause = f"({clause} OR subject_id IS NULL)"
            where.append(clause)
            args.extend(subject_ids)
        if tier:
            where.append("tier = ?")
            args.append(tier)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(where)} "
                "ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [self._to_memory(row) for row in rows]

    def _candidates(self, guild_id: int, query: str, subject_ids: Sequence[int],
                    now: float) -> list[tuple[float, sqlite3.Row]]:
        subject_set = set(subject_ids)
        scored: dict[int, tuple[float, sqlite3.Row]] = {}

        def score(row: sqlite3.Row, text_bonus: float) -> float:
            value = float(row["importance"])
            if row["subject_id"] in subject_set:
                value += 2.5
            elif row["subject_id"] is None:
                value += 0.5
            if row["tier"] == "short":
                value += 1.0
            age_days = max(0.0, now - row["updated_at"]) / 86400.0
            value += math.exp(-age_days / 14.0)
            return value + text_bonus

        with self._lock:
            marks = ",".join("?" for _ in subject_set)
            about = f"subject_id IN ({marks}) OR " if subject_set else ""
            base_rows = self._db.execute(
                "SELECT * FROM memories WHERE guild_id = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                f"AND ({about}subject_id IS NULL) "
                "ORDER BY importance DESC, updated_at DESC LIMIT 200",
                (guild_id, now, *subject_set),
            ).fetchall()

            hits: list[sqlite3.Row] = []
            match = fts_query(query)
            if match and self.fts:
                hits = self._db.execute(
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.guild_id = ? "
                    "AND (m.expires_at IS NULL OR m.expires_at > ?) "
                    "ORDER BY bm25(memories_fts) LIMIT 40",
                    (match, guild_id, now),
                ).fetchall()
            elif match:
                words = [w.strip('"*') for w in match.split(" OR ")]
                like = " OR ".join("lower(content) LIKE ?" for _ in words)
                hits = self._db.execute(
                    "SELECT * FROM memories WHERE guild_id = ? "
                    f"AND (expires_at IS NULL OR expires_at > ?) AND ({like}) LIMIT 40",
                    (guild_id, now, *[f"%{w}%" for w in words]),
                ).fetchall()

        for row in base_rows:
            scored[row["id"]] = (score(row, 0.0), row)
        for position, row in enumerate(hits):
            bonus = 3.0 * (1.0 - position / max(1, len(hits)))
            scored[row["id"]] = (score(row, bonus), row)
        return sorted(scored.values(), key=lambda item: (-item[0], -item[1]["updated_at"]))

    def iter_search(
        self,
        guild_id: int,
        query: str = "",
        *,
        subject_ids: Sequence[int] = (),
        limit: int = 12,
        now: float | None = None,
        touch: bool = True,
    ) -> Iterator[Memory]:
        """Liefert passende Erinnerungen nacheinander, die wichtigste zuerst.

        Berücksichtigt: Erinnerungen über die genannten Personen, allgemeine
        Erinnerungen des Servers und Volltexttreffer zur aktuellen Nachricht
        (so tauchen auch Erinnerungen über erwähnte Dritte auf).
        """
        now = time.time() if now is None else now
        used: list[int] = []
        try:
            for _, row in self._candidates(guild_id, query, subject_ids, now)[:limit]:
                used.append(row["id"])
                yield self._to_memory(row)
        finally:
            if touch and used:
                with self._tx() as db:
                    db.executemany(
                        "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
                        "WHERE id = ?", [(now, i) for i in used],
                    )

    def search(self, guild_id: int, query: str = "", **kwargs: Any) -> list[Memory]:
        return list(self.iter_search(guild_id, query, **kwargs))

    async def stream_search(
        self, guild_id: int, query: str = "", **kwargs: Any
    ) -> AsyncIterator[Memory]:
        """Asynchroner Strom über ``iter_search`` – blockiert den Bot nicht."""
        iterator = self.iter_search(guild_id, query, **kwargs)
        sentinel = object()
        try:
            while True:
                item = await asyncio.to_thread(next, iterator, sentinel)
                if item is sentinel:
                    return
                yield item  # type: ignore[misc]
        finally:
            await asyncio.to_thread(iterator.close)

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT (SELECT count(*) FROM messages) AS messages, "
                "(SELECT count(*) FROM memories WHERE tier = 'long') AS long_term, "
                "(SELECT count(*) FROM memories WHERE tier = 'short') AS short_term"
            ).fetchone()
        return dict(row)
