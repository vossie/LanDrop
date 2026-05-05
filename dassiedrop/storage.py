import hashlib
import hmac
import json
import mimetypes
import secrets
from pathlib import Path

from . import config, state


def ensure_upload_dir() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def uploads_index_path() -> Path:
    return config.UPLOAD_DIR / ".dassiedrop-workspaces.json"


def sanitize_filename(name: str) -> str:
    normalized = name.replace("\\", "/")
    raw_name = Path(normalized).name.strip().replace("\x00", "")
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
    path = config.UPLOAD_DIR / candidate
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


def make_workspace_id() -> str:
    return secrets.token_hex(6)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        return True
    return hmac.compare_digest(hash_password(password), password_hash)


def build_workspace(
    name: str,
    password_hash: str | None = None,
    workspace_id: str | None = None,
    created_at: float | None = None,
    last_used_at: float | None = None,
) -> dict:
    timestamp = config.now_ts() if created_at is None else created_at
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
    workspace = state.shared_state["workspaces"].get(config.DEFAULT_WORKSPACE_ID)
    if workspace is None:
        workspace = build_workspace(
            config.DEFAULT_WORKSPACE_NAME,
            workspace_id=config.DEFAULT_WORKSPACE_ID,
            created_at=0.0,
        )
        state.shared_state["workspaces"][config.DEFAULT_WORKSPACE_ID] = workspace
    return workspace


def workspace_sort_key(item: dict) -> tuple[int, str]:
    return (0 if item["id"] == config.DEFAULT_WORKSPACE_ID else 1, item["name"].lower())


def list_workspace_objects_locked() -> list[dict]:
    ensure_default_workspace_locked()
    return sorted(state.shared_state["workspaces"].values(), key=workspace_sort_key)


def get_workspace_locked(workspace_id: str) -> dict | None:
    ensure_default_workspace_locked()
    return state.shared_state["workspaces"].get(workspace_id)


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
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        return dict(workspace) if workspace is not None else None


def resolve_workspace_selector_locked(selector: str) -> dict | None:
    normalized = selector.strip()
    if not normalized:
        return None
    workspace = get_workspace_locked(normalized)
    if workspace is not None:
        return workspace
    return get_workspace_by_slug_locked(normalized)


def recompute_workspace_updated_at_locked(workspace: dict) -> None:
    timestamps = [item["created_at"] for item in workspace["texts"]]
    timestamps.extend(item["created_at"] for item in workspace["files"])
    workspace["updated_at"] = max(timestamps, default=workspace["created_at"])


def touch_workspace_locked(workspace: dict, persist_interval: float = 60.0) -> bool:
    previous = float(workspace.get("last_used_at") or workspace["created_at"])
    current = config.now_ts()
    workspace["last_used_at"] = current
    return current - previous >= persist_interval


def trim_workspace_history_locked(workspace: dict) -> None:
    overflow_files = []
    if len(workspace["texts"]) > config.MAX_TEXT_HISTORY:
        workspace["texts"] = workspace["texts"][: config.MAX_TEXT_HISTORY]

    if len(workspace["files"]) > config.MAX_FILE_HISTORY:
        overflow_files = workspace["files"][config.MAX_FILE_HISTORY :]
        workspace["files"] = workspace["files"][: config.MAX_FILE_HISTORY]

    for item in overflow_files:
        target = config.UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)

    recompute_workspace_updated_at_locked(workspace)


def prune_workspace_locked(workspace: dict) -> bool:
    cutoff = config.now_ts() - config.EXPIRY_SECONDS
    expired_files = [item for item in workspace["files"] if item["created_at"] < cutoff]
    before_texts = len(workspace["texts"])
    before_files = len(workspace["files"])
    workspace["texts"] = [item for item in workspace["texts"] if item["created_at"] >= cutoff]
    workspace["files"] = [item for item in workspace["files"] if item["created_at"] >= cutoff]
    for item in expired_files:
        target = config.UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)
    recompute_workspace_updated_at_locked(workspace)
    return before_texts != len(workspace["texts"]) or before_files != len(workspace["files"])


