import json

from dassiedrop import config

import app
from tests.support import CoreStateTestCase, make_app_handler


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
