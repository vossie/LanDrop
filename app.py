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
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
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
WORKSPACE_SUPER_PASSWORD = os.environ.get("WORKSPACE_SUPER_PASSWORD", "").strip()
HTTPS_ENABLED = os.environ.get("HTTPS", "").strip().lower() in {"1", "true", "yes", "on"}
HTTP_PORT = int(os.environ.get("HTTP_PORT", os.environ.get("PORT", "8000")))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))
HTTPS_CERT_FILE = Path(
    os.environ.get("HTTPS_CERT_FILE", str(BASE_DIR / "certs" / "dassiedrop-selfsigned.crt"))
).resolve()
HTTPS_KEY_FILE = Path(
    os.environ.get("HTTPS_KEY_FILE", str(BASE_DIR / "certs" / "dassiedrop-selfsigned.key"))
).resolve()
HTTPS_SELF_SIGNED_HOST = os.environ.get("HTTPS_SELF_SIGNED_HOST", "localhost").strip() or "localhost"
HTTPS_SELF_SIGNED_SANS = os.environ.get("HTTPS_SELF_SIGNED_SANS", "").strip()
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default"

state_lock = threading.Lock()
session_lock = threading.Lock()
websocket_lock = threading.Lock()
authorized_sessions: dict[str, dict] = {}
websocket_clients = set()
janitor_thread: threading.Thread | None = None
janitor_stop_event = threading.Event()
shared_state = {
    "workspaces": {},
}
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def uploads_index_path() -> Path:
    return UPLOAD_DIR / ".dassiedrop-workspaces.json"


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


def sanitize_workspace_name(name: str) -> str:
    value = " ".join(name.strip().split())
    return value[:80] or "Workspace"


def compact_workspace_name(name: str) -> str:
    return sanitize_workspace_name(name)[:16]


def workspace_slug(name: str) -> str:
    normalized = sanitize_workspace_name(name).lower()
    slug_chars = []
    last_was_dash = False
    for char in normalized:
        if char.isalnum():
            slug_chars.append(char)
            last_was_dash = False
        elif not last_was_dash:
            slug_chars.append("-")
            last_was_dash = True
    slug = "".join(slug_chars).strip("-")
    return slug or "workspace"


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


def make_workspace_id() -> str:
    return secrets.token_hex(6)


def session_cookie(session_id: str, secure: bool = False) -> str:
    secure_suffix = "; Secure" if secure else ""
    return f"session={session_id}; Path=/; HttpOnly; SameSite=Lax{secure_suffix}"


def is_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def default_https_subject_alt_names(hostname: str) -> str:
    entries = ["DNS:localhost", "IP:127.0.0.1"]
    if hostname and hostname != "localhost":
        if is_ip_literal(hostname):
            entries.append(f"IP:{hostname}")
        else:
            entries.append(f"DNS:{hostname}")
    return ",".join(entries)


def ensure_https_certificate() -> tuple[Path, Path]:
    cert_path = HTTPS_CERT_FILE
    key_path = HTTPS_KEY_FILE
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    subject_alt_names = HTTPS_SELF_SIGNED_SANS or default_https_subject_alt_names(
        HTTPS_SELF_SIGNED_HOST
    )
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-nodes",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-days",
        "365",
        "-subj",
        f"/CN={HTTPS_SELF_SIGNED_HOST}",
        "-addext",
        f"subjectAltName={subject_alt_names}",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "OpenSSL is required to generate a self-signed certificate. "
            "Install it or set HTTPS_CERT_FILE and HTTPS_KEY_FILE to existing files."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"Failed to generate a self-signed certificate with OpenSSL: {details}"
        ) from exc
    return cert_path, key_path


def build_server(host: str, port: int, use_https: bool = False) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), AppHandler)
    scheme = "http"
    server.is_https = use_https
    if use_https:
        cert_path, key_path = ensure_https_certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    return server, scheme


def parse_cookies(cookie_header: str) -> dict:
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        return True
    return hmac.compare_digest(hash_password(password), password_hash)


def get_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict | None]:
    cookies = parse_cookies(handler.headers.get("Cookie", ""))
    session_id = cookies.get("session")
    if not session_id:
        return (None, None)
    with session_lock:
        session = authorized_sessions.get(session_id)
        if session is None:
            return (None, None)
        return (session_id, session)


def is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not ACCESS_CODE:
        return True

    api_key = handler.headers.get("X-API-Key", "").strip()
    if api_key and hmac.compare_digest(api_key, ACCESS_CODE):
        return True

    session_id, _ = get_session(handler)
    return session_id is not None


def create_authorized_session(workspace_id: str | None = None) -> str:
    session_id = make_session_id()
    with session_lock:
        authorized_sessions[session_id] = {"workspace_id": workspace_id}
    return session_id


