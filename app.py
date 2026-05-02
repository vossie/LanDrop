#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import shutil
import struct
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATES_DIR = BASE_DIR / "templates"
VERSION_FILE = BASE_DIR / "VERSION"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))).resolve()
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB
EXPIRY_SECONDS = 24 * 60 * 60
MAX_TEXT_HISTORY = 200
MAX_FILE_HISTORY = 100
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL", "").strip()

state_lock = threading.Lock()
session_lock = threading.Lock()
websocket_lock = threading.Lock()
authorized_sessions = set()
websocket_clients = set()
janitor_thread: threading.Thread | None = None
janitor_stop_event = threading.Event()
shared_state = {
    "updated_at": 0.0,
    "texts": [],
    "files": [],
}
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def uploads_index_path() -> Path:
    return UPLOAD_DIR / ".landrop-files.json"


def now_ts() -> float:
    return time.time()


def load_app_version() -> str:
    configured = os.environ.get("APP_VERSION", "").strip()
    if configured:
        return configured
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    return version or "dev"


def sanitize_filename(name: str) -> str:
    raw_name = Path(name).name.strip().replace("\x00", "")
    safe_name = raw_name or "upload.bin"
    return safe_name


def unique_filename(name: str) -> str:
    candidate = sanitize_filename(name)
    path = UPLOAD_DIR / candidate
    if not path.exists():
        return candidate

    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    return f"{stem}-{secrets.token_hex(4)}{suffix}"


def make_id() -> str:
    return secrets.token_hex(8)


def make_short_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(4))


def make_session_id() -> str:
    return secrets.token_urlsafe(24)


def session_cookie(session_id: str) -> str:
    return f"session={session_id}; Path=/; HttpOnly; SameSite=Lax"


def parse_cookies(cookie_header: str) -> dict:
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not ACCESS_CODE:
        return True

    api_key = handler.headers.get("X-API-Key", "").strip()
    if api_key and hmac.compare_digest(api_key, ACCESS_CODE):
        return True

    cookies = parse_cookies(handler.headers.get("Cookie", ""))
    session_id = cookies.get("session")
    if not session_id:
        return False

    with session_lock:
        return session_id in authorized_sessions


def create_authorized_session() -> str:
    session_id = make_session_id()
    with session_lock:
        authorized_sessions.add(session_id)
    return session_id


def recompute_updated_at_locked() -> None:
    timestamps = [item["created_at"] for item in shared_state["texts"]]
    timestamps.extend(item["created_at"] for item in shared_state["files"])
    shared_state["updated_at"] = max(timestamps, default=0.0)


def persist_files_locked() -> None:
    ensure_upload_dir()
    payload = {"files": shared_state["files"]}
    index_path = uploads_index_path()
    temp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    temp_path.replace(index_path)


def prune_expired_locked() -> None:
    cutoff = now_ts() - EXPIRY_SECONDS

    expired_files = [
        item for item in shared_state["files"] if item["created_at"] < cutoff
    ]
    shared_state["texts"] = [
        item for item in shared_state["texts"] if item["created_at"] >= cutoff
    ]
    shared_state["files"] = [
        item for item in shared_state["files"] if item["created_at"] >= cutoff
    ]

    for item in expired_files:
        target = UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)

    recompute_updated_at_locked()
    if expired_files:
        persist_files_locked()


def trim_history_limits_locked() -> None:
    overflow_files = []

    if len(shared_state["texts"]) > MAX_TEXT_HISTORY:
        shared_state["texts"] = shared_state["texts"][:MAX_TEXT_HISTORY]

    if len(shared_state["files"]) > MAX_FILE_HISTORY:
        overflow_files = shared_state["files"][MAX_FILE_HISTORY:]
        shared_state["files"] = shared_state["files"][:MAX_FILE_HISTORY]

    for item in overflow_files:
        target = UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)

    recompute_updated_at_locked()
    if overflow_files:
        persist_files_locked()


