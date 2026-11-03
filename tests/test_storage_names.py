from pathlib import Path
import tempfile
import unittest

import test_project_manager as fixtures
import test_project_core as core_fixtures
from baton.errors import ProjectError
from baton.storage_names import registry_directory


class RegistryPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "baton"

    def test_missing_registry_is_resolved_without_writes(self) -> None:
        self.assertEqual(registry_directory(self.root), self.directory)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_registry_directory_is_preserved(self) -> None:
        self.directory.mkdir()
        identity = self.directory.stat().st_ino
        self.assertEqual(registry_directory(self.root), self.directory)
        self.assertEqual(self.directory.stat().st_ino, identity)

    def test_files_and_symlinks_are_refused_without_changes(self) -> None:
        self.directory.write_text("unexpected file")
        with self.assertRaisesRegex(ProjectError, "unexpected registry"):
            registry_directory(self.root)
        self.assertEqual(self.directory.read_text(), "unexpected file")
        self.directory.unlink()
        for target in ("missing", str(self.root)):
            with self.subTest(target=target):
                self.directory.symlink_to(target)
                with self.assertRaises(ProjectError):
                    registry_directory(self.root)
                self.assertEqual(self.directory.readlink(), Path(target))
                self.directory.unlink()


class NewStorageDefaultsTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    make_repo = fixtures.ProjectManagerTest.make_repo
    cli = fixtures.ProjectManagerTest.cli
    default_environment = core_fixtures.GenericDefaultsTest.default_environment

    def test_new_init_creates_canonical_registry_and_tracked_marker(self) -> None:
        self.default_environment()
        self.cli("init")
        registry = self.projects / ".git/baton"
        self.assertTrue(registry.is_dir())
        self.assertFalse(registry.is_symlink())
        marker = self.artifacts.parent / ".baton-artifacts"
        self.assertTrue(marker.is_file())
        self.assertFalse(marker.is_symlink())
        modes = {line.split()[3]: line.split()[0] for line in self.git(
            self.artifacts.parent, "ls-files", "--stage", "--", ".baton-artifacts").splitlines()}
        self.assertEqual(modes, {".baton-artifacts": "100644"})
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        self.cli("init")
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain", "--untracked-files=all"), "")
        self.make_repo("alpha")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3")
        directory = fixtures.artifact_path(self.projects, self.artifacts)
        for name in ("blackboard.sqlite", "blackboard-lock.md"):
            path = directory / name
            self.assertTrue(path.is_file())
            self.assertEqual(self.git(self.artifacts, "check-ignore", "--no-index", str(path)), str(path))
