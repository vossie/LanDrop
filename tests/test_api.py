import json
from pathlib import Path

from dassiedrop import config

import app
from tests.support import CoreHttpTestCase, CoreStateTestCase, make_app_handler


class ApiTests(CoreStateTestCase):
    def test_workspace_creation_and_text_drop_flow(self) -> None:
        workspace = app.create_workspace("QA Room", password="vault")
        app.add_text_entry("shell text", workspace_id=workspace["id"])

        snapshot = app.get_snapshot(workspace["id"])
        self.assertEqual(snapshot["workspace"]["slug"], "qa-room")
        self.assertEqual(snapshot["texts"][0]["content"], "shell text")

    def test_invalid_requests_return_proper_errors(self) -> None:
        handler = make_app_handler(
            headers={"Content-Length": "33"},
            body=json.dumps({"text": "shared", "hidden": "yes"}).encode("utf-8"),
        )
        result = handler.parse_text_request()

        self.assertIsNone(result)
        self.assertEqual(handler.error_status, 400)

    def test_parse_json_body_rejects_invalid_content_length(self) -> None:
        handler = make_app_handler(
            headers={"Content-Length": "abc"},
            body=b"{}",
        )

        result = handler.parse_json_body()

        self.assertIsNone(result)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Invalid Content-Length")

    def test_upload_parser_enforces_size_limit(self) -> None:
        handler = make_app_handler(
            headers={
                "Content-Type": "multipart/form-data; boundary=----TooBig",
                "Content-Length": str(config.MAX_FILE_SIZE + 1),
            },
            body=b"",
        )
        result = handler.parse_file_upload_request()

        self.assertIsNone(result)
        self.assertEqual(handler.error_status, 413)

    def test_upload_parser_rejects_invalid_content_length(self) -> None:
        handler = make_app_handler(
            headers={
                "Content-Type": "multipart/form-data; boundary=----BadLength",
                "Content-Length": "wat",
            },
            body=b"",
        )

        result = handler.parse_file_upload_request()

        self.assertIsNone(result)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Invalid Content-Length")

    def test_api_responses_are_consistent_and_parseable(self) -> None:
        payload = app.json_bytes({"ok": True, "type": "text"})
        parsed = json.loads(payload.decode("utf-8"))

        self.assertEqual(parsed["ok"], True)
        self.assertEqual(parsed["type"], "text")

    def test_curl_style_share_payload_works_end_to_end(self) -> None:
        app.add_text_entry("cli text")
        entry = app.find_text_entry(app.get_snapshot()["texts"][0]["id"])
        payload = app.share_payload("text", entry, "http://127.0.0.1:8000")

        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["content"], "cli text")
        self.assertIn("/s/", payload["share_path"])
        self.assertIn("http://127.0.0.1:8000", payload["share_url"])

    def test_share_payload_contract_for_text_is_stable(self) -> None:
        app.add_text_entry("contract text", sharer_name="Phone")
        entry = app.find_text_entry(app.get_snapshot()["texts"][0]["id"])

        payload = app.share_payload("text", entry, "http://127.0.0.1:8000")

        self.assertEqual(
            set(payload.keys()),
            {
                "type",
                "id",
                "short_code",
                "share_path",
                "share_url",
                "hidden",
                "password_required",
                "created_at",
                "expires_at",
                "workspace_id",
                "workspace_name",
                "workspace_slug",
                "workspace_path",
                "workspace_url",
                "content",
            },
        )

    def test_snapshot_contract_has_expected_top_level_keys(self) -> None:
        snapshot = app.get_snapshot()

        self.assertEqual(
            set(snapshot.keys()),
            {
                "workspace",
                "updated_at",
                "expires_after_seconds",
                "latest_text",
                "texts",
                "files",
            },
        )

    def test_upload_parser_spools_file_to_disk_without_returning_bytes(self) -> None:
        boundary = "----DassieDropBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
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

        result = handler.parse_file_upload_request()

        self.assertIsNotNone(result)
        self.assertNotIn("file_bytes", result)
        temp_path = Path(result["temp_path"])
        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_bytes(), b"payload")
        temp_path.unlink(missing_ok=True)


