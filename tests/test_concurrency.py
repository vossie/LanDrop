import json
import threading

import app
from dassiedrop import config

from tests.support import CoreHttpTestCase, CoreStateTestCase


class ConcurrencyStateTests(CoreStateTestCase):
    def test_concurrent_text_inserts_preserve_all_entries_and_unique_ids(self) -> None:
        barrier = threading.Barrier(8)
        errors = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                app.add_text_entry(f"text-{index}", sharer_name=f"Client-{index}")
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        snapshot = app.get_snapshot()
        self.assertEqual(len(snapshot["texts"]), 8)
        self.assertEqual(len({item["id"] for item in snapshot["texts"]}), 8)
        self.assertEqual(len({item["short_code"] for item in snapshot["texts"]}), 8)
        self.assertEqual({item["content"] for item in snapshot["texts"]}, {f"text-{i}" for i in range(8)})

    def test_concurrent_file_inserts_preserve_all_entries_and_unique_short_codes(self) -> None:
        barrier = threading.Barrier(6)
        errors = []

        def worker(index: int) -> None:
            target = config.UPLOAD_DIR / f"file-{index}.txt"
            target.write_text(f"payload-{index}", encoding="utf-8")
            try:
                barrier.wait(timeout=5)
                app.add_file(
                    f"file-{index}.txt",
                    f"file-{index}.txt",
                    target.stat().st_size,
                    sharer_name=f"Client-{index}",
                )
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        snapshot = app.get_snapshot()
        self.assertEqual(len(snapshot["files"]), 6)
        self.assertEqual(len({item["id"] for item in snapshot["files"]}), 6)
        self.assertEqual(len({item["short_code"] for item in snapshot["files"]}), 6)


class ConcurrencyHttpTests(CoreHttpTestCase):
    def test_concurrent_http_text_posts_all_land_in_history(self) -> None:
        self.start_server()
        barrier = threading.Barrier(8)
        results = []
        errors = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                response = self.request(
                    "POST",
                    "/api/text",
                    body=json.dumps({"text": f"payload-{index}", "name": f"Client-{index}"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                results.append(response["status"])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 8)
        snapshot = json.loads(self.request("GET", "/api/state")["body"])
        self.assertEqual(len(snapshot["texts"]), 8)
        self.assertEqual(
            {item["content"] for item in snapshot["texts"]},
            {f"payload-{index}" for index in range(8)},
        )

    def test_concurrent_http_file_uploads_all_land_in_history(self) -> None:
        self.start_server()
        barrier = threading.Barrier(5)
        results = []
        errors = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                response = self.upload_request(
                    f"upload-{index}.txt",
                    f"payload-{index}".encode("utf-8"),
                    name=f"Client-{index}",
                )
                results.append(response["status"])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 5)
        snapshot = json.loads(self.request("GET", "/api/state")["body"])
        self.assertEqual(len(snapshot["files"]), 5)
        self.assertEqual(
            {item["name"] for item in snapshot["files"]},
            {f"upload-{index}.txt" for index in range(5)},
        )