def ensure_browser_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict | None, str | None]:
    session_id, session = get_session(handler)
    if session_id is not None and session is not None:
        return (session_id, session, None)
    if ACCESS_CODE:
        return (None, None, None)
    session_id = create_authorized_session()
    with session_lock:
        session = authorized_sessions[session_id]
    return (session_id, session, session_cookie(session_id, secure=bool(getattr(handler.server, "is_https", False))))


def requested_workspace_selector(handler: BaseHTTPRequestHandler) -> str:
    parsed = urllib.parse.urlparse(handler.path)
    query_values = urllib.parse.parse_qs(parsed.query)
    query_value = query_values.get("workspace", [""])[0]
    query_name_value = query_values.get("workspace_name", [""])[0]
    header_value = handler.headers.get("X-Workspace-ID", "")
    header_name_value = handler.headers.get("X-Workspace-Name", "")
    value = (header_value or header_name_value or query_value or query_name_value).strip()
    return value


def requested_workspace_password(handler: BaseHTTPRequestHandler) -> str:
    parsed = urllib.parse.urlparse(handler.path)
    query_value = urllib.parse.parse_qs(parsed.query).get("workspace_password", [""])[0]
    header_value = handler.headers.get("X-Workspace-Password", "")
    return (header_value or query_value).strip()


def resolve_workspace_selector_locked(selector: str) -> dict | None:
    normalized = selector.strip()
    if not normalized:
        return None
    workspace = get_workspace_locked(normalized)
    if workspace is not None:
        return workspace
    return get_workspace_by_slug_locked(normalized)


def build_workspace(
    name: str,
    password_hash: str | None = None,
    workspace_id: str | None = None,
    created_at: float | None = None,
    last_used_at: float | None = None,
) -> dict:
    timestamp = now_ts() if created_at is None else created_at
    return {
        "id": workspace_id or make_workspace_id(),
        "name": sanitize_workspace_name(name),
        "password_hash": password_hash,
        "created_at": timestamp,
        "updated_at": 0.0,
        "last_used_at": timestamp if last_used_at is None else last_used_at,
        "texts": [],
        "files": [],
    }


def ensure_default_workspace_locked() -> dict:
    workspace = shared_state["workspaces"].get(DEFAULT_WORKSPACE_ID)
    if workspace is None:
        workspace = build_workspace(
            DEFAULT_WORKSPACE_NAME,
            workspace_id=DEFAULT_WORKSPACE_ID,
            created_at=0.0,
        )
        shared_state["workspaces"][DEFAULT_WORKSPACE_ID] = workspace
    return workspace


def workspace_sort_key(item: dict) -> tuple[int, str]:
    return (0 if item["id"] == DEFAULT_WORKSPACE_ID else 1, item["name"].lower())


def list_workspace_objects_locked() -> list[dict]:
    ensure_default_workspace_locked()
    return sorted(shared_state["workspaces"].values(), key=workspace_sort_key)


def get_workspace_locked(workspace_id: str) -> dict | None:
    ensure_default_workspace_locked()
    return shared_state["workspaces"].get(workspace_id)


def get_workspace_by_slug_locked(slug: str) -> dict | None:
    ensure_default_workspace_locked()
    target = slug.strip().lower()
    if not target:
        return None
    for workspace in list_workspace_objects_locked():
        if workspace_slug(workspace["name"]) == target:
            return workspace
    return None


def get_workspace(workspace_id: str) -> dict | None:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        return dict(workspace) if workspace is not None else None


def recompute_workspace_updated_at_locked(workspace: dict) -> None:
    timestamps = [item["created_at"] for item in workspace["texts"]]
    timestamps.extend(item["created_at"] for item in workspace["files"])
    workspace["updated_at"] = max(timestamps, default=workspace["created_at"])


def touch_workspace_locked(workspace: dict, persist_interval: float = 60.0) -> bool:
    previous = float(workspace.get("last_used_at") or workspace["created_at"])
    current = now_ts()
    workspace["last_used_at"] = current
    return current - previous >= persist_interval


def trim_workspace_history_locked(workspace: dict) -> None:
    overflow_files = []
    if len(workspace["texts"]) > MAX_TEXT_HISTORY:
        workspace["texts"] = workspace["texts"][:MAX_TEXT_HISTORY]

    if len(workspace["files"]) > MAX_FILE_HISTORY:
        overflow_files = workspace["files"][MAX_FILE_HISTORY:]
        workspace["files"] = workspace["files"][:MAX_FILE_HISTORY]

    for item in overflow_files:
        target = UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)

    recompute_workspace_updated_at_locked(workspace)


def prune_workspace_locked(workspace: dict) -> bool:
    cutoff = now_ts() - EXPIRY_SECONDS
    expired_files = [
        item for item in workspace["files"] if item["created_at"] < cutoff
    ]
    before_texts = len(workspace["texts"])
    before_files = len(workspace["files"])
    workspace["texts"] = [
        item for item in workspace["texts"] if item["created_at"] >= cutoff
    ]
    workspace["files"] = [
        item for item in workspace["files"] if item["created_at"] >= cutoff
    ]
    for item in expired_files:
        target = UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)
    recompute_workspace_updated_at_locked(workspace)
    return before_texts != len(workspace["texts"]) or before_files != len(workspace["files"])


