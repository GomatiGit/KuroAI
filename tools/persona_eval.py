#!/usr/bin/env python3
"""Hilft bei der Modellwahl: Wie nah kommt ein lokales Modell an die bisherige Kuro?

Drei Unterbefehle (alle arbeiten mit der alten ``conversation_history.json``):

  profile   Stil-Kennzahlen der bisherigen Kuro (GPT-5.1) ausgeben.
  examples  Stilbeispiele für das Prompt erzeugen (``data/style_examples.json``).
  run       Echte Nachrichten aus dem Verlauf an ein oder mehrere Modelle
            schicken und die Antworten neben das Original stellen – auf Wunsch
            als Blindtest. Ergebnis: ``eval_out/vergleich.md``.

Beispiel:

  python tools/persona_eval.py examples --history data/conversation_history.json
  python tools/persona_eval.py run --config data/config.json \\
      --history data/conversation_history.json \\
      --base-url http://localhost:11434/v1 \\
      --models mistral-small3.2:24b,gemma4:12b --samples 12 --blind

Die Ausgabedateien enthalten echte Nachrichten eurer Server – nicht committen
(``eval_out/`` und ``data/`` stehen in der .gitignore).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kuro import prompts  # noqa: E402
from kuro.memory import parse_legacy_user_line  # noqa: E402

CAT_SOUND = re.compile(r"\b(ny+a+h?|nyan|miau|mi+au|hmpf|mrr+p?|schnurr)", re.IGNORECASE)
ACTION = re.compile(r"\*[^*\n]{3,}\*")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿]")
NICKNAME = re.compile(r"\bdu\s+(?:\w+\s+)?[A-ZÄÖÜ][\w‑-]{3,}")
LIST_LINE = re.compile(r"(?m)^\s*(?:[-•]|\d+[.)])\s+\S")
FORBIDDEN = re.compile(r"gedanklich|imaginär|virtuell|symbolisch|in gedanken", re.IGNORECASE)
CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")

METRICS = (
    ("Zeichen (Median)", "median_chars"),
    ("Handlung in *Sternchen*", "action"),
    ("Katzenlaut", "cat_sound"),
    ("Emoji", "emoji"),
    ("Kreativer Spitzname", "nickname"),
    ("Liste/Aufzählung", "list"),
    ("Verbotene Abschwächung", "forbidden"),
    ("Endet mit Rückfrage", "question_end"),
    ("Fremde Schriftzeichen", "cjk"),
)


def style_metrics(texts: list[str]) -> dict[str, float]:
    """Anteil der Antworten (0–1) mit typischen Kuro-Merkmalen, plus Länge."""
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return {key: 0.0 for _, key in METRICS}

    def share(pattern: re.Pattern[str]) -> float:
        return sum(1 for t in texts if pattern.search(t)) / len(texts)

    return {
        "median_chars": float(statistics.median(len(t) for t in texts)),
        "action": share(ACTION),
        "cat_sound": share(CAT_SOUND),
        "emoji": share(EMOJI),
        "nickname": share(NICKNAME),
        "list": share(LIST_LINE),
        "forbidden": share(FORBIDDEN),
        "question_end": sum(1 for t in texts if t.rstrip(" 🐾😼🖤✨*~").endswith("?")) / len(texts),
        "cjk": share(CJK),
    }


def load_exchanges(history_path: Path) -> list[dict[str, Any]]:
    """Alle (Kontext, Nutzernachricht, Kuro-Antwort)-Tripel aus dem alten Verlauf."""
    data = json.loads(history_path.read_text(encoding="utf-8"))
    exchanges: list[dict[str, Any]] = []
    for channel, messages in data.items():
        rows: list[dict[str, Any]] = []
        for entry in messages:
            if entry.get("role") == "user":
                info = parse_legacy_user_line(entry.get("content", ""))
                rows.append({"role": "user", "content": info["text"], "author_name": info["name"],
                             "author_ref": info["ref"], "is_owner": info["is_owner"], "source": "text"})
            elif entry.get("role") == "assistant":
                rows.append({"role": "assistant", "content": entry.get("content", "")})
        for index in range(1, len(rows)):
            if rows[index]["role"] == "assistant" and rows[index - 1]["role"] == "user":
                exchanges.append({
                    "channel": channel,
                    "context": rows[max(0, index - 9):index - 1],
                    "user": rows[index - 1],
                    "reference": rows[index]["content"],
                })
    return exchanges


def pick_examples(exchanges: list[dict[str, Any]], count: int = 8, seed: int = 7) -> list[dict[str, str]]:
    """Wählt kurze, typische Antworten als Stilanker – gestreut über Kanäle und Sprecher."""
    def score(ex: dict[str, Any]) -> int:
        reply = ex["reference"]
        if not 60 <= len(reply) <= 420 or LIST_LINE.search(reply) or "```" in reply \
                or FORBIDDEN.search(reply) or len(ex["user"]["content"]) > 200 \
                or "<@" in reply or "http" in ex["user"]["content"]:
            return -1
        return sum(bool(p.search(reply)) for p in (ACTION, CAT_SOUND, NICKNAME, EMOJI))

    candidates = [ex for ex in exchanges if score(ex) >= 3]
    random.Random(seed).shuffle(candidates)
    candidates.sort(key=score, reverse=True)
    chosen: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ex in candidates:  # erst je Kanal/Sprecher nur eins, dann auffüllen
        key = (ex["channel"], ex["user"]["author_name"])
        if key not in seen:
            seen.add(key)
            chosen.append(ex)
    chosen += [ex for ex in candidates if ex not in chosen]
    return [{"user": ex["user"]["content"], "kuro": ex["reference"]} for ex in chosen[:count]]


def pick_samples(exchanges: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    usable = [ex for ex in exchanges if 3 <= len(ex["user"]["content"]) <= 400
              and "Bild" not in ex["user"]["content"] and len(ex["reference"]) <= 900]
    rng = random.Random(seed)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for ex in usable:
        by_channel.setdefault(ex["channel"], []).append(ex)
    for items in by_channel.values():
        rng.shuffle(items)
    picked: list[dict[str, Any]] = []
    while len(picked) < count and any(by_channel.values()):
        for channel in sorted(by_channel):
            if by_channel[channel] and len(picked) < count:
                picked.append(by_channel[channel].pop())
    return picked


def system_prompt_from_config(config: dict[str, Any], persona: str,
                              examples: list[dict[str, str]]) -> str:
    profile = config.get("owner_profile", {})
    owner = ""
    if isinstance(profile, dict) and (profile.get("name") or profile.get("context")):
        owner = (
            "OPTIONALES PROFIL DES VERIFIZIERTEN BETREIBERS:\n"
            f"Name des Betreibers: {profile.get('name', '')}\n"
            f"Persönlicher Hintergrund zum Betreiber: {profile.get('context', '')}"
        )
    return prompts.build_system_prompt(
        prompts.personality_from_config(config, persona),
        owner_profile=owner, tools_available=False, style_examples=examples,
    )


async def ask_model(llm: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    first: float | None = None
    parts: list[str] = []
    try:
        async for piece in llm.stream(messages):
            if first is None:
                first = time.perf_counter() - started
            parts.append(piece)
    except Exception as exc:  # Modell fehlt, Server aus …
        return {"text": f"[FEHLER: {exc}]", "first_token": None, "seconds": None, "error": True}
    return {"text": "".join(parts).strip(), "first_token": first,
            "seconds": time.perf_counter() - started, "error": False}


async def run_comparison(
    *,
    clients: dict[str, Any],
    samples: list[dict[str, Any]],
    system_prompt: str,
    blind: bool,
    seed: int,
) -> tuple[str, dict[str, Any]]:
    """Fragt alle Modelle ab und baut den Markdown-Bericht. Gibt (Bericht, Schlüssel) zurück."""
    rng = random.Random(seed)
    replies: dict[str, list[dict[str, Any]]] = {name: [] for name in clients}
    sections: list[str] = []
    key: dict[str, Any] = {}

    for number, sample in enumerate(samples, start=1):
        user = sample["user"]
        messages = prompts.build_messages(
            system_prompt, sample["context"], speaker_name=user["author_name"],
            speaker_ref=user["author_ref"], is_owner=user["is_owner"], user_text=user["content"],
        )
        answers = {"Original": sample["reference"]}
        for name, client in clients.items():
            result = await ask_model(client, messages)
            replies[name].append(result)
            answers[name] = result["text"]

        names = list(answers)
        if blind:
            rng.shuffle(names)
        labels = {name: (chr(ord("A") + i) if blind else name) for i, name in enumerate(names)}
        key[str(number)] = {label: name for name, label in labels.items()}

        who = f"{user['author_name']}{' (Owner)' if user['is_owner'] else ''}"
        block = [f"## Beispiel {number}", "", f"**{who}:** {user['content']}", ""]
        for name in names:
            block += [f"### {labels[name]}", "", answers[name] or "_(leer)_", ""]
        sections.append("\n".join(block))

    reference = style_metrics([s["reference"] for s in samples])
    header = ["| Merkmal | Original |" + "".join(f" {name} |" for name in clients),
              "|---|---|" + "---|" * len(clients)]
    per_model = {name: style_metrics([r["text"] for r in items if not r["error"]])
                 for name, items in replies.items()}
    for title, metric in METRICS:
        fmt = (lambda v: f"{v:.0f}") if metric == "median_chars" else (lambda v: f"{v:.0%}")
        header.append(f"| {title} | {fmt(reference[metric])} |"
                      + "".join(f" {fmt(per_model[name][metric])} |" for name in clients))
    speed = ["| Erstes Wort nach (s, Median) | – |", "| Antwortdauer (s, Median) | – |"]
    for name, items in replies.items():
        firsts = [r["first_token"] for r in items if r["first_token"] is not None]
        totals = [r["seconds"] for r in items if r["seconds"] is not None]
        speed[0] += f" {statistics.median(firsts):.2f} |" if firsts else " – |"
        speed[1] += f" {statistics.median(totals):.2f} |" if totals else " – |"

    intro = [
        "# Kuro-Modellvergleich", "",
        f"{len(samples)} echte Nachrichten aus dem Verlauf, gleicher System-Prompt für alle Modelle.",
        "Die Tabelle zeigt, wie oft ein Stilmerkmal vorkommt – je näher am Original, desto besser.",
        "Sie ersetzt das Lesen nicht: Witz, Wärme und Eigeninitiative erkennt nur ihr selbst.", "",
        *header, *speed, "",
    ]
    if blind:
        intro += ["**Blindtest:** Die Buchstaben sind je Beispiel neu gemischt; auch das Original "
                  "ist darunter. Auflösung in `schluessel.json`.", ""]
    return "\n".join(intro) + "\n" + "\n".join(sections), key


def print_profile(exchanges: list[dict[str, Any]]) -> None:
    metrics = style_metrics([ex["reference"] for ex in exchanges])
    print(f"Stilprofil aus {len(exchanges)} Kuro-Antworten:")
    for title, metric in METRICS:
        value = metrics[metric]
        print(f"  {title:28} {value:.0f}" if metric == "median_chars" else f"  {title:28} {value:.0%}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_profile = sub.add_parser("profile", help="Stil-Kennzahlen des bisherigen Verlaufs")
    p_profile.add_argument("--history", type=Path, default=Path("data/conversation_history.json"))

    p_examples = sub.add_parser("examples", help="Stilbeispiele fürs Prompt erzeugen")
    p_examples.add_argument("--history", type=Path, default=Path("data/conversation_history.json"))
    p_examples.add_argument("--out", type=Path, default=Path("data/style_examples.json"))
    p_examples.add_argument("--count", type=int, default=8)

    p_run = sub.add_parser("run", help="Modelle mit echten Nachrichten vergleichen")
    p_run.add_argument("--config", type=Path, default=Path("data/config.json"))
    p_run.add_argument("--history", type=Path, default=Path("data/conversation_history.json"))
    p_run.add_argument("--base-url", default="http://localhost:11434/v1")
    p_run.add_argument("--api-key", default="")
    p_run.add_argument("--models", required=True, help="kommagetrennt, z. B. a:24b,b:12b")
    p_run.add_argument("--samples", type=int, default=12)
    p_run.add_argument("--persona", default="standard")
    p_run.add_argument("--temperature", type=float, default=0.85)
    p_run.add_argument("--max-tokens", type=int, default=700)
    p_run.add_argument("--examples", type=Path, default=Path("data/style_examples.json"))
    p_run.add_argument("--out", type=Path, default=Path("eval_out"))
    p_run.add_argument("--blind", action="store_true")
    p_run.add_argument("--seed", type=int, default=11)

    args = parser.parse_args(argv)
    exchanges = load_exchanges(args.history)

    if args.command == "profile":
        print_profile(exchanges)
        return 0

    if args.command == "examples":
        examples = pick_examples(exchanges, args.count)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(examples)} Stilbeispiele nach {args.out} geschrieben. Bitte kurz durchlesen "
              "und Unpassendes löschen – sie prägen Kuros Ton.")
        return 0

    from kuro.llm import LLMClient

    config = json.loads(args.config.read_text(encoding="utf-8"))
    examples = json.loads(args.examples.read_text(encoding="utf-8")) if args.examples.is_file() else []
    example_users = {e.get("user") for e in examples}
    samples = pick_samples(
        [ex for ex in exchanges if ex["user"]["content"] not in example_users],
        args.samples, args.seed,
    )
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    async def go() -> tuple[str, dict[str, Any]]:
        clients = {
            model: LLMClient(args.base_url, model, args.api_key,
                             params={"temperature": args.temperature, "max_tokens": args.max_tokens},
                             max_concurrency=1)
            for model in models
        }
        try:
            return await run_comparison(
                clients=clients, samples=samples, blind=args.blind, seed=args.seed,
                system_prompt=system_prompt_from_config(config, args.persona, examples),
            )
        finally:
            for client in clients.values():
                await client.close()

    report, key = asyncio.run(go())
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "vergleich.md").write_text(report, encoding="utf-8")
    (args.out / "schluessel.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bericht: {args.out / 'vergleich.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
