import base64
import http.client
import io
import json
import os
import socket
import struct
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import app
from dassiedrop import config, state


def reset_app_state() -> None:
    with state.state_lock:
        state.shared_state["workspaces"] = {}
    with state.session_lock:
        state.authorized_sessions.clear()
    app.stop_background_tasks()


class CoreStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.original_upload_dir = config.UPLOAD_DIR
        self.original_access_code = config.ACCESS_CODE
        self.original_share_base_url = config.SHARE_BASE_URL
        self.original_workspace_super_password = config.WORKSPACE_SUPER_PASSWORD
        self.original_now_ts = config.now_ts
        self.original_version_file = config.VERSION_FILE
        config.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        config.ACCESS_CODE = ""
        config.SHARE_BASE_URL = ""
        config.WORKSPACE_SUPER_PASSWORD = ""
        config.now_ts = self.fake_now
        config.VERSION_FILE = Path(self.temp_dir.name) / "VERSION"
        config.VERSION_FILE.write_text("9.9.9", encoding="utf-8")
        self.current_time = 1_700_000_000.0
        app.ensure_upload_dir()
        reset_app_state()

    def tearDown(self) -> None:
        reset_app_state()
        config.UPLOAD_DIR = self.original_upload_dir
        config.ACCESS_CODE = self.original_access_code
        config.SHARE_BASE_URL = self.original_share_base_url
        config.WORKSPACE_SUPER_PASSWORD = self.original_workspace_super_password
        config.now_ts = self.original_now_ts
        config.VERSION_FILE = self.original_version_file
        self.temp_dir.cleanup()

    def fake_now(self) -> float:
        return self.current_time


class CoreHttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.original_upload_dir = config.UPLOAD_DIR
        self.original_access_code = config.ACCESS_CODE
        self.original_share_base_url = config.SHARE_BASE_URL
        self.original_workspace_super_password = config.WORKSPACE_SUPER_PASSWORD
        self.original_now_ts = config.now_ts
        self.original_version_file = config.VERSION_FILE
        self.current_time = 1_700_100_000.0
        config.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        config.ACCESS_CODE = ""
        config.SHARE_BASE_URL = ""
        config.WORKSPACE_SUPER_PASSWORD = ""
        config.now_ts = self.fake_now
        config.VERSION_FILE = Path(self.temp_dir.name) / "VERSION"
        config.VERSION_FILE.write_text("9.9.9", encoding="utf-8")
        app.ensure_upload_dir()
        reset_app_state()
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        reset_app_state()
        config.UPLOAD_DIR = self.original_upload_dir
        config.ACCESS_CODE = self.original_access_code
        config.SHARE_BASE_URL = self.original_share_base_url
        config.WORKSPACE_SUPER_PASSWORD = self.original_workspace_super_password
        config.now_ts = self.original_now_ts
        config.VERSION_FILE = self.original_version_file
        self.temp_dir.cleanup()

    def fake_now(self) -> float:
        return self.current_time

    def start_server(self, access_code: str = "") -> None:
        config.ACCESS_CODE = access_code
        reset_app_state()
        app.start_background_tasks()
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = {
            "status": response.status,
            "headers": dict(response.getheaders()),
            "body": payload,
            "text": payload.decode("utf-8", errors="replace"),
        }
        connection.close()
        return result

    def upload_request(
        self,
        filename: str,
        content: bytes,
        cookie: str | None = None,
        hidden: bool = False,
        password: str = "",
        name: str = "",
    ):
        boundary = "----DassieDropBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content
        body += (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="hidden"\r\n\r\n'
            f"{'true' if hidden else 'false'}"
        ).encode("utf-8")
        body += (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="password"\r\n\r\n'
            f"{password}"
        ).encode("utf-8")
        body += (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n\r\n'
            f"{name}"
        ).encode("utf-8")
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        if cookie:
            headers["Cookie"] = cookie
        return self.request("POST", "/api/upload", body=body, headers=headers)

    def login(self, access_code: str) -> str:
        response = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": access_code}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response["status"], 200)
        return response["headers"]["Set-Cookie"].split(";", 1)[0]


class DummyServer:
    is_https = False
    server_port = 8000
    server_address = ("127.0.0.1", 8000)


class DummyHandler:
    def __init__(
        self,
        *,
        path: str = "/",
        headers: dict | None = None,
        body: bytes = b"",
        client_address: tuple[str, int] = ("127.0.0.1", 12345),
    ) -> None:
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = client_address
        self.server = DummyServer()
        self.error_status = None
        self.error_message = None

    def send_error(self, status, message):
        self.error_status = int(status)
        self.error_message = message


def make_app_handler(
    *,
    path: str = "/",
    headers: dict | None = None,
    body: bytes = b"",
    client_address: tuple[str, int] = ("127.0.0.1", 12345),
):
    handler = object.__new__(app.AppHandler)
    dummy = DummyHandler(path=path, headers=headers, body=body, client_address=client_address)
    handler.path = dummy.path
    handler.headers = dummy.headers
    handler.rfile = dummy.rfile
    handler.wfile = dummy.wfile
    handler.client_address = dummy.client_address
    handler.server = dummy.server
    handler.error_status = None
    handler.error_message = None

    def send_error(status, message):
        handler.error_status = int(status)
        handler.error_message = message

    handler.send_error = send_error
    return handler
