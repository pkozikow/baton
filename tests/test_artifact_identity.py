import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from baton.errors import ProjectError
from baton.manager import Manager
import test_project_manager as fixtures


class ArtifactIdentityTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    held = fixtures.ProjectManagerTest.held

    def project(self) -> dict:
        return fixtures.read_state(self.projects)["projects"]["p3"]

    def manager(self) -> Manager:
        manager = Manager()
        manager.load()
        return manager


    def test_new_path_uses_uuid_and_stays_stable_on_rename(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        original = self.project()
        directory = self.artifacts / f"example-{original['uuid'][:8]}"
        self.assertEqual(original["artifacts_path"], str(directory))
        by_uuid = json.loads(self.cli("status", original["uuid"], "--json").stdout)
        self.assertEqual(by_uuid["uuid"], original["uuid"])
        board = self.cli("blackboard", "read", "--selector", original["uuid"], "--json")
        self.assertEqual(json.loads(board.stdout)["project"], "example")
        self.assertEqual(json.loads((self.slots / "p3" / "project.json").read_text())["artifacts_path"], str(directory))
        (directory / "plan.md").write_text("Project plan\n")
        self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(directory / "plan.md"))
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        lock = directory / "blackboard-lock.md"
        inode = lock.stat().st_ino
        self.cli("rename", "renamed", "example")
        renamed = self.project()
        self.assertEqual(renamed["artifacts_path"], str(directory))
        self.assertEqual(renamed["blackboard"]["directory"], str(directory))
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        status = json.loads(self.cli("status", "renamed", "--json").stdout)
        self.assertEqual(status["master_path"], str(directory / "plan.md"))
        self.cli("blackboard", "post", "--selector", "renamed", "--agent", "parent", "--expected-generation",
                 renamed["blackboard"]["generation"], "--text", "Continue after rename")
        board = json.loads(self.cli("blackboard", "read", "--selector", "renamed", "--json").stdout)
        self.assertEqual(board["project"], "renamed")
        self.assertIn("Continue after rename", board["text"])



    def test_uuid_selectors_preserve_historical_identity_when_name_is_reused(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        old = self.project()
        self.cli("cleanup", "p3")
        self.create("alpha")
        current = self.project()
        self.assertNotEqual(old["artifacts_path"], current["artifacts_path"])
        with patch.dict(os.environ, self.env, clear=True):
            manager = self.manager()
            self.assertEqual(manager.artifact_project(old["uuid"])["artifacts_path"], old["artifacts_path"])
            self.assertEqual(manager.artifact_project(current["uuid"])["uuid"], current["uuid"])
            self.assertEqual(manager.artifact_project("p3")["uuid"], current["uuid"])
            self.assertEqual(manager.artifact_project("example")["uuid"], current["uuid"])
            self.cli("cleanup", "p3")
            manager.load()
            with self.assertRaisesRegex(ProjectError, "full project UUID"):
                manager.artifact_project("example")

    def test_uuid_collision_never_reuses_existing_symlink_or_tracked_deleted_directory(self) -> None:
        self.make_repo("alpha")
        identity = "12345678" + "0" * 24
        target = self.artifacts / "example-12345678"
        for kind in ("directory", "symlink", "tracked-deleted"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(self.root / "missing", target_is_directory=True)
                else:
                    target.mkdir()
                    if kind == "tracked-deleted":
                        (target / "plan.md").write_text("Reserved artifact\n")
                        self.git(self.artifacts, "add", str(target / "plan.md"))
                        self.git(self.artifacts, "commit", "-m", "Reserve artifact directory")
                        (target / "plan.md").unlink()
                        target.rmdir()
                with patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.slots / "p3"), \
                        patch("baton.lifecycle.uuid.uuid4", return_value=SimpleNamespace(hex=identity)):
                    manager = self.manager()
                    with manager.command_lock(None, create=True):
                        with self.assertRaisesRegex(ProjectError, "Artifact destination"):
                            manager.create("example", ["alpha"])
                self.assertEqual(fixtures.read_state(self.projects)["projects"], {})
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    target.rmdir()

    def test_archived_path_collision_is_refused_even_when_directory_was_removed(self) -> None:
        self.make_repo("alpha")
        archived = {"name": "old", "slot": "p4", "uuid": "a" * 32, "repos": [],
                    "artifacts_path": str(self.artifacts / "example-12345678")}
        archive = self.projects / ".git" / "baton" / "v2" / "archive" / f"000001-{archived['uuid']}.json"
        archive.write_text(json.dumps(archived))
        with patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.slots / "p3"), \
                patch("baton.lifecycle.uuid.uuid4", return_value=SimpleNamespace(hex="12345678" + "0" * 24)):
            manager = self.manager()
            with manager.command_lock(None, create=True):
                with self.assertRaisesRegex(ProjectError, "Artifact destination belongs"):
                    manager.create("example", ["alpha"])
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_live_reservation_blocks_a_matching_artifact_path(self) -> None:
        self.make_repo("alpha")
        identity = "12345678" + "0" * 24
        layout = self.projects / ".git" / "baton" / "v2"
        reservation = layout / "reservations" / "p4.json"
        reservation.write_text(json.dumps({"claims": [["artifacts", str(self.artifacts / "example-12345678")]]}))
        lock = self.projects / ".git" / "baton" / "locks" / "p4.lock"
        with self.held(lock), patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.slots / "p3"), \
                patch("baton.lifecycle.uuid.uuid4", return_value=SimpleNamespace(hex=identity)):
            manager = self.manager()
            with manager.command_lock(None, create=True):
                with self.assertRaisesRegex(ProjectError, "Artifact destination belongs"):
                    manager.create("example", ["alpha"])
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})
