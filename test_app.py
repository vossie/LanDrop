import base64
import http.client
import json
import os
import socket
import struct
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import app


def reset_app_state() -> None:
    with app.state_lock:
        app.shared_state["updated_at"] = 0.0
        app.shared_state["texts"] = []
        app.shared_state["files"] = []
    with app.session_lock:
        app.authorized_sessions.clear()
    app.stop_background_tasks()


class AppStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.original_upload_dir = app.UPLOAD_DIR
        self.original_access_code = app.ACCESS_CODE
        self.original_share_base_url = app.SHARE_BASE_URL
        self.original_now_ts = app.now_ts
        self.original_version_file = app.VERSION_FILE
        app.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        app.ACCESS_CODE = ""
        app.SHARE_BASE_URL = ""
        app.now_ts = self.fake_now
        app.VERSION_FILE = Path(self.temp_dir.name) / "VERSION"
        app.VERSION_FILE.write_text("9.9.9", encoding="utf-8")
        self.current_time = 1_700_000_000.0
        app.ensure_upload_dir()
        reset_app_state()

    def tearDown(self) -> None:
        reset_app_state()
        app.UPLOAD_DIR = self.original_upload_dir
        app.ACCESS_CODE = self.original_access_code
        app.SHARE_BASE_URL = self.original_share_base_url
        app.now_ts = self.original_now_ts
        app.VERSION_FILE = self.original_version_file
        self.temp_dir.cleanup()

    def fake_now(self) -> float:
        return self.current_time

    def test_snapshot_contains_latest_text_and_file_history(self) -> None:
        app.add_text_entry("first", sharer_name="Alice", sharer_ip="192.168.1.10")
        self.current_time += 5
        app.add_text_entry("second", sharer_name="Bob", sharer_ip="192.168.1.11")
        app.add_file(
            "hello.txt",
            "stored-hello.txt",
            12,
            sharer_name="Bob",
            sharer_ip="192.168.1.11",
        )

        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["latest_text"], "second")
        self.assertEqual(len(snapshot["texts"]), 2)
        self.assertEqual(snapshot["texts"][0]["content"], "second")
        self.assertEqual(snapshot["texts"][0]["sharer_name"], "Bob")
        self.assertEqual(snapshot["texts"][0]["sharer_ip"], "192.168.1.11")
        self.assertEqual(len(snapshot["texts"][0]["short_code"]), 4)
        self.assertEqual(len(snapshot["files"]), 1)
        self.assertEqual(snapshot["files"][0]["name"], "hello.txt")
        self.assertEqual(snapshot["files"][0]["sharer_name"], "Bob")
        self.assertEqual(snapshot["files"][0]["sharer_ip"], "192.168.1.11")
        self.assertEqual(len(snapshot["files"][0]["short_code"]), 4)
        self.assertEqual(snapshot["expires_after_seconds"], app.EXPIRY_SECONDS)

    def test_app_version_comes_from_version_file_or_env(self) -> None:
        self.assertEqual(app.get_app_version(), "9.9.9")
        original_value = os.environ.get("APP_VERSION")
        os.environ["APP_VERSION"] = "2.3.4"
        try:
            self.assertEqual(app.get_app_version(), "2.3.4")
        finally:
            if original_value is None:
                os.environ.pop("APP_VERSION", None)
            else:
                os.environ["APP_VERSION"] = original_value

    def test_text_entries_can_be_marked_hidden(self) -> None:
        app.add_text_entry("secret", hidden=True)

        snapshot = app.get_snapshot()

        self.assertTrue(snapshot["texts"][0]["hidden"])
        self.assertEqual(snapshot["latest_text"], "secret")

    def test_password_protected_text_is_masked_in_snapshot(self) -> None:
        app.add_text_entry("secret value", hidden=True, password="open-sesame")

        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["latest_text"], "")
        self.assertTrue(snapshot["texts"][0]["hidden"])
        self.assertTrue(snapshot["texts"][0]["password_required"])
        self.assertIsNone(snapshot["texts"][0]["content"])
        self.assertEqual(snapshot["texts"][0]["masked_content"], "****** *****")

    def test_text_snapshot_contains_plain_text_only(self) -> None:
        app.add_text_entry("Hello world", sharer_name="Alice")

        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["texts"][0]["content"], "Hello world")
        self.assertEqual(snapshot["texts"][0]["sharer_name"], "Alice")
        self.assertNotIn("rich", snapshot["texts"][0])
        self.assertNotIn("content_html", snapshot["texts"][0])

    def test_delete_file_entry_removes_file_from_disk(self) -> None:
        target = app.UPLOAD_DIR / "stored.txt"
        target.write_text("payload", encoding="utf-8")
        app.add_file("original.txt", "stored.txt", target.stat().st_size)
        file_id = app.get_snapshot()["files"][0]["id"]

        deleted = app.delete_file_entry(file_id)

        self.assertTrue(deleted)
        self.assertFalse(target.exists())
        self.assertEqual(app.get_snapshot()["files"], [])

    def test_expired_entries_are_removed_and_expired_files_deleted(self) -> None:
        expired_file = app.UPLOAD_DIR / "old.txt"
        expired_file.write_text("old", encoding="utf-8")
        app.add_text_entry("old text")
        app.add_file("old.txt", "old.txt", expired_file.stat().st_size)
        self.current_time += app.EXPIRY_SECONDS + 1

        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["texts"], [])
        self.assertEqual(snapshot["files"], [])
        self.assertFalse(expired_file.exists())

    def test_text_history_is_capped_at_200_newest_entries(self) -> None:
        for index in range(app.MAX_TEXT_HISTORY + 5):
            app.add_text_entry(f"text-{index}")
            self.current_time += 1

        snapshot = app.get_snapshot()

        self.assertEqual(len(snapshot["texts"]), app.MAX_TEXT_HISTORY)
        self.assertEqual(snapshot["texts"][0]["content"], "text-204")
        self.assertEqual(snapshot["texts"][-1]["content"], "text-5")

    def test_file_history_is_capped_at_100_and_oldest_files_are_deleted(self) -> None:
        oldest_target = None
        newest_target = None

        for index in range(app.MAX_FILE_HISTORY + 3):
            stored_name = f"stored-{index}.txt"
            target = app.UPLOAD_DIR / stored_name
            target.write_text(f"payload-{index}", encoding="utf-8")
            app.add_file(f"original-{index}.txt", stored_name, target.stat().st_size)
            if index == 0:
                oldest_target = target
            if index == app.MAX_FILE_HISTORY + 2:
                newest_target = target
            self.current_time += 1

        snapshot = app.get_snapshot()

        self.assertEqual(len(snapshot["files"]), app.MAX_FILE_HISTORY)
        self.assertEqual(snapshot["files"][0]["name"], "original-102.txt")
        self.assertEqual(snapshot["files"][-1]["name"], "original-3.txt")
        self.assertIsNotNone(oldest_target)
        self.assertFalse(oldest_target.exists())
        self.assertIsNotNone(newest_target)
        self.assertTrue(newest_target.exists())

    def test_file_metadata_is_persisted_and_reloaded_after_restart(self) -> None:
        target = app.UPLOAD_DIR / "persisted.txt"
        target.write_text("payload", encoding="utf-8")

        app.add_file(
            "persisted.txt",
            "persisted.txt",
            target.stat().st_size,
            hidden=True,
            password="vault",
            sharer_name="Laptop",
            sharer_ip="192.168.1.9",
        )
        original_snapshot = app.get_snapshot()
        original_entry = original_snapshot["files"][0]

        with app.state_lock:
            app.shared_state["files"] = []
            app.shared_state["updated_at"] = 0.0

        app.load_persisted_files()

        reloaded_snapshot = app.get_snapshot()
        reloaded_entry = reloaded_snapshot["files"][0]
        self.assertEqual(len(reloaded_snapshot["files"]), 1)
        self.assertEqual(reloaded_entry["id"], original_entry["id"])
        self.assertEqual(reloaded_entry["name"], "persisted.txt")
        self.assertEqual(reloaded_entry["stored_name"], "persisted.txt")
        self.assertTrue(reloaded_entry["hidden"])
        self.assertTrue(reloaded_entry["password_required"])
        self.assertEqual(reloaded_entry["sharer_name"], "Laptop")
        self.assertEqual(reloaded_entry["sharer_ip"], "192.168.1.9")
        self.assertEqual(reloaded_entry["short_code"], original_entry["short_code"])

    def test_reload_skips_missing_files_and_cleans_index(self) -> None:
        target = app.UPLOAD_DIR / "gone.txt"
        target.write_text("payload", encoding="utf-8")
        app.add_file("gone.txt", "gone.txt", target.stat().st_size)
        target.unlink()

        with app.state_lock:
            app.shared_state["files"] = []
            app.shared_state["updated_at"] = 0.0

        app.load_persisted_files()

        self.assertEqual(app.get_snapshot()["files"], [])
        index_payload = json.loads(app.uploads_index_path().read_text(encoding="utf-8"))
        self.assertEqual(index_payload["files"], [])


class HttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.original_upload_dir = app.UPLOAD_DIR
        self.original_access_code = app.ACCESS_CODE
        self.original_share_base_url = app.SHARE_BASE_URL
        self.original_now_ts = app.now_ts
        self.original_version_file = app.VERSION_FILE
        self.current_time = 1_700_100_000.0
        app.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        app.SHARE_BASE_URL = ""
        app.now_ts = self.fake_now
        app.VERSION_FILE = Path(self.temp_dir.name) / "VERSION"
        app.VERSION_FILE.write_text("9.9.9", encoding="utf-8")
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
        app.UPLOAD_DIR = self.original_upload_dir
        app.ACCESS_CODE = self.original_access_code
        app.SHARE_BASE_URL = self.original_share_base_url
        app.now_ts = self.original_now_ts
        app.VERSION_FILE = self.original_version_file
        self.temp_dir.cleanup()

    def fake_now(self) -> float:
        return self.current_time

    def start_server(self, access_code: str = "") -> None:
        app.ACCESS_CODE = access_code
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

    def open_websocket(self, cookie: str | None = None):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            "GET /ws HTTP/1.1",
            f"Host: 127.0.0.1:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {websocket_key}",
            "Sec-WebSocket-Version: 13",
        ]
        if cookie:
            headers.append(f"Cookie: {cookie}")
        request = "\r\n".join(headers) + "\r\n\r\n"
        connection.sendall(request.encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        header_blob, _, remainder = response.partition(b"\r\n\r\n")
        return connection, header_blob.decode("utf-8", errors="replace"), websocket_key, remainder

    def read_websocket_frame(self, connection: socket.socket, buffered: bytes = b"") -> tuple[bytes, bytes]:
        pending = bytearray(buffered)

        def read_exact(length: int) -> bytes:
            while len(pending) < length:
                chunk = connection.recv(4096)
                if not chunk:
                    raise AssertionError("WebSocket connection closed early")
                pending.extend(chunk)
            data = bytes(pending[:length])
            del pending[:length]
            return data

        header = read_exact(2)
        first_byte, second_byte = header
        self.assertEqual(first_byte & 0x0F, 0x1)
        payload_length = second_byte & 0x7F
        if payload_length == 126:
            payload_length = struct.unpack("!H", read_exact(2))[0]
        elif payload_length == 127:
            payload_length = struct.unpack("!Q", read_exact(8))[0]
        return read_exact(payload_length), bytes(pending)

    def upload_request(
        self,
        filename: str,
        content: bytes,
        cookie: str | None = None,
        hidden: bool = False,
        password: str = "",
        name: str = "",
    ):
        boundary = "----LanDropBoundary"
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

    def test_text_file_and_delete_flow_without_auth(self) -> None:
        self.start_server()

        home = self.request("GET", "/")
        self.assertEqual(home["status"], 200)
        self.assertIn("<title>LanDrop</title>", home["text"])
        self.assertIn("v9.9.9", home["text"])

        text_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "shared text", "name": "Laptop"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(text_response["status"], 200)
        text_snapshot = json.loads(text_response["body"])
        text_id = text_snapshot["texts"][0]["id"]
        self.assertEqual(text_snapshot["latest_text"], "shared text")

        latest_text_response = self.request("GET", "/api/latest-text")
        self.assertEqual(latest_text_response["status"], 200)
        latest_text_entry = json.loads(latest_text_response["body"])
        self.assertEqual(latest_text_entry["id"], text_id)
        self.assertEqual(latest_text_entry["content"], "shared text")
        self.assertFalse(latest_text_entry["hidden"])
        self.assertEqual(latest_text_entry["sharer_name"], "Laptop")
        self.assertEqual(latest_text_entry["sharer_ip"], "127.0.0.1")
        text_short_code = latest_text_entry["short_code"]

        hidden_text_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "top secret", "hidden": True, "name": "Carel"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(hidden_text_response["status"], 200)
        hidden_snapshot = json.loads(hidden_text_response["body"])
        self.assertTrue(hidden_snapshot["texts"][0]["hidden"])
        self.assertEqual(hidden_snapshot["texts"][0]["sharer_name"], "Carel")

        upload_response = self.upload_request("note.txt", b"network payload", name="Phone")
        self.assertEqual(upload_response["status"], 200)
        upload_snapshot = json.loads(upload_response["body"])
        file_entry = upload_snapshot["files"][0]
        file_id = file_entry["id"]

        saved_file = app.UPLOAD_DIR / file_entry["stored_name"]
        self.assertTrue(saved_file.exists())

        latest_file_response = self.request("GET", "/api/latest-file")
        self.assertEqual(latest_file_response["status"], 200)
        latest_file_entry = json.loads(latest_file_response["body"])
        self.assertEqual(latest_file_entry["id"], file_id)
        self.assertEqual(latest_file_entry["name"], "note.txt")
        self.assertEqual(latest_file_entry["sharer_name"], "Phone")
        self.assertEqual(latest_file_entry["sharer_ip"], "127.0.0.1")
        file_short_code = latest_file_entry["short_code"]

        latest_file_content_response = self.request("GET", "/api/latest-file/content")
        self.assertEqual(latest_file_content_response["status"], 200)
        self.assertEqual(latest_file_content_response["body"], b"network payload")
        self.assertEqual(
            latest_file_content_response["headers"]["Content-Type"],
            "text/plain",
        )

        shared_text_response = self.request("GET", f"/s/{text_short_code}")
        self.assertEqual(shared_text_response["status"], 200)
        self.assertEqual(shared_text_response["body"], b"shared text")
        self.assertEqual(
            shared_text_response["headers"]["Content-Type"],
            "text/plain; charset=utf-8",
        )

        shared_file_response = self.request("GET", f"/s/{file_short_code}")
        self.assertEqual(shared_file_response["status"], 200)
        self.assertEqual(shared_file_response["body"], b"network payload")
        self.assertEqual(shared_file_response["headers"]["Content-Type"], "text/plain")

        download_response = self.request("GET", f"/download/{file_id}")
        self.assertEqual(download_response["status"], 200)
        self.assertEqual(download_response["body"], b"network payload")
        self.assertEqual(download_response["headers"]["Content-Type"], "text/plain")

        delete_text_response = self.request("DELETE", f"/api/text/{text_id}")
        self.assertEqual(delete_text_response["status"], 200)
        remaining_texts = json.loads(delete_text_response["body"])["texts"]
        self.assertEqual(len(remaining_texts), 1)
        self.assertEqual(remaining_texts[0]["content"], "top secret")

        delete_file_response = self.request("DELETE", f"/api/file/{file_id}")
        self.assertEqual(delete_file_response["status"], 200)
        self.assertEqual(json.loads(delete_file_response["body"])["files"], [])
        self.assertFalse(saved_file.exists())

    def test_configured_share_base_url_is_rendered_into_page(self) -> None:
        app.SHARE_BASE_URL = "http://192.168.1.24:8000/"
        self.start_server()

        home = self.request("GET", "/")

        self.assertEqual(home["status"], 200)
        self.assertIn('const configuredShareBaseUrl = "http://192.168.1.24:8000";', home["text"])

    def test_text_share_returns_plain_text(self) -> None:
        self.start_server()

        create_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "Hello world"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(create_response["status"], 200)
        created_entry = json.loads(create_response["body"])["texts"][0]
        self.assertEqual(created_entry["content"], "Hello world")

        latest_text_response = self.request("GET", "/api/latest-text")
        self.assertEqual(latest_text_response["status"], 200)
        latest_text_entry = json.loads(latest_text_response["body"])
        self.assertEqual(latest_text_entry["content"], "Hello world")

        shared_text_response = self.request("GET", f"/s/{created_entry['short_code']}")
        self.assertEqual(shared_text_response["status"], 200)
        self.assertEqual(
            shared_text_response["headers"]["Content-Type"],
            "text/plain; charset=utf-8",
        )
        self.assertEqual(shared_text_response["body"], b"Hello world")

    def test_share_text_endpoint_returns_compact_share_payload(self) -> None:
        self.start_server()

        response = self.request(
            "POST",
            "/api/share-text",
            body=json.dumps({"text": "shell text", "name": "CLI"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response["status"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["content"], "shell text")
        self.assertEqual(payload["share_path"], f"/s/{payload['short_code']}")
        self.assertEqual(
            payload["share_url"],
            f"http://127.0.0.1:{self.port}{payload['share_path']}",
        )

    def test_share_file_endpoint_returns_compact_share_payload(self) -> None:
        self.start_server()

        boundary = "----LanDropShareBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="cli.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello from bash"
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n\r\n'
            "CLI"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        response = self.request(
            "POST",
            "/api/share-file",
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )

        self.assertEqual(response["status"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["type"], "file")
        self.assertEqual(payload["name"], "cli.txt")
        self.assertEqual(payload["share_path"], f"/s/{payload['short_code']}")
        self.assertEqual(
            payload["share_url"],
            f"http://127.0.0.1:{self.port}{payload['share_path']}",
        )
        self.assertEqual(
            payload["download_url"],
            f"http://127.0.0.1:{self.port}/download/{payload['id']}",
        )

    def test_share_endpoints_accept_x_api_key_when_access_code_is_enabled(self) -> None:
        self.start_server(access_code="secret-code")

        text_response = self.request(
            "POST",
            "/api/share-text",
            body=json.dumps({"text": "shell text"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-Key": "secret-code",
            },
        )
        self.assertEqual(text_response["status"], 200)
        text_payload = json.loads(text_response["body"])
        self.assertEqual(text_payload["type"], "text")
        self.assertEqual(text_payload["content"], "shell text")

        boundary = "----LanDropApiKeyBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="cli.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello from bash"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        file_response = self.request(
            "POST",
            "/api/share-file",
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "X-API-Key": "secret-code",
            },
        )
        self.assertEqual(file_response["status"], 200)
        file_payload = json.loads(file_response["body"])
        self.assertEqual(file_payload["type"], "file")
        self.assertEqual(file_payload["name"], "cli.txt")

    def test_text_update_rejects_non_boolean_hidden_flag(self) -> None:
        self.start_server()

        response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "shared text", "hidden": "yes"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response["status"], 400)

    def test_password_protected_text_requires_reveal_password(self) -> None:
        self.start_server()

        create_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps(
                {"text": "classified", "hidden": True, "password": "swordfish"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(create_response["status"], 200)
        created_entry = json.loads(create_response["body"])["texts"][0]
        self.assertTrue(created_entry["password_required"])
        self.assertIsNone(created_entry["content"])

        latest_text_response = self.request("GET", "/api/latest-text")
        self.assertEqual(latest_text_response["status"], 200)
        latest_text_entry = json.loads(latest_text_response["body"])
        self.assertIsNone(latest_text_entry["content"])
        self.assertTrue(latest_text_entry["password_required"])

        wrong_reveal = self.request(
            "POST",
            f"/api/text/{created_entry['id']}/reveal",
            body=json.dumps({"password": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(wrong_reveal["status"], 403)

        reveal = self.request(
            "POST",
            f"/api/text/{created_entry['id']}/reveal",
            body=json.dumps({"password": "swordfish"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(reveal["status"], 200)
        self.assertEqual(json.loads(reveal["body"])["content"], "classified")

        blocked_share = self.request("GET", f"/s/{created_entry['short_code']}")
        self.assertEqual(blocked_share["status"], 403)

        allowed_share = self.request(
            "GET", f"/s/{created_entry['short_code']}?password=swordfish"
        )
        self.assertEqual(allowed_share["status"], 200)
        self.assertEqual(allowed_share["body"], b"classified")

    def test_hidden_file_requires_password_for_upload_and_download(self) -> None:
        self.start_server()

        missing_password_upload = self.upload_request("locked.txt", b"secret", hidden=True)
        self.assertEqual(missing_password_upload["status"], 400)

        upload_response = self.upload_request(
            "locked.txt", b"secret", hidden=True, password="vault"
        )
        self.assertEqual(upload_response["status"], 200)
        file_entry = json.loads(upload_response["body"])["files"][0]
        self.assertTrue(file_entry["password_required"])
        self.assertTrue(file_entry["hidden"])

        blocked_download = self.request("GET", f"/download/{file_entry['id']}")
        self.assertEqual(blocked_download["status"], 403)

        allowed_download = self.request(
            "GET", f"/download/{file_entry['id']}?password=vault"
        )
        self.assertEqual(allowed_download["status"], 200)
        self.assertEqual(allowed_download["body"], b"secret")

        blocked_share = self.request("GET", f"/s/{file_entry['short_code']}")
        self.assertEqual(blocked_share["status"], 403)

        allowed_share = self.request(
            "GET", f"/s/{file_entry['short_code']}?password=vault"
        )
        self.assertEqual(allowed_share["status"], 200)
        self.assertEqual(allowed_share["body"], b"secret")

    def test_access_code_is_enforced_and_login_unlocks_api(self) -> None:
        self.start_server(access_code="secret-code")

        home = self.request("GET", "/")
        self.assertEqual(home["status"], 200)
        self.assertIn("Access Code", home["text"])

        unauthorized = self.request("GET", "/api/state")
        self.assertEqual(unauthorized["status"], 401)

        wrong_login = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(wrong_login["status"], 401)

        login = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "secret-code"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(login["status"], 200)
        cookie = login["headers"]["Set-Cookie"].split(";", 1)[0]

        authorized_state = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(authorized_state["status"], 200)

        protected_latest_text_missing = self.request(
            "GET",
            "/api/latest-text",
            headers={"Cookie": cookie},
        )
        self.assertEqual(protected_latest_text_missing["status"], 404)

        protected_upload = self.upload_request("secure.txt", b"secure-data", cookie=cookie)
        self.assertEqual(protected_upload["status"], 200)
        file_id = json.loads(protected_upload["body"])["files"][0]["id"]

        protected_latest_file = self.request(
            "GET",
            "/api/latest-file",
            headers={"Cookie": cookie},
        )
        self.assertEqual(protected_latest_file["status"], 200)
        self.assertEqual(json.loads(protected_latest_file["body"])["id"], file_id)

        protected_latest_file_content = self.request(
            "GET",
            "/api/latest-file/content",
            headers={"Cookie": cookie},
        )
        self.assertEqual(protected_latest_file_content["status"], 200)
        self.assertEqual(protected_latest_file_content["body"], b"secure-data")

        protected_download = self.request(
            "GET",
            f"/download/{file_id}",
            headers={"Cookie": cookie},
        )
        self.assertEqual(protected_download["status"], 200)
        self.assertEqual(protected_download["body"], b"secure-data")

    def test_websocket_receives_initial_snapshot_and_live_updates(self) -> None:
        self.start_server()

        websocket, handshake, websocket_key, buffered = self.open_websocket()
        self.addCleanup(websocket.close)

        self.assertIn("101 Switching Protocols", handshake)
        expected_accept = app.websocket_accept_value(websocket_key)
        self.assertIn(f"Sec-WebSocket-Accept: {expected_accept}", handshake)

        initial_frame, buffered = self.read_websocket_frame(websocket, buffered)
        initial_snapshot = json.loads(initial_frame.decode("utf-8"))
        self.assertEqual(initial_snapshot["texts"], [])
        self.assertEqual(initial_snapshot["files"], [])

        text_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "live update"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(text_response["status"], 200)

        pushed_frame, buffered = self.read_websocket_frame(websocket, buffered)
        pushed_snapshot = json.loads(pushed_frame.decode("utf-8"))
        self.assertEqual(pushed_snapshot["texts"][0]["content"], "live update")

    def test_websocket_requires_authorization_when_access_code_is_enabled(self) -> None:
        self.start_server(access_code="secret-code")

        unauthorized_socket, handshake, _, _ = self.open_websocket()
        self.addCleanup(unauthorized_socket.close)
        self.assertIn("401", handshake)

        login = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "secret-code"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(login["status"], 200)
        cookie = login["headers"]["Set-Cookie"].split(";", 1)[0]

        authorized_socket, authorized_handshake, websocket_key, _ = self.open_websocket(
            cookie=cookie
        )
        self.addCleanup(authorized_socket.close)
        self.assertIn("101 Switching Protocols", authorized_handshake)
        expected_accept = app.websocket_accept_value(websocket_key)
        self.assertIn(f"Sec-WebSocket-Accept: {expected_accept}", authorized_handshake)

    def test_websocket_accepts_x_api_key_when_access_code_is_enabled(self) -> None:
        self.start_server(access_code="secret-code")

        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(connection.close)
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /ws HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "X-API-Key: secret-code\r\n\r\n"
        )
        connection.sendall(request.encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
        handshake = response.partition(b"\r\n\r\n")[0].decode("utf-8", errors="replace")
        self.assertIn("101 Switching Protocols", handshake)

    def test_latest_endpoints_return_not_found_when_history_is_empty(self) -> None:
        self.start_server()

        latest_text = self.request("GET", "/api/latest-text")
        self.assertEqual(latest_text["status"], 404)

        latest_file = self.request("GET", "/api/latest-file")
        self.assertEqual(latest_file["status"], 404)

        latest_file_content = self.request("GET", "/api/latest-file/content")
        self.assertEqual(latest_file_content["status"], 404)

    def test_short_link_returns_not_found_when_code_is_missing(self) -> None:
        self.start_server()

        response = self.request("GET", "/s/ABCD")

        self.assertEqual(response["status"], 404)

    def test_expired_file_is_removed_from_disk_when_state_is_read(self) -> None:
        self.start_server()

        upload_response = self.upload_request("old.txt", b"old-data")
        self.assertEqual(upload_response["status"], 200)
        file_entry = json.loads(upload_response["body"])["files"][0]
        saved_file = app.UPLOAD_DIR / file_entry["stored_name"]
        self.assertTrue(saved_file.exists())

        self.current_time += app.EXPIRY_SECONDS + 1
        state_response = self.request("GET", "/api/state")

        self.assertEqual(state_response["status"], 200)
        snapshot = json.loads(state_response["body"])
        self.assertEqual(snapshot["files"], [])
        self.assertFalse(saved_file.exists())


class ScriptTests(unittest.TestCase):
    def test_bash_api_help_doc_mentions_share_endpoints(self) -> None:
        doc = (
            Path(__file__).resolve().parent / "docs" / "bash-api.md"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/share-text", doc)
        self.assertIn("/api/share-file", doc)
        self.assertIn("curl", doc)

    def test_developer_guide_mentions_versioning_and_main_rule(self) -> None:
        doc = (
            Path(__file__).resolve().parent / "docs" / "developer-guide.md"
        ).read_text(encoding="utf-8")
        self.assertIn("VERSION", doc)
        self.assertIn("Versions roll up when committing to `main`.", doc)

    def test_readme_and_license_cover_local_control_and_isc_license(self) -> None:
        root = Path(__file__).resolve().parent
        readme = (root / "README.md").read_text(encoding="utf-8")
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "1.0.4")
        self.assertIn("you control the app", readme)
        self.assertIn("know where the data lives", readme)
        self.assertIn("Developer: Carel Vosloo", readme)
        self.assertIn("ISC license", readme)
        self.assertIn("ISC License", license_text)
        self.assertIn("Copyright (c) 2026 Carel Vosloo", license_text)

    def test_github_install_upgrade_script_uses_github_archive_and_env_file(self) -> None:
        script = (
            Path(__file__).resolve().parent / "github-install-upgrade.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--port", script)
        self.assertIn("https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${ref}.tar.gz", script)
        self.assertIn('bash "${SOURCE_DIR}/install-ubuntu-service.sh"', script)
        self.assertIn('if [[ ! -f "${ENV_FILE}" ]]; then', script)
        self.assertIn('export "${key}=${value}"', script)

    def test_github_install_upgrade_script_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "github-install-upgrade.sh"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_script_deploys_assets_and_templates(self) -> None:
        script = (
            Path(__file__).resolve().parent / "install-ubuntu-service.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--port", script)
        self.assertIn('SCRIPT_DIR}/VERSION', script)
        self.assertIn('APP_DIR}/VERSION', script)
        self.assertIn('SCRIPT_DIR}/assets', script)
        self.assertIn('APP_DIR}/assets', script)
        self.assertIn('SCRIPT_DIR}/templates', script)
        self.assertIn('APP_DIR}/templates', script)

    def test_install_script_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "install-ubuntu-service.sh"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_script_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "uninstall-ubuntu-service.sh"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