def workspace_is_inactive_locked(workspace: dict) -> bool:
    if workspace["id"] == DEFAULT_WORKSPACE_ID:
        return False
    last_used_at = float(
        workspace.get("last_used_at") or workspace["updated_at"] or workspace["created_at"]
    )
    return last_used_at < (now_ts() - EXPIRY_SECONDS)


def workspace_password_is_valid(workspace: dict, password: str) -> bool:
    return verify_password(password, workspace.get("password_hash"))


def workspace_delete_password_is_valid(workspace: dict, password: str) -> bool:
    if workspace.get("password_hash") is None:
        return True
    candidate = password.strip()
    if not candidate:
        return False
    if WORKSPACE_SUPER_PASSWORD and hmac.compare_digest(candidate, WORKSPACE_SUPER_PASSWORD):
        return True
    return workspace_password_is_valid(workspace, candidate)


def serialize_workspace_summary(workspace: dict) -> dict:
    return {
        "id": workspace["id"],
        "name": workspace["name"],
        "slug": workspace_slug(workspace["name"]),
        "path": f"/w/{urllib.parse.quote(workspace_slug(workspace['name']))}",
        "password_required": bool(workspace.get("password_hash")),
        "created_at": workspace["created_at"],
        "updated_at": workspace["updated_at"],
        "text_count": len(workspace["texts"]),
        "file_count": len(workspace["files"]),
    }


def serialize_persisted_workspace(workspace: dict) -> dict:
    return {
        "id": workspace["id"],
        "name": workspace["name"],
        "password_hash": workspace.get("password_hash"),
        "created_at": workspace["created_at"],
        "updated_at": workspace["updated_at"],
        "last_used_at": workspace.get("last_used_at", workspace["created_at"]),
        "files": workspace["files"],
    }


def persist_workspaces_locked() -> None:
    ensure_upload_dir()
    ensure_default_workspace_locked()
    payload = {
        "workspaces": [
            serialize_persisted_workspace(workspace)
            for workspace in list_workspace_objects_locked()
        ]
    }
    index_path = uploads_index_path()
    temp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    temp_path.replace(index_path)


def load_persisted_workspaces() -> None:
    ensure_upload_dir()
    index_path = uploads_index_path()
    loaded_workspaces = {}

    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

        raw_workspaces = payload.get("workspaces")
        if isinstance(raw_workspaces, list):
            for item in raw_workspaces:
                if not isinstance(item, dict):
                    continue
                workspace_id = str(item.get("id") or make_workspace_id()).strip() or make_workspace_id()
                workspace = build_workspace(
                    str(item.get("name") or DEFAULT_WORKSPACE_NAME),
                    password_hash=item.get("password_hash")
                    if isinstance(item.get("password_hash"), str)
                    else None,
                    workspace_id=workspace_id,
                    created_at=float(item.get("created_at") or now_ts()),
                    last_used_at=float(
                        item.get("last_used_at")
                        or item.get("updated_at")
                        or item.get("created_at")
                        or now_ts()
                    ),
                )
                raw_files = item.get("files", [])
                if not isinstance(raw_files, list):
                    raw_files = []
                restored_files = []
                for file_item in raw_files:
                    if not isinstance(file_item, dict):
                        continue
                    stored_name = file_item.get("stored_name")
                    if not isinstance(stored_name, str):
                        continue
                    target = UPLOAD_DIR / stored_name
                    if not target.exists() or not target.is_file():
                        continue
                    restored_files.append(
                        {
                            "id": str(file_item.get("id") or make_id()),
                            "name": sanitize_filename(str(file_item.get("name") or stored_name)),
                            "stored_name": stored_name,
                            "size": int(file_item.get("size") or target.stat().st_size),
                            "hidden": bool(file_item.get("hidden", False)),
                            "password_hash": file_item.get("password_hash")
                            if isinstance(file_item.get("password_hash"), str)
                            else None,
                            "sharer_name": str(file_item.get("sharer_name") or "").strip(),
                            "sharer_ip": str(file_item.get("sharer_ip") or "").strip(),
                            "short_code": str(file_item.get("short_code") or make_short_code()).upper(),
                            "created_at": float(file_item.get("created_at") or now_ts()),
                            "expires_at": float(
                                file_item.get("expires_at") or (now_ts() + EXPIRY_SECONDS)
                            ),
                        }
                    )
                restored_files.sort(key=lambda entry: entry["created_at"], reverse=True)
                workspace["files"] = restored_files
                trim_workspace_history_locked(workspace)
                prune_workspace_locked(workspace)
                loaded_workspaces[workspace["id"]] = workspace
        else:
            raw_files = payload.get("files", [])
            if isinstance(raw_files, list):
                workspace = build_workspace(
                    DEFAULT_WORKSPACE_NAME,
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    created_at=0.0,
                )
                restored_files = []
                for file_item in raw_files:
                    if not isinstance(file_item, dict):
                        continue
                    stored_name = file_item.get("stored_name")
                    if not isinstance(stored_name, str):
                        continue
                    target = UPLOAD_DIR / stored_name
                    if not target.exists() or not target.is_file():
                        continue
                    restored_files.append(
                        {
                            "id": str(file_item.get("id") or make_id()),
                            "name": sanitize_filename(str(file_item.get("name") or stored_name)),
                            "stored_name": stored_name,
                            "size": int(file_item.get("size") or target.stat().st_size),
                            "hidden": bool(file_item.get("hidden", False)),
                            "password_hash": file_item.get("password_hash")
                            if isinstance(file_item.get("password_hash"), str)
                            else None,
                            "sharer_name": str(file_item.get("sharer_name") or "").strip(),
                            "sharer_ip": str(file_item.get("sharer_ip") or "").strip(),
                            "short_code": str(file_item.get("short_code") or make_short_code()).upper(),
                            "created_at": float(file_item.get("created_at") or now_ts()),
                            "expires_at": float(
                                file_item.get("expires_at") or (now_ts() + EXPIRY_SECONDS)
                            ),
                        }
                    )
                restored_files.sort(key=lambda entry: entry["created_at"], reverse=True)
                workspace["files"] = restored_files
                trim_workspace_history_locked(workspace)
                prune_workspace_locked(workspace)
                loaded_workspaces[workspace["id"]] = workspace

    with state_lock:
        shared_state["workspaces"] = loaded_workspaces
        ensure_default_workspace_locked()
        persist_workspaces_locked()


