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
      - /pfad/zum/ordner:/storage # frei waehlbarer Host-Ordner
    restart: unless-stopped
```

## Start

```bash
docker compose up -d --build
```

Danach ist das Webinterface unter `http://<server-ip>:8000` erreichbar.

## Hinweis

- In der Oberfläche trägst du deine SMTP-Daten, den Absendernamen, Empfänger und das Prüfintervall ein.
- Den Host-Ordner auf der linken Seite der Compose-Datei wählst du selbst.
- Im Webinterface kannst du danach die überwachten Ordner und den Backup-Ordner frei festlegen, zum Beispiel `/storage/scans` oder `/storage/mein-backup`.