class ApiContractHttpTests(CoreHttpTestCase):
    def test_root_page_sets_content_security_policy_header(self) -> None:
        self.start_server()

        response = self.request("GET", "/workspaces")

        self.assertEqual(response["status"], 200)
        self.assertIn("Content-Security-Policy", response["headers"])
        self.assertIn("default-src 'self'", response["headers"]["Content-Security-Policy"])
        self.assertIn('<meta name="dassiedrop-csrf-token" content="', response["text"])
        self.assertNotIn("window.LANDROP_CONFIG", response["text"])

    def test_root_page_renders_configured_share_base_url_in_meta_tag(self) -> None:
        config.SHARE_BASE_URL = "https://share.example.test/base"
        self.start_server()

        page = self.request("GET", "/workspaces")
        cookie = page["headers"]["Set-Cookie"].split(";", 1)[0]
        token = page["text"].split('<meta name="dassiedrop-csrf-token" content="', 1)[1].split('"', 1)[0]

        enter = self.request(
            "POST",
            "/api/workspaces/default/enter",
            body=json.dumps({"password": ""}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-CSRF-Token": token,
            },
        )
        self.assertEqual(enter["status"], 200)

        page = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(page["status"], 200)
        self.assertIn(
            '<meta name="dassiedrop-share-base-url" content="https://share.example.test/base">',
            page["text"],
        )

    def test_api_state_contract_headers_and_keys_are_stable(self) -> None:
        self.start_server()
        self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "hello", "name": "Phone"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        response = self.request("GET", "/api/state")
        payload = json.loads(response["body"])

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(
            set(payload.keys()),
            {"workspace", "updated_at", "expires_after_seconds", "latest_text", "texts", "files"},
        )
        self.assertEqual(
            set(payload["workspace"].keys()),
            {"id", "name", "slug", "path", "password_required", "created_at", "updated_at", "text_count", "file_count"},
        )

    def test_latest_text_contract_headers_and_keys_are_stable(self) -> None:
        self.start_server()
        self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "contract", "name": "Phone"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        response = self.request("GET", "/api/latest-text")
        payload = json.loads(response["body"])

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(
            set(payload.keys()),
            {"id", "hidden", "password_required", "sharer_name", "sharer_ip", "short_code", "created_at", "expires_at", "masked_content", "content"},
        )

    def test_latest_file_contract_headers_and_keys_are_stable(self) -> None:
        self.start_server()
        upload = self.upload_request("contract.txt", b"payload", name="Phone")
        self.assertEqual(upload["status"], 200)

        response = self.request("GET", "/api/latest-file")
        payload = json.loads(response["body"])

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(
            set(payload.keys()),
            {"id", "name", "content_type", "size", "hidden", "password_required", "sharer_name", "sharer_ip", "short_code", "created_at", "expires_at"},
        )

    def test_download_contract_headers_are_stable(self) -> None:
        self.start_server()
        upload = self.upload_request("contract.txt", b"payload", name="Phone")
        file_id = json.loads(upload["body"])["files"][0]["id"]

        response = self.request("GET", f"/download/{file_id}")

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "text/plain")
        self.assertEqual(response["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment;", response["headers"]["Content-Disposition"])
        self.assertEqual(response["body"], b"payload")

    def test_https_responses_include_hsts_header(self) -> None:
        self.start_server()
        self.server.is_https = True

        response = self.request("GET", "/workspaces")

        self.assertEqual(response["status"], 200)
        self.assertIn("Strict-Transport-Security", response["headers"])

    def test_error_responses_include_security_headers(self) -> None:
        self.start_server()
        self.server.is_https = True

        response = self.request("GET", "/missing")

        self.assertEqual(response["status"], 404)
        self.assertEqual(response["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertIn("Strict-Transport-Security", response["headers"])
        self.assertIn("Content-Security-Policy", response["headers"])

    def test_text_share_contract_headers_are_stable(self) -> None:
        self.start_server()
        create = self.request(
            "POST",
            "/api/text",
            body=json.dumps({"text": "share me"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        short_code = json.loads(create["body"])["texts"][0]["short_code"]

        response = self.request("GET", f"/s/{short_code}")

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(response["body"], b"share me")

    def test_lan_link_unknown_code_returns_generic_access_denied_json(self) -> None:
        self.start_server(access_code="secret-code", api_key="api-secret")

        response = self.request("GET", "/s/AbC123XyZ9")

        self.assertEqual(response["status"], 401)
        self.assertEqual(response["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(response["body"]), {"message": "Access denied"})
