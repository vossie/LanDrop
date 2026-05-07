import base64
import hashlib
import json
import struct
import threading

from . import auth, config, state, storage


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WEBSOCKET_CLOSE_POLICY_VIOLATION = 1008


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
    def __init__(self, connection, workspace_id: str, session_id: str | None = None) -> None:
        self.connection = connection
        self.workspace_id = workspace_id
        self.session_id = session_id
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

    def send_close(self, code: int) -> bool:
        return self.send_frame(0x8, struct.pack("!H", code))

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
    with state.websocket_lock:
        state.websocket_clients.add(client)


def unregister_websocket_client(client: WebSocketClient) -> None:
    with state.websocket_lock:
        state.websocket_clients.discard(client)
    client.close()


def close_workspace_clients(workspace_id: str) -> None:
    with state.websocket_lock:
        clients = [client for client in state.websocket_clients if client.workspace_id == workspace_id]
        for client in clients:
            state.websocket_clients.discard(client)
    for client in clients:
        client.close()


def broadcast_snapshot(workspace_id: str, snapshot: dict | None = None) -> None:
    payload = snapshot or storage.get_snapshot(workspace_id)
    with state.websocket_lock:
        clients = [client for client in state.websocket_clients if client.workspace_id == workspace_id]

    failed_clients = []
    for client in clients:
        if client.session_id and auth.get_session_by_id(client.session_id, touch=False) is None:
            client.send_close(WEBSOCKET_CLOSE_POLICY_VIOLATION)
            failed_clients.append(client)
            continue
        if not client.send_json(payload):
            failed_clients.append(client)

    for client in failed_clients:
        unregister_websocket_client(client)


def start_background_tasks() -> None:
    if state.janitor_thread and state.janitor_thread.is_alive():
        return

    state.janitor_stop_event.clear()

    def run_janitor() -> None:
        while not state.janitor_stop_event.wait(config.JANITOR_INTERVAL_SECONDS):
            from . import auth
            import app as app_module

            auth.cleanup_throttle_failures()
            auth.cleanup_rate_limit_events()
            auth.cleanup_authorized_sessions()
            app_module.check_for_updates()
            for workspace_id in storage.prune_expired_entries():
                broadcast_snapshot(workspace_id)

    state.janitor_thread = threading.Thread(target=run_janitor, daemon=True)
    state.janitor_thread.start()


def stop_background_tasks() -> None:
    state.janitor_stop_event.set()
    if state.janitor_thread is not None:
        state.janitor_thread.join(timeout=2)
    state.janitor_thread = None

    with state.websocket_lock:
        clients = list(state.websocket_clients)
        state.websocket_clients.clear()
    for client in clients:
        client.close()
