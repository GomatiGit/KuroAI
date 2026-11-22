"""KuroAI – Bausteine für lokalen Betrieb, Gedächtnis und Sprache.

Die Module sind bewusst unabhängig von discord.py gehalten (Ausnahme:
``kuro.voice.discord_io``), damit sie ohne laufenden Bot testbar bleiben.
"""

__all__ = ["settings", "llm", "prompts", "memory", "memory_agent", "speech"]
