import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY = REPO_ROOT / "dist" / ("dassiedrop.exe" if sys.platform == "win32" else "dassiedrop")
_BINARY_PRESENT = BINARY.exists()
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/workspaces")
            if conn.getresponse().status == 200:
                return True
        except OSError:
            pass
        finally:
            conn.close()
        time.sleep(0.1)
    return False


def _http(
    method: str,
    path: str,
    *,
    port: int,
    body: bytes | None = None,
    headers: dict | None = None,
) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    result = {
        "status": resp.status,
        "headers": {k.lower(): v for k, v in resp.getheaders()},
        "body": raw,
        "text": raw.decode("utf-8", errors="replace"),
    }
    conn.close()
    return result


def _upload(port: int, filename: str, content: bytes) -> dict:
    boundary = "----DassieDropBinaryTest"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content
    body += (
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="hidden"\r\n\r\n'
        f"false"
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="password"\r\n\r\n'
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n\r\n'
        f"\r\n--{boundary}--\r\n"
    ).encode()
    return _http(
        "POST",
        "/api/upload",
        port=port,
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )


def _start_binary(upload_dir: Path, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "HTTP_PORT": str(port),
        "HOST": "127.0.0.1",
        "UPLOAD_DIR": str(upload_dir),
        "HTTPS": "",
    }
    return subprocess.Popen(
        [str(BINARY)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_binary(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@unittest.skipUnless(_BINARY_PRESENT, f"binary not found at {BINARY} — run: pyinstaller app.spec")
class BinaryTests(unittest.TestCase):
    """Black-box HTTP tests against the compiled binary.

    A single binary process is shared across all test methods in this class.
    Tests must be tolerant of pre-existing entries created by earlier methods
    since the server state is not reset between tests.
    """

    _proc: subprocess.Popen
    _port: int
    _temp_dir: TemporaryDirectory
    _upload_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dir = TemporaryDirectory()
        cls._upload_dir = Path(cls._temp_dir.name) / "uploads"
        cls._port = _find_free_port()
        cls._proc = _start_binary(cls._upload_dir, cls._port)
        if not _wait_for_server(cls._port):
            _stop_binary(cls._proc)
            raise RuntimeError(
                f"Binary at {BINARY} did not start within 15 s — check stderr above"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_binary(cls._proc)
        cls._temp_dir.cleanup()

    # ------------------------------------------------------------------
    # Startup / static resource checks
    # ------------------------------------------------------------------

    def test_workspaces_page_serves_html(self) -> None:
        r = _http("GET", "/workspaces", port=self._port)
        self.assertEqual(r["status"], 200)
        self.assertIn("text/html", r["headers"].get("content-type", ""))
        self.assertIn("DassieDrop", r["text"])

    def test_assets_are_served(self) -> None:
        r = _http("GET", "/assets/app.js", port=self._port)
        self.assertEqual(r["status"], 200)
        self.assertIn("javascript", r["headers"].get("content-type", ""))

    def test_version_matches_version_file(self) -> None:
        version_file = REPO_ROOT / "VERSION"
        if not version_file.exists():
            self.skipTest("VERSION file not present in repo root")
        expected = version_file.read_text(encoding="utf-8").strip()
        self.assertNotEqual(expected, "", "VERSION file is empty")
        r = _http("GET", "/workspaces", port=self._port)
        self.assertEqual(r["status"], 200)
        self.assertIn(
            expected,
            r["text"],
            f"Version '{expected}' not found in workspaces page — "
            "VERSION file may not have been bundled (check app.spec datas)",
        )

    # ------------------------------------------------------------------
    # Text API
    # ------------------------------------------------------------------

    def test_text_drop_latest_and_short_link(self) -> None:
        text_content = "binary-functional-test-content"
        r = _http(
            "POST",
            "/api/text",
            port=self._port,
            body=json.dumps({"text": text_content}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r["status"], 200)
        snapshot = json.loads(r["body"])
        matching = [t for t in snapshot["texts"] if t.get("content") == text_content]
        self.assertTrue(matching, "Uploaded text not found in snapshot")
        short_code = matching[0]["short_code"]

        latest = _http("GET", "/api/latest-text", port=self._port)
        self.assertEqual(latest["status"], 200)
        self.assertIn("content", json.loads(latest["body"]))

        shared = _http("GET", f"/s/{short_code}", port=self._port)
        self.assertEqual(shared["status"], 200)
        self.assertEqual(shared["body"], text_content.encode())
        self.assertIn("text/plain", shared["headers"].get("content-type", ""))

    # ------------------------------------------------------------------
    # File API
    # ------------------------------------------------------------------

    def test_file_upload_and_download(self) -> None:
        file_content = b"binary-test-file-payload"
        r = _upload(self._port, "test.txt", file_content)
        self.assertEqual(r["status"], 200)
        snapshot = json.loads(r["body"])
        self.assertTrue(snapshot["files"], "File list empty after upload")
        file_id = snapshot["files"][0]["id"]

        dl = _http("GET", f"/download/{file_id}", port=self._port)
        self.assertEqual(dl["status"], 200)
        self.assertEqual(dl["body"], file_content)
        self.assertIn("attachment", dl["headers"].get("content-disposition", ""))

    def test_file_delete_removes_entry(self) -> None:
        r = _upload(self._port, "delete-me.txt", b"to-be-deleted")
        self.assertEqual(r["status"], 200)
        file_id = json.loads(r["body"])["files"][0]["id"]

        self.assertEqual(_http("GET", f"/download/{file_id}", port=self._port)["status"], 200)

        delete_r = _http("DELETE", f"/api/file/{file_id}", port=self._port)
        self.assertEqual(delete_r["status"], 200)

        self.assertEqual(_http("GET", f"/download/{file_id}", port=self._port)["status"], 404)

    def test_upload_dir_receives_file_and_is_not_in_meipass(self) -> None:
        r = _upload(self._port, "dir-check.bin", b"payload-for-dir-check")
        self.assertEqual(r["status"], 200)

        upload_files = [p for p in self._upload_dir.iterdir() if not p.name.startswith(".")]
        self.assertTrue(
            upload_files,
            "No files found in the env-controlled UPLOAD_DIR — "
            "uploads may be going to the ephemeral _MEIPASS temp dir instead",
        )
        for p in upload_files:
            self.assertNotIn(
                "_MEI",
                str(p),
                f"Upload file path contains _MEI (PyInstaller temp dir): {p}",
            )

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def test_websocket_handshake_and_initial_snapshot(self) -> None:
        conn = socket.create_connection(("127.0.0.1", self._port), timeout=5)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self._port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            conn.sendall(request.encode())

            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk

            header_blob = raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
            self.assertIn("101 Switching Protocols", header_blob)

            expected_accept = base64.b64encode(
                hashlib.sha1(f"{key}{_WEBSOCKET_GUID}".encode()).digest()
            ).decode("ascii")
            self.assertIn(f"Sec-WebSocket-Accept: {expected_accept}", header_blob)
        finally:
            conn.close()


@unittest.skipUnless(_BINARY_PRESENT, f"binary not found at {BINARY} — run: pyinstaller app.spec")
class BinaryRestartTests(unittest.TestCase):
    """Tests that start, stop, and restart the binary within the test body.

    These validate that uploads and workspace state written to the
    env-controlled UPLOAD_DIR survive process restarts — the critical
    property that would break if uploads landed in PyInstaller's ephemeral
    _MEIPASS temp directory.
    """

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self._upload_dir = Path(self._temp_dir.name) / "uploads"
        self._procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._procs:
            _stop_binary(proc)
        self._temp_dir.cleanup()

    def _start(self, port: int) -> subprocess.Popen:
        proc = _start_binary(self._upload_dir, port)
        self._procs.append(proc)
        self.assertTrue(_wait_for_server(port), "Binary did not start within 15 s")
        return proc

    def _stop(self, proc: subprocess.Popen) -> None:
        _stop_binary(proc)
        self._procs.remove(proc)

    def test_uploads_persist_across_restart(self) -> None:
        port_a = _find_free_port()
        proc_a = self._start(port_a)

        file_content = b"persist-me-across-restart"
        r = _upload(port_a, "persist.bin", file_content)
        self.assertEqual(r["status"], 200, "Upload failed on first run")
        snapshot = json.loads(r["body"])
        file_id = snapshot["files"][0]["id"]
        short_code = snapshot["files"][0]["short_code"]

        self._stop(proc_a)

        port_b = _find_free_port()
        self._start(port_b)

        dl = _http("GET", f"/download/{file_id}", port=port_b)
        self.assertEqual(
            dl["status"],
            200,
            "File not accessible after restart — UPLOAD_DIR may not be persistent "
            "(uploads may have gone to the ephemeral _MEIPASS temp dir)",
        )
        self.assertEqual(dl["body"], file_content)

        shared = _http("GET", f"/s/{short_code}", port=port_b)
        self.assertEqual(shared["status"], 200)
        self.assertEqual(shared["body"], file_content)

    def test_workspace_index_is_written_to_upload_dir(self) -> None:
        port = _find_free_port()
        self._start(port)

        _http(
            "POST",
            "/api/text",
            port=port,
            body=json.dumps({"text": "index-write-check"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        time.sleep(0.2)

        index_file = self._upload_dir / ".dassiedrop-workspaces.json"
        self.assertTrue(
            index_file.exists(),
            "Workspace index (.dassiedrop-workspaces.json) not written to UPLOAD_DIR — "
            "it may have been written to the ephemeral _MEIPASS temp dir",
        )
        self.assertNotIn(
            "_MEI",
            str(index_file.resolve()),
            "Workspace index path contains _MEI (PyInstaller temp dir)",
        )
        payload = json.loads(index_file.read_text(encoding="utf-8"))
        self.assertIn("workspaces", payload)

    def test_env_file_is_loaded(self) -> None:
        port = _find_free_port()
        env_file = BINARY.parent / "dassiedrop.env"
        env_file.write_text(f"HTTP_PORT={port}\n", encoding="utf-8")
        try:
            env = {
                **os.environ,
                "HOST": "127.0.0.1",
                "UPLOAD_DIR": str(self._upload_dir),
                "HTTPS": "",
            }
            env.pop("HTTP_PORT", None)
            env.pop("PORT", None)
            proc = subprocess.Popen(
                [str(BINARY)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._procs.append(proc)
            self.assertTrue(
                _wait_for_server(port),
                f"Binary did not bind on port {port} from dassiedrop.env — "
                "env file may not be loaded",
            )
        finally:
            env_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
