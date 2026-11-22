"""Sprachfunktion: Zuhören, Verstehen, Sprechen, Unterbrechen.

Aufbau der Pipeline (pro Server eine ``VoiceSession``):

    Discord-Audio je Nutzer -> VAD (``vad``) -> STT (``stt``)
        -> Gespräch (LLM-Stream) -> Sätze + Tonfall (``kuro.speech``)
        -> TTS-Stream (``tts``) -> Wiedergabe im Sprachkanal

Nur ``discord_io`` und ``manager`` hängen an discord.py; der Rest ist ohne
Discord testbar.
"""
