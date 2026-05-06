import json

import app
from dassiedrop import config, state

from tests.support import CoreStateTestCase


class PersistenceTests(CoreStateTestCase):
    def test_text_entries_survive_reload(self) -> None:
        workspace = app.create_workspace("Ops Room", password="vault")
        app.add_text_entry("default text", sharer_name="Phone")
        app.add_text_entry("workspace text", hidden=True, password="vault", workspace_id=workspace["id"])

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        reloaded_default = app.get_snapshot()
        reloaded_ops = app.get_snapshot(workspace["id"])
        self.assertEqual(reloaded_default["texts"][0]["content"], "default text")
        self.assertEqual(reloaded_ops["texts"][0]["masked_content"], "*****")
        self.assertTrue(reloaded_ops["texts"][0]["password_required"])

    def test_multiple_workspaces_and_files_survive_reload(self) -> None:
        default_target = config.UPLOAD_DIR / "default.txt"
        default_target.write_text("default payload", encoding="utf-8")
        workspace = app.create_workspace("Ops Room", password="vault")
        workspace_target = config.UPLOAD_DIR / "ops.txt"
        workspace_target.write_text("ops payload", encoding="utf-8")

        app.add_file("default.txt", "default.txt", default_target.stat().st_size, sharer_name="Phone")
        app.add_file(
            "ops.txt",
            "ops.txt",
            workspace_target.stat().st_size,
            hidden=True,
            password="vault",
            sharer_name="Laptop",
            workspace_id=workspace["id"],
        )

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        reloaded_default = app.get_snapshot()
        reloaded_ops = app.get_snapshot(workspace["id"])
        self.assertEqual(reloaded_default["files"][0]["name"], "default.txt")
        self.assertEqual(reloaded_ops["workspace"]["slug"], "ops-room")
        self.assertEqual(reloaded_ops["files"][0]["name"], "ops.txt")
        self.assertTrue(reloaded_ops["files"][0]["password_required"])

    def test_corrupted_index_recovers_to_default_workspace(self) -> None:
        app.uploads_index_path().write_text("{not-json", encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        listed = app.list_workspaces()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], app.DEFAULT_WORKSPACE_ID)

    def test_corrupted_metadata_fields_are_ignored_without_crashing_reload(self) -> None:
        target = config.UPLOAD_DIR / "kept.txt"
        target.write_text("payload", encoding="utf-8")
        payload = {
            "workspaces": [
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "slug": "ops-room",
                    "created_at": "not-a-float",
                    "updated_at": "also-bad",
                    "last_used_at": "still-bad",
                    "texts": [
                        {
                            "id": "text123",
                            "content": "alpha",
                            "hidden": False,
                            "short_code": "ABCD",
                            "created_at": "broken",
                            "expires_at": "broken-too",
                        }
                    ],
                    "files": [
                        {
                            "id": "file123",
                            "name": "kept.txt",
                            "stored_name": "kept.txt",
                            "size": "abc",
                            "hidden": False,
                            "short_code": "EFGH",
                            "created_at": "broken",
                            "expires_at": "broken-too",
                        }
                    ],
                }
            ]
        }
        app.uploads_index_path().write_text(json.dumps(payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        snapshot = app.get_snapshot("ops123")
        self.assertEqual(snapshot["texts"][0]["content"], "alpha")
        self.assertEqual(snapshot["files"][0]["name"], "kept.txt")
        self.assertEqual(snapshot["files"][0]["size"], target.stat().st_size)

    def test_negative_persisted_file_size_falls_back_to_actual_file_size(self) -> None:
        target = config.UPLOAD_DIR / "negative.txt"
        target.write_text("payload", encoding="utf-8")
        payload = {
            "workspaces": [
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "slug": "ops-room",
                    "created_at": self.current_time,
                    "updated_at": self.current_time,
                    "last_used_at": self.current_time,
                    "texts": [],
                    "files": [
                        {
                            "id": "file123",
                            "name": "negative.txt",
                            "stored_name": "negative.txt",
                            "size": -5,
                            "hidden": False,
                            "short_code": "EFGH",
                            "created_at": self.current_time,
                            "expires_at": self.current_time + app.EXPIRY_SECONDS,
                        }
                    ],
                }
            ]
        }
        app.uploads_index_path().write_text(json.dumps(payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        snapshot = app.get_snapshot("ops123")
        self.assertEqual(snapshot["files"][0]["size"], target.stat().st_size)

    def test_non_finite_persisted_timestamps_fall_back_to_defaults(self) -> None:
        target = config.UPLOAD_DIR / "finite.txt"
        target.write_text("payload", encoding="utf-8")
        payload = {
            "workspaces": [
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "slug": "ops-room",
                    "created_at": "NaN",
                    "updated_at": "Infinity",
                    "last_used_at": "-Infinity",
                    "texts": [
                        {
                            "id": "text123",
                            "content": "alpha",
                            "hidden": False,
                            "short_code": "ABCD",
                            "created_at": "NaN",
                            "expires_at": "Infinity",
                        }
                    ],
                    "files": [
                        {
                            "id": "file123",
                            "name": "finite.txt",
                            "stored_name": "finite.txt",
                            "size": 7,
                            "hidden": False,
                            "short_code": "EFGH",
                            "created_at": "-Infinity",
                            "expires_at": "NaN",
                        }
                    ],
                }
            ]
        }
        app.uploads_index_path().write_text(json.dumps(payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        snapshot = app.get_snapshot("ops123")
        self.assertEqual(snapshot["texts"][0]["created_at"], self.current_time)
        self.assertEqual(
            snapshot["texts"][0]["expires_at"],
            self.current_time + app.EXPIRY_SECONDS,
        )
        self.assertEqual(snapshot["files"][0]["created_at"], self.current_time)
        self.assertEqual(
            snapshot["files"][0]["expires_at"],
            self.current_time + app.EXPIRY_SECONDS,
        )

    def test_reload_ignores_unknown_workspace_records_and_repairs_index(self) -> None:
        stray = config.UPLOAD_DIR / "kept.txt"
        stray.write_text("payload", encoding="utf-8")
        broken_payload = {
            "workspaces": [
                "invalid",
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "password_hash": None,
                    "created_at": self.current_time,
                    "updated_at": self.current_time,
                    "last_used_at": self.current_time,
                    "files": [
                        {
                            "id": "file123",
                            "name": "../../kept.txt",
                            "stored_name": "kept.txt",
                            "size": 7,
                            "hidden": False,
                            "short_code": "A1B2",
                            "created_at": self.current_time,
                            "expires_at": self.current_time + app.EXPIRY_SECONDS,
                        }
                    ],
                },
            ]
        }
        app.uploads_index_path().write_text(json.dumps(broken_payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        snapshot = app.get_snapshot("ops123")
        self.assertEqual(snapshot["files"][0]["name"], "kept.txt")
        repaired = json.loads(app.uploads_index_path().read_text(encoding="utf-8"))
        self.assertEqual(len(repaired["workspaces"]), 2)

    def test_workspace_deletion_removes_persisted_file_artifacts(self) -> None:
        workspace = app.create_workspace("Delete Room", password="vault")
        target = config.UPLOAD_DIR / "delete-room.txt"
        target.write_text("payload", encoding="utf-8")
        app.add_file(
            "delete-room.txt",
            "delete-room.txt",
            target.stat().st_size,
            workspace_id=workspace["id"],
        )

        deleted, message = app.delete_workspace(workspace["id"], password="vault")

        self.assertTrue(deleted)
        self.assertEqual(message, "")
        self.assertFalse(target.exists())
        persisted = json.loads(app.uploads_index_path().read_text(encoding="utf-8"))
        self.assertNotIn(workspace["id"], {item["id"] for item in persisted["workspaces"]})

    def test_duplicate_workspace_names_get_unique_stable_slugs_after_reload(self) -> None:
        first = app.create_workspace("Ops Room")
        second = app.create_workspace("Ops-Room")

        self.assertEqual(first["slug"], "ops-room")
        self.assertEqual(second["slug"], "ops-room-2")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()
        listed = {
            workspace["id"]: workspace["slug"]
            for workspace in app.list_workspaces()
            if workspace["id"] in {first["id"], second["id"]}
        }
        self.assertEqual(listed[first["id"]], "ops-room")
        self.assertEqual(listed[second["id"]], "ops-room-2")

    def test_workspace_named_default_does_not_collide_with_built_in_default_slug_after_reload(self) -> None:
        created = app.create_workspace("Default")
        self.assertEqual(created["slug"], "default-2")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0
            state.shared_state["reserved_upload_names"] = set()

        app.load_persisted_workspaces()

        listed = {workspace["id"]: workspace["slug"] for workspace in app.list_workspaces()}
        self.assertEqual(listed[app.DEFAULT_WORKSPACE_ID], "default")
        self.assertEqual(listed[created["id"]], "default-2")

    def test_duplicate_restored_short_codes_are_repaired_on_reload(self) -> None:
        payload = {
            "workspaces": [
                {
                    "id": app.DEFAULT_WORKSPACE_ID,
                    "name": app.DEFAULT_WORKSPACE_NAME,
                    "slug": "default",
                    "created_at": 0.0,
                    "updated_at": self.current_time,
                    "last_used_at": self.current_time,
                    "texts": [
                        {
                            "id": "text-1",
                            "content": "alpha",
                            "hidden": False,
                            "short_code": "ABCD",
                            "created_at": self.current_time,
                            "expires_at": self.current_time + app.EXPIRY_SECONDS,
                        }
                    ],
                    "files": [],
                },
                {
                    "id": "ops123",
                    "name": "Ops Room",
                    "slug": "ops-room",
                    "created_at": self.current_time,
                    "updated_at": self.current_time,
                    "last_used_at": self.current_time,
                    "texts": [
                        {
                            "id": "text-2",
                            "content": "bravo",
                            "hidden": False,
                            "short_code": "ABCD",
                            "created_at": self.current_time - 1,
                            "expires_at": self.current_time + app.EXPIRY_SECONDS,
                        }
                    ],
                    "files": [],
                },
            ]
        }
        app.uploads_index_path().write_text(json.dumps(payload), encoding="utf-8")

        with state.state_lock:
            state.shared_state["workspaces"] = {}
            state.shared_state["reserved_upload_bytes"] = 0

        app.load_persisted_workspaces()

        default_snapshot = app.get_snapshot()
        ops_snapshot = app.get_snapshot("ops123")
        self.assertEqual(default_snapshot["texts"][0]["short_code"], "ABCD")
        self.assertNotEqual(ops_snapshot["texts"][0]["short_code"], "ABCD")
