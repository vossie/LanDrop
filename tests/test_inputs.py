from pathlib import Path

import app

from tests.support import CoreStateTestCase, make_app_handler


class InputValidationTests(CoreStateTestCase):
    def test_create_workspace_truncates_very_long_name(self) -> None:
        workspace = app.create_workspace("W" * 400)

        self.assertEqual(len(workspace["name"]), 80)
        self.assertEqual(workspace["slug"], "w" * 80)

    def test_text_request_accepts_long_text_without_truncating_content(self) -> None:
        long_text = "A" * 50000
        app.add_text_entry(long_text)

        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["texts"][0]["content"], long_text)
        self.assertEqual(snapshot["latest_text"], long_text)

    def test_text_request_trims_surrounding_whitespace_only(self) -> None:
        body = b'{"text":"  keep middle  text  ","name":"  Carel  "}'
        handler = make_app_handler(
            headers={"Content-Length": str(len(body))},
            body=body,
        )

        parsed = handler.parse_text_request()

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["text"], "keep middle  text")
        self.assertEqual(parsed["name"], "Carel")

    def test_text_request_rejects_non_string_name(self) -> None:
        body = b'{"text":"hello","name":false}'
        handler = make_app_handler(
            headers={"Content-Length": str(len(body))},
            body=body,
        )

        parsed = handler.parse_text_request()

        self.assertIsNone(parsed)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Name must be a string")

    def test_file_upload_parser_rejects_missing_boundary(self) -> None:
        handler = make_app_handler(
            headers={"Content-Type": "multipart/form-data", "Content-Length": "5"},
            body=b"hello",
        )

        parsed = handler.parse_file_upload_request()

        self.assertIsNone(parsed)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Missing multipart boundary")

    def test_file_upload_parser_rejects_hidden_file_without_password(self) -> None:
        boundary = "----DassieDropBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "payload"
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="hidden"\r\n\r\n'
            "true"
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

        self.assertIsNone(parsed)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Hidden files require a password")
        if parsed is not None:
            Path(parsed["temp_path"]).unlink(missing_ok=True)
