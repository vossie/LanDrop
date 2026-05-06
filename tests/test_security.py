import json
import threading
from pathlib import Path

import app
from dassiedrop import auth, config, state, storage

from tests.support import CoreHttpTestCase, CoreStateTestCase, make_app_handler


class SecurityTests(CoreStateTestCase):
    def test_workspace_name_is_sanitised_and_truncated(self) -> None:
        raw_name = "  <script>alert(1)</script>    " + ("Ops " * 40)

        workspace = app.create_workspace(raw_name)

        self.assertLessEqual(len(workspace["name"]), 80)
        self.assertEqual(workspace["name"], storage.sanitize_workspace_name(raw_name))
        self.assertEqual(workspace["slug"], storage.workspace_slug(workspace["name"]))

    def test_filename_sanitiser_strips_absolute_paths_and_windows_drives(self) -> None:
        self.assertEqual(app.sanitize_filename("/etc/passwd"), "passwd")
        self.assertEqual(app.sanitize_filename("C:\\temp\\report.pdf"), "report.pdf")
        self.assertEqual(app.sanitize_filename("..\\..\\secret.txt\x00.png"), "secret.txt.png")

    def test_password_hashes_are_salted_and_verify(self) -> None:
        first = storage.hash_password("vault")
        second = storage.hash_password("vault")

        self.assertNotEqual(first, second)
        self.assertTrue(storage.verify_password("vault", first))
        self.assertFalse(storage.verify_password("wrong", first))

    def test_legacy_sha256_hashes_still_verify(self) -> None:
        legacy = __import__("hashlib").sha256(b"vault").hexdigest()

        self.assertTrue(storage.verify_password("vault", legacy))
        self.assertFalse(storage.verify_password("wrong", legacy))

    def test_file_upload_parser_normalises_dodgy_filename(self) -> None:
        boundary = "----DassieDropBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="../../etc/passwd.txt"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "payload"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        handler = make_app_handler(
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            body=body,
        )

        parsed = handler.parse_file_upload_request()

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["filename"], "passwd.txt")
        Path(parsed["temp_path"]).unlink(missing_ok=True)

    def test_parse_json_body_rejects_non_object_payloads(self) -> None:
        handler = make_app_handler(
            headers={"Content-Length": "10"},
            body=b"[1, 2, 3]",
        )

        payload = handler.parse_json_body()

        self.assertIsNone(payload)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Invalid JSON")

    def test_throttle_is_isolated_per_client(self) -> None:
        first = make_app_handler(client_address=("127.0.0.1", 10001))
        second = make_app_handler(client_address=("127.0.0.2", 10002))

        for _ in range(config.AUTH_MAX_FAILURES):
            auth.record_throttle_failure(first, "login")

        allowed_first, _ = auth.throttle_status(first, "login")
        allowed_second, _ = auth.throttle_status(second, "login")

        self.assertFalse(allowed_first)
        self.assertTrue(allowed_second)

    def test_throttle_scopes_are_isolated(self) -> None:
        handler = make_app_handler(client_address=("127.0.0.1", 10001))

        for _ in range(config.AUTH_MAX_FAILURES):
            auth.record_throttle_failure(handler, "login")

        login_allowed, _ = auth.throttle_status(handler, "login")
        reveal_allowed, _ = auth.throttle_status(handler, "text-reveal", "entry-1")

        self.assertFalse(login_allowed)
        self.assertTrue(reveal_allowed)

    def test_success_can_clear_accumulated_failures(self) -> None:
        handler = make_app_handler(client_address=("127.0.0.1", 10001))

        for _ in range(config.AUTH_MAX_FAILURES - 1):
            auth.record_throttle_failure(handler, "login")

        allowed_before, _ = auth.throttle_status(handler, "login")
        auth.clear_throttle_failures(handler, "login")
        allowed_after, _ = auth.throttle_status(handler, "login")

        self.assertTrue(allowed_before)
        self.assertTrue(allowed_after)

    def test_cleanup_throttle_failures_removes_expired_entries(self) -> None:
        handler = make_app_handler(client_address=("127.0.0.1", 10001))
        auth.record_throttle_failure(handler, "login")

        self.current_time += config.AUTH_FAILURE_WINDOW_SECONDS + 1
        auth.cleanup_throttle_failures()

        self.assertEqual(state.auth_attempts, {})

    def test_close_workspace_clients_removes_clients_without_deadlock(self) -> None:
        class DummyClient:
            def __init__(self, workspace_id: str) -> None:
                self.workspace_id = workspace_id
                self.closed = False

            def close(self) -> None:
                self.closed = True

        keep = DummyClient("keep")
        close_one = DummyClient("close")
        close_two = DummyClient("close")
        with state.websocket_lock:
            state.websocket_clients = {keep, close_one, close_two}

        app.close_workspace_clients("close")

        self.assertTrue(close_one.closed)
        self.assertTrue(close_two.closed)
        self.assertFalse(keep.closed)
        self.assertEqual(state.websocket_clients, {keep})

    def test_client_ip_ignores_spoofed_forwarded_for_header(self) -> None:
        handler = make_app_handler(
            headers={"X-Forwarded-For": "203.0.113.10"},
            client_address=("127.0.0.1", 12345),
        )

        self.assertEqual(auth.client_ip(handler), "127.0.0.1")

    def test_concurrent_failures_trigger_single_shared_lockout(self) -> None:
        handler = make_app_handler(client_address=("127.0.0.1", 10001))

        def record_failure() -> None:
            auth.record_throttle_failure(handler, "login")

        threads = [threading.Thread(target=record_failure) for _ in range(config.AUTH_MAX_FAILURES + 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        allowed, retry_after = auth.throttle_status(handler, "login")
        key = auth.throttle_key(handler, "login")
        attempt = state.auth_attempts[key]

        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)
        self.assertEqual(attempt["failures"], [])


class SecurityHttpTests(CoreHttpTestCase):
    def test_cookie_backed_mutations_require_csrf_token(self) -> None:
        self.start_server()
        cookie = self.request("GET", "/workspaces")["headers"]["Set-Cookie"].split(";", 1)[0]

        blocked = self.request(
            "POST",
            "/api/workspaces",
            body=json.dumps({"name": "Ops Room", "password": ""}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": "http://127.0.0.1",
            },
        )

        self.assertEqual(blocked["status"], 403)

    def test_cookie_backed_mutations_accept_valid_csrf_token(self) -> None:
        self.start_server()
        page = self.request("GET", "/workspaces")
        cookie = page["headers"]["Set-Cookie"].split(";", 1)[0]
        marker = 'csrfToken: "'
        token = page["text"].split(marker, 1)[1].split('"', 1)[0]

        allowed = self.request(
            "POST",
            "/api/workspaces",
            body=json.dumps({"name": "Ops Room", "password": ""}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": "http://127.0.0.1",
                "X-CSRF-Token": token,
            },
        )

        self.assertEqual(allowed["status"], 200)

    def test_upload_is_rejected_when_global_storage_quota_is_exceeded(self) -> None:
        self.start_server()
        config.MAX_TOTAL_STORAGE_BYTES = 3

        response = self.upload_request("quota.txt", b"payload", name="Phone")

        self.assertEqual(response["status"], 507)

    def test_upload_with_path_traversal_filename_stays_inside_upload_dir(self) -> None:
        self.start_server()

        response = self.upload_request("../../escape.txt", b"payload", name="Phone")

        self.assertEqual(response["status"], 200)
        file_entry = json.loads(response["body"])["files"][0]
        saved_path = config.UPLOAD_DIR / app.find_file_entry(file_entry["id"])["stored_name"]
        self.assertEqual(file_entry["name"], "escape.txt")
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.parent, config.UPLOAD_DIR)

    def test_persisted_out_of_tree_file_reference_is_ignored(self) -> None:
        self.start_server()
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        payload = {
            "workspaces": [
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "created_at": self.current_time,
                    "updated_at": self.current_time,
                    "last_used_at": self.current_time,
                    "files": [
                        {
                            "id": "file123",
                            "name": "outside.txt",
                            "stored_name": "../outside.txt",
                            "size": 6,
                            "hidden": False,
                            "short_code": "ABCD",
                            "created_at": self.current_time,
                            "expires_at": self.current_time + app.EXPIRY_SECONDS,
                        }
                    ],
                }
            ]
        }
        app.uploads_index_path().write_text(json.dumps(payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
        app.load_persisted_workspaces()

        snapshot = app.get_snapshot("ops123")
        self.assertEqual(snapshot["files"], [])

    def test_asset_symlink_outside_assets_dir_is_rejected(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        asset_dir = Path(self.temp_dir.name) / "assets"
        asset_dir.mkdir()
        link = asset_dir / "escape.txt"
        link.symlink_to(outside)
        original_assets_dir = config.ASSETS_DIR
        config.ASSETS_DIR = asset_dir
        try:
            handler = make_app_handler(path="/assets/escape.txt")
            handler.serve_asset("escape.txt")
            self.assertEqual(handler.error_status, 404)
        finally:
            config.ASSETS_DIR = original_assets_dir

    def test_login_is_rate_limited_after_repeated_wrong_access_codes(self) -> None:
        self.start_server(access_code="secret-code")

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request(
                "POST",
                "/login",
                body=json.dumps({"code": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response["status"], 401)

        throttled = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(throttled["status"], 429)
        self.assertEqual(throttled["headers"]["Retry-After"], str(config.AUTH_LOCKOUT_SECONDS))

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "secret-code"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(recovered["status"], 200)

    def test_text_reveal_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()

        created = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "classified", "hidden": True, "password": "vault"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(created["status"], 200)
        entry_id = json.loads(created["body"])["texts"][0]["id"]

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request(
                "POST",
                f"/api/text/{entry_id}/reveal",
                body=json.dumps({"password": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response["status"], 403)

        throttled = self.request(
            "POST",
            f"/api/text/{entry_id}/reveal",
            body=json.dumps({"password": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "POST",
            f"/api/text/{entry_id}/reveal",
            body=json.dumps({"password": "vault"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(recovered["status"], 200)
        self.assertEqual(json.loads(recovered["body"])["content"], "classified")

    def test_workspace_entry_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()
        workspace = app.create_workspace("Vault Room", password="vault")
        cookie = self.request("GET", "/")["headers"]["Set-Cookie"].split(";", 1)[0]

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request(
                "POST",
                f"/api/workspaces/{workspace['id']}/enter",
                body=json.dumps({"password": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            self.assertEqual(response["status"], 403)

        throttled = self.request(
            "POST",
            f"/api/workspaces/{workspace['id']}/enter",
            body=json.dumps({"password": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "POST",
            f"/api/workspaces/{workspace['id']}/enter",
            body=json.dumps({"password": "vault"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        self.assertEqual(recovered["status"], 200)

    def test_retry_after_decreases_during_lockout_window(self) -> None:
        self.start_server(access_code="secret-code")

        for _ in range(config.AUTH_MAX_FAILURES):
            self.request(
                "POST",
                "/login",
                body=json.dumps({"code": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        blocked_now = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(blocked_now["status"], 429)

        self.current_time += 2
        blocked_later = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(blocked_later["status"], 429)
        self.assertLess(int(blocked_later["headers"]["Retry-After"]), int(blocked_now["headers"]["Retry-After"]))

    def test_login_success_resets_failure_window(self) -> None:
        self.start_server(access_code="secret-code")

        for _ in range(config.AUTH_MAX_FAILURES - 1):
            response = self.request(
                "POST",
                "/login",
                body=json.dumps({"code": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response["status"], 401)

        success = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "secret-code"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(success["status"], 200)

        for _ in range(config.AUTH_MAX_FAILURES - 1):
            response = self.request(
                "POST",
                "/login",
                body=json.dumps({"code": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response["status"], 401)

        not_blocked_yet = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(not_blocked_yet["status"], 401)

        blocked = self.request(
            "POST",
            "/login",
            body=json.dumps({"code": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(blocked["status"], 429)

    def test_query_string_passwords_no_longer_unlock_protected_text_or_files(self) -> None:
        self.start_server()

        text_response = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "classified", "hidden": True, "password": "swordfish"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        text_entry = json.loads(text_response["body"])["texts"][0]

        file_response = self.upload_request("locked.txt", b"secret", hidden=True, password="vault")
        file_entry = json.loads(file_response["body"])["files"][0]

        self.assertEqual(self.request("GET", f"/s/{text_entry['short_code']}?password=swordfish")["status"], 403)
        self.assertEqual(self.request("GET", f"/download/{file_entry['id']}?password=vault")["status"], 403)
        self.assertEqual(self.request("GET", f"/preview/{file_entry['id']}?password=vault")["status"], 403)
        self.assertEqual(self.request("GET", f"/s/{file_entry['short_code']}?password=vault")["status"], 403)

    def test_query_string_workspace_password_no_longer_unlocks_workspace_api(self) -> None:
        self.start_server()
        app.create_workspace("Secure Space", password="vault")

        response = self.request("GET", "/api/state?workspace=secure-space&workspace_password=vault")

        self.assertEqual(response["status"], 403)

    def test_short_link_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()
        upload_response = self.upload_request("locked.txt", b"secret", hidden=True, password="vault")
        file_entry = json.loads(upload_response["body"])["files"][0]

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request("GET", f"/s/{file_entry['short_code']}")
            self.assertEqual(response["status"], 403)

        throttled = self.request("GET", f"/s/{file_entry['short_code']}")
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "GET",
            f"/s/{file_entry['short_code']}",
            headers={"X-Entry-Password": "vault"},
        )
        self.assertEqual(recovered["status"], 200)

    def test_download_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()
        upload_response = self.upload_request("locked.txt", b"secret", hidden=True, password="vault")
        file_entry = json.loads(upload_response["body"])["files"][0]

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request("GET", f"/download/{file_entry['id']}")
            self.assertEqual(response["status"], 403)

        throttled = self.request("GET", f"/download/{file_entry['id']}")
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "GET",
            f"/download/{file_entry['id']}",
            headers={"X-Entry-Password": "vault"},
        )
        self.assertEqual(recovered["status"], 200)

    def test_preview_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()
        upload_response = self.upload_request("locked.txt", b"secret", hidden=True, password="vault")
        file_entry = json.loads(upload_response["body"])["files"][0]

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request("GET", f"/preview/{file_entry['id']}")
            self.assertEqual(response["status"], 403)

        throttled = self.request("GET", f"/preview/{file_entry['id']}")
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        recovered = self.request(
            "GET",
            f"/preview/{file_entry['id']}",
            headers={"X-Entry-Password": "vault"},
        )
        self.assertEqual(recovered["status"], 200)

    def test_workspace_delete_is_rate_limited_after_repeated_wrong_passwords(self) -> None:
        self.start_server()
        workspace = app.create_workspace("Delete Vault", password="vault")

        for _ in range(config.AUTH_MAX_FAILURES):
            response = self.request(
                "DELETE",
                f"/api/workspaces/{workspace['id']}",
                body=json.dumps({"password": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response["status"], 403)

        throttled = self.request(
            "DELETE",
            f"/api/workspaces/{workspace['id']}",
            body=json.dumps({"password": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(throttled["status"], 429)

        self.current_time += config.AUTH_LOCKOUT_SECONDS + 1
        deleted = self.request(
            "DELETE",
            f"/api/workspaces/{workspace['id']}",
            body=json.dumps({"password": "vault"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(deleted["status"], 200)

    def test_malformed_password_headers_fail_cleanly(self) -> None:
        self.start_server()
        upload_response = self.upload_request("locked.txt", b"secret", hidden=True, password="vault")
        file_entry = json.loads(upload_response["body"])["files"][0]

        for header_value in ["", "   ", "vault,another", "vault wrong", "X" * 8192]:
            response = self.request(
                "GET",
                f"/download/{file_entry['id']}",
                headers={"X-Entry-Password": header_value},
            )
            self.assertIn(response["status"], {403, 429})
