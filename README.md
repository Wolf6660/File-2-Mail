# File-2-Mail

File-2-Mail laeuft per Docker auf Raspberry Pi, Synology und anderen Linux-Systemen. Die Einrichtung erfolgt komplett ueber das Webinterface.

## Funktionen

- Webinterface mit den Bereichen `SMTP Versand`, `Ordner`, `System & Healthcheck` und `Logs`
- SMTP-Einstellungen mit Testversand
- Mehrere überwachte Ordner mit eigenem Anzeigenamen
- Pro Ordner eine Haupt-Empfängeradresse und optionale weitere Empfänger
- PDF, Bilder und andere Dateien können als Anhang versendet werden
- Grafische Ordnerauswahl aus dem gemounteten Docker-Bereich
- Backup-Funktion für versendete Dateien
- Scanner-Schutz durch Mindestalter, Stabilitätsprüfungen und Prüfpausen
- OCR pro Ordner einzeln aktivierbar, wird aber nur für PDFs und Bilddateien angewendet
- Pro Ordner optionale Benachrichtigungs-E-Mail für Erfolg und/oder Fehler
- Fehlerhafte Dateien werden in den Unterordner `Fehler` verschoben
- Dashboard mit farbigen Logs pro Empfänger und Filterung
- Logs können komplett oder pro Empfänger gelöscht werden
- Docker-Healthcheck mit konfigurierbaren Regeln im Webinterface
- Spendenbutton im Webinterface und auf GitHub

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

OCR ist bereits im gleichen Container integriert. Es wird kein zusätzlicher OCR-Container benötigt.

## Start

Projekt auf den Server holen:

```bash
git clone git@github.com:Wolf6660/File-2-Mail.git
cd File-2-Mail
```

```bash
sudo docker compose up -d --build
```

Danach ist das Webinterface unter `http://<server-ip>:8000` erreichbar.

## Unterstützung

[Spenden per PayPal](https://www.paypal.com/donate?business=news%40spider-wolf.de&item_name=File-2-Mail+unterstuetzen&currency_code=EUR)

## Update

```bash
git pull
sudo docker compose up -d --build
```

Die Einstellungen bleiben erhalten, solange `./data:/app/data` in der Compose-Datei eingebunden bleibt. Dort werden die Konfiguration und die Datenbank gespeichert.

## Hinweis

- In `SMTP Versand` pflegst du SMTP-Zugang, Absender und Testversand.
- In `Ordner` legst du überwachte Ordner an, bearbeitest sie, aktivierst OCR und konfigurierst Benachrichtigungen pro Ordner.
- In `System & Healthcheck` steuerst du, wann `/health` als gesund oder ungesund gelten soll.
- In `Logs` filterst und löschst du Protokolle komplett oder pro Empfänger.
- Wenn du echte Ordner vom Host überwachen willst, muss dieser Pfad per Docker gemountet werden.

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
