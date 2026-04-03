# Scan-2-Mail

Scan-2-Mail laeuft per Docker auf Raspberry Pi, Synology und anderen Linux-Systemen. Die Einrichtung erfolgt komplett ueber das Webinterface.

## Docker Compose

```yaml
services:
  scan2mail:
    build: .
    ports:
      - "8000:8000" # Webinterface
    volumes:
      - ./data:/app/data
      - ./files:/files # Scan- und Backup-Ordner
    restart: unless-stopped
```

## Start

```bash
docker compose up -d --build
```

Danach ist das Webinterface unter `http://<server-ip>:8000` erreichbar.

## Hinweis

- In der Oberfläche trägst du deine SMTP-Daten, den Absendernamen, Empfänger und das Prüfintervall ein.
- Für überwachte Ordner und Backups nutzt du Pfade innerhalb von `/files`, zum Beispiel `/files/eingang` oder `/files/backup`.
- Wenn du lieber echte Hostpfade mounten willst, kannst du `./files:/files` in der Compose-Datei durch einen eigenen Pfad ersetzen.
