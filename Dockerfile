FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot_multiserver.py /app/bot_multiserver.py
COPY assets /app/assets

CMD ["python", "bot_multiserver.py"]