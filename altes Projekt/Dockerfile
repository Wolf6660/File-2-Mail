FROM python:3.11-slim

WORKDIR /app

# Systemabhängigkeiten (optional: watchdog benötigt ggf. gcc)
RUN apt-get update && apt-get install -y gcc python3-dev libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# pip aktualisieren + watchdog installieren
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

CMD ["python", "/data/main.py"]