def prune_expired_entries() -> bool:
    with state_lock:
        before_texts = len(shared_state["texts"])
        before_files = len(shared_state["files"])
        prune_expired_locked()
        return before_texts != len(shared_state["texts"]) or before_files != len(
            shared_state["files"]
        )


def load_persisted_files() -> None:
    ensure_upload_dir()
    index_path = uploads_index_path()
    if not index_path.exists():
        with state_lock:
            shared_state["files"] = []
            recompute_updated_at_locked()
        return

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}

    raw_files = payload.get("files", [])
    if not isinstance(raw_files, list):
        raw_files = []

    restored_files = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        stored_name = item.get("stored_name")
        if not isinstance(stored_name, str):
            continue
        target = UPLOAD_DIR / stored_name
        if not target.exists() or not target.is_file():
            continue

        restored_files.append(
            {
                "id": str(item.get("id") or make_id()),
                "name": sanitize_filename(str(item.get("name") or stored_name)),
                "stored_name": stored_name,
                "size": int(item.get("size") or target.stat().st_size),
                "hidden": bool(item.get("hidden", False)),
                "password_hash": item.get("password_hash")
                if isinstance(item.get("password_hash"), str)
                else None,
                "sharer_name": str(item.get("sharer_name") or "").strip(),
                "sharer_ip": str(item.get("sharer_ip") or "").strip(),
                "short_code": str(item.get("short_code") or make_short_code()).upper(),
                "created_at": float(item.get("created_at") or now_ts()),
                "expires_at": float(item.get("expires_at") or (now_ts() + EXPIRY_SECONDS)),
            }
        )

    restored_files.sort(key=lambda item: item["created_at"], reverse=True)

    with state_lock:
        shared_state["files"] = restored_files
        prune_expired_locked()
        trim_history_limits_locked()
        persist_files_locked()


def mask_text_value(value: str) -> str:
    return "".join("*" if not char.isspace() else char for char in value)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        return True
    return hmac.compare_digest(hash_password(password), password_hash)


def serialize_text_entry(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "hidden": entry["hidden"],
        "password_required": bool(entry.get("password_hash")),
        "sharer_name": entry.get("sharer_name", ""),
        "sharer_ip": entry.get("sharer_ip", ""),
        "short_code": entry["short_code"],
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
        "masked_content": mask_text_value(entry["content"]),
        "content": None
        if entry["hidden"] and entry.get("password_hash")
        else entry["content"],
    }


def serialize_file_entry(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "name": entry["name"],
        "stored_name": entry["stored_name"],
        "size": entry["size"],
        "hidden": entry.get("hidden", False),
        "password_required": bool(entry.get("password_hash")),
        "sharer_name": entry.get("sharer_name", ""),
        "sharer_ip": entry.get("sharer_ip", ""),
        "short_code": entry["short_code"],
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
    }


def get_snapshot() -> dict:
    with state_lock:
        prune_expired_locked()
        latest_text = ""
        if shared_state["texts"]:
            latest_entry = shared_state["texts"][0]
            if not (latest_entry["hidden"] and latest_entry.get("password_hash")):
                latest_text = latest_entry["content"]
        return {
            "updated_at": shared_state["updated_at"],
            "expires_after_seconds": EXPIRY_SECONDS,
            "latest_text": latest_text,
            "texts": [serialize_text_entry(item) for item in shared_state["texts"]],
            "files": [serialize_file_entry(item) for item in shared_state["files"]],
        }


def get_latest_text_entry() -> dict | None:
    with state_lock:
        prune_expired_locked()
        if not shared_state["texts"]:
            return None
        return serialize_text_entry(shared_state["texts"][0])


def get_latest_file_entry() -> dict | None:
    with state_lock:
        prune_expired_locked()
        if not shared_state["files"]:
            return None
        return serialize_file_entry(shared_state["files"][0])


