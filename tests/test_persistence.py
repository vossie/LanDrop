import json

import app
from dassiedrop import config, state

from tests.support import CoreStateTestCase


class PersistenceTests(CoreStateTestCase):
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

        app.load_persisted_workspaces()

        listed = app.list_workspaces()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], app.DEFAULT_WORKSPACE_ID)

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
