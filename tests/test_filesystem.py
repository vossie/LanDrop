from dassiedrop import config

import app
from tests.support import CoreStateTestCase


class FilesystemTests(CoreStateTestCase):
    def test_filenames_are_sanitised(self) -> None:
        self.assertEqual(app.sanitize_filename("../unsafe.txt"), "unsafe.txt")
        self.assertEqual(app.sanitize_filename("..\\unsafe.txt"), "unsafe.txt")
        self.assertEqual(app.sanitize_filename(""), "upload.bin")

    def test_uploads_cannot_escape_designated_storage_directory(self) -> None:
        stored_name = app.unique_filename("../unsafe.txt")
        target = config.UPLOAD_DIR / stored_name
        self.assertEqual(target.parent, config.UPLOAD_DIR)

    def test_delete_flow_removes_item_from_listing_and_disk(self) -> None:
        target = config.UPLOAD_DIR / "delete-me.txt"
        target.write_bytes(b"payload")
        app.add_file("delete-me.txt", "delete-me.txt", target.stat().st_size)

        file_id = app.get_snapshot()["files"][0]["id"]
        self.assertTrue(app.delete_file_entry(file_id))
        self.assertFalse(target.exists())
        self.assertEqual(app.get_snapshot()["files"], [])

    def test_subsequent_fetch_returns_not_found(self) -> None:
        target = config.UPLOAD_DIR / "gone.txt"
        target.write_bytes(b"payload")
        app.add_file("gone.txt", "gone.txt", target.stat().st_size)
        file_id = app.get_snapshot()["files"][0]["id"]
        app.delete_file_entry(file_id)

        self.assertIsNone(app.find_file_entry(file_id))