def make_unique_short_code_locked() -> str:
    existing = {item["short_code"] for item in shared_state["texts"]}
    existing.update(item["short_code"] for item in shared_state["files"])
    while True:
        candidate = make_short_code()
        if candidate not in existing:
            return candidate


def add_text_entry(
    value: str,
    hidden: bool = False,
    password: str = "",
    sharer_name: str = "",
    sharer_ip: str = "",
) -> None:
    with state_lock:
        prune_expired_locked()
        created_at = now_ts()
        shared_state["texts"].insert(
            0,
            {
                "id": make_id(),
                "content": value,
                "hidden": hidden,
                "password_hash": hash_password(password) if password else None,
                "sharer_name": sharer_name.strip(),
                "sharer_ip": sharer_ip.strip(),
                "short_code": make_unique_short_code_locked(),
                "created_at": created_at,
                "expires_at": created_at + EXPIRY_SECONDS,
            },
        )
        trim_history_limits_locked()


def delete_text_entry(entry_id: str) -> bool:
    with state_lock:
        prune_expired_locked()
        original_len = len(shared_state["texts"])
        shared_state["texts"] = [
            item for item in shared_state["texts"] if item["id"] != entry_id
        ]
        recompute_updated_at_locked()
        return len(shared_state["texts"]) != original_len


def add_file(
    original_name: str,
    stored_name: str,
    size: int,
    hidden: bool = False,
    password: str = "",
    sharer_name: str = "",
    sharer_ip: str = "",
) -> None:
    with state_lock:
        prune_expired_locked()
        created_at = now_ts()
        shared_state["files"].insert(
            0,
            {
                "id": make_id(),
                "name": original_name,
                "stored_name": stored_name,
                "size": size,
                "hidden": hidden,
                "password_hash": hash_password(password) if password else None,
                "sharer_name": sharer_name.strip(),
                "sharer_ip": sharer_ip.strip(),
                "short_code": make_unique_short_code_locked(),
                "created_at": created_at,
                "expires_at": created_at + EXPIRY_SECONDS,
            },
        )
        trim_history_limits_locked()
        persist_files_locked()


def delete_file_entry(file_id: str) -> bool:
    with state_lock:
        prune_expired_locked()
        removed = None
        kept = []
        for item in shared_state["files"]:
            if item["id"] == file_id and removed is None:
                removed = item
            else:
                kept.append(item)
        shared_state["files"] = kept
        recompute_updated_at_locked()
        if removed is not None:
            persist_files_locked()

    if removed is None:
        return False

    target = UPLOAD_DIR / removed["stored_name"]
    if target.exists():
        target.unlink(missing_ok=True)
    return True


def find_file_entry(file_id: str) -> dict | None:
    with state_lock:
        prune_expired_locked()
        for item in shared_state["files"]:
            if item["id"] == file_id:
                return dict(item)
    return None


def find_text_entry(text_id: str) -> dict | None:
    with state_lock:
        prune_expired_locked()
        for item in shared_state["texts"]:
            if item["id"] == text_id:
                return dict(item)
    return None


def find_entry_by_short_code(short_code: str) -> tuple[str, dict] | None:
    normalized = short_code.strip().upper()
    with state_lock:
        prune_expired_locked()
        for item in shared_state["texts"]:
            if item["short_code"] == normalized:
                return ("text", dict(item))
        for item in shared_state["files"]:
            if item["short_code"] == normalized:
                return ("file", dict(item))
    return None


