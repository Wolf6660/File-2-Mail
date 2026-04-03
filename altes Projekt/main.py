# === Konfiguration ===
watch_folder     = "/volume1/Scan/Belege_FIBU"
sent_folder      = "/volume1/Scan/Belege_FIBU/Versendet"
log_folder       = "/volume1/Scan/Belege_FIBU/Logs"
retry_folder     = "/volume1/Scan/Belege_FIBU/Warteschlange"

smtp_host        = "mxe85a.netcup.net"
smtp_port        = 587
smtp_user        = "belege@bueroservice-kopf.de"
smtp_pass        = "jnDjTSzW_.sgFJXs)auxQD62,X,PwaXuM"

recipient_email  = "belege@kopf-edv.de"
admin_email      = "daniel@kopf1.de"

# === Ab hier beginnt das eigentliche Skript ===
import os, time, shutil, smtplib, logging
from email.message import EmailMessage
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

MAX_RETRIES = 5
RETRY_INTERVAL = 300  # 5 Minuten
STABILITY_SECONDS = 15

os.makedirs(sent_folder, exist_ok=True)
os.makedirs(log_folder, exist_ok=True)
os.makedirs(retry_folder, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(log_folder, time.strftime("log_%Y-%m-%d.log")),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg):
    print(msg)
    logging.info(msg)

def send_email(file_path):
    try:
        msg = EmailMessage()
        msg['From'] = smtp_user
        msg['To'] = recipient_email
        msg['Subject'] = f"PDF-Datei: {os.path.basename(file_path)}"
        msg.set_content(f"Im Anhang findest du die Datei {os.path.basename(file_path)}.")

        with open(file_path, 'rb') as f:
            msg.add_attachment(f.read(), maintype='application', subtype='pdf', filename=os.path.basename(file_path))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        log(f"✅ Gesendet: {file_path}")
        return True
    except Exception as e:
        log(f"❌ Fehler beim Senden von {file_path}: {e}")
        return False

def notify_admin(filename, errormsg):
    try:
        msg = EmailMessage()
        msg['From'] = smtp_user
        msg['To'] = admin_email
        msg['Subject'] = f"❗ Fehler beim PDF-Versand: {filename}"
        msg.set_content(f"Die Datei {filename} konnte nach {MAX_RETRIES} Versuchen nicht versendet werden.\nFehler: {errormsg}")

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        log(f"📧 Admin-Benachrichtigung gesendet für {filename}")
    except Exception as e:
        log(f"❌ Fehler beim Senden der Admin-Benachrichtigung: {e}")

def wait_for_stability(path):
    stable_count = 0
    last_size = -1
    for _ in range(STABILITY_SECONDS):
        try:
            current_size = os.path.getsize(path)
            if current_size == last_size:
                stable_count += 1
            else:
                stable_count = 0
            last_size = current_size
            with open(path, 'rb') as f:
                f.read(1)
            if stable_count >= 3:
                return True
        except:
            stable_count = 0
        time.sleep(1)
    return False

def handle_file(file_path, retry=False):
    filename = os.path.basename(file_path)
    retry_file = os.path.join(retry_folder, filename + ".retry")

    if not wait_for_stability(file_path):
        log(f"🕒 Datei nicht stabil: {filename} – wird später erneut versucht.")
        if not retry:
            shutil.move(file_path, os.path.join(retry_folder, filename))
        return

    if send_email(file_path):
        if os.path.exists(retry_file):
            os.remove(retry_file)
        shutil.move(file_path, os.path.join(sent_folder, filename))
        log(f"📁 Verschoben: {filename}")
    else:
        count = 1
        if os.path.exists(retry_file):
            with open(retry_file, 'r') as f:
                count = int(f.read().strip()) + 1
        with open(retry_file, 'w') as f:
            f.write(str(count))
        if count >= MAX_RETRIES:
            notify_admin(filename, f"Sendefehler nach {count} Versuchen.")
        if not retry:
            shutil.move(file_path, os.path.join(retry_folder, filename))

class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".pdf"):
            return
        time.sleep(1)
        handle_file(event.src_path)

def process_existing():
    for fname in os.listdir(watch_folder):
        if fname.lower().endswith(".pdf"):
            handle_file(os.path.join(watch_folder, fname))

def retry_failed():
    for fname in os.listdir(retry_folder):
        if fname.lower().endswith(".pdf"):
            handle_file(os.path.join(retry_folder, fname), retry=True)

if __name__ == "__main__":
    log("🚀 Starte PDF-Überwachung")
    process_existing()

    observer = Observer()
    observer.schedule(PDFHandler(), watch_folder, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(RETRY_INTERVAL)
            retry_failed()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
