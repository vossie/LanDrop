import json

import app
from dassiedrop import config, storage

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

    def test_parse_json_body_rejects_non_object_payloads(self) -> None:
        handler = make_app_handler(
            headers={"Content-Length": "10"},
            body=b"[1, 2, 3]",
        )

        payload = handler.parse_json_body()

        self.assertIsNone(payload)
        self.assertEqual(handler.error_status, 400)
        self.assertEqual(handler.error_message, "Invalid JSON")


class SecurityHttpTests(CoreHttpTestCase):
    def test_upload_with_path_traversal_filename_stays_inside_upload_dir(self) -> None:
        self.start_server()

        response = self.upload_request("../../escape.txt", b"payload", name="Phone")

        self.assertEqual(response["status"], 200)
        file_entry = json.loads(response["body"])["files"][0]
        saved_path = config.UPLOAD_DIR / file_entry["stored_name"]
        self.assertEqual(file_entry["name"], "escape.txt")
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.parent, config.UPLOAD_DIR)