def workspace_is_inactive_locked(workspace: dict) -> bool:
    if workspace["id"] == config.DEFAULT_WORKSPACE_ID:
        return False
    last_used_at = float(
        workspace.get("last_used_at") or workspace["updated_at"] or workspace["created_at"]
    )
    return last_used_at < (config.now_ts() - config.EXPIRY_SECONDS)


def workspace_password_is_valid(workspace: dict, password: str) -> bool:
    return verify_password(password, workspace.get("password_hash"))


def workspace_delete_password_is_valid(workspace: dict, password: str) -> bool:
    if workspace.get("password_hash") is None:
        return True
    candidate = password.strip()
    if not candidate:
        return False
    if config.WORKSPACE_SUPER_PASSWORD and hmac.compare_digest(
        candidate, config.WORKSPACE_SUPER_PASSWORD
    ):
        return True
    return workspace_password_is_valid(workspace, candidate)


def serialize_workspace_summary(workspace: dict) -> dict:
    return {
        "id": workspace["id"],
        "name": workspace["name"],
        "slug": workspace_slug(workspace["name"]),
        "path": f"/w/{workspace_slug(workspace['name'])}",
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
                    str(item.get("name") or config.DEFAULT_WORKSPACE_NAME),
                    password_hash=item.get("password_hash")
                    if isinstance(item.get("password_hash"), str)
                    else None,
                    workspace_id=workspace_id,
                    created_at=float(item.get("created_at") or config.now_ts()),
                    last_used_at=float(
                        item.get("last_used_at")
                        or item.get("updated_at")
                        or item.get("created_at")
                        or config.now_ts()
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
                    target = config.UPLOAD_DIR / stored_name
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
                            "created_at": float(file_item.get("created_at") or config.now_ts()),
                            "expires_at": float(
                                file_item.get("expires_at") or (config.now_ts() + config.EXPIRY_SECONDS)
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
                    config.DEFAULT_WORKSPACE_NAME,
                    workspace_id=config.DEFAULT_WORKSPACE_ID,
                    created_at=0.0,
                )
                restored_files = []
                for file_item in raw_files:
                    if not isinstance(file_item, dict):
                        continue
                    stored_name = file_item.get("stored_name")
                    if not isinstance(stored_name, str):
                        continue
                    target = config.UPLOAD_DIR / stored_name
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
                            "created_at": float(file_item.get("created_at") or config.now_ts()),
                            "expires_at": float(
                                file_item.get("expires_at") or (config.now_ts() + config.EXPIRY_SECONDS)
                            ),
                        }
                    )
                restored_files.sort(key=lambda entry: entry["created_at"], reverse=True)
                workspace["files"] = restored_files
                trim_workspace_history_locked(workspace)
                prune_workspace_locked(workspace)
                loaded_workspaces[workspace["id"]] = workspace

    with state.state_lock:
        state.shared_state["workspaces"] = loaded_workspaces
        ensure_default_workspace_locked()
        persist_workspaces_locked()


def load_persisted_files() -> None:
    load_persisted_workspaces()


def delete_workspace_artifacts(workspace: dict) -> None:
    from .auth import clear_workspace_selection_for_deleted_workspace
    from .websocket import close_workspace_clients

    for item in workspace["files"]:
        target = config.UPLOAD_DIR / item["stored_name"]
        if target.exists():
            target.unlink(missing_ok=True)
    clear_workspace_selection_for_deleted_workspace(workspace["id"])
    close_workspace_clients(workspace["id"])


def prune_expired_entries() -> list[str]:
    changed_workspace_ids = []
    removed_workspaces = []
    with state.state_lock:
        for workspace in list(state.shared_state["workspaces"].values()):
            pruned = prune_workspace_locked(workspace)
            inactive = workspace_is_inactive_locked(workspace)
            if inactive:
                removed_workspaces.append(state.shared_state["workspaces"].pop(workspace["id"]))
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
        "expires_after_seconds": config.EXPIRY_SECONDS,
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


