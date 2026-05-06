import threading


state_lock = threading.Lock()
session_lock = threading.Lock()
websocket_lock = threading.Lock()
auth_attempt_lock = threading.Lock()
authorized_sessions: dict[str, dict] = {}
websocket_clients = set()
auth_attempts: dict[str, dict] = {}
janitor_thread: threading.Thread | None = None
janitor_stop_event = threading.Event()
shared_state = {
    "workspaces": {},
    "reserved_upload_bytes": 0,
    "reserved_upload_names": set(),
    "update_check": {
        "checking": False,
        "last_checked_at": 0.0,
        "latest_version": "",
        "update_available": False,
    },
}