def load_persisted_files() -> None:
    load_persisted_workspaces()


def delete_workspace_artifacts(workspace: dict) -> None:
    for item in workspace["files"]:
        target = UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)
    clear_workspace_selection_for_deleted_workspace(workspace["id"])
    close_workspace_clients(workspace["id"])


def prune_expired_entries() -> list[str]:
    changed_workspace_ids = []
    removed_workspaces = []
    with state_lock:
        for workspace in list(shared_state["workspaces"].values()):
            pruned = prune_workspace_locked(workspace)
            inactive = workspace_is_inactive_locked(workspace)
            if inactive:
                removed_workspaces.append(shared_state["workspaces"].pop(workspace["id"]))
            elif pruned:
                changed_workspace_ids.append(workspace["id"])
        if removed_workspaces:
            ensure_default_workspace_locked()
        if changed_workspace_ids or removed_workspaces:
            persist_workspaces_locked()
    for workspace in removed_workspaces:
        delete_workspace_artifacts(workspace)
    return changed_workspace_ids


def mask_text_value(value: str) -> str:
    return "*****" if value else ""


def guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


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
        "content_type": guess_content_type(entry["name"]),
        "size": entry["size"],
        "hidden": entry.get("hidden", False),
        "password_required": bool(entry.get("password_hash")),
        "sharer_name": entry.get("sharer_name", ""),
        "sharer_ip": entry.get("sharer_ip", ""),
        "short_code": entry["short_code"],
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
    }


def serialize_workspace_payload(workspace: dict) -> dict:
    return {
        "workspace": serialize_workspace_summary(workspace),
        "updated_at": workspace["updated_at"],
        "expires_after_seconds": EXPIRY_SECONDS,
        "latest_text": ""
        if not workspace["texts"]
        else (
            ""
            if workspace["texts"][0]["hidden"] and workspace["texts"][0].get("password_hash")
            else workspace["texts"][0]["content"]
        ),
        "texts": [serialize_text_entry(item) for item in workspace["texts"]],
        "files": [serialize_file_entry(item) for item in workspace["files"]],
    }


