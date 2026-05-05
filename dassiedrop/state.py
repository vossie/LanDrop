import threading


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
