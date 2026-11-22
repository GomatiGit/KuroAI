FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libopus: Audio für Sprachkanäle. git: nur für die optionale Zuhör-Erweiterung.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

# Optional: Erweiterung zum Zuhören im Sprachkanal (siehe docs/LOKALER_BETRIEB.md).
# Beispiel: --build-arg VOICE_RECV_SPEC="git+https://github.com/<fork>/discord-ext-voice-recv@<commit>"
ARG VOICE_RECV_SPEC=""
RUN if [ -n "$VOICE_RECV_SPEC" ]; then pip install --no-cache-dir "$VOICE_RECV_SPEC"; fi

COPY bot_multiserver.py /app/bot_multiserver.py
COPY kuro /app/kuro
COPY assets /app/assets

CMD ["python", "bot_multiserver.py"]
