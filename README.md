# File-2-Mail

File-2-Mail laeuft per Docker auf Raspberry Pi, Synology und anderen Linux-Systemen. Die Einrichtung erfolgt komplett ueber das Webinterface.

## Docker Compose

```yaml
services:
  file2mail:
    build: .
    ports:
      - "8000:8000" # Webinterface
    volumes:
      - ./data:/app/data
      - /mein/pfad:/storage
    restart: unless-stopped
```

`8000:8000` bedeutet:

- Links `8000` = externer Port auf deinem Server
- Rechts `8000` = interner Port im Container

## Start

Projekt auf den Server holen:

```bash
git clone git@github.com:Wolf6660/File-2-Mail.git
cd File-2-Mail
```

```bash
docker compose up -d --build
```

Danach ist das Webinterface unter `http://<server-ip>:8000` erreichbar.

## Update

```bash
git pull
docker compose up -d --build
```

Die Einstellungen bleiben erhalten, solange `./data:/app/data` in der Compose-Datei eingebunden bleibt. Dort werden die Konfiguration und die Datenbank gespeichert.

## Hinweis

- In der Oberfläche trägst du deine SMTP-Daten, den Absendernamen, Empfänger und das Prüfintervall ein.
- Über das Webinterface kannst du überwachte Ordner und den Backup-Ordner frei festlegen.
- Im Reiter `System & Healthcheck` kannst du einstellen, wann `/health` als gesund oder ungesund gelten soll.
- Wenn du echte Ordner vom Host überwachen willst, muss dieser Pfad zusätzlich per Docker gemountet werden.

Empfohlen für mehrere frei wählbare Unterordner im Webinterface:

```yaml
volumes:
  - ./data:/app/data
  - /mein/pfad:/storage
```

Dann kannst du im Webinterface zum Beispiel `/storage/scans` oder `/storage/backup` eintragen.

Direkter Mount für genau einen bestimmten Ordner ist auch möglich:

```yaml
volumes:
  - ./data:/app/data
  - /mein/scanner-ordner:/scanner
```

Dann trägst du im Webinterface zum Beispiel `/scanner` als überwachten Ordner ein.
