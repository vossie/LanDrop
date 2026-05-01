#!/usr/bin/env python3
import hashlib
import hmac
import html
import json
import os
import secrets
import shutil
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))).resolve()
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB
EXPIRY_SECONDS = 24 * 60 * 60
MAX_TEXT_HISTORY = 200
MAX_FILE_HISTORY = 100
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL", "").strip()

state_lock = threading.Lock()
session_lock = threading.Lock()
authorized_sessions = set()
shared_state = {
    "updated_at": 0.0,
    "texts": [],
    "files": [],
}


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def uploads_index_path() -> Path:
    return UPLOAD_DIR / ".landrop-files.json"


def now_ts() -> float:
    return time.time()


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
    payload = {
        "id": entry["id"],
        "hidden": entry["hidden"],
        "password_required": bool(entry.get("password_hash")),
        "sharer_name": entry.get("sharer_name", ""),
        "sharer_ip": entry.get("sharer_ip", ""),
        "short_code": entry["short_code"],
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
        "masked_content": mask_text_value(entry["content"]),
    }
    payload["content"] = (
        None if entry["hidden"] and entry.get("password_hash") else entry["content"]
    )
    return payload


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


def get_share_base_url() -> str:
    return SHARE_BASE_URL.rstrip("/")


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LanDrop</title>
  <link rel="icon" type="image/png" href="/assets/logo-landrop-v1.png">
  <style>
    :root {
      --bg: #ffffff;
      --panel: #ffffff;
      --ink: #0f2a7a;
      --muted: #45619d;
      --line: #bed6ff;
      --accent: #12c8f4;
      --accent-strong: #1497ff;
      --danger: #ff9f1a;
      --shadow: rgba(15, 42, 122, 0.14);
      --ring: #0f2a7a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, rgba(18, 200, 244, 0.05), transparent 22rem),
        radial-gradient(circle at bottom right, rgba(20, 151, 255, 0.04), transparent 28rem),
        linear-gradient(180deg, #ffffff 0%, var(--bg) 100%);
      color: var(--ink);
    }
    .shell {
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px;
    }
    .hero {
      padding: 24px 0 12px;
      max-width: 920px;
      margin: 0 auto;
    }
    h1 {
      margin: 0;
      font-size: clamp(1.35rem, 3vw, 2.15rem);
      line-height: 1;
      letter-spacing: -0.04em;
    }
    .subhead {
      max-width: 48rem;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.5;
      margin-top: 12px;
    }
    .hero-input-row {
      margin-top: 16px;
      max-width: 920px;
    }
    .grid {
      display: block;
    }
    .tabs {
      display: flex;
      gap: 10px;
      margin: 0 0 16px;
      flex-wrap: wrap;
      max-width: 920px;
      margin-left: auto;
      margin-right: auto;
    }
    .tab-btn {
      min-width: 120px;
      gap: 8px;
      box-shadow: none;
      background: #edf5ff;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    .tab-btn.has-update::after {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #ff9f1a;
      box-shadow: 0 0 0 3px rgba(255, 159, 26, 0.18);
      flex: 0 0 auto;
    }
    .tab-btn.active.has-update::after {
      background: #ff9f1a;
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.3);
    }
    .tab-btn:hover {
      background: #e2efff;
    }
    .tab-btn.active {
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: #fff;
      border-color: transparent;
      box-shadow: 0 10px 24px rgba(20, 151, 255, 0.22);
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 12px 30px var(--shadow);
    }
    .panel.tab-panel.hidden {
      display: none;
    }
    .tab-panel {
      max-width: 920px;
      margin: 0 auto 20px;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 1.2rem;
    }
    .meta {
      color: var(--muted);
      font-size: 0.92rem;
    }
    .panel-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .panel-title h2 {
      margin: 0;
    }
    textarea {
      width: 100%;
      min-height: 220px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fdfefe;
      padding: 14px;
      font: inherit;
      color: var(--ink);
    }
    textarea:focus, input:focus {
      outline: 2px solid rgba(18, 200, 244, 0.35);
      outline-offset: 2px;
      border-color: var(--accent-strong);
    }
    button, .file-label, .file-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font: inherit;
      cursor: pointer;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: #fff;
      text-decoration: none;
      transition: transform 120ms ease, background 120ms ease;
      box-shadow: 0 10px 24px rgba(20, 151, 255, 0.22);
    }
    button:hover, .file-label:hover, .file-link:hover {
      background: linear-gradient(135deg, #08d0f8 0%, #058cff 100%);
      transform: translateY(-1px);
    }
    .danger {
      background: #fff;
      color: var(--danger);
      border: 1px solid rgba(255, 159, 26, 0.45);
      box-shadow: none;
    }
    .danger:hover {
      background: #fff7ec;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .row.stack {
      flex-direction: column;
      align-items: flex-start;
      gap: 12px;
    }
    .checkbox-row {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .checkbox-row input {
      width: 18px;
      height: 18px;
      accent-color: var(--accent);
    }
    .hidden-options {
      display: none;
      width: 100%;
    }
    .hidden-options.visible {
      display: block;
    }
    .inline-input {
      width: min(100%, 320px);
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
      padding: 12px 14px;
      font: inherit;
      color: var(--ink);
    }
    input[type="file"] {
      display: none;
    }
    .status {
      min-height: 1.2rem;
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.95rem;
      opacity: 1;
      transition: opacity 260ms ease;
    }
    .status.fading {
      opacity: 0;
    }
    .history-list {
      margin: 14px 0 0;
      padding: 0;
      list-style: none;
    }
    .history-item {
      border-top: 1px solid var(--line);
      padding: 14px 0;
    }
    .history-item:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .history-head {
      display: block;
    }
    .history-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-left: auto;
      padding-left: 20px;
    }
    .history-actions button, .history-actions a {
      padding: 8px 12px;
      font-size: 0.92rem;
    }
    .share-link {
      color: var(--accent-strong);
      text-decoration: none;
      font-size: 0.85rem;
      align-self: center;
      padding: 4px 0;
    }
    .share-link:hover {
      text-decoration: underline;
    }
    .history-item.copyable {
      cursor: pointer;
    }
    .history-item.copyable:hover .text-card {
      transform: translateY(-1px);
      border-color: #2f2a28;
      background: #fffdfa;
    }
    .delete-btn {
      width: 46px;
      height: 46px;
      min-width: 46px;
      padding: 0;
      border-radius: 14px;
      font-size: 1.1rem;
    }
    .history-body {
      margin-top: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
    }
    .text-row {
      display: flex;
      align-items: flex-end;
      gap: 18px;
    }
    .text-card-wrap {
      flex: 1 1 auto;
      min-width: 0;
    }
    .text-card-label {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0 0 10px 12px;
      color: #3d3532;
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .copied-pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      background: rgba(18, 200, 244, 0.14);
      color: #1497ff;
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0;
    }
    .entry-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      margin-bottom: 12px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: linear-gradient(180deg, #fbfdff 0%, #f5faff 100%);
    }
    .entry-table th,
    .entry-table td {
      padding: 10px 14px;
      border-bottom: 1px solid rgba(190, 214, 255, 0.7);
      text-align: left;
      vertical-align: top;
      font-size: 0.9rem;
    }
    .entry-table tr:last-child th,
    .entry-table tr:last-child td {
      border-bottom: 0;
    }
    .entry-table th {
      width: 118px;
      color: var(--muted);
      font-weight: 700;
    }
    .entry-table td {
      color: var(--ink);
    }
    .entry-table a {
      color: var(--accent-strong);
      text-decoration: none;
      word-break: break-all;
    }
    .entry-table a:hover {
      text-decoration: underline;
    }
    .text-card {
      min-height: 0;
      max-height: 22rem;
      overflow: auto;
      margin-top: 0;
      border: 3px solid #3a3330;
      border-radius: 28px;
      background: #fffdfa;
      padding: 24px 28px;
      color: #4b433f;
      box-shadow: none;
      transition: transform 120ms ease, background 120ms ease, border-color 120ms ease;
      line-height: 1.65;
    }
    .text-card.flash-copy {
      animation: copiedFlash 1.25s ease;
    }
    .text-card.masked {
      letter-spacing: 0.04em;
    }
    @keyframes copiedFlash {
      0% {
        background: #fff3cf;
        border-color: #ffb648;
        box-shadow: 0 0 0 0 rgba(255, 182, 72, 0.35);
      }
      35% {
        background: #fff9e8;
        border-color: #ffb648;
        box-shadow: 0 0 0 10px rgba(255, 182, 72, 0.08);
      }
      100% {
        background: #fffdfa;
        border-color: #3a3330;
        box-shadow: 0 0 0 0 rgba(255, 182, 72, 0);
      }
    }
    .text-card-actions {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      padding-bottom: 12px;
    }
    .text-delete-label {
      color: var(--danger);
      font-size: 0.8rem;
      font-weight: 700;
    }
    .file-name {
      font-weight: 600;
      word-break: break-word;
    }
    .file-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: linear-gradient(180deg, #fbfdff 0%, #f7fbff 100%);
      padding: 16px;
    }
    .file-card-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }
    .file-card-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-left: auto;
    }
    .dropzone {
      margin-top: 12px;
      border: 2px dashed var(--line);
      border-radius: 14px;
      padding: 18px;
      text-align: center;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.85);
      transition: border-color 120ms ease, background 120ms ease, color 120ms ease;
    }
    .dropzone.active {
      border-color: var(--accent);
      background: rgba(18, 200, 244, 0.12);
      color: var(--ink);
    }
    .muted {
      color: var(--muted);
      font-size: 0.9rem;
    }
    @media (max-width: 720px) {
      .shell { padding: 18px; }
      .text-row {
        flex-direction: column;
        align-items: stretch;
        gap: 12px;
      }
      .text-card-actions {
        flex-direction: row;
        justify-content: flex-end;
        align-items: center;
        padding-bottom: 0;
      }
      .file-card-top {
        flex-direction: column;
      }
      .file-card-actions {
        margin-left: 0;
      }
      .history-actions {
        padding-left: 0;
      }
      .entry-table th,
      .entry-table td {
        display: block;
        width: 100%;
      }
      .entry-table th {
        padding-bottom: 4px;
        border-bottom: 0;
      }
      .entry-table td {
        padding-top: 0;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <h1>LAN Text And File Sharing.<br>Fast, local, simple.</h1>
      <p class="subhead">
        Share clipboard text and files between devices on the same network with direct LAN links,
        optional passwords, and automatic cleanup after 24 hours.
      </p>
      <div class="hero-input-row">
        <input id="sharerName" class="inline-input" type="text" maxlength="60" placeholder="Your name or device name">
      </div>
    </section>

    <div class="tabs" aria-label="Sections">
      <button id="textTabBtn" class="tab-btn active" type="button">Text</button>
      <button id="fileTabBtn" class="tab-btn" type="button">Files</button>
    </div>

    <section class="grid">
      <article id="textPanel" class="panel tab-panel">
        <div class="panel-title">
          <h2>Shared Text</h2>
          <div class="meta" id="textMeta">Waiting for updates…</div>
        </div>
        <textarea id="sharedText" placeholder="Paste text here"></textarea>
        <div class="row stack">
          <label class="checkbox-row" for="hiddenText">
            <input id="hiddenText" type="checkbox">
            <span>Hidden</span>
          </label>
          <div class="hidden-options" id="textHiddenOptions">
            <input id="textPassword" class="inline-input" type="password" placeholder="Optional password to reveal">
          </div>
          <button id="saveTextBtn">Just Add</button>
        </div>
        <div class="status" id="textStatus"></div>
        <ul class="history-list" id="textHistory"></ul>
      </article>

      <article id="filePanel" class="panel tab-panel">
        <div class="panel-title">
          <h2>Shared Files</h2>
          <div class="meta">Uploaded files stay available for 24 hours unless deleted earlier.</div>
        </div>
        <div class="row">
          <label class="file-label" for="fileInput">Choose File</label>
          <input id="fileInput" type="file">
          <button id="uploadBtn" type="button">Upload File</button>
        </div>
        <div class="row stack">
          <label class="checkbox-row" for="hiddenFile">
            <input id="hiddenFile" type="checkbox">
            <span>Hidden file</span>
          </label>
          <div class="hidden-options" id="fileHiddenOptions">
            <input id="filePassword" class="inline-input" type="password" placeholder="Password required for hidden files">
          </div>
        </div>
        <div class="dropzone" id="dropZone">Drag and drop a file here</div>
        <div class="status" id="fileStatus"></div>
        <ul class="history-list" id="fileList"></ul>
      </article>
    </section>
  </main>

  <script>
    const configuredShareBaseUrl = __SHARE_BASE_URL__;
    const sharedText = document.getElementById("sharedText");
    const sharerName = document.getElementById("sharerName");
    const textPanel = document.getElementById("textPanel");
    const filePanel = document.getElementById("filePanel");
    const textTabBtn = document.getElementById("textTabBtn");
    const fileTabBtn = document.getElementById("fileTabBtn");
    const hiddenText = document.getElementById("hiddenText");
    const textHiddenOptions = document.getElementById("textHiddenOptions");
    const textPassword = document.getElementById("textPassword");
    const saveTextBtn = document.getElementById("saveTextBtn");
    const fileInput = document.getElementById("fileInput");
    const hiddenFile = document.getElementById("hiddenFile");
    const fileHiddenOptions = document.getElementById("fileHiddenOptions");
    const filePassword = document.getElementById("filePassword");
    const uploadBtn = document.getElementById("uploadBtn");
    const textMeta = document.getElementById("textMeta");
    const textStatus = document.getElementById("textStatus");
    const fileStatus = document.getElementById("fileStatus");
    const fileList = document.getElementById("fileList");
    const textHistory = document.getElementById("textHistory");
    const dropZone = document.getElementById("dropZone");

    let pendingTextPush = false;
    let activeTab = "text";
    const revealedTextIds = new Set();
    const revealedTextContent = new Map();
    let snapshotInitialized = false;
    let latestTextId = null;
    let latestFileId = null;
    let unreadText = false;
    let unreadFiles = false;
    let suppressedTextId = null;
    let suppressedFileId = null;
    let textStatusTimer = null;
    let copiedTextId = null;
    let copiedTextTimer = null;
    let lastRenderedTexts = [];

    function updateTabIndicators() {
      textTabBtn.classList.toggle("has-update", unreadText);
      fileTabBtn.classList.toggle("has-update", unreadFiles);
    }

    function syncTabs() {
      const showingText = activeTab === "text";
      textPanel.classList.toggle("hidden", !showingText);
      filePanel.classList.toggle("hidden", showingText);
      textTabBtn.classList.toggle("active", showingText);
      fileTabBtn.classList.toggle("active", !showingText);
      updateTabIndicators();
    }

    function setActiveTab(tabName) {
      activeTab = tabName;
      if (tabName === "text") {
        unreadText = false;
      } else {
        unreadFiles = false;
      }
      syncTabs();
    }

    function clearActiveTabIndicator() {
      if (activeTab === "text" && unreadText) {
        unreadText = false;
        updateTabIndicators();
      } else if (activeTab === "file" && unreadFiles) {
        unreadFiles = false;
        updateTabIndicators();
      }
    }

    function setTextStatus(message, fade = false) {
      if (textStatusTimer) {
        window.clearTimeout(textStatusTimer);
        textStatusTimer = null;
      }
      textStatus.classList.remove("fading");
      textStatus.textContent = message;

      if (!fade || !message) {
        return;
      }

      textStatusTimer = window.setTimeout(() => {
        textStatus.classList.add("fading");
        textStatusTimer = window.setTimeout(() => {
          textStatus.textContent = "";
          textStatus.classList.remove("fading");
          textStatusTimer = null;
        }, 260);
      }, 1100);
    }

    function formatDate(ts) {
      if (!ts) return "No content yet";
      return new Date(ts * 1000).toLocaleString();
    }

    function formatSize(bytes) {
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
      return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    }

    function lanSharePath(shortCode) {
      return `/s/${encodeURIComponent(shortCode)}`;
    }

    function lanShareUrl(shortCode) {
      const baseUrl = configuredShareBaseUrl || window.location.origin;
      return `${baseUrl}${lanSharePath(shortCode)}`;
    }

    function withPassword(path, password) {
      return `${path}?password=${encodeURIComponent(password)}`;
    }

    function updateHiddenOptions() {
      textHiddenOptions.classList.toggle("visible", hiddenText.checked);
      fileHiddenOptions.classList.toggle("visible", hiddenFile.checked);
      if (!hiddenText.checked) {
        textPassword.value = "";
      }
      if (!hiddenFile.checked) {
        filePassword.value = "";
      }
    }

    function isTextFormActive() {
      const active = document.activeElement;
      return (
        active === sharedText ||
        active === textPassword ||
        active === sharerName ||
        active === hiddenText ||
        active === saveTextBtn
      );
    }

    function fallbackCopyText(content) {
      const temp = document.createElement("textarea");
      temp.value = content;
      temp.setAttribute("readonly", "");
      temp.style.position = "fixed";
      temp.style.opacity = "0";
      temp.style.pointerEvents = "none";
      document.body.appendChild(temp);
      temp.focus();
      temp.select();

      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch (error) {
        copied = false;
      }

      document.body.removeChild(temp);
      return copied;
    }

    function showCopiedState(entryId) {
      copiedTextId = entryId;
      if (copiedTextTimer) {
        window.clearTimeout(copiedTextTimer);
      }
      renderTextHistory(lastRenderedTexts);
      copiedTextTimer = window.setTimeout(() => {
        copiedTextId = null;
        copiedTextTimer = null;
        renderTextHistory(lastRenderedTexts);
      }, 1400);
    }

    async function copyText(content) {
      clearActiveTabIndicator();
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(content);
        } else if (!fallbackCopyText(content)) {
          throw new Error("Fallback copy failed");
        }
        return true;
      } catch (error) {
        if (fallbackCopyText(content)) {
          return true;
        } else {
          setTextStatus("Clipboard copy failed.");
          return false;
        }
      }
    }

    async function deleteText(id) {
      try {
        const response = await fetch(`/api/text/${encodeURIComponent(id)}`, {
          method: "DELETE"
        });
        if (!response.ok) {
          throw new Error(`Delete failed: ${response.status}`);
        }
        renderSnapshot(await response.json());
        textStatus.textContent = "Text entry deleted.";
      } catch (error) {
        textStatus.textContent = "Text delete failed.";
      }
    }

    function maskText(content) {
      return content.replace(/[^\\s]/g, "*");
    }

    async function revealProtectedText(entry) {
      if (!entry.password_required) {
        const content = entry.content ?? "";
        revealedTextContent.set(entry.id, content);
        revealedTextIds.add(entry.id);
        return true;
      }

      const password = window.prompt("Password required to reveal this text.");
      if (!password) {
        return false;
      }

      try {
        const response = await fetch(`/api/text/${encodeURIComponent(entry.id)}/reveal`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password })
        });
        if (!response.ok) {
          throw new Error(`Reveal failed: ${response.status}`);
        }
        const payload = await response.json();
        revealedTextContent.set(entry.id, payload.content);
        revealedTextIds.add(entry.id);
        textStatus.textContent = "Text revealed.";
        return true;
      } catch (error) {
        textStatus.textContent = "Wrong password.";
        return false;
      }
    }

    function openProtectedPath(path, statusElement) {
      const password = window.prompt("Password required.");
      if (!password) {
        return;
      }
      if (statusElement) {
        statusElement.textContent = "Opening protected item…";
      }
      window.location.href = withPassword(path, password);
    }

    async function deleteFile(id) {
      try {
        const response = await fetch(`/api/file/${encodeURIComponent(id)}`, {
          method: "DELETE"
        });
        if (!response.ok) {
          throw new Error(`Delete failed: ${response.status}`);
        }
        renderSnapshot(await response.json());
        fileStatus.textContent = "File deleted.";
      } catch (error) {
        fileStatus.textContent = "File delete failed.";
      }
    }

    function renderTextHistory(texts) {
      lastRenderedTexts = texts;
      textHistory.innerHTML = "";
      if (!texts.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "No text history yet.";
        textHistory.appendChild(li);
        return;
      }

      for (const entry of texts) {
        const li = document.createElement("li");
        li.className = "history-item copyable";
        li.addEventListener("click", async () => {
          if (entry.hidden && !revealedTextIds.has(entry.id)) {
            const revealed = await revealProtectedText(entry);
            if (revealed) {
              renderTextHistory(texts);
            }
            return;
          }

          const content = revealedTextContent.get(entry.id) ?? entry.content;
          if (content !== null) {
            const copied = await copyText(content);
            if (copied) {
              showCopiedState(entry.id);
            }
          }
        });

        const head = document.createElement("div");
        head.className = "history-head";

        const infoTable = document.createElement("table");
        infoTable.className = "entry-table";

        const savedRow = document.createElement("tr");
        const savedHead = document.createElement("th");
        savedHead.textContent = "Saved";
        const savedValue = document.createElement("td");
        savedValue.textContent = formatDate(entry.created_at);
        savedRow.appendChild(savedHead);
        savedRow.appendChild(savedValue);

        const fromRow = document.createElement("tr");
        const fromHead = document.createElement("th");
        fromHead.textContent = "Shared by";
        const fromValue = document.createElement("td");
        fromValue.textContent = entry.sharer_name || "Anonymous";
        if (entry.sharer_ip) {
          fromValue.textContent += ` (${entry.sharer_ip})`;
        }
        fromRow.appendChild(fromHead);
        fromRow.appendChild(fromValue);

        const expiresRow = document.createElement("tr");
        const expiresHead = document.createElement("th");
        expiresHead.textContent = "Expires";
        const expiresValue = document.createElement("td");
        expiresValue.textContent = formatDate(entry.expires_at);
        expiresRow.appendChild(expiresHead);
        expiresRow.appendChild(expiresValue);

        const linkRow = document.createElement("tr");
        const linkHead = document.createElement("th");
        linkHead.textContent = "LAN link";
        const linkValue = document.createElement("td");
        const shareLink = document.createElement("a");
        shareLink.href = lanSharePath(entry.short_code);
        shareLink.textContent = lanShareUrl(entry.short_code);
        shareLink.title = "Open this text directly over the LAN";
        shareLink.addEventListener("click", (event) => {
          event.stopPropagation();
          if (entry.password_required) {
            event.preventDefault();
            openProtectedPath(lanSharePath(entry.short_code), textStatus);
          }
        });
        linkValue.appendChild(shareLink);
        linkRow.appendChild(linkHead);
        linkRow.appendChild(linkValue);

        infoTable.appendChild(savedRow);
        infoTable.appendChild(fromRow);
        infoTable.appendChild(expiresRow);
        infoTable.appendChild(linkRow);

        if (entry.hidden) {
          const toggleBtn = document.createElement("button");
          toggleBtn.type = "button";
          const isRevealed = revealedTextIds.has(entry.id);
          toggleBtn.textContent = isRevealed ? "Hide" : "Reveal";
          toggleBtn.addEventListener("click", async (event) => {
            event.stopPropagation();
            if (revealedTextIds.has(entry.id)) {
              revealedTextIds.delete(entry.id);
              revealedTextContent.delete(entry.id);
            } else {
              const revealed = await revealProtectedText(entry);
              if (!revealed) {
                return;
              }
            }
            renderTextHistory(texts);
          });
          const revealRow = document.createElement("tr");
          const revealHead = document.createElement("th");
          revealHead.textContent = "Reveal";
          const revealValue = document.createElement("td");
          revealValue.appendChild(toggleBtn);
          revealRow.appendChild(revealHead);
          revealRow.appendChild(revealValue);
          infoTable.appendChild(revealRow);
        }
        head.appendChild(infoTable);

        const bodyRow = document.createElement("div");
        bodyRow.className = "text-row";

        const cardWrap = document.createElement("div");
        cardWrap.className = "text-card-wrap";

        const label = document.createElement("div");
        label.className = "text-card-label";
        label.textContent = "Click to copy...";
        if (copiedTextId === entry.id) {
          const copiedPill = document.createElement("span");
          copiedPill.className = "copied-pill";
          copiedPill.textContent = "Copied";
          label.appendChild(copiedPill);
        }

        const body = document.createElement("div");
        body.className = "history-body text-card";
        if (copiedTextId === entry.id) {
          body.classList.add("flash-copy");
        }
        const isMasked = entry.hidden && !revealedTextIds.has(entry.id);
        if (isMasked) {
          body.classList.add("masked");
        }
        body.textContent = isMasked
          ? (entry.masked_content || maskText(entry.content || ""))
          : (revealedTextContent.get(entry.id) ?? entry.content ?? "");

        const deleteWrap = document.createElement("div");
        deleteWrap.className = "text-card-actions";

        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "danger delete-btn";
        deleteBtn.textContent = "🗑";
        deleteBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          deleteText(entry.id);
        });

        deleteBtn.textContent = "🗑";
        const deleteLabel = document.createElement("div");
        deleteLabel.className = "text-delete-label";
        deleteLabel.textContent = "Delete";

        cardWrap.appendChild(label);
        cardWrap.appendChild(body);
        deleteWrap.appendChild(deleteBtn);
        deleteWrap.appendChild(deleteLabel);
        bodyRow.appendChild(cardWrap);
        bodyRow.appendChild(deleteWrap);

        li.appendChild(head);
        li.appendChild(bodyRow);
        textHistory.appendChild(li);
      }
    }

    function renderFiles(files) {
      fileList.innerHTML = "";
      if (!files.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "No file history yet.";
        fileList.appendChild(li);
        return;
      }

      for (const file of files) {
        const li = document.createElement("li");
        li.className = "history-item";

        const head = document.createElement("div");
        head.className = "history-head";

        const card = document.createElement("div");
        card.className = "file-card";

        const top = document.createElement("div");
        top.className = "file-card-top";

        const details = document.createElement("div");
        const name = document.createElement("div");
        name.className = "file-name";
        name.textContent = file.name;
        const infoTable = document.createElement("table");
        infoTable.className = "entry-table";

        const sizeRow = document.createElement("tr");
        const sizeHead = document.createElement("th");
        sizeHead.textContent = "Size";
        const sizeValue = document.createElement("td");
        sizeValue.textContent = formatSize(file.size);
        sizeRow.appendChild(sizeHead);
        sizeRow.appendChild(sizeValue);

        const fromRow = document.createElement("tr");
        const fromHead = document.createElement("th");
        fromHead.textContent = "Shared by";
        const fromValue = document.createElement("td");
        fromValue.textContent = file.sharer_name || "Anonymous";
        if (file.sharer_ip) {
          fromValue.textContent += ` (${file.sharer_ip})`;
        }
        fromRow.appendChild(fromHead);
        fromRow.appendChild(fromValue);

        const uploadedRow = document.createElement("tr");
        const uploadedHead = document.createElement("th");
        uploadedHead.textContent = "Uploaded";
        const uploadedValue = document.createElement("td");
        uploadedValue.textContent = formatDate(file.created_at);
        uploadedRow.appendChild(uploadedHead);
        uploadedRow.appendChild(uploadedValue);

        const expiresRow = document.createElement("tr");
        const expiresHead = document.createElement("th");
        expiresHead.textContent = "Expires";
        const expiresValue = document.createElement("td");
        expiresValue.textContent = formatDate(file.expires_at);
        expiresRow.appendChild(expiresHead);
        expiresRow.appendChild(expiresValue);

        const linkRow = document.createElement("tr");
        const linkHead = document.createElement("th");
        linkHead.textContent = "LAN link";
        const linkValue = document.createElement("td");

        const shareLink = document.createElement("a");
        shareLink.href = lanSharePath(file.short_code);
        shareLink.textContent = lanShareUrl(file.short_code);
        shareLink.title = "Open this file directly over the LAN";
        if (file.password_required) {
          shareLink.addEventListener("click", (event) => {
            event.preventDefault();
            openProtectedPath(lanSharePath(file.short_code), fileStatus);
          });
        }
        linkValue.appendChild(shareLink);
        linkRow.appendChild(linkHead);
        linkRow.appendChild(linkValue);

        infoTable.appendChild(sizeRow);
        infoTable.appendChild(fromRow);
        infoTable.appendChild(uploadedRow);
        infoTable.appendChild(expiresRow);
        infoTable.appendChild(linkRow);
        if (file.password_required) {
          const accessRow = document.createElement("tr");
          const accessHead = document.createElement("th");
          accessHead.textContent = "Access";
          const accessValue = document.createElement("td");
          accessValue.textContent = "Password protected";
          accessRow.appendChild(accessHead);
          accessRow.appendChild(accessValue);
          infoTable.appendChild(accessRow);
        }

        details.appendChild(name);
        details.appendChild(infoTable);

        const actions = document.createElement("div");
        actions.className = "file-card-actions";

        const link = document.createElement("a");
        link.className = "file-link";
        link.href = `/download/${encodeURIComponent(file.id)}`;
        link.textContent = "Download";
        link.addEventListener("click", () => {
          clearActiveTabIndicator();
        });
        if (file.password_required) {
          link.addEventListener("click", (event) => {
            event.preventDefault();
            openProtectedPath(`/download/${encodeURIComponent(file.id)}`, fileStatus);
          });
        }

        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "danger";
        deleteBtn.textContent = "Delete";
        deleteBtn.addEventListener("click", () => deleteFile(file.id));

        actions.appendChild(link);
        actions.appendChild(deleteBtn);
        top.appendChild(details);
        top.appendChild(actions);
        card.appendChild(top);
        head.appendChild(card);
        li.appendChild(head);
        fileList.appendChild(li);
      }
    }

    function renderSnapshot(snapshot) {
      const nextTextId = snapshot.texts && snapshot.texts.length ? snapshot.texts[0].id : null;
      const nextFileId = snapshot.files && snapshot.files.length ? snapshot.files[0].id : null;

      if (snapshotInitialized) {
        if (nextTextId && nextTextId !== latestTextId && nextTextId !== suppressedTextId) {
          unreadText = true;
        }
        if (nextFileId && nextFileId !== latestFileId && nextFileId !== suppressedFileId) {
          unreadFiles = true;
        }
      }

      if (nextTextId === suppressedTextId) {
        suppressedTextId = null;
      }
      if (nextFileId === suppressedFileId) {
        suppressedFileId = null;
      }

      latestTextId = nextTextId;
      latestFileId = nextFileId;
      if (!snapshotInitialized) {
        snapshotInitialized = true;
      }

      if (!pendingTextPush && !isTextFormActive()) {
        sharedText.value = "";
      }
      textMeta.textContent = `Last update: ${formatDate(snapshot.updated_at)} • Auto-delete after 24 hours`;
      renderTextHistory(snapshot.texts || []);
      renderFiles(snapshot.files || []);
      updateTabIndicators();
    }

    async function fetchState() {
      try {
        const response = await fetch("/api/state");
        if (!response.ok) {
          throw new Error(`State request failed: ${response.status}`);
        }
        renderSnapshot(await response.json());
      } catch (error) {
        textStatus.textContent = "Could not refresh shared data.";
      }
    }

    async function saveText() {
      const content = sharedText.value.trim();
      if (!content) {
        textStatus.textContent = "Paste some text first.";
        return;
      }
      const submittedText = sharedText.value;
      const submittedHidden = hiddenText.checked;
      const submittedPassword = textPassword.value.trim();

      pendingTextPush = true;
      textStatus.textContent = "Saving…";
      try {
        const response = await fetch("/api/text", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: submittedText,
            hidden: submittedHidden,
            password: textPassword.value,
            name: sharerName.value
          })
        });
        if (!response.ok) {
          throw new Error(`Save failed: ${response.status}`);
        }
        const snapshot = await response.json();
        if (snapshot.texts && snapshot.texts.length > 0) {
          suppressedTextId = snapshot.texts[0].id;
          unreadText = false;
        }
        renderSnapshot(snapshot);
        sharedText.value = "";
        hiddenText.checked = false;
        textPassword.value = "";
        updateHiddenOptions();
        textStatus.textContent = "Text added to history.";
      } catch (error) {
        textStatus.textContent = "Text save failed.";
      } finally {
        pendingTextPush = false;
      }
    }

    async function uploadFile(file = fileInput.files[0]) {
      if (!file) {
        fileStatus.textContent = "Choose a file first.";
        return;
      }
      if (hiddenFile.checked && !filePassword.value.trim()) {
        fileStatus.textContent = "Add a password before uploading a hidden file.";
        filePassword.focus();
        return;
      }

      const formData = new FormData();
      formData.append("file", file);
      formData.append("hidden", hiddenFile.checked ? "true" : "false");
      formData.append("password", filePassword.value);
      formData.append("name", sharerName.value);
      fileStatus.textContent = `Uploading ${file.name}…`;

      try {
        const response = await fetch("/api/upload", {
          method: "POST",
          body: formData
        });
        if (!response.ok) {
          const message = await response.text();
          throw new Error(message || `Upload failed: ${response.status}`);
        }
        const snapshot = await response.json();
        if (snapshot.files && snapshot.files.length > 0) {
          suppressedFileId = snapshot.files[0].id;
          unreadFiles = false;
        }
        renderSnapshot(snapshot);
        fileStatus.textContent = `Uploaded ${file.name}.`;
        fileInput.value = "";
        hiddenFile.checked = false;
        filePassword.value = "";
        updateHiddenOptions();
      } catch (error) {
        fileStatus.textContent = error.message || "Upload failed.";
      }
    }

    saveTextBtn.addEventListener("click", saveText);
    hiddenText.addEventListener("change", updateHiddenOptions);
    hiddenFile.addEventListener("change", updateHiddenOptions);
    textTabBtn.addEventListener("click", () => setActiveTab("text"));
    fileTabBtn.addEventListener("click", () => setActiveTab("file"));
    textPanel.addEventListener("click", clearActiveTabIndicator);
    filePanel.addEventListener("click", clearActiveTabIndicator);
    uploadBtn.addEventListener("click", () => uploadFile());
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files.length > 0) {
        uploadFile();
      }
    });

    sharedText.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        saveText();
      }
    });

    dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropZone.classList.add("active");
    });

    dropZone.addEventListener("dragleave", () => {
      dropZone.classList.remove("active");
    });

    dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropZone.classList.remove("active");
      const droppedFile = event.dataTransfer?.files?.[0];
      if (droppedFile) {
        uploadFile(droppedFile);
      }
    });

    fetchState();
    updateHiddenOptions();
    syncTabs();
    setInterval(fetchState, 2000);
  </script>
