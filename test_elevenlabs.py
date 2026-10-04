import os
from elevenlabs.client import ElevenLabs

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
KURO_VOICE_ID = os.getenv("KURO_VOICE_ID", "").strip()

if not ELEVENLABS_API_KEY:
    raise RuntimeError("ELEVENLABS_API_KEY fehlt.")

if not KURO_VOICE_ID:
    raise RuntimeError("KURO_VOICE_ID fehlt.")

client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

text = (
    "[cheerfully] Nya~ Willkommen in Gomatis Taverne! "
    "[annoyed] Hmpf! Natürlich bleibt die ganze Arbeit wieder an mir hängen. "
    "[teasing] Na schön, du Schlingel. Dieses eine Mal helfe ich dir."
)

audio = client.text_to_speech.convert(
    voice_id=KURO_VOICE_ID,
    model_id="eleven_v4",
    output_format="mp3_44100_128",
    text=text,
)

with open("kuro_test.mp3", "wb") as f:
    for chunk in audio:
        if chunk:
            f.write(chunk)

print("kuro_test.mp3 wurde erfolgreich erzeugt.")