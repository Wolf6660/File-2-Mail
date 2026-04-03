import os
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
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "file2mail.db"

DEFAULT_SETTINGS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "sender_email": "",
    "sender_name": "File-2-Mail",
    "admin_email": "",
    "scan_interval": "30",
    "backup_enabled": "0",
    "backup_folder": "",
    "use_tls": "1",
    "health_fail_on_config_error": "0",
    "health_fail_on_recent_errors": "1",
    "health_error_window_minutes": "15",
    "health_max_idle_seconds": "180",
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
                additional_recipients TEXT NOT NULL DEFAULT '',
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

        watched_folder_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(watched_folders)")
        }
        if "additional_recipients" not in watched_folder_columns:
            connection.execute(
                "ALTER TABLE watched_folders ADD COLUMN additional_recipients TEXT NOT NULL DEFAULT ''"
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
                SELECT id, folder_path, recipient_email, additional_recipients, display_name, is_active, created_at
                FROM watched_folders
                ORDER BY is_active DESC, recipient_email ASC, folder_path ASC
                """
            )
        )


def normalize_recipient_list(primary_recipient: str, additional_recipients: list[str] | None = None) -> tuple[str, str]:
    candidates = [primary_recipient.strip()]
    if additional_recipients:
        candidates.extend(item.strip() for item in additional_recipients if item and item.strip())

    unique: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        lowered = entry.lower()
        if entry and lowered not in seen:
            unique.append(entry)
            seen.add(lowered)

    primary = unique[0] if unique else ""
    extras = ",".join(unique[1:]) if len(unique) > 1 else ""
    return primary, extras


def folder_exists(folder_path: str) -> bool:
    normalized = str(Path(folder_path).expanduser())
    with db_cursor() as connection:
        row = connection.execute(
            "SELECT 1 FROM watched_folders WHERE folder_path = ?",
            (normalized,),
        ).fetchone()
    return row is not None


def insert_watched_folder(
    folder_path: str,
    recipient_email: str,
    additional_recipients: list[str] | None,
    display_name: str,
) -> None:
    normalized = str(Path(folder_path).expanduser())
    primary_recipient, extra_recipients = normalize_recipient_list(recipient_email, additional_recipients)
    with db_cursor() as connection:
        connection.execute(
            """
            INSERT INTO watched_folders(folder_path, recipient_email, additional_recipients, display_name, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                normalized,
                primary_recipient,
                extra_recipients,
                display_name.strip(),
                datetime.now().isoformat(),
            ),
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


def delete_logs(recipient_email: str = "") -> None:
    with db_cursor() as connection:
        if recipient_email:
            connection.execute("DELETE FROM email_logs WHERE recipient_email = ?", (recipient_email,))
        else:
            connection.execute("DELETE FROM email_logs")


def count_recent_errors(window_minutes: int) -> int:
    if window_minutes <= 0:
        return 0

    with db_cursor() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS error_count
            FROM email_logs
            WHERE status = 'error'
              AND datetime(REPLACE(created_at, 'T', ' ')) >= datetime('now', ?)
            """,
            (f"-{window_minutes} minutes",),
        ).fetchone()
    return int(row["error_count"] if row else 0)


def get_folder_recipients(folder_row: sqlite3.Row) -> list[str]:
    recipients = [folder_row["recipient_email"].strip()]
    additional = folder_row["additional_recipients"].strip()
    if additional:
        recipients.extend(item.strip() for item in additional.split(",") if item.strip())
    return recipients


def unique_path(destination_dir: Path, filename: str) -> Path:
    destination = destination_dir / filename
    if not destination.exists():
        return destination

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    stamp = datetime.now().strftime("%H%M%S")
    return destination_dir / f"{stem}_{stamp}{suffix}"


def move_to_error_folder(folder_path: str, file_path: Path) -> Path:
    error_dir = Path(folder_path).expanduser() / "Fehler"
    error_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_path(error_dir, file_path.name)
    shutil.move(str(file_path), str(destination))
    return destination


def browser_roots() -> list[Path]:
    configured = os.environ.get("FILE2MAIL_BROWSER_ROOTS", "/storage,/scanner")
    roots = [Path(item.strip()).expanduser() for item in configured.split(",") if item.strip()]
    available = [root for root in roots if root.exists() and root.is_dir()]
    return available or [Path("/")]


def path_is_browsable(target: Path) -> bool:
    resolved_target = target.resolve()
    for root in browser_roots():
        try:
            resolved_target.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def list_browser_entries(target: Path | None = None) -> dict[str, Any]:
    roots = browser_roots()
    if target is None:
        return {
            "current_path": "",
            "parent_path": "",
            "entries": [
                {
                    "name": root.name or str(root),
                    "path": str(root),
                }
                for root in roots
            ],
            "roots": [str(root) for root in roots],
        }

    resolved = target.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir() or not path_is_browsable(resolved):
        return list_browser_entries(None)

    entries = []
    for entry in sorted(resolved.iterdir(), key=lambda item: item.name.lower()):
        if entry.is_dir() and path_is_browsable(entry):
            entries.append({"name": entry.name, "path": str(entry)})

    parent_path = ""
    if path_is_browsable(resolved.parent) and resolved.parent != resolved:
        parent_path = str(resolved.parent)

    return {
        "current_path": str(resolved),
        "parent_path": parent_path,
        "entries": entries,
        "roots": [str(root) for root in roots],
    }


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


def build_test_message(settings: dict[str, str], recipient_email: str) -> EmailMessage:
    sender_email = settings["sender_email"].strip() or settings["smtp_username"].strip()
    sender_name = settings["sender_name"].strip()
    from_value = sender_email if not sender_name else f"{sender_name} <{sender_email}>"

    msg = EmailMessage()
    msg["From"] = from_value
    msg["To"] = recipient_email
    msg["Subject"] = "File-2-Mail Testversand"
    msg.set_content("Dieser Test wurde erfolgreich ueber File-2-Mail ausgelöst.")
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


def send_test_email(settings: dict[str, str], recipient_email: str) -> None:
    msg = build_test_message(settings, recipient_email)
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
    msg["Subject"] = f"File-2-Mail Fehler fuer {recipient_email}"
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
    recipients = get_folder_recipients(folder_row)
    recipient_email = folder_row["recipient_email"]
    folder_path = folder_row["folder_path"]
    filename = file_path.name

    if not wait_for_stability(file_path):
        add_log(recipient_email, folder_path, filename, "warning", "Datei ist noch nicht stabil und wird später erneut geprüft.")
        return

    send_errors: list[str] = []
    try:
        for current_recipient in recipients:
            try:
                send_email(settings, current_recipient, file_path)
                add_log(current_recipient, folder_path, filename, "success", "Datei wurde erfolgreich versendet.")
            except Exception as exc:  # noqa: BLE001
                error_message = f"Versand fehlgeschlagen: {exc}"
                send_errors.append(f"{current_recipient}: {exc}")
                add_log(current_recipient, folder_path, filename, "error", error_message)
                try:
                    notify_admin(settings, current_recipient, file_path, error_message)
                except Exception as admin_exc:  # noqa: BLE001
                    add_log(current_recipient, folder_path, filename, "warning", f"Admin-Hinweis fehlgeschlagen: {admin_exc}")

        if send_errors:
            destination = move_to_error_folder(folder_path, file_path)
            add_log(
                recipient_email,
                folder_path,
                filename,
                "warning",
                f"Datei wurde in den Fehler-Ordner verschoben: {destination}",
            )
            return

        backup_file(settings, recipient_email, file_path)
        file_path.unlink()
    except Exception as exc:  # noqa: BLE001
        destination = move_to_error_folder(folder_path, file_path)
        add_log(recipient_email, folder_path, filename, "error", f"Datei wurde in den Fehler-Ordner verschoben: {destination} ({exc})")


class FolderMonitor:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last_config_error = ""
        self._last_successful_cycle = time.time()
        self._last_cycle_started = 0.0
        self._last_cycle_finished = 0.0

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._last_cycle_started = time.time()
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
                self._last_successful_cycle = time.time()

            self._last_cycle_finished = time.time()

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

    def status(self) -> dict[str, float | str]:
        return {
            "last_config_error": self._last_config_error,
            "last_successful_cycle": self._last_successful_cycle,
            "last_cycle_started": self._last_cycle_started,
            "last_cycle_finished": self._last_cycle_finished,
        }


app = FastAPI(title="File-2-Mail")
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
def settings_page(request: Request, message: str = "", message_type: str = "info"):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": get_settings(),
            "watched_folders": get_watched_folders(),
            "active_tab": "smtp",
            "message": message,
            "message_type": message_type,
        },
    )


@app.get("/settings/folders")
def folder_settings_page(request: Request, message: str = "", message_type: str = "info"):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": get_settings(),
            "watched_folders": get_watched_folders(),
            "active_tab": "folders",
            "message": message,
            "message_type": message_type,
        },
    )


@app.get("/settings/system")
def system_settings_page(request: Request, message: str = "", message_type: str = "info"):
    current_settings = get_settings()
    health_state = evaluate_health(current_settings)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": current_settings,
            "watched_folders": get_watched_folders(),
            "active_tab": "system",
            "health_state": health_state,
            "message": message,
            "message_type": message_type,
        },
    )


@app.get("/settings/logs")
def log_settings_page(
    request: Request,
    recipient: str = "",
    status: str = "",
    search: str = "",
    message: str = "",
    message_type: str = "info",
):
    logs = get_logs(recipient=recipient, status=status, search=search)
    summary = get_log_summary()
    recipients = [row["recipient_email"] for row in summary]
    colors = recipient_colors(recipients)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": get_settings(),
            "watched_folders": get_watched_folders(),
            "active_tab": "logs",
            "logs": logs,
            "summary": summary,
            "recipient_filter": recipient,
            "status_filter": status,
            "search_filter": search,
            "recipient_colors": colors,
            "message": message,
            "message_type": message_type,
        },
    )


@app.post("/settings")
def save_settings(
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    sender_email: str = Form(""),
    sender_name: str = Form("File-2-Mail"),
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


@app.post("/settings/system")
def save_system_settings(
    health_fail_on_config_error: str | None = Form(None),
    health_fail_on_recent_errors: str | None = Form(None),
    health_error_window_minutes: str = Form("15"),
    health_max_idle_seconds: str = Form("180"),
):
    update_settings(
        {
            "health_fail_on_config_error": "1" if health_fail_on_config_error else "0",
            "health_fail_on_recent_errors": "1" if health_fail_on_recent_errors else "0",
            "health_error_window_minutes": health_error_window_minutes.strip() or "15",
            "health_max_idle_seconds": health_max_idle_seconds.strip() or "180",
        }
    )
    return RedirectResponse(url="/settings/system", status_code=303)


def evaluate_health(settings: dict[str, str]) -> dict[str, Any]:
    watched_folders = [row for row in get_watched_folders() if row["is_active"]]
    config_errors = validate_runtime(settings, watched_folders)
    monitor_state = monitor.status()

    try:
        max_idle_seconds = max(int(settings.get("health_max_idle_seconds", "180") or "180"), 30)
    except ValueError:
        max_idle_seconds = 180

    try:
        error_window_minutes = max(int(settings.get("health_error_window_minutes", "15") or "15"), 0)
    except ValueError:
        error_window_minutes = 15

    issues: list[str] = []

    if settings.get("health_fail_on_config_error", "0") == "1" and config_errors:
        issues.extend(config_errors)

    idle_seconds = int(max(0, time.time() - float(monitor_state["last_successful_cycle"] or 0)))
    if idle_seconds > max_idle_seconds:
        issues.append(f"Letzter erfolgreicher Prüfzyklus liegt {idle_seconds} Sekunden zurück.")

    recent_error_count = 0
    if settings.get("health_fail_on_recent_errors", "1") == "1":
        recent_error_count = count_recent_errors(error_window_minutes)
        if recent_error_count > 0:
            issues.append(
                f"In den letzten {error_window_minutes} Minuten wurden {recent_error_count} Fehler protokolliert."
            )

    return {
        "ok": not issues,
        "issues": issues,
        "idle_seconds": idle_seconds,
        "recent_error_count": recent_error_count,
        "error_window_minutes": error_window_minutes,
        "max_idle_seconds": max_idle_seconds,
        "last_config_error": monitor_state["last_config_error"],
        "last_cycle_started": monitor_state["last_cycle_started"],
        "last_cycle_finished": monitor_state["last_cycle_finished"],
    }


@app.get("/health")
def healthcheck():
    settings = get_settings()
    health_state = evaluate_health(settings)
    status_code = 200 if health_state["ok"] else 503
    return JSONResponse(
        status_code=status_code,
        content={
        "status": "ok" if health_state["ok"] else "unhealthy",
        "watched_folders": len(get_watched_folders()),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "issues": health_state["issues"],
        "idle_seconds": health_state["idle_seconds"],
        "recent_error_count": health_state["recent_error_count"],
        },
    )


@app.get("/api/browse")
def browse(path: str = ""):
    target = Path(path) if path else None
    return list_browser_entries(target)


@app.post("/settings/test-smtp")
def test_smtp(test_recipient: str = Form("")):
    settings = get_settings()
    recipient = test_recipient.strip() or settings.get("sender_email", "").strip() or settings.get("admin_email", "").strip()
    if not recipient:
        return RedirectResponse(
            url=f"/settings?message={quote_plus('Bitte eine Test-E-Mail-Adresse angeben.')}&message_type=error",
            status_code=303,
        )

    try:
        send_test_email(settings, recipient)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings?message={quote_plus(f'SMTP-Test fehlgeschlagen: {exc}')}&message_type=error",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/settings?message={quote_plus(f'SMTP-Test erfolgreich an {recipient}.')}&message_type=success",
        status_code=303,
    )


@app.post("/folders")
def add_folder(
    folder_path: str = Form(...),
    recipient_email: str = Form(...),
    additional_recipients: list[str] | None = Form(None),
    display_name: str = Form(...),
):
    normalized_path = folder_path.strip()
    if not normalized_path or not recipient_email.strip() or not display_name.strip():
        return RedirectResponse(
            url=f"/settings/folders?message={quote_plus('Bitte alle Pflichtfelder ausfuellen.')}&message_type=error",
            status_code=303,
        )

    if folder_exists(normalized_path):
        return RedirectResponse(
            url=f"/settings/folders?message={quote_plus('Dieser Ordner wird bereits ueberwacht.')}&message_type=error",
            status_code=303,
        )

    insert_watched_folder(normalized_path, recipient_email, additional_recipients, display_name)
    return RedirectResponse(
        url=f"/settings/folders?message={quote_plus('Ordner wurde erfolgreich angelegt.')}&message_type=success",
        status_code=303,
    )


@app.post("/folders/{folder_id}/toggle")
def toggle_folder(folder_id: int, is_active: str = Form("0")):
    set_folder_active(folder_id, is_active == "1")
    return RedirectResponse(url="/settings/folders", status_code=303)


@app.post("/folders/{folder_id}/delete")
def remove_folder(folder_id: int):
    delete_folder(folder_id)
    return RedirectResponse(url="/settings/folders", status_code=303)


@app.post("/logs/delete")
def remove_logs(recipient_email: str = Form("")):
    delete_logs(recipient_email.strip())
    if recipient_email.strip():
        return RedirectResponse(
            url=f"/settings/logs?message={quote_plus(f'Logs fuer {recipient_email.strip()} wurden geloescht.')}&message_type=success",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/settings/logs?message={quote_plus('Alle Logs wurden geloescht.')}&message_type=success",
        status_code=303,
    )