</body>
</html>
"""


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LanDrop Login</title>
  <link rel="icon" type="image/png" href="/assets/logo-landrop-v1.png">
  <style>
    :root {
      --bg: #ffffff;
      --panel: #ffffff;
      --ink: #0f2a7a;
      --muted: #45619d;
      --line: #bed6ff;
      --accent: #12c8f4;
      --accent-strong: #1497ff;
      --danger: #ff9f1a;
      --shadow: rgba(15, 42, 122, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, rgba(18, 200, 244, 0.05), transparent 22rem),
        radial-gradient(circle at bottom right, rgba(20, 151, 255, 0.04), transparent 28rem),
        linear-gradient(180deg, #ffffff 0%, var(--bg) 100%);
      color: var(--ink);
    }
    .card {
      width: min(100%, 420px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 24px;
      box-shadow: 0 12px 30px var(--shadow);
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(2rem, 5vw, 3rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }
    p {
      margin: 0 0 16px;
      color: var(--muted);
      line-height: 1.5;
    }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
      padding: 14px;
      font: inherit;
      color: var(--ink);
    }
    input:focus {
      outline: 2px solid rgba(18, 200, 244, 0.35);
      outline-offset: 2px;
      border-color: var(--accent-strong);
    }
    button {
      margin-top: 12px;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font: inherit;
      cursor: pointer;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: #fff;
      box-shadow: 0 10px 24px rgba(20, 151, 255, 0.22);
    }
    .error {
      min-height: 1.2rem;
      margin-top: 12px;
      color: var(--danger);
    }
  </style>
</head>
<body>
  <main class="card">
    <h1>Access Code</h1>
    <p>Enter the shared access code to use this page from another browser on your network.</p>
    <input id="accessCode" type="password" placeholder="Access code" autofocus>
    <button id="loginBtn" type="button">Unlock</button>
    <div class="error" id="loginStatus"></div>
  </main>
  <script>
    const accessCode = document.getElementById("accessCode");
    const loginBtn = document.getElementById("loginBtn");
    const loginStatus = document.getElementById("loginStatus");

    async function login() {
      loginStatus.textContent = "Checking…";
      try {
        const response = await fetch("/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: accessCode.value })
        });
        if (!response.ok) {
          loginStatus.textContent = "Wrong access code.";
          return;
        }
        window.location.href = "/";
      } catch (error) {
        loginStatus.textContent = "Login failed.";
      }
    }

    loginBtn.addEventListener("click", login);
    accessCode.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        login();
      }
    });
  </script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LanDrop/1.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.serve_asset("logo-landrop-v1.png")
            return

        if parsed.path.startswith("/assets/"):
            asset_name = parsed.path.removeprefix("/assets/")
            self.serve_asset(asset_name)
            return

        if parsed.path == "/":
            if not is_authorized(self):
                self.send_html(LOGIN_HTML)
                return
            self.send_html(INDEX_HTML.replace("__SHARE_BASE_URL__", json.dumps(get_share_base_url())))
            return

        if ACCESS_CODE and not is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
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

        if parsed.path.startswith("/api/text/") and parsed.path.endswith("/reveal"):
            entry_id = urllib.parse.unquote(
                parsed.path.removeprefix("/api/text/").removesuffix("/reveal")
            )
            self.handle_text_reveal(entry_id)
            return

        if parsed.path == "/api/upload":
            self.handle_file_upload()
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
        if not text.strip():
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
            return

        add_text_entry(
            text,
            hidden=hidden,
            password=password.strip(),
            sharer_name=sharer_name.strip(),
            sharer_ip=self.client_address[0],
        )
        self.send_json(get_snapshot())

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

        self.send_json({"content": entry["content"]})

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
        self.send_json(get_snapshot())

    def handle_file_delete(self, file_id: str) -> None:
        if not delete_file_entry(file_id):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        self.send_json(get_snapshot())

    def handle_file_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        boundary = None
        for item in content_type.split(";"):
            item = item.strip()
            if item.startswith("boundary="):
                boundary = item.split("=", 1)[1].encode("utf-8")
                break

        if not boundary:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing multipart boundary")
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Empty upload")
            return
        if length > MAX_FILE_SIZE:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
            return

        body = self.rfile.read(length)
        filename, file_bytes, fields = self.parse_multipart_file(body, boundary)
        if filename is None or file_bytes is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Could not read uploaded file")
            return
        hidden = fields.get("hidden", "false").lower() == "true"
        password = fields.get("password", "").strip()
        sharer_name = fields.get("name", "").strip()
        if hidden and not password:
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden files require a password")
            return

        ensure_upload_dir()
        stored_name = unique_filename(filename)
        target = UPLOAD_DIR / stored_name
        with target.open("wb") as handle:
            handle.write(file_bytes)

        add_file(
            filename,
            stored_name,
            len(file_bytes),
            hidden=hidden,
            password=password,
            sharer_name=sharer_name,
            sharer_ip=self.client_address[0],
        )
        self.send_json(get_snapshot())

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

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
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

        content_type = "application/octet-stream"
        if target.suffix.lower() == ".png":
            content_type = "image/png"
        elif target.suffix.lower() in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        elif target.suffix.lower() == ".svg":
            content_type = "image/svg+xml"
        elif target.suffix.lower() == ".ico":
            content_type = "image/x-icon"

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
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Serving LanDrop on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
