import app

from tests.support import CoreStateTestCase


class TextTests(CoreStateTestCase):
    def test_uploading_text_snippet_stores_and_returns_content(self) -> None:
        app.add_text_entry("shared text", sharer_name="Laptop", sharer_ip="127.0.0.1")

        snapshot = app.get_snapshot()
        self.assertEqual(snapshot["latest_text"], "shared text")
        self.assertEqual(snapshot["texts"][0]["content"], "shared text")
        self.assertEqual(snapshot["texts"][0]["sharer_name"], "Laptop")

    def test_text_drop_is_visible_via_latest_entry_lookup(self) -> None:
        app.add_text_entry("latest drop")

        latest = app.get_latest_text_entry()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["content"], "latest drop")