def get_snapshot(workspace_id: str = config.DEFAULT_WORKSPACE_ID) -> dict:
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        if touch_workspace_locked(workspace):
            persist_workspaces_locked()
        return serialize_workspace_payload(workspace)


def get_latest_text_entry(workspace_id: str = config.DEFAULT_WORKSPACE_ID) -> dict | None:
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return None
        prune_workspace_locked(workspace)
        if touch_workspace_locked(workspace):
            persist_workspaces_locked()
        if not workspace["texts"]:
            return None
        return serialize_text_entry(workspace["texts"][0])


def get_latest_file_entry(workspace_id: str = config.DEFAULT_WORKSPACE_ID) -> dict | None:
    with state.state_lock:
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
    for workspace in state.shared_state["workspaces"].values():
        existing.update(item["short_code"] for item in workspace["texts"])
        existing.update(item["short_code"] for item in workspace["files"])
    while True:
        candidate = make_short_code()
        if candidate not in existing:
            return candidate


def create_workspace(name: str, password: str = "") -> dict:
    with state.state_lock:
        ensure_default_workspace_locked()
        workspace_name = sanitize_workspace_name(name)
        workspace = build_workspace(
            workspace_name,
            password_hash=hash_password(password.strip()) if password.strip() else None,
        )
        state.shared_state["workspaces"][workspace["id"]] = workspace
        persist_workspaces_locked()
        return serialize_workspace_summary(workspace)


def list_workspaces() -> list[dict]:
    with state.state_lock:
        removed_workspaces = []
        changed = False
        for workspace in list(state.shared_state["workspaces"].values()):
            if prune_workspace_locked(workspace):
                changed = True
            if workspace_is_inactive_locked(workspace):
                removed_workspaces.append(state.shared_state["workspaces"].pop(workspace["id"]))
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


def enter_workspace(session_id: str, workspace_id: str, password: str = "") -> tuple[bool, str]:
    from .auth import set_session_workspace

    with state.state_lock:
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
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            return (False, "Workspace not found")
        if not workspace_delete_password_is_valid(workspace, password):
            return (False, "Wrong workspace password")
        removed_workspace = state.shared_state["workspaces"].pop(workspace_id)
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
    workspace_id: str = config.DEFAULT_WORKSPACE_ID,
) -> None:
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        created_at = config.now_ts()
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
                "expires_at": created_at + config.EXPIRY_SECONDS,
            },
        )
        trim_workspace_history_locked(workspace)
        touch_workspace_locked(workspace, persist_interval=0.0)
        persist_workspaces_locked()


def delete_text_entry(entry_id: str, workspace_id: str = config.DEFAULT_WORKSPACE_ID) -> bool:
    with state.state_lock:
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
    workspace_id: str = config.DEFAULT_WORKSPACE_ID,
) -> None:
    with state.state_lock:
        workspace = get_workspace_locked(workspace_id)
        if workspace is None:
            workspace = ensure_default_workspace_locked()
        prune_workspace_locked(workspace)
        created_at = config.now_ts()
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
                "expires_at": created_at + config.EXPIRY_SECONDS,
            },
        )
        trim_workspace_history_locked(workspace)
        touch_workspace_locked(workspace, persist_interval=0.0)
        persist_workspaces_locked()


def delete_file_entry(file_id: str, workspace_id: str = config.DEFAULT_WORKSPACE_ID) -> bool:
    with state.state_lock:
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

    target = config.UPLOAD_DIR / removed["stored_name"]
    if target.exists():
        target.unlink(missing_ok=True)
    return True


def find_file_entry(file_id: str, workspace_id: str | None = None) -> dict | None:
    with state.state_lock:
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
    with state.state_lock:
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
    with state.state_lock:
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
