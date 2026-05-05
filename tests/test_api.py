import json

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


class ApiContractHttpTests(CoreHttpTestCase):
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
            {"id", "name", "stored_name", "content_type", "size", "hidden", "password_required", "sharer_name", "sharer_ip", "short_code", "created_at", "expires_at"},
        )

    def test_download_contract_headers_are_stable(self) -> None:
        self.start_server()
        upload = self.upload_request("contract.txt", b"payload", name="Phone")
        file_id = json.loads(upload["body"])["files"][0]["id"]

        response = self.request("GET", f"/download/{file_id}")

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "text/plain")
        self.assertIn("attachment;", response["headers"]["Content-Disposition"])
        self.assertEqual(response["body"], b"payload")

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
