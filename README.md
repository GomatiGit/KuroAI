# KuroAI

<p align="center">
  <img src="assets/avatar.png" width="180" alt="KuroAI Avatar">
</p>

<h1 align="center">🐾 KuroAI</h1>

<p align="center">
Ein Discord-Rollenspiel-Bot mit lokalem Sprachmodell oder OpenAI, Gedächtnis, Sprachkanal-Unterstützung, mehreren Persönlichkeiten, Bildanalyse und serverindividueller Konfiguration.
</p>

<p align="center">

![Discord.py](https://img.shields.io/badge/Discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-GHCR-2496ED?logo=docker)
![Ollama](https://img.shields.io/badge/Ollama-lokal-000000?logo=ollama)
![OpenAI](https://img.shields.io/badge/OpenAI-API-10A37F)
![License](https://img.shields.io/badge/License-MIT-green)

</p>

---

## ✨ Features

- 🐾 Mehrere Persönlichkeiten (Personas)
- 🧠 Gedächtnis in SQLite: Chatkontext, Kurzzeit- und Langzeitgedächtnis – Kuro entscheidet selbst, was sie sich merkt
- 🤖 Lokales Sprachmodell über jeden OpenAI-kompatiblen Endpunkt (Ollama, llama.cpp, vLLM …) oder wie bisher OpenAI
- 😴 Schlafmodus: Ist der PC mit dem Sprachmodell aus, schläft Kuro und wacht von selbst wieder auf, sobald das Modell erreichbar ist
- 🎨 Stil wie im Original: Stilbeispiele, keine wiederholten Gesten, Absätze, Katzenlaute und keine KI-Gedankenstriche
- 🎬 Eigeninitiative im Rollenspiel: Kuro spielt Szenen aktiv weiter und bezieht ihre Gesprächspartner mit ein
- 🎙️ (optional) Sprachkanal: Kuro hört zu, unterscheidet die Sprecher, antwortet mit eigener Stimme und lässt sich unterbrechen
- 👯 Eindeutige Identifikation der Chatter über ihre Discord-User-ID
- 🖼️ Analyse hochgeladener Bilder (auch mit lokalen Vision-Modellen)
- 📊 Discord-Umfragen per Prompt
- 🔐 Datenschutz für alle: eigene Erinnerungen privat ansehen, löschen oder das Speichern ganz abschalten
- 👋 Welcome- und Goodbye-Nachrichten
- 🌍 Multi-Server-Unterstützung
- 🔒 Server-Whitelist
- 📜 Keyword-Reaktionen
- 🎭 Rollenspiel als Tabaxi-Zauberin aus dem DnD Universum
- 🐳 Fertiges Docker-Image über GitHub Container Registry (GHCR)
- 📝 Log-Channel für Fehler
- 🛡️ Administrativer Persona-Wechsel mit Auswahlliste / Chathistorie löschen / Befehl "!say" im Botkontext – nur für Serverowner, Administratoren und den Bot-Owner

---

## 📸 Bilder

### Bot-Discord-Banner

![](assets/background.png)

### Bot-Discord-Avatar

<p align="center">
<img src="assets/avatar.png" width="250">
</p>

### Look

<p align="center">
  <img src="assets/kuro.png" alt="Kuro als Maid" height="600"/>
  &nbsp;&nbsp;&nbsp;
  <img src="assets/kuro_summer.png" alt="Kuro im Sommeroutfit" height="600"/>
</p>

<p align="center">
  <sub>Arbeitskleidung • Sommerkleidung</sub>
</p>

---

## 1. Voraussetzungen

Du brauchst:

- Docker Engine mit Docker Compose v2 oder Docker Desktop
- einen Discord Bot Token
- **entweder** einen lokalen LLM-Server, z. B. Ollama auf einem PC mit Grafikkarte (empfohlen: `gemma4:12b`, siehe [docs/LOKALER_BETRIEB.md](docs/LOKALER_BETRIEB.md))
- **oder** einen OpenAI API Key mit eingerichtetem Billing
- aktivierten Discord Developer Mode
- deine Discord Server-ID
- deine Discord User-ID
- Git zum Klonen des Repositories  
  alternativ kannst du das Repository als ZIP herunterladen

**Python muss auf dem Host nicht installiert sein.**  
Python und alle benötigten Bibliotheken sind bereits im Docker-Image enthalten.

Das fertige Image wird über die GitHub Container Registry bereitgestellt:

```text
ghcr.io/gomatigit/kuroai:latest
```

---

## 2. Discord Bot erstellen

1. Gehe zu <https://discord.com/developers/applications>
2. Erstelle eine neue Application.
3. Öffne den Bereich **Bot** und erstelle den Bot.
4. Aktiviere unter **Privileged Gateway Intents**:
   - Message Content Intent
   - Server Members Intent
5. Kopiere den Bot Token und speichere ihn sicher.
6. Erstelle unter **OAuth2** einen Einladungslink für den Bot mit den Scopes `bot` und `applications.commands` und füge ihn deinem Discord-Server hinzu. Ohne `applications.commands` fehlen die Slash-Befehle `/ask`, `/erinnerungen` und `/setmodus`.  
   Passende Berechtigungen (inklusive Sprachkanal und Umfragen) ergeben den Wert `563224834591808`.  
   Damit Kuro den Befehl bei `!erinnerungen` aus dem Kanal entfernen kann, braucht sie **Nachrichten verwalten**.
7. Aktiviere in Discord unter **Einstellungen → Erweitert** den **Entwicklermodus**.
8. Kopiere anschließend deine Discord User-ID und die Server-ID.

---

## 3. OpenAI API Key (nur ohne lokales Modell)

Für den lokalen Betrieb wird kein API Key benötigt. Dann trägst du in der `config.json` den Bereich `llm` ein (siehe [docs/LOKALER_BETRIEB.md](docs/LOKALER_BETRIEB.md)) und überspringst diesen Schritt.

Erstelle einen API Key im OpenAI Dashboard auf <https://platform.openai.com/> und speichere ihn sicher.

Es wird empfohlen, im OpenAI-Dashboard ein monatliches Budget bzw. Nutzungslimit festzulegen. Der Bot verwendet API-Tokens, wodurch Kosten entstehen.

**API Keys und Tokens gehören NIEMALS in die `config.json` und nicht in das Docker-Image.**  
Sie werden ausschließlich als Docker-Umgebungsvariablen bereitgestellt.

---

## 🚀 Installation

### 4. Repository herunterladen

Das Repository wird für `docker-compose.yml`, `.env.example` und `config.example.json` benötigt. Der eigentliche Bot wird anschließend als fertiges Docker-Image von GHCR geladen.

```bash
git clone https://github.com/GomatiGit/KuroAI.git
cd KuroAI
```

Alternativ kann das Repository über GitHub als ZIP heruntergeladen und entpackt werden.

### 5. Persistentes Datenverzeichnis erstellen

```bash
mkdir -p data
```

Kopiere anschließend die Beispielkonfiguration nach `data/config.json`:

```bash
cp config.example.json data/config.json
```

Unter Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force data
Copy-Item config.example.json data\config.json
```

Passe danach `data/config.json` an deinen Discord-Server und deine gewünschten Personas an.

Der Ordner `data/` ist das persistente Datenverzeichnis von Kuro und wird nicht in das Docker-Image eingebaut.

Dort befinden sich später beispielsweise:

```text
data/
├── config.json
├── kuro.sqlite3
├── style_examples.json
├── conversation_history.json
├── known_members.json
├── ghetto_day.json
├── mode_override.json
└── .avatar_applied
```

Die Laufzeitdateien werden vom Bot bei Bedarf angelegt bzw. aktualisiert.

### 6. Umgebungsvariablen einrichten

Erstelle aus der Vorlage eine lokale `.env`-Datei:

```bash
cp .env.example .env
```

Unter Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Trage dort mindestens deine eigenen Werte ein:

```env
DISCORD_BOT_TOKEN=dein_discord_bot_token
KURO_OWNER_USER_ID=deine_discord_user_id
```

Nur für den OpenAI-Betrieb (kein `llm`-Bereich in der `config.json`):

```env
OPENAI_API_KEY=dein_openai_api_key
```

Optional, falls eigene LLM-, Spracherkennungs- oder Stimmen-Endpunkte einen Schlüssel verlangen:

```env
KURO_LLM_API_KEY=
KURO_STT_API_KEY=
KURO_TTS_API_KEY=
```

Die Variablen `ELEVENLABS_API_KEY` und `KURO_VOICE_ID` stammen aus der früheren Planung und werden im lokalen Betrieb nicht benötigt.

Die Datei `.env` ist in `.gitignore` ausgeschlossen und darf nicht veröffentlicht werden.

Alternativ können die Variablen direkt in der jeweiligen Docker-, NAS- oder Container-Verwaltung gesetzt werden. Auf TrueNAS SCALE können sie beispielsweise über die Environment-Variablen der App bzw. des Containers hinterlegt werden.

### 7. Konfiguration anpassen

Öffne:

```text
data/config.json
```

und passe mindestens die Server- und Persona-Einstellungen an deine Umgebung an.

Wichtige Bereiche:

- `allowed_guild_ids` bestimmt, auf welchen Discord-Servern Kuro aktiv sein darf.
- Unter `guilds` können serverindividuelle Einstellungen hinterlegt werden.
- Unter `personas` werden die verschiedenen Persönlichkeiten definiert.
- `keyword_rules` enthält Keyword-Reaktionen.
- `llm` legt Endpunkt und Modell fest. Fehlt der Bereich, nutzt Kuro wie bisher OpenAI mit `model` und `reasoning_effort`.  
  Dort lassen sich auch Schlafmodus (`sleep_messages`, `sleep_status`), Aufwärmen (`warm_up`) und die Stil-Nachbearbeitung (`paragraphs`, `drop_repeated_actions`, `avoid_dashes`) einstellen.
- `memory.context_messages` bestimmt, wie viele Nachrichten je Channel als Gesprächskontext mitgegeben werden (Standard 30). Das frühere `max_history_per_channel` wird nicht mehr ausgewertet.
- `voice` schaltet die Sprachfunktion ein und konfiguriert Spracherkennung und Stimme. Mit `join_permission: "everyone"` dürfen alle `!komm` / `!geh` benutzen, Standard sind nur Serverowner, Admins und Bot-Owner.
- Alle Einzelheiten dazu stehen in [docs/LOKALER_BETRIEB.md](docs/LOKALER_BETRIEB.md).
- `bot_reply_limits` begrenzt, wie häufig Kuro mit anderen Bots kommunizieren darf, falls du noch andere Textbots hast, die auf Nachrichten reagieren.

### 8. Kuro starten

Zuerst das aktuelle Image laden:

```bash
docker compose pull
```

Danach Kuro starten:

```bash
docker compose up -d
```

Das Repository wird dabei nicht lokal als Anwendung ausgeführt. Docker verwendet das veröffentlichte Image:

```text
ghcr.io/gomatigit/kuroai:latest
```

Die lokale `data/`-Struktur wird in den Container unter `/app/data` eingebunden und bleibt deshalb auch beim Austausch oder Update des Containers erhalten.

### 9. Logs anzeigen

```bash
docker compose logs -f kuroai
```

Beenden mit:

```text
Ctrl+C
```

Der Container selbst läuft dabei weiter.

### 10. Kuro aktualisieren

Neue Versionen können ohne Neuinstallation übernommen werden:

```bash
docker compose pull
docker compose up -d
```

Das Docker-Image wird aktualisiert, während die Inhalte des lokalen `data/`-Ordners erhalten bleiben.

---

## ⚙️ Daten- und Containerstruktur

Kuro trennt bewusst Programmcode, Secrets und persistente Daten voneinander:

```text
GitHub / GHCR
├── Bot-Code
├── Python-Laufzeit
├── benötigte Bibliotheken
└── Assets

Docker-Umgebung
├── DISCORD_BOT_TOKEN
├── KURO_OWNER_USER_ID
└── OPENAI_API_KEY            (nur im OpenAI-Betrieb)

Persistentes data/
├── config.json
├── kuro.sqlite3              (Gedächtnis: Verlauf, Kurzzeit, Langzeit)
├── style_examples.json       (optional: Stilbeispiele für lokale Modelle)
├── conversation_history.json (Altbestand, wird einmalig übernommen)
├── known_members.json
├── ghetto_day.json
├── mode_override.json
└── .avatar_applied
```

Dadurch kann das Docker-Image aktualisiert oder vollständig ersetzt werden, ohne die individuelle Konfiguration oder den gespeicherten Zustand von Kuro zu verlieren.

Das Image enthält **keine persönlichen API Keys, Tokens oder Server-Konfigurationen**.

---

## 🎭 Personas

| Persona | Beschreibung |
|---------|--------------|
| Standard | Freundlich, verspielt und hilfsbereit |
| Frech | Sonntags etwas lockerer und sarkastischer. Sie hat da nämlich frei |
| Ghetto Kuro | Ein zufälliger Tag pro Monat mit besonders schlechter Laune |

Die Persona wird automatisch gewählt. Admins können sie pro Server mit `!setmodus <modus>` oder `/setmodus` (mit Auswahlliste) festlegen und mit `auto` wieder zur Automatik zurückkehren. `!setmodus` ohne Angabe und `!hilfe` zählen alle verfügbaren Modi auf. Neue Personas aus der `config.json` erscheinen dort automatisch; mit `"description"` bekommt eine Persona einen eigenen Kurztext.

---

## 🖼️ Bilderkennung

Erwähne Kuro und hänge ein Bild an:

> @Kuro Was siehst du auf diesem Bild?

Alternativ kann ein Bild auch als Antwort auf eine Nachricht von Kuro gesendet werden.

---

## 📊 Discord-Umfragen per Prompt

Erstelle echte Discord-Umfragen durch eine Nachricht an Kuro oder über `/ask` – mit 2–10 Antwortmöglichkeiten, einstellbarer Laufzeit und optionaler Mehrfachauswahl. Die erforderlichen Kanalrechte werden automatisch geprüft.

---

## 🧠 Lokaler Betrieb, Gedächtnis und Sprache

Kuro kann vollständig auf eigener Hardware laufen. Die Einrichtung – Modellwahl, Speicherbedarf, Gedächtnis, Sprachkanal und der Modellvergleich mit `tools/persona_eval.py` – ist in **[docs/LOKALER_BETRIEB.md](docs/LOKALER_BETRIEB.md)** beschrieben.

Ist das Sprachmodell nicht erreichbar, schläft Kuro: Sie zeigt das im Status an, antwortet höchstens mit einem kurzen Schlafhinweis und wacht von selbst wieder auf, sobald der PC läuft. Ein Rückfall auf ein langsames CPU-Modell findet bewusst nicht statt.

**Befehle für alle**

| Befehl | Wirkung |
|--------|---------|
| `!hilfe` | zeigt alle Befehle und die verfügbaren Modi |
| `!ping` | prüft, ob der Bot online ist |
| `/ask` | fragt Kuro per Slash-Befehl |
| `!erinnerungen` | schickt dir per DM, was Kuro über dich gespeichert hat; `/erinnerungen` zeigt es nur dir im Kanal |
| `!vergissmich` | löscht deine Erinnerungen und gespeicherten Nachrichten auf diesem Server |
| `!ignoriermich` / `!hörmich` | Opt-out: Kuro speichert auf keinem Server mehr etwas von dir und hört dir im Sprachkanal nicht zu / wieder einschalten |
| `!stopp` | unterbricht Kuro beim Sprechen (im Sprachkanal reicht „Kuro, stopp“) |

**Befehle für Serverowner, Administratoren und den Bot-Owner**

| Befehl | Wirkung |
|--------|---------|
| `!modus` | zeigt die aktive Persona und alle verfügbaren Modi |
| `!setmodus <modus>` / `/setmodus` | legt die Persona für diesen Server fest; ohne Angabe listet `!setmodus` die Modi, `/setmodus` schlägt sie in einer Auswahlliste vor |
| `!reset` | löscht den Gesprächskontext dieses Channels (Erinnerungen bleiben) |
| `!keywords` | zeigt die aktiven Keyword-Regeln |
| `!say <text>` | Kuro schreibt den Text, dein Befehl wird gelöscht |
| `!komm` / `!geh` | Kuro betritt bzw. verlässt deinen Sprachkanal |

Tests: `pip install -r requirements-dev.txt` und danach `python -m pytest`.

---

## 🛣️ Roadmap

- [x] Lokales Sprachmodell statt OpenAI (OpenAI bleibt als Rückfallweg erhalten)
- [x] Gedächtnis mit Kurzzeit- und Langzeitspeicher in SQLite
- [ ] Sprachkanal im Live-Betrieb erproben (Zuhören benötigt eine DAVE-fähige Empfangserweiterung)
- [ ] Eigene lokale Stimme für Kuro entwerfen und trainieren (ohne kostenpflichtige Dienste)
- [x] Schlafmodus, wenn das lokale Modell nicht erreichbar ist
- [x] Modellvergleich mit dem GPT-5.1-Original (`tools/persona_eval.py`), Ergebnis: `gemma4:12b`
- [x] Datenschutz-Befehle für alle Nutzer (`!erinnerungen`, `!vergissmich`, `!ignoriermich`)
- [x] Admin-Befehle nur für Serverowner, Administratoren und den Bot-Owner
- [ ] Feintuning des lokalen Modells auf Kuros Stil (LoRA)
- [x] Bessere Personalisierung unterteilt nach einzelnen Discord Servern/Guilds durch zusätzliche serverspezifische Hintergrundinformationen
- [x] Optionales Owner-Profil für individuelle Namen und Hintergrundinformationen in der privaten Konfiguration
- [x] Discord-Umfragen per Prompt oder `/ask` erstellen, einschließlich Laufzeit, Mehrfachauswahl und Berechtigungsprüfung
- [x] Nachrichten mit Zeitkontext, sodass der Bot die Tageszeit und/oder Datum interpretieren kann
- [x] Temporäre Discord Gäste werden über Einladungslink individuell gegrüßt
- [ ] Neue Personalisierungs- und Umfragefunktionen im laufenden Discord-Betrieb testen

---

## ⚠️ Hinweis

Ich habe den Bot mit Hilfe von KI erstellt und programmiert. Die Bilder und auch Teile des Codes sind aus meinen Prompts entstanden und können Fehler enthalten.

Du bist für die Sicherheit deiner Zugangsdaten, deiner Konfiguration und deines Systems selbst verantwortlich.

Wenn dir ein Fehler oder eine sinnvolle Verbesserung auffällt, kannst du gerne ein Issue oder einen Pull Request erstellen.

---

## 🔒 Sicherheit

- Tokens und API Keys niemals in `config.json` eintragen.
- Secrets ausschließlich als Docker-Umgebungsvariablen oder über eine lokale `.env`-Datei setzen.
- `.env` niemals committen oder veröffentlichen.
- Der lokale `data/`-Ordner wird nicht in das Docker-Image eingebaut und sollte nicht ins Repository committed werden.
- `config.example.json` und `.env.example` sind ausschließlich Vorlagen und enthalten keine echten Zugangsdaten.
- Das veröffentlichte Docker-Image enthält keine persönlichen Secrets.
- Nicht autorisierte Discord-Server werden automatisch verlassen.
- Administrative Befehle sind auf Serverowner, Administratoren und den Bot-Owner beschränkt.
- Jeder Nutzer kann seine gespeicherten Daten privat einsehen (`!erinnerungen` per DM oder `/erinnerungen`), löschen (`!vergissmich`) und das Speichern abschalten (`!ignoriermich`). Im Sprachkanal wird nur Text gespeichert, keine Tonaufnahmen.
- API Keys sollten nach Möglichkeit mit passenden Berechtigungen und Nutzungslimits versehen werden.

---

## 🤝 Mitwirken

Pull Requests, Verbesserungsvorschläge und Bugreports sind jederzeit willkommen.

---

## 👤 Autor

KuroAI wurde von **GomatiGit** erstellt.

Dieses Projekt steht unter der MIT-Lizenz. Bei Weiterverwendung des Codes muss der enthaltene Copyright- und Lizenzhinweis erhalten bleiben.

---

## 📜 Lizenz

Copyright (c) 2026 GomatiGit
