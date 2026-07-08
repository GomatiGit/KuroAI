
# KuroAI

<p align="center">
  <img src="assets/background.png" alt="KuroAI Banner">
</p>

<p align="center">
  <img src="assets/avatar.png" width="180" alt="KuroAI Avatar">
</p>

<h1 align="center">🐾 KuroAI</h1>

<p align="center">
Ein Discord-Rollenspielbot mit OpenAI, mehreren Persönlichkeiten, Bildanalyse und serverindividueller Konfiguration.
</p>

<p align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)
![Discord.py](https://img.shields.io/badge/Discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--5-10A37F)
![License](https://img.shields.io/badge/License-MIT-green)

</p>

---

## ✨ Features

- 🐾 Mehrere Persönlichkeiten (Personas)
- 🤖 OpenAI GPT-5 Integration
- 🖼️ Analyse hochgeladener Bilder
- 👋 Welcome- und Goodbye-Nachrichten
- 🌍 Multi-Server-Unterstützung
- 🔒 Server-Whitelist
- 📜 Keyword-Reaktionen
- 🎭 Rollenspiel als Tabaxi-Zauberin
- ⚙️ Docker Ready
- 📝 Log-Channel für Fehler
- 🛡️ Administrativer Persona-Wechsel

---

## 📸 Bilder

### Banner

![](assets/background.png)

### Avatar

<p align="center">
<img src="assets/avatar.png" width="250">
</p>

---

## 🚀 Installation

1. Repository klonen

```bash
git clone https://github.com/GomatiGit/KuroAI.git
cd KuroAI
```

2. Konfiguration erstellen

```bash
cp config.example.json config.json
```

3. `config.json` anpassen.

4. Docker-Umgebungsvariablen setzen:

```yaml
environment:
  DISCORD_BOT_TOKEN: "DEIN_DISCORD_TOKEN"
  OPENAI_API_KEY: "DEIN_OPENAI_API_KEY"
```

5. Starten

```bash
docker compose up -d
```

---

## ⚙️ Konfiguration

- `allowed_guild_ids` bestimmt, auf welchen Servern Kuro aktiv ist.
- Jeder Server besitzt einen eigenen Eintrag unter `guilds`.
- Persönlichkeiten werden unter `personas` definiert.
- Keyword-Reaktionen befinden sich unter `keyword_rules`.

---

## 🎭 Personas

| Persona | Beschreibung |
|---------|--------------|
| Standard | Freundlich, verspielt und hilfsbereit |
| Frech | Sonntags etwas lockerer und sarkastischer. Sie hat da nämlich frei |
| Ghetto Kuro | Ein zufälliger Tag pro Monat mit besonders schlechter Laune |

---

## 🖼️ Bilderkennung

Erwähne Kuro und hänge ein Bild an:

> @Kuro Was siehst du auf diesem Bild?

Alternativ kann ein Bild auch als Antwort auf eine Nachricht von Kuro gesendet werden.

---

## 🛣️ Roadmap

- [ ] derzeit nichts weiter geplant

---

## 🔒 Sicherheit

- Tokens niemals in die `config.json` eintragen.
- Secrets ausschließlich über Docker-Umgebungsvariablen setzen.
- `config.example.json` als Vorlage verwenden.
- Nicht autorisierte Server werden automatisch verlassen.

---

## 🤝 Mitwirken

Pull Requests, Verbesserungsvorschläge und Bugreports sind jederzeit willkommen.

---

## 📜 Lizenz

Dieses Projekt steht unter der MIT License.
