from dassiedrop import config

import app
from tests.support import CoreStateTestCase


class ExpiryTests(CoreStateTestCase):
    def test_items_expire_after_configured_time(self) -> None:
        target = config.UPLOAD_DIR / "expired.txt"
        target.write_text("payload", encoding="utf-8")
        app.add_text_entry("old text")
        app.add_file("expired.txt", "expired.txt", target.stat().st_size)

        self.current_time += app.EXPIRY_SECONDS + 1
        snapshot = app.get_snapshot()

        self.assertEqual(snapshot["texts"], [])
        self.assertEqual(snapshot["files"], [])

    def test_cleanup_removes_metadata_and_physical_files(self) -> None:
        target = config.UPLOAD_DIR / "cleanup.txt"
        target.write_text("payload", encoding="utf-8")
        app.add_text_entry("drop")
        app.add_file("cleanup.txt", "cleanup.txt", target.stat().st_size)

        self.current_time += app.EXPIRY_SECONDS + 1
        changed = app.prune_expired_entries()

        self.assertIn(app.DEFAULT_WORKSPACE_ID, changed)
        snapshot = app.get_snapshot()
        self.assertEqual(snapshot["texts"], [])
        self.assertEqual(snapshot["files"], [])
        self.assertFalse(target.exists())
