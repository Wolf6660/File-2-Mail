import shutil
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scan2mail.db"

DEFAULT_SETTINGS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "sender_email": "",
    "sender_name": "Scan-2-Mail",
    "admin_email": "",
    "scan_interval": "30",
    "backup_enabled": "0",
    "backup_folder": "",
    "use_tls": "1",
}


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_data_dirs()
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


@contextmanager
def db_cursor() -> Any:
    connection = get_connection()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with db_cursor() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS watched_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                recipient_email TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                recipient_email TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )

        existing = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM settings")
        }
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )


def get_settings() -> dict[str, str]:
    with db_cursor() as connection:
        stored = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM settings")
        }
    settings = DEFAULT_SETTINGS.copy()
    settings.update(stored)
    return settings


def update_settings(values: dict[str, str]) -> None:
    with db_cursor() as connection:
        for key, value in values.items():
            connection.execute(
                """
                INSERT INTO settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )


def get_watched_folders() -> list[sqlite3.Row]:
    with db_cursor() as connection:
        return list(
            connection.execute(
                """
                SELECT id, folder_path, recipient_email, display_name, is_active, created_at
                FROM watched_folders
                ORDER BY is_active DESC, recipient_email ASC, folder_path ASC
                """
            )
        )


def insert_watched_folder(folder_path: str, recipient_email: str, display_name: str) -> None:
    normalized = str(Path(folder_path).expanduser())
    with db_cursor() as connection:
        connection.execute(
            """
            INSERT INTO watched_folders(folder_path, recipient_email, display_name, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (normalized, recipient_email.strip(), display_name.strip(), datetime.now().isoformat()),
        )


def set_folder_active(folder_id: int, is_active: bool) -> None:
    with db_cursor() as connection:
        connection.execute(
            "UPDATE watched_folders SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, folder_id),
        )


def delete_folder(folder_id: int) -> None:
    with db_cursor() as connection:
        connection.execute("DELETE FROM watched_folders WHERE id = ?", (folder_id,))


def add_log(recipient_email: str, folder_path: str, filename: str, status: str, message: str) -> None:
    with db_cursor() as connection:
        connection.execute(
            """
            INSERT INTO email_logs(created_at, recipient_email, folder_path, filename, status, message)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                recipient_email,
                folder_path,
                filename,
                status,
                message,
            ),
        )


def get_logs(recipient: str = "", status: str = "", search: str = "", limit: int = 300) -> list[sqlite3.Row]:
    query = """
        SELECT id, created_at, recipient_email, folder_path, filename, status, message
        FROM email_logs
        WHERE 1=1
    """
    params: list[Any] = []

    if recipient:
        query += " AND recipient_email = ?"
        params.append(recipient)
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND (filename LIKE ? OR folder_path LIKE ? OR message LIKE ?)"
        token = f"%{search}%"
        params.extend([token, token, token])

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as connection:
        return list(connection.execute(query, params))


def get_log_summary() -> list[sqlite3.Row]:
    with db_cursor() as connection:
        return list(
            connection.execute(
                """
                SELECT recipient_email,
                       COUNT(*) AS total_count,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
                FROM email_logs
                GROUP BY recipient_email
                ORDER BY recipient_email ASC
                """
            )
        )


def wait_for_stability(file_path: Path, attempts: int = 4, pause_seconds: float = 1.5) -> bool:
    last_size = -1
    stable_reads = 0
    for _ in range(attempts):
        try:
            current_size = file_path.stat().st_size
            with file_path.open("rb") as handle:
                handle.read(1)
            if current_size == last_size and current_size > 0:
                stable_reads += 1
            else:
                stable_reads = 0
            last_size = current_size
        except OSError:
            stable_reads = 0

        if stable_reads >= 2:
            return True
        time.sleep(pause_seconds)
    return False


def build_message(settings: dict[str, str], recipient_email: str, file_path: Path) -> EmailMessage:
    sender_email = settings["sender_email"].strip() or settings["smtp_username"].strip()
    sender_name = settings["sender_name"].strip()
    from_value = sender_email if not sender_name else f"{sender_name} <{sender_email}>"

    msg = EmailMessage()
    msg["From"] = from_value
    msg["To"] = recipient_email
    msg["Subject"] = f"Scan: {file_path.name}"
    msg.set_content(f"Im Anhang befindet sich die Datei {file_path.name}.")

    with file_path.open("rb") as handle:
        msg.add_attachment(
            handle.read(),
            maintype="application",
            subtype="pdf",
            filename=file_path.name,
        )

    return msg


def send_email(settings: dict[str, str], recipient_email: str, file_path: Path) -> None:
    msg = build_message(settings, recipient_email, file_path)
    smtp_port = int(settings["smtp_port"] or "587")

    with smtplib.SMTP(settings["smtp_host"], smtp_port, timeout=30) as server:
        if settings.get("use_tls", "1") == "1":
            server.starttls()
        if settings["smtp_username"].strip():
            server.login(settings["smtp_username"], settings["smtp_password"])
        server.send_message(msg)


def notify_admin(settings: dict[str, str], recipient_email: str, file_path: Path, error_message: str) -> None:
    admin_email = settings.get("admin_email", "").strip()
    if not admin_email:
        return

    sender_email = settings["sender_email"].strip() or settings["smtp_username"].strip()
    sender_name = settings["sender_name"].strip()
    from_value = sender_email if not sender_name else f"{sender_name} <{sender_email}>"

    msg = EmailMessage()
    msg["From"] = from_value
    msg["To"] = admin_email
    msg["Subject"] = f"Scan-2-Mail Fehler fuer {recipient_email}"
    msg.set_content(
        "\n".join(
            [
                "Beim Versand einer Datei ist ein Fehler aufgetreten.",
                f"Empfaenger: {recipient_email}",
                f"Datei: {file_path.name}",
                f"Ordner: {file_path.parent}",
                f"Fehler: {error_message}",
            ]
        )
    )

    smtp_port = int(settings["smtp_port"] or "587")
    with smtplib.SMTP(settings["smtp_host"], smtp_port, timeout=30) as server:
        if settings.get("use_tls", "1") == "1":
            server.starttls()
        if settings["smtp_username"].strip():
            server.login(settings["smtp_username"], settings["smtp_password"])
        server.send_message(msg)


def backup_file(settings: dict[str, str], recipient_email: str, file_path: Path) -> None:
    if settings.get("backup_enabled", "0") != "1":
        return

    backup_folder = settings.get("backup_folder", "").strip()
    if not backup_folder:
        return

    destination_dir = Path(backup_folder).expanduser() / recipient_email / datetime.now().strftime("%Y-%m-%d")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / file_path.name

    if destination.exists():
        stamp = datetime.now().strftime("%H%M%S")
        destination = destination_dir / f"{file_path.stem}_{stamp}{file_path.suffix}"

    shutil.copy2(file_path, destination)


def validate_runtime(settings: dict[str, str], watched_folders: list[sqlite3.Row]) -> list[str]:
    errors: list[str] = []
    required_keys = ["smtp_host", "sender_email"]
    for key in required_keys:
        if not settings.get(key, "").strip():
            errors.append(f"Einstellung '{key}' fehlt.")

    if settings.get("smtp_username", "").strip() and not settings.get("smtp_password", ""):
        errors.append("SMTP-Passwort fehlt.")

    if settings.get("backup_enabled", "0") == "1" and not settings.get("backup_folder", "").strip():
        errors.append("Backup ist aktiv, aber kein Backup-Ordner eingetragen.")

    if not watched_folders:
        errors.append("Es ist noch kein überwachter Ordner angelegt.")

    return errors


def process_file(settings: dict[str, str], folder_row: sqlite3.Row, file_path: Path) -> None:
    recipient_email = folder_row["recipient_email"]
    folder_path = folder_row["folder_path"]
    filename = file_path.name

    if not wait_for_stability(file_path):
        add_log(recipient_email, folder_path, filename, "warning", "Datei ist noch nicht stabil und wird später erneut geprüft.")
        return

    try:
        send_email(settings, recipient_email, file_path)
        backup_file(settings, recipient_email, file_path)
        file_path.unlink()
        add_log(recipient_email, folder_path, filename, "success", "Datei wurde erfolgreich versendet.")
    except Exception as exc:  # noqa: BLE001
        error_message = f"Versand fehlgeschlagen: {exc}"
        add_log(recipient_email, folder_path, filename, "error", error_message)
        try:
            notify_admin(settings, recipient_email, file_path, error_message)
        except Exception as admin_exc:  # noqa: BLE001
            add_log(recipient_email, folder_path, filename, "warning", f"Admin-Hinweis fehlgeschlagen: {admin_exc}")


class FolderMonitor:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last_config_error = ""

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = get_settings()
            watched_folders = [row for row in get_watched_folders() if row["is_active"]]
            config_errors = validate_runtime(settings, watched_folders)

            if config_errors:
                joined = " | ".join(config_errors)
                if joined != self._last_config_error:
                    add_log("System", "-", "-", "warning", joined)
                    self._last_config_error = joined
            else:
                self._last_config_error = ""
                for folder_row in watched_folders:
                    self._process_folder(settings, folder_row)

            try:
                interval = max(int(settings.get("scan_interval", "30") or "30"), 5)
            except ValueError:
                interval = 30
            self._stop_event.wait(interval)

    def _process_folder(self, settings: dict[str, str], folder_row: sqlite3.Row) -> None:
        folder_path = Path(folder_row["folder_path"]).expanduser()
        recipient_email = folder_row["recipient_email"]

        if not folder_path.exists():
            add_log(recipient_email, str(folder_path), "-", "error", "Überwachter Ordner existiert nicht.")
            return

        if not folder_path.is_dir():
            add_log(recipient_email, str(folder_path), "-", "error", "Pfad ist kein Ordner.")
            return

        try:
            pdf_files = sorted(
                [entry for entry in folder_path.iterdir() if entry.is_file() and entry.suffix.lower() == ".pdf"],
                key=lambda item: item.stat().st_mtime,
            )
        except OSError as exc:
            add_log(recipient_email, str(folder_path), "-", "error", f"Ordner konnte nicht gelesen werden: {exc}")
            return

        for file_path in pdf_files:
            process_file(settings, folder_row, file_path)


app = FastAPI(title="Scan-2-Mail")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
monitor = FolderMonitor()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    monitor.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    monitor.stop()


def recipient_colors(recipients: list[str]) -> dict[str, str]:
    palette = [
        "#006d77",
        "#f4a261",
        "#bc4749",
        "#457b9d",
        "#6a994e",
        "#7f5539",
        "#8338ec",
        "#ff006e",
    ]
    return {
        recipient: palette[index % len(palette)]
        for index, recipient in enumerate(sorted(set(recipients)))
    }


@app.get("/")
def dashboard(request: Request, recipient: str = "", status: str = "", search: str = ""):
    logs = get_logs(recipient=recipient, status=status, search=search)
    summary = get_log_summary()
    recipients = [row["recipient_email"] for row in summary]
    colors = recipient_colors(recipients)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "logs": logs,
            "summary": summary,
            "settings": get_settings(),
            "watched_folders": get_watched_folders(),
            "recipient_filter": recipient,
            "status_filter": status,
            "search_filter": search,
            "recipient_colors": colors,
        },
    )


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": get_settings(),
            "watched_folders": get_watched_folders(),
        },
    )


@app.post("/settings")
def save_settings(
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    sender_email: str = Form(""),
    sender_name: str = Form("Scan-2-Mail"),
    admin_email: str = Form(""),
    scan_interval: str = Form("30"),
    backup_folder: str = Form(""),
    backup_enabled: str | None = Form(None),
    use_tls: str | None = Form(None),
):
    update_settings(
        {
            "smtp_host": smtp_host.strip(),
            "smtp_port": smtp_port.strip() or "587",
            "smtp_username": smtp_username.strip(),
            "smtp_password": smtp_password,
            "sender_email": sender_email.strip(),
            "sender_name": sender_name.strip(),
            "admin_email": admin_email.strip(),
            "scan_interval": scan_interval.strip() or "30",
            "backup_folder": backup_folder.strip(),
            "backup_enabled": "1" if backup_enabled else "0",
            "use_tls": "1" if use_tls else "0",
        }
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/health")
def healthcheck():
    return {
        "status": "ok",
        "watched_folders": len(get_watched_folders()),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


@app.post("/folders")
def add_folder(
    folder_path: str = Form(...),
    recipient_email: str = Form(...),
    display_name: str = Form(""),
):
    insert_watched_folder(folder_path, recipient_email, display_name)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/folders/{folder_id}/toggle")
def toggle_folder(folder_id: int, is_active: str = Form("0")):
    set_folder_active(folder_id, is_active == "1")
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/folders/{folder_id}/delete")
def remove_folder(folder_id: int):
    delete_folder(folder_id)
    return RedirectResponse(url="/settings", status_code=303)