def entry_password_is_valid(entry: dict, password: str) -> bool:
    return verify_password(password, entry.get("password_hash"))


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def websocket_accept_value(key: str) -> str:
    digest = hashlib.sha1(f"{key}{WEBSOCKET_GUID}".encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def websocket_frame(opcode: int, payload: bytes = b"") -> bytes:
    first_byte = 0x80 | (opcode & 0x0F)
    payload_len = len(payload)
    if payload_len < 126:
        header = bytes([first_byte, payload_len])
    elif payload_len < 65536:
        header = bytes([first_byte, 126]) + struct.pack("!H", payload_len)
    else:
        header = bytes([first_byte, 127]) + struct.pack("!Q", payload_len)
    return header + payload


class WebSocketClient:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.write_lock = threading.Lock()
        self.closed = False

    def send_frame(self, opcode: int, payload: bytes = b"") -> bool:
        with self.write_lock:
            if self.closed:
                return False
            try:
                self.connection.sendall(websocket_frame(opcode, payload))
                return True
            except OSError:
                self.closed = True
                return False

    def send_json(self, payload: dict) -> bool:
        return self.send_frame(0x1, json.dumps(payload).encode("utf-8"))

    def close(self) -> None:
        with self.write_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.connection.close()
            except OSError:
                pass


def register_websocket_client(client: WebSocketClient) -> None:
    with websocket_lock:
        websocket_clients.add(client)


def unregister_websocket_client(client: WebSocketClient) -> None:
    with websocket_lock:
        websocket_clients.discard(client)
    client.close()


def broadcast_snapshot(snapshot: dict | None = None) -> None:
    payload = snapshot or get_snapshot()
    with websocket_lock:
        clients = list(websocket_clients)

    failed_clients = []
    for client in clients:
        if not client.send_json(payload):
            failed_clients.append(client)

    for client in failed_clients:
        unregister_websocket_client(client)


def start_background_tasks() -> None:
    global janitor_thread
    if janitor_thread and janitor_thread.is_alive():
        return

    janitor_stop_event.clear()

    def run_janitor() -> None:
        while not janitor_stop_event.wait(1.0):
            if prune_expired_entries():
                broadcast_snapshot()

    janitor_thread = threading.Thread(target=run_janitor, daemon=True)
    janitor_thread.start()


def stop_background_tasks() -> None:
    global janitor_thread
    janitor_stop_event.set()
    if janitor_thread is not None:
        janitor_thread.join(timeout=2)
    janitor_thread = None

    with websocket_lock:
        clients = list(websocket_clients)
        websocket_clients.clear()
    for client in clients:
        client.close()


def get_share_base_url() -> str:
    return SHARE_BASE_URL.rstrip("/")


def get_app_version() -> str:
    return load_app_version()


def base_url_from_request(handler: BaseHTTPRequestHandler) -> str:
    configured = get_share_base_url()
    if configured:
        return configured

    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").strip()
    proto = forwarded_proto or ("https" if getattr(handler.server, "server_port", 0) == 443 else "http")
    host = handler.headers.get("Host", "").strip()
    if host:
        return f"{proto}://{host}".rstrip("/")

    server_host, server_port = handler.server.server_address[:2]
    return f"http://{server_host}:{server_port}"


def share_payload(entry_type: str, entry: dict, base_url: str) -> dict:
    path = f"/s/{urllib.parse.quote(entry['short_code'])}"
    payload = {
        "type": entry_type,
        "id": entry["id"],
        "short_code": entry["short_code"],
        "share_path": path,
        "share_url": f"{base_url.rstrip('/')}{path}",
        "hidden": bool(entry.get("hidden", False)),
        "password_required": bool(entry.get("password_hash")),
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
    }
    if entry_type == "text":
        payload["content"] = entry["content"]
    else:
        payload["name"] = entry["name"]
        payload["size"] = entry["size"]
        payload["download_path"] = f"/download/{urllib.parse.quote(entry['id'])}"
        payload["download_url"] = f"{base_url.rstrip('/')}{payload['download_path']}"
    return payload


def render_template(name: str, replacements: dict[str, str] | None = None) -> str:
    template_path = TEMPLATES_DIR / name
    body = template_path.read_text(encoding="utf-8")
    for needle, value in (replacements or {}).items():
        body = body.replace(needle, value)
    return body


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LanDrop/1.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.serve_asset("landrop-favicon.svg")
            return

        if parsed.path.startswith("/assets/"):
            asset_name = parsed.path.removeprefix("/assets/")
            self.serve_asset(asset_name)
            return

        if parsed.path == "/":
            if not is_authorized(self):
                self.send_html(render_template("login.html"))
                return
            self.send_html(
                render_template(
                    "index.html",
                    {
                        "__SHARE_BASE_URL__": json.dumps(get_share_base_url()),
                        "__APP_VERSION__": html.escape(get_app_version()),
                    },
                )
            )
            return

        if ACCESS_CODE and not is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path == "/ws":
            self.handle_websocket()
            return

        if parsed.path == "/api/state":
            self.send_json(get_snapshot())
            return

        if parsed.path == "/api/latest-text":
            self.handle_latest_text()
            return

        if parsed.path == "/api/latest-file":
            self.handle_latest_file()
            return

        if parsed.path == "/api/latest-file/content":
            self.handle_latest_file_content()
            return

        if parsed.path.startswith("/s/"):
            short_code = urllib.parse.unquote(parsed.path.removeprefix("/s/"))
            password = urllib.parse.parse_qs(parsed.query).get("password", [""])[0]
            self.handle_short_link(short_code, password)
            return

        if parsed.path.startswith("/download/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/download/"))
            password = urllib.parse.parse_qs(parsed.query).get("password", [""])[0]
            self.serve_download(file_id, password)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            self.handle_login()
            return

        if ACCESS_CODE and not is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path == "/api/text":
            self.handle_text_update()
            return

        if parsed.path == "/api/share-text":
            self.handle_text_share()
            return

        if parsed.path.startswith("/api/text/") and parsed.path.endswith("/reveal"):
            entry_id = urllib.parse.unquote(
                parsed.path.removeprefix("/api/text/").removesuffix("/reveal")
            )
            self.handle_text_reveal(entry_id)
            return

        if parsed.path == "/api/upload":
            self.handle_file_upload()
            return

        if parsed.path == "/api/share-file":
            self.handle_file_share()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if ACCESS_CODE and not is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path.startswith("/api/text/"):
            entry_id = urllib.parse.unquote(parsed.path.removeprefix("/api/text/"))
            self.handle_text_delete(entry_id)
            return

        if parsed.path.startswith("/api/file/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/api/file/"))
            self.handle_file_delete(file_id)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_login(self) -> None:
        if not ACCESS_CODE:
            self.send_json({"ok": True})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        code = payload.get("code", "")
        if not isinstance(code, str) or code != ACCESS_CODE:
            self.send_error(HTTPStatus.UNAUTHORIZED, "Wrong access code")
            return

        session_id = create_authorized_session()
        self.send_json({"ok": True}, cookie=session_cookie(session_id))

    def handle_text_update(self) -> None:
        entry = self.parse_text_request()
        if entry is None:
            return

        add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
        )
        snapshot = get_snapshot()
        self.send_json(snapshot)
        broadcast_snapshot(snapshot)

    def handle_text_share(self) -> None:
        entry = self.parse_text_request()
        if entry is None:
            return

        add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
        )
        created = find_text_entry(get_snapshot()["texts"][0]["id"])
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create text entry")
            return
        snapshot = get_snapshot()
        self.send_json(share_payload("text", created, base_url_from_request(self)))
        broadcast_snapshot(snapshot)

    def parse_text_request(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        text = payload.get("text", "")
        if not isinstance(text, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Text must be a string")
            return
        normalized_text = text.strip()
        if not normalized_text:
            self.send_error(HTTPStatus.BAD_REQUEST, "Text cannot be empty")
            return
        hidden = payload.get("hidden", False)
        if not isinstance(hidden, bool):
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden must be a boolean")
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        sharer_name = payload.get("name", "")
        if not isinstance(sharer_name, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Name must be a string")
            return None

        return {
            "text": normalized_text,
            "hidden": hidden,
            "password": password.strip(),
            "name": sharer_name.strip(),
        }

    def handle_text_reveal(self, entry_id: str) -> None:
        entry = find_text_entry(entry_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Text entry not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        if not entry_password_is_valid(entry, password):
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return

        self.send_json(
            {
                "content": entry["content"],
            }
        )

    def handle_latest_text(self) -> None:
        entry = get_latest_text_entry()
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No text entries found")
            return
        self.send_json(entry)

    def handle_latest_file(self) -> None:
        entry = get_latest_file_entry()
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.send_json(entry)

    def handle_latest_file_content(self) -> None:
        entry = get_latest_file_entry()
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.serve_download_entry(entry)

    def handle_short_link(self, short_code: str, password: str = "") -> None:
        entry = find_entry_by_short_code(short_code)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Shared item not found")
            return

        entry_type, payload = entry
        if entry_type == "text":
            if payload.get("password_hash") and not entry_password_is_valid(payload, password):
                self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
                return
            self.send_text(payload["content"])
            return

        if payload.get("password_hash") and not entry_password_is_valid(payload, password):
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return
        self.serve_download_entry(payload)

    def handle_text_delete(self, entry_id: str) -> None:
        if not delete_text_entry(entry_id):
            self.send_error(HTTPStatus.NOT_FOUND, "Text entry not found")
            return
        snapshot = get_snapshot()
        self.send_json(snapshot)
        broadcast_snapshot(snapshot)

    def handle_file_delete(self, file_id: str) -> None:
        if not delete_file_entry(file_id):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        snapshot = get_snapshot()
        self.send_json(snapshot)
        broadcast_snapshot(snapshot)

    def handle_file_upload(self) -> None:
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        self.store_file_upload(parsed)
        snapshot = get_snapshot()
        self.send_json(snapshot)
        broadcast_snapshot(snapshot)

    def handle_file_share(self) -> None:
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        created = self.store_file_upload(parsed)
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create file entry")
            return
        snapshot = get_snapshot()
        self.send_json(share_payload("file", created, base_url_from_request(self)))
        broadcast_snapshot(snapshot)

    def parse_file_upload_request(self) -> dict | None:
        content_type = self.headers.get("Content-Type", "")
        boundary = None
        for item in content_type.split(";"):
            item = item.strip()
            if item.startswith("boundary="):
                boundary = item.split("=", 1)[1].encode("utf-8")
                break

        if not boundary:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing multipart boundary")
            return None

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Empty upload")
            return None
        if length > MAX_FILE_SIZE:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
            return None

        body = self.rfile.read(length)
        filename, file_bytes, fields = self.parse_multipart_file(body, boundary)
        if filename is None or file_bytes is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Could not read uploaded file")
            return None
        hidden = fields.get("hidden", "false").lower() == "true"
        password = fields.get("password", "").strip()
        sharer_name = fields.get("name", "").strip()
        if hidden and not password:
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden files require a password")
            return None

        return {
            "filename": filename,
            "file_bytes": file_bytes,
            "hidden": hidden,
            "password": password,
            "name": sharer_name,
        }

    def store_file_upload(self, parsed: dict) -> dict | None:
        ensure_upload_dir()
        stored_name = unique_filename(parsed["filename"])
        target = UPLOAD_DIR / stored_name
        with target.open("wb") as handle:
            handle.write(parsed["file_bytes"])

        add_file(
            parsed["filename"],
            stored_name,
            len(parsed["file_bytes"]),
            hidden=parsed["hidden"],
            password=parsed["password"],
            sharer_name=parsed["name"],
            sharer_ip=self.client_address[0],
        )
        return find_file_entry(get_snapshot()["files"][0]["id"])

    def parse_multipart_file(self, body: bytes, boundary: bytes):
        marker = b"--" + boundary
        parts = body.split(marker)
        fields = {}
        upload_name = None
        upload_payload = None
        for part in parts:
            if not part or part in (b"--\r\n", b"--"):
                continue
            part = part.lstrip(b"\r\n")
            headers_blob, separator, payload = part.partition(b"\r\n\r\n")
            if not separator:
                continue

            headers_text = headers_blob.decode("utf-8", errors="ignore")
            field_name = None
            filename = None
            for line in headers_text.split("\r\n"):
                lower = line.lower()
                if lower.startswith("content-disposition:"):
                    for piece in line.split(";"):
                        piece = piece.strip()
                        if piece.startswith("name="):
                            field_name = piece.split("=", 1)[1].strip("\"")
                        if piece.startswith("filename="):
                            filename = piece.split("=", 1)[1].strip("\"")

            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if payload.endswith(b"--"):
                payload = payload[:-2]

            if field_name == "file":
                upload_name = sanitize_filename(filename or "upload.bin")
                upload_payload = payload
            elif field_name:
                fields[field_name] = payload.decode("utf-8", errors="ignore")

        return upload_name, upload_payload, fields

    def handle_websocket(self) -> None:
        upgrade = self.headers.get("Upgrade", "")
        connection = self.headers.get("Connection", "")
        websocket_key = self.headers.get("Sec-WebSocket-Key", "")
        websocket_version = self.headers.get("Sec-WebSocket-Version", "")

        if upgrade.lower() != "websocket" or "upgrade" not in connection.lower():
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected WebSocket upgrade")
            return
        if not websocket_key or websocket_version != "13":
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid WebSocket headers")
            return

        accept_value = websocket_accept_value(websocket_key)
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_value)
        self.end_headers()

        client = WebSocketClient(self.connection)
        register_websocket_client(client)
        client.send_json(get_snapshot())

        try:
            while True:
                opcode, payload = self.read_websocket_frame()
                if opcode is None:
                    break
                if opcode == 0x8:
                    client.send_frame(0x8, payload[:2] if payload else b"")
                    break
                if opcode == 0x9:
                    client.send_frame(0xA, payload)
        finally:
            unregister_websocket_client(client)

    def read_exact(self, length: int) -> bytes | None:
        remaining = length
        chunks = []
        while remaining > 0:
            try:
                chunk = self.rfile.read(remaining)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_websocket_frame(self) -> tuple[int | None, bytes]:
        header = self.read_exact(2)
        if not header:
            return (None, b"")

        first_byte, second_byte = header
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        payload_length = second_byte & 0x7F

        if payload_length == 126:
            extended = self.read_exact(2)
            if extended is None:
                return (None, b"")
            payload_length = struct.unpack("!H", extended)[0]
        elif payload_length == 127:
            extended = self.read_exact(8)
            if extended is None:
                return (None, b"")
            payload_length = struct.unpack("!Q", extended)[0]

        masking_key = self.read_exact(4) if masked else b""
        if masked and masking_key is None:
            return (None, b"")

        payload = self.read_exact(payload_length) if payload_length else b""
        if payload is None:
            return (None, b"")

        if masked:
            payload = bytes(
                byte ^ masking_key[index % 4] for index, byte in enumerate(payload)
            )
        return (opcode, payload)

    def serve_download(self, file_id: str, password: str = "") -> None:
        entry = find_file_entry(file_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        if entry.get("password_hash") and not entry_password_is_valid(entry, password):
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return

        self.serve_download_entry(entry)

    def serve_download_entry(self, entry: dict) -> None:
        target = UPLOAD_DIR / entry["stored_name"]
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = mimetypes.guess_type(entry["name"])[0] or "application/octet-stream"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{urllib.parse.quote(entry['name'])}",
        )
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def serve_asset(self, asset_name: str) -> None:
        safe_name = Path(asset_name).name
        target = ASSETS_DIR / safe_name
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, cookie: str | None = None) -> None:
        data = json_bytes(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        print(f"[{self.log_date_time_string()}] {html.escape(message)}")


def main() -> None:
    ensure_upload_dir()
    load_persisted_files()
    start_background_tasks()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Serving LanDrop on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