def get_snapshot(workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        if touch_workspace_locked(workspace):
            persist_workspaces_locked()
        return serialize_workspace_payload(workspace)


def get_latest_text_entry(workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict | None:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return None
        prune_workspace_locked(workspace)
        if touch_workspace_locked(workspace):
            persist_workspaces_locked()
        if not workspace["texts"]:
            return None
        return serialize_text_entry(workspace["texts"][0])


def get_latest_file_entry(workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict | None:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return None
        prune_workspace_locked(workspace)
        if touch_workspace_locked(workspace):
            persist_workspaces_locked()
        if not workspace["files"]:
            return None
        return serialize_file_entry(workspace["files"][0])


def make_unique_short_code_locked() -> str:
    existing = set()
    for workspace in shared_state["workspaces"].values():
        existing.update(item["short_code"] for item in workspace["texts"])
        existing.update(item["short_code"] for item in workspace["files"])
    while True:
        candidate = make_short_code()
        if candidate not in existing:
            return candidate


def create_workspace(name: str, password: str = "") -> dict:
    with state_lock:
        ensure_default_workspace_locked()
        workspace_name = sanitize_workspace_name(name)
        workspace = build_workspace(
            workspace_name,
            password_hash=hash_password(password.strip()) if password.strip() else None,
        )
        shared_state["workspaces"][workspace["id"]] = workspace
        persist_workspaces_locked()
        return serialize_workspace_summary(workspace)


def list_workspaces() -> list[dict]:
    with state_lock:
        removed_workspaces = []
        changed = False
        for workspace in list(shared_state["workspaces"].values()):
            if prune_workspace_locked(workspace):
                changed = True
            if workspace_is_inactive_locked(workspace):
                removed_workspaces.append(shared_state["workspaces"].pop(workspace["id"]))
                changed = True
        if removed_workspaces:
            ensure_default_workspace_locked()
        if changed:
            persist_workspaces_locked()
        summaries = [
            serialize_workspace_summary(workspace) for workspace in list_workspace_objects_locked()
        ]
    for workspace in removed_workspaces:
        delete_workspace_artifacts(workspace)
    return summaries


def set_session_workspace(session_id: str, workspace_id: str | None) -> None:
    with session_lock:
        session = authorized_sessions.get(session_id)
        if session is not None:
            session["workspace_id"] = workspace_id


def clear_workspace_selection_for_deleted_workspace(workspace_id: str) -> None:
    with session_lock:
        for session in authorized_sessions.values():
            if session.get("workspace_id") == workspace_id:
                session["workspace_id"] = None


def enter_workspace(session_id: str, workspace_id: str, password: str = "") -> tuple[bool, str]:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return (False, "Workspace not found")
        if workspace.get("password_hash") and not workspace_password_is_valid(
            workspace, password.strip()
        ):
            return (False, "Wrong workspace password")
        touch_workspace_locked(workspace, persist_interval=0.0)
        persist_workspaces_locked()
    set_session_workspace(session_id, workspace_id)
    return (True, "")


def delete_workspace(workspace_id: str, password: str = "") -> tuple[bool, str]:
    removed_workspace = None
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return (False, "Workspace not found")
        if not workspace_delete_password_is_valid(workspace, password):
            return (False, "Wrong workspace password")
        removed_workspace = shared_state["workspaces"].pop(workspace_id)
        ensure_default_workspace_locked()
        persist_workspaces_locked()

    delete_workspace_artifacts(removed_workspace)
    return (True, "")


def add_text_entry(
    value: str,
    hidden: bool = False,
    password: str = "",
    sharer_name: str = "",
    sharer_ip: str = "",
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> None:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        created_at = now_ts()
        workspace["texts"].insert(
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
        trim_workspace_history_locked(workspace)
        touch_workspace_locked(workspace, persist_interval=0.0)
        persist_workspaces_locked()


def delete_text_entry(entry_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> bool:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return False
        prune_workspace_locked(workspace)
        original_len = len(workspace["texts"])
        workspace["texts"] = [item for item in workspace["texts"] if item["id"] != entry_id]
        recompute_workspace_updated_at_locked(workspace)
        changed = len(workspace["texts"]) != original_len
        if changed:
            touch_workspace_locked(workspace, persist_interval=0.0)
            persist_workspaces_locked()
        return changed


def add_file(
    original_name: str,
    stored_name: str,
    size: int,
    hidden: bool = False,
    password: str = "",
    sharer_name: str = "",
    sharer_ip: str = "",
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> None:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        created_at = now_ts()
        workspace["files"].insert(
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
        trim_workspace_history_locked(workspace)
        touch_workspace_locked(workspace, persist_interval=0.0)
        persist_workspaces_locked()


def delete_file_entry(file_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> bool:
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return False
        prune_workspace_locked(workspace)
        removed = None
        kept = []
        for item in workspace["files"]:
            if item["id"] == file_id and removed is None:
                removed = item
            else:
                kept.append(item)
        workspace["files"] = kept
        recompute_workspace_updated_at_locked(workspace)
        if removed is not None:
            touch_workspace_locked(workspace, persist_interval=0.0)
            persist_workspaces_locked()

    if removed is None:
        return False

    target = UPLOAD_DIR / removed["stored_name"]
    if target.exists():
        target.unlink(missing_ok=True)
    return True


def find_file_entry(file_id: str, workspace_id: str | None = None) -> dict | None:
    with state_lock:
        workspaces = (
            [get_workspace_locked(workspace_id)] if workspace_id is not None else list_workspace_objects_locked()
        )
        for workspace in workspaces:
            if workspace is None:
                continue
            prune_workspace_locked(workspace)
            for item in workspace["files"]:
                if item["id"] == file_id:
                    payload = dict(item)
                    payload["workspace_id"] = workspace["id"]
                    return payload
    return None


def find_text_entry(text_id: str, workspace_id: str | None = None) -> dict | None:
    with state_lock:
        workspaces = (
            [get_workspace_locked(workspace_id)] if workspace_id is not None else list_workspace_objects_locked()
        )
        for workspace in workspaces:
            if workspace is None:
                continue
            prune_workspace_locked(workspace)
            for item in workspace["texts"]:
                if item["id"] == text_id:
                    payload = dict(item)
                    payload["workspace_id"] = workspace["id"]
                    return payload
    return None


def find_entry_by_short_code(short_code: str) -> tuple[str, dict] | None:
    normalized = short_code.strip().upper()
    with state_lock:
        for workspace in list_workspace_objects_locked():
            prune_workspace_locked(workspace)
            for item in workspace["texts"]:
                if item["short_code"] == normalized:
                    payload = dict(item)
                    payload["workspace_id"] = workspace["id"]
                    return ("text", payload)
            for item in workspace["files"]:
                if item["short_code"] == normalized:
                    payload = dict(item)
                    payload["workspace_id"] = workspace["id"]
                    return ("file", payload)
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
    def __init__(self, connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id
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


def close_workspace_clients(workspace_id: str) -> None:
    with websocket_lock:
        clients = [client for client in websocket_clients if client.workspace_id == workspace_id]
    for client in clients:
        unregister_websocket_client(client)


def broadcast_snapshot(workspace_id: str, snapshot: dict | None = None) -> None:
    payload = snapshot or get_snapshot(workspace_id)
    with websocket_lock:
        clients = [client for client in websocket_clients if client.workspace_id == workspace_id]

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
            for workspace_id in prune_expired_entries():
                broadcast_snapshot(workspace_id)

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
    proto = forwarded_proto or (
        "https" if getattr(handler.server, "server_port", 0) == 443 else "http"
    )
    host = handler.headers.get("Host", "").strip()
    if host:
        return f"{proto}://{host}".rstrip("/")

    server_host, server_port = handler.server.server_address[:2]
    return f"http://{server_host}:{server_port}"


def share_payload(entry_type: str, entry: dict, base_url: str) -> dict:
    path = f"/s/{urllib.parse.quote(entry['short_code'])}"
    workspace_id = entry.get("workspace_id", DEFAULT_WORKSPACE_ID)
    workspace_name = DEFAULT_WORKSPACE_NAME
    workspace_slug_value = workspace_slug(DEFAULT_WORKSPACE_NAME)
    with state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is not None:
            workspace_name = workspace["name"]
            workspace_slug_value = workspace_slug(workspace["name"])
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
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_slug": workspace_slug_value,
        "workspace_path": f"/w/{urllib.parse.quote(workspace_slug_value)}",
        "workspace_url": f"{base_url.rstrip('/')}/w/{urllib.parse.quote(workspace_slug_value)}",
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
    server_version = "DassieDrop/1.2"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.serve_asset("DassieDrop-dassie-icon.png")
            return

        if parsed.path.startswith("/assets/"):
            asset_name = parsed.path.removeprefix("/assets/")
            self.serve_asset(asset_name)
            return

        if parsed.path == "/":
            self.handle_root()
            return

        if parsed.path == "/workspaces":
            self.handle_workspaces_page()
            return

        if parsed.path.startswith("/w/"):
            workspace_slug_value = urllib.parse.unquote(parsed.path.removeprefix("/w/"))
            self.handle_workspace_shortcut(workspace_slug_value)
            return

        if parsed.path == "/api/workspaces":
            if ACCESS_CODE and not is_authorized(self):
                self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
                return
            self.send_json(self.workspace_list_payload())
            return

        if ACCESS_CODE and not is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path == "/ws":
            self.handle_websocket()
            return

        if parsed.path == "/api/state":
            workspace_id = self.require_workspace_context()
            if workspace_id is None:
                return
            self.send_json(get_snapshot(workspace_id))
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

        if parsed.path.startswith("/preview/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/preview/"))
            password = urllib.parse.parse_qs(parsed.query).get("password", [""])[0]
            self.serve_preview(file_id, password)
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

        if parsed.path == "/api/workspaces":
            self.handle_workspace_create()
            return

        if parsed.path.startswith("/api/workspaces/") and parsed.path.endswith("/enter"):
            workspace_id = urllib.parse.unquote(
                parsed.path.removeprefix("/api/workspaces/").removesuffix("/enter")
            )
            self.handle_workspace_enter(workspace_id)
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

        if parsed.path.startswith("/api/workspaces/"):
            workspace_id = urllib.parse.unquote(parsed.path.removeprefix("/api/workspaces/"))
            self.handle_workspace_delete(workspace_id)
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

    def current_session_workspace_id(self) -> str | None:
        _, session = get_session(self)
        if session is None:
            return None
        workspace_id = session.get("workspace_id")
        if not workspace_id:
            return None
        with state_lock:
            if get_workspace_locked(workspace_id) is None:
                return None
        return workspace_id

    def workspace_list_payload(self) -> dict:
        return {
            "workspaces": list_workspaces(),
            "current_workspace_id": self.current_session_workspace_id(),
        }

    def handle_root(self) -> None:
        if ACCESS_CODE and not is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        session_id, session, cookie = ensure_browser_session(self)
        if session is None or session_id is None:
            self.send_html(render_template("login.html"))
            return

        workspace_id = session.get("workspace_id")
        with state_lock:
            workspace = get_workspace_locked(workspace_id) if workspace_id else None
        if workspace is None:
            self.redirect("/workspaces", cookie=cookie)
            return

        self.send_html(
            render_template(
                "index.html",
                {
                    "__SHARE_BASE_URL__": json.dumps(get_share_base_url()),
                    "__APP_VERSION__": html.escape(get_app_version()),
                    "__WORKSPACE_NAME__": html.escape(compact_workspace_name(workspace["name"])),
                },
            ),
            cookie=cookie,
        )

    def handle_workspaces_page(self) -> None:
        if ACCESS_CODE and not is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        _, _, cookie = ensure_browser_session(self)
        self.send_html(
            render_template(
                "workspaces.html",
                {"__APP_VERSION__": html.escape(get_app_version())},
            ),
            cookie=cookie,
        )

    def handle_workspace_shortcut(self, workspace_slug_value: str) -> None:
        if ACCESS_CODE and not is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        session_id, session, cookie = ensure_browser_session(self)
        if session is None or session_id is None:
            self.send_html(render_template("login.html"))
            return

        with state_lock:
            workspace = get_workspace_by_slug_locked(workspace_slug_value)
            if workspace is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Workspace not found")
                return
            if workspace.get("password_hash"):
                current_workspace_id = session.get("workspace_id")
                password = requested_workspace_password(self)
                if current_workspace_id != workspace["id"] and not workspace_password_is_valid(
                    workspace, password
                ):
                    self.redirect(
                        f"/workspaces?workspace={urllib.parse.quote(workspace_slug_value)}",
                        cookie=cookie,
                    )
                    return
            touch_workspace_locked(workspace, persist_interval=0.0)
            persist_workspaces_locked()

        set_session_workspace(session_id, workspace["id"])
        self.redirect("/", cookie=cookie)

    def handle_login(self) -> None:
        if not ACCESS_CODE:
            session_id = create_authorized_session()
            self.send_json(
                {"ok": True},
                cookie=session_cookie(session_id, secure=bool(getattr(self.server, "is_https", False))),
            )
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
        self.send_json(
            {"ok": True},
            cookie=session_cookie(session_id, secure=bool(getattr(self.server, "is_https", False))),
        )

    def parse_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None
        return payload

    def handle_workspace_create(self) -> None:
        payload = self.parse_json_body()
        if payload is None:
            return

        name = payload.get("name", "")
        if not isinstance(name, str) or not name.strip():
            self.send_error(HTTPStatus.BAD_REQUEST, "Workspace name is required")
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return

        workspace = create_workspace(name, password=password.strip())
        self.send_json(
            {
                "workspace": workspace,
                "workspaces": list_workspaces(),
                "current_workspace_id": self.current_session_workspace_id(),
            }
        )

    def handle_workspace_enter(self, workspace_id: str) -> None:
        session_id, session = get_session(self)
        if session_id is None or session is None:
            self.send_error(HTTPStatus.UNAUTHORIZED, "Session required")
            return
        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        ok, message = enter_workspace(session_id, workspace_id, password=password)
        if not ok:
            status = HTTPStatus.NOT_FOUND if message == "Workspace not found" else HTTPStatus.FORBIDDEN
            self.send_error(status, message)
            return
        self.send_json(
            {
                "ok": True,
                "workspace_id": workspace_id,
            }
        )

    def handle_workspace_delete(self, workspace_id: str) -> None:
        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        ok, message = delete_workspace(workspace_id, password=password)
        if not ok:
            status = HTTPStatus.NOT_FOUND if message == "Workspace not found" else HTTPStatus.FORBIDDEN
            self.send_error(status, message)
            return
        self.send_json(self.workspace_list_payload())

    def require_workspace_context(self) -> str | None:
        explicit_workspace_selector = requested_workspace_selector(self)
        if explicit_workspace_selector:
            with state_lock:
                workspace = resolve_workspace_selector_locked(explicit_workspace_selector)
                if workspace is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Workspace not found")
                    return None
                if workspace.get("password_hash") and not workspace_password_is_valid(
                    workspace, requested_workspace_password(self)
                ):
                    self.send_error(HTTPStatus.FORBIDDEN, "Wrong workspace password")
                    return None
                return workspace["id"]

        session_id, session = get_session(self)
        if session_id is not None and session is not None:
            workspace_id = session.get("workspace_id")
            if not workspace_id:
                self.send_error(HTTPStatus.CONFLICT, "Workspace not selected")
                return None
            with state_lock:
                workspace = get_workspace_locked(workspace_id)
            if workspace is None:
                set_session_workspace(session_id, None)
                self.send_error(HTTPStatus.CONFLICT, "Workspace not selected")
                return None
            return workspace_id

        if ACCESS_CODE:
            if self.headers.get("X-API-Key", "").strip() == ACCESS_CODE:
                return DEFAULT_WORKSPACE_ID
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return None

        return DEFAULT_WORKSPACE_ID

    def handle_text_update(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = self.parse_text_request()
        if entry is None:
            return

        add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
            workspace_id=workspace_id,
        )
        snapshot = get_snapshot(workspace_id)
        self.send_json(snapshot)
        broadcast_snapshot(workspace_id, snapshot)

    def handle_text_share(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = self.parse_text_request()
        if entry is None:
            return

        add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
            workspace_id=workspace_id,
        )
        created = find_text_entry(get_snapshot(workspace_id)["texts"][0]["id"], workspace_id=workspace_id)
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create text entry")
            return
        snapshot = get_snapshot(workspace_id)
        self.send_json(share_payload("text", created, base_url_from_request(self)))
        broadcast_snapshot(workspace_id, snapshot)

    def parse_text_request(self) -> dict | None:
        payload = self.parse_json_body()
        if payload is None:
            return None

        text = payload.get("text", "")
        if not isinstance(text, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Text must be a string")
            return None
        normalized_text = text.strip()
        if not normalized_text:
            self.send_error(HTTPStatus.BAD_REQUEST, "Text cannot be empty")
            return None
        hidden = payload.get("hidden", False)
        if not isinstance(hidden, bool):
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden must be a boolean")
            return None
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return None
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

        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        if not entry_password_is_valid(entry, password):
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return

        self.send_json({"content": entry["content"]})

    def handle_latest_text(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = get_latest_text_entry(workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No text entries found")
            return
        self.send_json(entry)

    def handle_latest_file(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = get_latest_file_entry(workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.send_json(entry)

    def handle_latest_file_content(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        latest_entry = get_latest_file_entry(workspace_id)
        if latest_entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        entry = find_file_entry(latest_entry["id"], workspace_id=workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.serve_file_entry(entry, as_attachment=True)

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
        self.serve_file_entry(payload, as_attachment=True)

    def handle_text_delete(self, entry_id: str) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        if not delete_text_entry(entry_id, workspace_id=workspace_id):
            self.send_error(HTTPStatus.NOT_FOUND, "Text entry not found")
            return
        snapshot = get_snapshot(workspace_id)
        self.send_json(snapshot)
        broadcast_snapshot(workspace_id, snapshot)

    def handle_file_delete(self, file_id: str) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        if not delete_file_entry(file_id, workspace_id=workspace_id):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        snapshot = get_snapshot(workspace_id)
        self.send_json(snapshot)
        broadcast_snapshot(workspace_id, snapshot)

    def handle_file_upload(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        self.store_file_upload(parsed, workspace_id)
        snapshot = get_snapshot(workspace_id)
        self.send_json(snapshot)
        broadcast_snapshot(workspace_id, snapshot)

    def handle_file_share(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        created = self.store_file_upload(parsed, workspace_id)
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create file entry")
            return
        snapshot = get_snapshot(workspace_id)
        self.send_json(share_payload("file", created, base_url_from_request(self)))
        broadcast_snapshot(workspace_id, snapshot)

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

    def store_file_upload(self, parsed: dict, workspace_id: str) -> dict | None:
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
            workspace_id=workspace_id,
        )
        return find_file_entry(get_snapshot(workspace_id)["files"][0]["id"], workspace_id=workspace_id)

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
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return

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

        client = WebSocketClient(self.connection, workspace_id)
        register_websocket_client(client)
        client.send_json(get_snapshot(workspace_id))

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

        self.serve_file_entry(entry, as_attachment=True)

    def serve_preview(self, file_id: str, password: str = "") -> None:
        entry = find_file_entry(file_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        if entry.get("password_hash") and not entry_password_is_valid(entry, password):
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return

        self.serve_file_entry(entry, as_attachment=False)

    def serve_file_entry(self, entry: dict, as_attachment: bool) -> None:
        target = UPLOAD_DIR / entry["stored_name"]
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = guess_content_type(entry["name"])

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        disposition = "attachment" if as_attachment else "inline"
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename*=UTF-8''{urllib.parse.quote(entry['name'])}",
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

        content_type = guess_content_type(target.name)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def send_html(self, body: str, cookie: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
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

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        print(f"[{self.log_date_time_string()}] {html.escape(message)}")


def main() -> None:
    ensure_upload_dir()
    load_persisted_workspaces()
    start_background_tasks()
    host = os.environ.get("HOST", "0.0.0.0")
    if HTTPS_ENABLED and HTTP_PORT == HTTPS_PORT:
        raise RuntimeError("HTTP_PORT and HTTPS_PORT must be different when HTTPS is enabled.")

    http_server, http_scheme = build_server(host, HTTP_PORT, use_https=False)
    print(f"Serving DassieDrop on {http_scheme}://{host}:{HTTP_PORT}")

    if not HTTPS_ENABLED:
        http_server.serve_forever()
        return

    https_server, https_scheme = build_server(host, HTTPS_PORT, use_https=True)
    print(f"Serving DassieDrop on {https_scheme}://{host}:{HTTPS_PORT}")

    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    https_server.serve_forever()


if __name__ == "__main__":
    main()
