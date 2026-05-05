from dassiedrop import config

import app
from tests.support import CoreStateTestCase


class UploadTests(CoreStateTestCase):
    def test_uploading_file_stores_and_returns_identical_bytes(self) -> None:
        target = config.UPLOAD_DIR / "note.txt"
        target.write_bytes(b"network payload")
        app.add_file("note.txt", "note.txt", target.stat().st_size, sharer_name="Phone")

        latest = app.get_latest_file_entry()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["name"], "note.txt")
        self.assertEqual(target.read_bytes(), b"network payload")

    def test_multiple_uploads_from_different_clients_are_handled_correctly(self) -> None:
        first_target = config.UPLOAD_DIR / "a.txt"
        second_target = config.UPLOAD_DIR / "b.txt"
        first_target.write_bytes(b"a")
        second_target.write_bytes(b"bb")
        app.add_file("a.txt", "a.txt", first_target.stat().st_size, sharer_name="Phone")
        app.add_file("b.txt", "b.txt", second_target.stat().st_size, sharer_name="Laptop")

        snapshot = app.get_snapshot()
        self.assertEqual(len(snapshot["files"]), 2)
        self.assertEqual(snapshot["files"][0]["name"], "b.txt")
        self.assertEqual(snapshot["files"][0]["sharer_name"], "Laptop")
        self.assertEqual(snapshot["files"][1]["name"], "a.txt")
        self.assertEqual(snapshot["files"][1]["sharer_name"], "Phone")
