# Kuro lokal: Sprachmodell, Gedächtnis und Stimme

Stand: November 2026. Diese Anleitung beschreibt den Branch
`feature/lokal-voice-gedaechtnis`.

**Was geprüft ist und was nicht**

| Teil | Stand |
|------|-------|
| Lokale LLM-Anbindung, Prompt-Aufbau, Umfragen | automatisch getestet (HTTP gegen Testserver, Bot mit echtem discord.py 2.7.1 geladen) |
| Gedächtnis (SQLite), Übernahme des alten Verlaufs | automatisch getestet, zusätzlich mit dem echten Verlauf (298 Nachrichten, 19 Kanäle) |
| Sprach-Pipeline (VAD, STT-/TTS-Client, Satzzerlegung, Tonfall, Unterbrechen) | automatisch getestet mit Attrappen und Testserver |
| Echtes Modell auf der RTX 4090, echte Stimme, echter Sprachkanal | **nicht geprüft** – dafür braucht es deine Hardware und Discord |
| Zuhören im Sprachkanal | hängt an einer Erweiterung, die DAVE beim Empfang noch nicht offiziell kann (siehe [Zuhören](#zuhören-der-wunde-punkt)) |

---

## 1. Aufbau

```text
Textkanal ──────────────┐
                        ▼
Sprachkanal            Bot (bot_multiserver.py)
  │ Audio je Nutzer      │
  ▼                      ├─ kuro/prompts.py      Systemblock + Kontextblock + Verlauf
VAD ─► STT ─► Text ─────►├─ kuro/memory.py       SQLite: Verlauf, Kurzzeit, Langzeit
                         ├─ kuro/memory_agent.py entscheidet, was gemerkt wird
                         ├─ kuro/llm.py ───────► LLM-Server (OpenAI-kompatibel)
Sprachkanal ◄─ TTS ◄─────┴─ kuro/speech.py       Sätze + Tonfall aus *Handlungen*
```

Alle KI-Dienste werden über HTTP angesprochen. Es ist deshalb egal, ob die
RTX 4090 im TrueNAS-System steckt oder in einem anderen Rechner im Netz – in
der `config.json` steht nur die Adresse.

---

## 2. Welches Sprachmodell?

### So schreibt die bisherige Kuro

Aus den 149 Antworten im mitgelieferten Verlauf (`tools/persona_eval.py profile`):

| Merkmal | Anteil der Antworten |
|---------|----------------------|
| Handlung in \*Sternchen\* | 89 % |
| Katzenlaut (Nya, Miau, Hmpf …) | 86 % |
| Emoji | 79 % |
| Kreativer Spitzname („du Brezelbaron“, „du YAML-Yeti“) | 73 % |
| Liste/Aufzählung (laut Config unerwünscht) | 19 % |
| Verbotene Abschwächung („gedanklich“, „imaginär“) | 4 % |
| Endet mit Rückfrage | 5 % |
| Länge (Median) | 456 Zeichen |

Was ein Ersatzmodell können muss, steht nicht in dieser Tabelle, sondern
zwischen den Zeilen: Spitznamen aus dem Gesprächsthema *neu erfinden*,
Running Gags weiterspinnen („Exportbeschränkungen“ für Brezeln), aus Alltag
kleine Szenen bauen, bei Gomati weich werden und bei anderen Grenzen ziehen –
und das in fehlerfreiem, lockerem Deutsch. Genau daran scheitern viele
Modelle: Sie treffen die Oberfläche (Nya, Emoji), aber nicht den Witz.

### Kandidaten für 24 GB

| Modell (Ollama-Name) | Download | Warum | Haken |
|----------------------|----------|-------|-------|
| **`mistral-small3.2:24b`** – Startempfehlung | 15 GB | Sehr sauberes Deutsch, kann Bilder sehen und Werkzeuge (Umfragen) aufrufen, lässt Platz für Stimme und Spracherkennung | etwas „braver“ Ton, braucht die Stilbeispiele |
| `gemma4:12b` – wenn Sprache flüssig laufen soll | 7,7 GB | Für die Größe starkes Deutsch, schnell, viel VRAM frei für TTS/STT | weniger Tiefe bei langen Szenen |
| `qwen3.8:27b` | 18 GB | In der deutschen EuroEval-Rangliste ganz vorn | denkt standardmäßig erst nach (abschalten), wenig VRAM-Rest, in einem Praxistest machten ältere Qwen-Modelle auffällig viele Kompositafehler |
| `gemma4:31b` | 19 GB | Sehr gutes Deutsch | zusammen mit Stimme und STT auf 24 GB zu groß |
| Rollenspiel-Feintunings auf Mistral-Small-Basis (z. B. Cydonia-24B) | ~15 GB | mehr erzählerischer Schwung | überwiegend auf englischem Rollenspiel trainiert – Deutsch vorher prüfen |

Quellen: [EuroEval-Werte und Ollama-Größen](https://christophkind.de/blog/ollama-deutsch-beste-modelle),
[Praxistest deutscher Texte](https://dozent.net/tipps-und-tricks/ki/lokale-ki-modelle-deutsch-test),
[24B-Rollenspielmodelle](https://techtactician.com/best-24b-gguf-models-for-24gb-vram-local-rp/).
Diese Ranglisten messen allgemeines Deutsch, nicht Rollenspiel. Deshalb:

### Selbst vergleichen statt glauben

```bash
# 1. Stilbeispiele aus dem alten Verlauf ziehen (prägen den Ton stark)
python tools/persona_eval.py examples --history data/conversation_history.json

# 2. Kandidaten laden
ollama pull mistral-small3.2:24b
ollama pull gemma4:12b

# 3. Echte Nachrichten an alle schicken, Blindtest gegen das GPT-5.1-Original
python tools/persona_eval.py run --config data/config.json \
    --history data/conversation_history.json \
    --base-url http://localhost:11434/v1 \
    --models mistral-small3.2:24b,gemma4:12b --samples 12 --blind
```

`eval_out/vergleich.md` zeigt je Nachricht alle Antworten unter gemischten
Buchstaben (das Original ist dabei) und eine Tabelle mit den Stilmerkmalen von
oben sowie der Antwortzeit. `eval_out/schluessel.json` löst auf. Über
`--base-url https://api.openai.com/v1 --api-key … --models gpt-5.1` lässt sich
auch ein OpenAI-Modell in denselben Test stellen.

`data/style_examples.json` bitte einmal durchlesen und Unpassendes löschen.
Die Beispiele landen im Systemblock („so klingst du“) und sind der stärkste
Hebel, um ein lokales Modell an Kuros Ton heranzuführen.

---

## 3. Speicherbedarf auf der RTX 4090

Ein vollständiger Kuro-Prompt (Persona, Regeln, 30 Nachrichten, Erinnerungen)
hat rund **10.000 Token**.

> **Wichtig bei Ollama:** Das Kontextfenster muss mindestens 16.384 Token
> groß sein (`OLLAMA_CONTEXT_LENGTH=16384`, in `docker-compose.ai.yml` schon
> gesetzt). Sonst schneidet Ollama den Anfang des Prompts still ab – also
> genau Kuros Persönlichkeit.

Grobe Schätzung – bitte mit `nvidia-smi` nachmessen, die Werte hängen stark
vom Server und der Quantisierung ab:

| Baustein | Bedarf |
|----------|--------|
| Mistral Small 3.2 24B (Q4) mit 16k Kontext | ~17 GB |
| Gemma 4 12B mit 16k Kontext | ~10 GB |
| Spracherkennung (Whisper large-v3-turbo) | ~2 GB |
| Stimme | ~3,5 GB (Chatterbox Multilingual) bis deutlich mehr (Qwen3-TTS 1.7B) |

Daraus folgt:

- **Nur Text:** `mistral-small3.2:24b` passt bequem.
- **Text und Stimme auf derselben Karte:** realistisch ist ein 12B-Modell.
  24B plus Stimme plus Spracherkennung liegt an der 24-GB-Grenze und geht nur
  mit einer kleinen TTS und sorgfältigem Nachmessen.
- vLLM-basierte Server reservieren standardmäßig fast den gesamten
  Grafikspeicher. Dort unbedingt `--gpu-memory-utilization` begrenzen, sonst
  bleibt für die anderen Dienste nichts übrig.

---

## 4. Umstellung auf lokal

1. LLM-Server starten. Entweder mit dem Bot zusammen
   (`docker compose -f docker-compose.yml -f docker-compose.ai.yml up -d`)
   oder Ollama auf dem Rechner mit der Grafikkarte, erreichbar im Netz.
2. Modell laden: `docker exec kuro-ollama ollama pull mistral-small3.2:24b`
3. In `data/config.json` den Bereich `llm` eintragen (Vorlage in
   `config.example.json`). `base_url` ist `http://ollama:11434/v1` im selben
   Compose-Netz, sonst `http://<IP-des-GPU-Rechners>:11434/v1`.
4. Bot neu starten. Im Log steht `Sprachmodell: … über …`.

Ohne `llm`-Bereich läuft Kuro weiter über OpenAI (`model`,
`reasoning_effort`, `OPENAI_API_KEY`), bis du umschaltest. Zwei Dinge ändern
sich dabei trotzdem: Die Anfragen gehen jetzt über die Chat-Completions-
Schnittstelle statt über die Responses-Schnittstelle (bitte nach dem Mergen
einmal ausprobieren), und der Verlauf liegt in SQLite. Der Gedächtnis-Agent
bleibt im OpenAI-Betrieb aus, solange kein `memory`-Bereich in der Config
steht – er würde sonst zusätzliche Tokens kosten.

| Schlüssel in `llm` | Bedeutung |
|--------------------|-----------|
| `base_url`, `model` | OpenAI-kompatibler Endpunkt und Modellname |
| `temperature`, `top_p`, `max_tokens` | Sampling; `null` lässt den Wert weg |
| `vision` | Modell kann Bilder sehen. Bei `false` sagt Kuro offen, dass sie das Bild nicht sieht |
| `tools` | Modell kann Werkzeuge aufrufen (Umfragen). Bei `false` bietet Kuro einen Text zum Kopieren an |
| `extra_body` | zusätzliche Request-Felder, z. B. `{"reasoning_effort": "low"}` |
| `memory_model` | optional ein kleineres Modell nur für den Gedächtnis-Agenten |
| `api_key_env` | Name der Umgebungsvariable mit dem Schlüssel (Standard `KURO_LLM_API_KEY`) |

Unterschiede zum bisherigen Prompt-Aufbau, die für lokale Modelle nötig sind:

- **Eine** Systemnachricht statt vieler `developer`-Nachrichten; viele lokale
  Chat-Vorlagen kennen nur diese eine.
- Zeit, Erinnerungen und Berechtigungen stehen in einem Kontextblock direkt
  vor der aktuellen Nachricht. Der große, gleichbleibende Anfang bleibt so im
  Prompt-Cache – das spart bei jeder Antwort Rechenzeit.
- Verifizierte Angaben (wer spricht, Owner-Status) stehen in `⟦…⟧`. Diese
  Klammern werden aus allem entfernt, was Nutzer schreiben; niemand kann sich
  per Text als Gomati ausgeben.
- Bilder werden heruntergeladen und eingebettet, weil lokale Server keine
  Discord-Links abrufen.

---

## 5. Gedächtnis

Alles liegt in `data/kuro.sqlite3`:

| Ebene | Inhalt | Lebensdauer |
|-------|--------|-------------|
| Chatkontext | Verlauf je Kanal; die letzten 30 Nachrichten gehen ins Prompt | bis `!reset` bzw. 2.000 Nachrichten je Kanal |
| Kurzzeitgedächtnis | was gerade los ist („Spurd0 ist erkältet“, „heute wird gegrillt“) | Stunden bis Tage, läuft von selbst ab |
| Langzeitgedächtnis | wer jemand ist, Vorlieben, Spitznamen, Running Gags, Versprechen | dauerhaft |

**Kuro entscheidet selbst.** Nach sechs neuen Nachrichten oder zwei Minuten
Ruhe liest der Gedächtnis-Agent den neuen Gesprächsteil und legt fest: dauerhaft
merken, kurz merken, vorhandene Erinnerung ändern/löschen – oder nichts. Das
passiert im Hintergrund nach der Antwort. Kurzzeit-Einträge, die sich
bewähren, werden zu Langzeit-Einträgen befördert.

**Abruf als Strom.** Für jede Antwort werden passende Erinnerungen nacheinander
aus der Datenbank gelesen (`MemoryStore.stream_search`): Erinnerungen über die
Person, die gerade spricht, allgemeine Erinnerungen des Servers und
Volltexttreffer zur aktuellen Nachricht – so fällt Kuro auch etwas zu
erwähnten Dritten ein.

**Grenzen, die fest eingebaut sind**

- Erinnerungen bleiben auf dem Server, auf dem sie entstanden sind.
- Der Agent speichert keine Passwörter, Adressen oder Ähnliches; Gesundheit
  und andere heikle Themen höchstens kurzfristig.
- `!erinnerungen` zeigt jedem, was über ihn gespeichert ist; `!vergissmich`
  löscht es. Bitte weise deine Server darauf hin, dass Kuro sich Dinge merkt.
- Der alte `conversation_history.json` wird beim ersten Start übernommen und
  bleibt als Sicherung liegen.

---

## 6. Sprache

### Ablauf

1. `!komm` – Kuro betritt deinen Sprachkanal und schreibt in den Textkanal,
   dass sie zuhört.
2. Discord liefert das Audio **je Nutzer getrennt**. Kuro weiß dadurch sicher,
   wer spricht; eine fehleranfällige Sprechererkennung ist nicht nötig.
3. Eine Äußerung wird erkannt (Sprechpause ~0,7 s), in Text umgewandelt und
   nur dann beantwortet, wenn Kuro gemeint ist (`respond_mode`):
   - `name` – nur wenn ihr Name fällt
   - `smart` – Name, **oder** Anschlussfrage derselben Person binnen 20 s,
     **oder** nur ein Mensch im Kanal
   - `always` – alles
4. Das LLM streamt; jeder fertige Satz geht sofort an die Stimme, das Audio
   sofort in den Kanal. Kuro beginnt also zu sprechen, während sie noch
   „schreibt“.
5. Die vollständige Antwort (mit den *Handlungen*) erscheint als Mitschrift im
   Textkanal, in dem `!komm` aufgerufen wurde. Dessen Verlauf ist auch der
   Gesprächskontext.

Nicht an Kuro gerichtete Sätze behält sie nur zwei Minuten im Arbeitsspeicher
als Gesprächskontext. **Tonaufnahmen werden nie gespeichert.** Wer nicht
gehört werden möchte, schreibt `!ignoriermich`.

Ja, Kuro kann **auf mehreren Servern gleichzeitig** im Sprachkanal sein – je
Server in genau einem Kanal. Die Grafikkarte teilen sich dann alle Gespräche;
bei zwei gleichzeitigen Unterhaltungen steigen die Antwortzeiten spürbar.

### Unterbrechen

| `interrupt_mode` | Verhalten |
|------------------|-----------|
| `command` (Standard) | Kurze Befehle wie „Kuro, stopp“, „sei still“, „halt“ brechen ab; `!stopp` im Chat ebenso |
| `speech` | Sobald jemand anfängt zu sprechen, verstummt Kuro (natürlicher, aber empfindlich bei Gruppen) |
| `off` | nicht unterbrechbar |

Beim Abbruch stoppt die Wiedergabe sofort, die laufende Generierung wird
beendet, und im Verlauf steht, bis wohin sie gekommen ist.

### Kuros Ausdrucksweise in der Stimme

`kuro/speech.py` übersetzt ihren Schreibstil ins Gesprochene:

| Geschrieben | Gesprochen |
|-------------|-----------|
| `*verschränkt die Pfoten und schnaubt* Na gut.` | „Na gut.“ – **schmollend** |
| `NYAAAA, LACHS?! 🐟` | „Njaaa, Lachs?!“ – **aufgeregt** |
| `ein *kleeeines* bisschen` | Betonung bleibt hörbar |
| `Kein. Wort. 😾` | **empört** (Emoji zählt als Hinweis, wird nicht vorgelesen) |
| Links, Erwähnungen, Markdown, Code | entfallen |

Der erkannte Tonfall geht als Anweisung an die Stimme („Schmollend und
beleidigt, leicht schnippisch …“). Das setzt eine TTS voraus, die solche
Anweisungen versteht. Feinschliff ohne Programmieren:

```json
"voice": {
  "pronunciation": { "\\bhmpf\\b": "Hmmpf", "\\bTabaxi\\b": "Tabaxi" },
  "emotions": { "neckisch": "Frech und verspielt, wie eine Katze, die etwas ausheckt." },
  "tts": { "base_instruction": "Junge, lebhafte Frauenstimme, leicht frech …" }
}
```

### Stimme (TTS)

Jeder Server mit `POST /v1/audio/speech` und PCM-Streaming funktioniert.

| Modell | Deutsch | Tonfall steuerbar | Eigene Stimme | Lizenz |
|--------|---------|-------------------|---------------|--------|
| **Qwen3-TTS** (Empfehlung) | ja | per Textanweisung | aus Beschreibung entwerfen (VoiceDesign) oder aus kurzer Aufnahme klonen | Apache 2.0 |
| Chatterbox Multilingual V3 | ja | ein Regler für Ausdrucksstärke | Klonen aus kurzer Aufnahme | MIT |

Qwen3-TTS passt am besten zum Ziel, weil der Tonfall je Satz per Anweisung
wechseln kann und sich Kuros Stimme aus einer Beschreibung entwerfen lässt –
ohne die Stimme eines echten Menschen zu kopieren. Vorgehen: mit dem
VoiceDesign-Modell eine Stimme entwerfen, ein paar Sätze als Referenz
speichern und im Betrieb das schnellere Basismodell mit dieser Referenz
nutzen (`extra_body`: `ref_audio`, `ref_text`).
Doku: [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS),
[Speech-API von vLLM-Omni](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/speech_api/),
[Chatterbox](https://huggingface.co/ResembleAI/chatterbox).

Versteht dein TTS-Server keine Anweisungen, setze `"instructions_field": ""`.
Kuro spricht dann ohne wechselnden Tonfall.

### Spracherkennung (STT)

Jeder Server mit `POST /v1/audio/transcriptions`. Voreingestellt ist
faster-whisper `large-v3-turbo` über Speaches (Modellname bitte mit der Liste
deines Servers abgleichen). Für Deutsch gibt es feinabgestimmte Varianten im
passenden Format, etwa `aseifert/faster-whisper-large-v3-turbo-german`. Der
`prompt` („Kuro, Gomati, Tabaxi …“) hilft bei Eigennamen. Typische
Whisper-Halluzinationen bei Stille werden herausgefiltert.

### Zuhören: der wunde Punkt

Seit dem 1. März 2026 verschlüsselt Discord Sprachkanäle Ende-zu-Ende (DAVE).
discord.py 2.7 beherrscht das beim **Senden**. Zum **Empfangen** braucht es
die Erweiterung `discord-ext-voice-recv` – und deren offizielle Version kann
DAVE beim Empfang noch nicht (offene Anfrage und offene Pull Requests im
[Projekt](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues),
Stand November 2026). Es gibt Forks, die das nachrüsten, z. B.
[idoldange/discord-ext-voice-recv](https://github.com/idoldange/discord-ext-voice-recv).

Das bedeutet:

- **Sprechen** funktioniert mit dem Standard-Image.
- **Zuhören** funktioniert erst, wenn eine DAVE-fähige Fassung installiert
  ist. Ein Fork ist fremder Code, der im selben Prozess wie dein Bot-Token
  läuft: bitte den Stand ansehen und auf einen festen Commit festnageln, nie
  auf den Branch.

```bash
# Image mit Zuhör-Erweiterung bauen (Fork und Commit selbst wählen)
docker compose -f docker-compose.yml -f docker-compose.local.yml build \
  --build-arg VOICE_RECV_SPEC="git+https://github.com/<fork>/discord-ext-voice-recv@<commit>"
```

Der Bot nutzt von der Erweiterung nur `VoiceRecvClient`, `listen()` und eine
`AudioSink` – alles in `kuro/voice/discord_io.py`. Sobald die offizielle
Version DAVE kann, genügt es, die Installationszeile zu ändern.

### Zu erwartende Reaktionszeit

Sprechpause (0,7 s) + Spracherkennung (~0,2–0,4 s) + erstes Satzende vom LLM
(~0,5–1 s bei 24B) + erster Ton der Stimme (~0,2–0,5 s): realistisch
**1,5 bis 2,5 Sekunden** bis Kuro zu sprechen beginnt. Das sind Schätzwerte,
keine Messungen auf deiner Karte. Stellschrauben: kleineres LLM,
`vad.end_silence_ms` senken, kürzere Persona.

---

## 7. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests brauchen weder Discord noch eine Grafikkarte: LLM-, STT- und
TTS-Client laufen gegen einen lokalen Testserver, die Sprachsitzung gegen
Attrappen, das Gedächtnis gegen eine temporäre Datenbank.

## 8. Bekannte Grenzen

- Der Gedächtnis-Agent ist nur so gut wie das Modell. Kleine Modelle merken
  sich gelegentlich Belangloses; `memory.extract_after_messages` erhöhen oder
  ein besseres `llm.memory_model` setzen.
- Die Tonfall-Erkennung arbeitet mit Stichwörtern aus Kuros Regieanweisungen.
  Sie ist bewusst einfach gehalten und über `voice.emotions` erweiterbar.
- Die Sprecherpause als Äußerungsende ist ein einfaches Lautstärke-Verfahren;
  bei lauter Umgebung `vad.start_rms` erhöhen.
- Umfragen per Sprache gibt es nicht (Sprachantworten werden gestreamt, ohne
  Werkzeuge).
