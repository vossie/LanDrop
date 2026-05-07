#!/usr/bin/env python3
import logging
import os
import threading

from http.server import ThreadingHTTPServer

from dassiedrop import config, state
from dassiedrop.auth import (
    clear_workspace_selection_for_deleted_workspace,
    create_authorized_session,
    ensure_browser_session,
    get_session,
    is_authorized,
    parse_cookies,
    requested_workspace_password,
    requested_workspace_selector,
    session_cookie,
    set_session_workspace,
)
from dassiedrop.routes import (
    AppHandler,
    base_url_from_request,
    build_server,
    get_app_version,
    get_share_base_url,
    render_template,
    share_payload,
)
from dassiedrop.storage import (
    add_file,
    add_text_entry,
    compact_workspace_name,
    create_workspace,
    delete_file_entry,
    delete_text_entry,
    delete_workspace,
    enter_workspace,
    ensure_default_workspace_locked,
    ensure_upload_dir,
    entry_password_is_valid,
    find_entry_by_short_code,
    find_file_entry,
    find_text_entry,
    get_latest_file_entry,
    get_latest_text_entry,
    get_snapshot,
    get_workspace,
    get_workspace_by_slug_locked,
    get_workspace_locked,
    guess_content_type,
    json_bytes,
    list_workspace_objects_locked,
    list_workspaces,
    load_persisted_files,
    load_persisted_workspaces,
    make_id,
    persist_workspaces_locked,
    prune_expired_entries,
    prune_workspace_locked,
    resolve_workspace_selector_locked,
    sanitize_filename,
    sanitize_workspace_name,
    serialize_file_entry,
    serialize_text_entry,
    serialize_workspace_payload,
    serialize_workspace_summary,
    touch_workspace_locked,
    trim_workspace_history_locked,
    unique_filename,
    uploads_index_path,
    verify_password,
    workspace_delete_password_is_valid,
    workspace_is_inactive_locked,
    workspace_password_is_valid,
    workspace_slug,
)
from dassiedrop.websocket import (
    WebSocketClient,
    broadcast_snapshot,
    close_workspace_clients,
    register_websocket_client,
    start_background_tasks,
    stop_background_tasks,
    unregister_websocket_client,
    websocket_accept_value,
    websocket_frame,
)


DEFAULT_WORKSPACE_ID = config.DEFAULT_WORKSPACE_ID
DEFAULT_WORKSPACE_NAME = config.DEFAULT_WORKSPACE_NAME
EXPIRY_SECONDS = config.EXPIRY_SECONDS
MAX_TEXT_HISTORY = config.MAX_TEXT_HISTORY
MAX_FILE_HISTORY = config.MAX_FILE_HISTORY
MAX_FILE_SIZE = config.MAX_FILE_SIZE
logger = logging.getLogger("dassiedrop.app")


def __getattr__(name):
    if hasattr(config, name):
        return getattr(config, name)
    if hasattr(state, name):
        return getattr(state, name)
    raise AttributeError(name)


def reset_update_check_state() -> None:
    with state.state_lock:
        state.shared_state["update_check"] = {
            "checking": False,
            "last_checked_at": 0.0,
            "latest_version": "",
            "update_available": False,
        }


def get_update_check_state() -> dict:
    with state.state_lock:
        return dict(state.shared_state.get("update_check", {}))


def check_for_updates(force: bool = False) -> bool:
    if not config.UPDATE_CHECK_ENABLED:
        return False

    now = config.now_ts()
    with state.state_lock:
        update_state = state.shared_state.setdefault(
            "update_check",
            {
                "checking": False,
                "last_checked_at": 0.0,
                "latest_version": "",
                "update_available": False,
            },
        )
        if update_state.get("checking"):
            return bool(update_state.get("update_available"))
        last_checked_at = float(update_state.get("last_checked_at") or 0.0)
        if not force and now - last_checked_at < config.UPDATE_CHECK_INTERVAL_SECONDS:
            return bool(update_state.get("update_available"))
        update_state["checking"] = True

    remote_version = config.fetch_remote_app_version()
    current_version = get_app_version()
    update_available = bool(remote_version) and config.is_remote_version_newer(current_version, remote_version)

    with state.state_lock:
        update_state = state.shared_state.setdefault("update_check", {})
        update_state["checking"] = False
        update_state["last_checked_at"] = now
        update_state["latest_version"] = remote_version or ""
        update_state["update_available"] = update_available
        return update_available


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ensure_upload_dir()
    load_persisted_workspaces()
    if config.UPDATE_CHECK_ENABLED:
        check_for_updates(force=True)
    start_background_tasks()
    host = os.environ.get("HOST", "0.0.0.0")
    if config.HTTPS_ENABLED and config.HTTP_PORT == config.HTTPS_PORT:
        raise RuntimeError("HTTP_PORT and HTTPS_PORT must be different when HTTPS is enabled.")

    http_server, http_scheme = build_server(host, config.HTTP_PORT, use_https=False)
    logger.info("Serving DassieDrop on %s://%s:%s", http_scheme, host, config.HTTP_PORT)

    if not config.HTTPS_ENABLED:
        http_server.serve_forever()
        return

    https_server, https_scheme = build_server(host, config.HTTPS_PORT, use_https=True)
    logger.info("Serving DassieDrop on %s://%s:%s", https_scheme, host, config.HTTPS_PORT)

    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    https_server.serve_forever()


if __name__ == "__main__":
    main()
