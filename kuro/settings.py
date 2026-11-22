"""Standardwerte und Auflösung der neuen Konfigurationsbereiche.

Alle neuen Bereiche (``llm``, ``memory``, ``voice``) sind optional. Fehlt
``llm`` in der config.json, verhält sich der Bot wie bisher und spricht mit
OpenAI (``model`` / ``reasoning_effort`` auf oberster Ebene).
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

OPENAI_BASE_URL = "https://api.openai.com/v1"

DEFAULTS: dict[str, Any] = {
    "llm": {
        # Jeder OpenAI-kompatible Endpunkt: Ollama, llama.cpp, vLLM, LM Studio …
        "base_url": "http://ollama:11434/v1",
        "model": "mistral-small3.2:24b",
        # Name der Umgebungsvariable mit dem API-Key (lokal meist leer).
        "api_key_env": "KURO_LLM_API_KEY",
        "temperature": 0.85,
        "top_p": 0.95,
        "max_tokens": 700,
        # Bremst Wiederholungen (Ollama, vLLM und llama.cpp verstehen beides).
        "frequency_penalty": 0.3,
        "presence_penalty": 0.5,
        "request_timeout": 120,
        # Ist der Rechner mit dem Sprachmodell aus, "schläft" Kuro:
        # so lange auf die Verbindung warten, dann als nicht erreichbar werten …
        "connect_timeout": 4,
        # … und erst nach so vielen Sekunden erneut versuchen.
        "retry_after_seconds": 45,
        # Nach dem Start und beim Aufwachen das Modell vorladen, damit die erste
        # echte Antwort nicht auf das Laden in den Grafikspeicher warten muss.
        "warm_up": True,
        # Im Hintergrund prüfen, ob das Modell (wieder) da ist; 0 = aus.
        "health_check_seconds": 60,
        # Pro Kanal höchstens so oft mit einem Schlaf-Text antworten, sonst nur 💤.
        "sleep_reply_cooldown_minutes": 10,
        "sleep_status": "Schläft tief und fest 💤",
        "sleep_messages": [
            "*liegt zusammengerollt neben dem Kamin und schnurrt leise im Schlaf* Mrrr… später… 💤",
            "Zzz… *ein Ohr zuckt, dann dreht sich Kuro auf die andere Seite* …nyaa… noch fünf Minuten… 💤",
            "*schläft tief und fest auf dem Tresen, der Schwanz zuckt im Traum* …Lachs… meiner… 💤",
            "*hebt nicht einmal ein Augenlid und zieht sich die Decke über die Ohren* Hmpf… schlafe… 💤",
        ],
        # Kann das Modell Bilder sehen bzw. Werkzeuge (Umfragen) aufrufen?
        "vision": True,
        "tools": True,
        # Zusätzliche Felder für den Request, z. B. {"reasoning_effort": "low"}.
        "extra_body": {},
        # Optional ein kleineres/schnelleres Modell für den Gedächtnis-Agenten.
        "memory_model": "",
        # Datei im Datenverzeichnis mit Stilbeispielen (siehe tools/persona_eval.py).
        "style_examples_file": "style_examples.json",
        "max_style_examples": 6,
        # Kurze Erinnerung direkt vor jeder Nachricht. Lokale Modelle verlieren
        # Details eines langen Systemprompts leichter als GPT ("" = aus).
        "style_reminder": (
            "Bleib ganz Kuro: frech, eigenwillig, stolz und mit eigenem Kopf. Spiel die Szene "
            "aktiv weiter, statt nur zu reagieren: Greif auf, was dein Gegenüber gerade tut oder "
            "sagt, und bau es in deine Handlung ein – reich ihm etwas, stups es an, zieh es mit, "
            "fordere es heraus, nutze seinen Namen. Bring eigene Ideen, kleine Wendungen, "
            "Streiche oder Pläne ein und lass die Szene sich weiterentwickeln. Mehrere kurze "
            "Handlungen sind gut, aber jede anders und nie die aus deinen letzten Antworten. "
            "Katzenlaute wie Nya~, Miau, Mrrp oder Hmpf natürlich einstreuen, ein frischer "
            "Spitzname passend zum Thema. Zärtlichkeit nur, wenn die Szene es hergibt. "
            "Handlungen grammatisch korrekt in der dritten Person oder ohne Subjekt "
            "(*stellt …*). Kurze Absätze, Deutsch."
        ),
        # Fast gleiche Handlungen innerhalb einer Antwort nur einmal behalten.
        "drop_repeated_actions": True,
        # Lange Antworten ohne Absätze automatisch in Absätze gliedern.
        "paragraphs": True,
        # Kuros eigene Gesten der letzten Antworten nennen, damit sie sich nicht wiederholt.
        "avoid_repeated_actions": True,
    },
    "memory": {
        "enabled": True,
        "db_file": "kuro.sqlite3",
        # Chatkontext: so viele letzte Nachrichten pro Kanal gehen ins Prompt.
        "context_messages": 30,
        # Kurzzeitgedächtnis: Standard-Lebensdauer in Stunden.
        "short_term_hours": 48,
        "max_memories_in_prompt": 12,
        # Gedächtnis-Agent: ab so vielen neuen Nachrichten oder nach Leerlauf.
        "extract_after_messages": 6,
        "extract_idle_seconds": 120,
        "max_messages_per_channel": 2000,
        "max_long_term_per_guild": 1500,
    },
    "voice": {
        "enabled": False,
        # Wer darf Kuro in einen Sprachkanal holen: "owner" | "admin" | "everyone"
        "join_permission": "admin",
        # Wann antwortet sie: "name" | "smart" | "always"
        "respond_mode": "smart",
        "names": ["kuro", "kuro-chan", "kurochan", "curo", "kuhro", "koro", "kuru"],
        "followup_seconds": 20,
        # Unterbrechen: "command" (Stopp-Wörter) | "speech" (jede Wortmeldung) | "off"
        "interrupt_mode": "command",
        "stop_phrases": [
            "stopp", "stop", "halt", "ruhe", "sei still", "sei ruhig",
            "hör auf", "hoer auf", "klappe", "warte mal", "moment mal", "pscht",
        ],
        "interrupt_ack": ["Hmpf. Na gut.", "Schon gut, ich bin still.", "Miau. Bitte."],
        "post_transcript": True,
        # Wird beim Betreten des Sprachkanals gesprochen ("" = keine Begrüßung).
        "greeting": "Nya~ Da bin ich. Wer mich braucht, sagt einfach meinen Namen.",
        "leave_when_alone_seconds": 120,
        "max_reply_tokens": 220,
        "stt": {
            "base_url": "http://speaches:8000/v1",
            "model": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            "language": "de",
            "api_key_env": "KURO_STT_API_KEY",
            # Hilft Whisper bei Eigennamen.
            "prompt": "Kuro, Gomati, Tabaxi, Taverne, Nya, Headpats.",
            "request_timeout": 30,
        },
        "tts": {
            "base_url": "http://tts:8091/v1",
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "voice": "vivian",
            "api_key_env": "KURO_TTS_API_KEY",
            "sample_rate": 24000,
            "channels": 1,
            # Feldname für Stil-/Emotionsanweisungen ("" = Server kann das nicht).
            "instructions_field": "instructions",
            "base_instruction": (
                "Junge, lebhafte Frauenstimme, verspielt und leicht frech, "
                "wie eine selbstbewusste Katzen-Zauberin. Sprich Deutsch."
            ),
            "extra_body": {"language": "German", "stream_format": "audio"},
            "request_timeout": 60,
        },
        "vad": {
            "start_rms": 500,
            "min_speech_ms": 240,
            "end_silence_ms": 700,
            "max_utterance_seconds": 25,
            "min_utterance_ms": 350,
        },
        # Aussprache-Lexikon (Regex -> Ersatz), ergänzt die eingebauten Regeln.
        "pronunciation": {},
        # Emotion -> Anweisung für die Stimme, ergänzt die eingebauten.
        "emotions": {},
    },
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Verschachteltes Zusammenführen; ``override`` gewinnt, Listen werden ersetzt."""
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def section(config: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Bereich ``name`` aus der config.json, aufgefüllt mit Standardwerten."""
    user = config.get(name, {})
    if not isinstance(user, Mapping):
        user = {}
    return deep_merge(DEFAULTS[name], user)


def resolve_llm(config: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Ermittelt Endpunkt, Modell und Parameter für den Chat.

    Ohne ``llm``-Bereich: bisheriges Verhalten (OpenAI mit ``OPENAI_API_KEY``).
    """
    if isinstance(config.get("llm"), Mapping):
        cfg = section(config, "llm")
        cfg["api_key"] = (env.get(str(cfg.get("api_key_env") or ""), "") or "").strip()
        cfg["legacy_openai"] = False
    else:
        cfg = deep_merge(DEFAULTS["llm"], {
            "base_url": OPENAI_BASE_URL,
            "model": config.get("model", "gpt-5.1"),
            # Reasoning-Modelle von OpenAI lehnen temperature/top_p/max_tokens ab.
            "temperature": None,
            "top_p": None,
            "max_tokens": None,
            "frequency_penalty": None,
            "presence_penalty": None,
            "extra_body": {"reasoning_effort": config.get("reasoning_effort", "low")},
            # OpenAI braucht keine Dauerprüfung und etwas mehr Geduld beim Verbinden.
            "connect_timeout": 10,
            "health_check_seconds": 0,
            "warm_up": False,
        })
        cfg["api_key"] = (env.get("OPENAI_API_KEY", "") or "").strip()
        cfg["legacy_openai"] = True

    cfg["base_url"] = str(cfg["base_url"]).rstrip("/")
    cfg["needs_api_key"] = "api.openai.com" in cfg["base_url"]
    return cfg


def resolve_endpoint(cfg: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Ergänzt einen stt-/tts-Bereich um den API-Key aus der Umgebung."""
    out = dict(cfg)
    out["base_url"] = str(out.get("base_url", "")).rstrip("/")
    out["api_key"] = (env.get(str(out.get("api_key_env") or ""), "") or "").strip()
    return out
