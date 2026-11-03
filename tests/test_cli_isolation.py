"""The packaged CLI operates on its own registry without cross-installation routing."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_project_manager as fixtures
from baton.status import StatusMixin


class CLIIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.new = fixtures.ProjectManagerTest()
        self.new.setUp()
        self.addCleanup(self.new.doCleanups)
        self.old = fixtures.ProjectManagerTest()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        for fixture, name in ((self.new, "new-project"), (self.old, "old-project")):
            fixture.make_repo("alpha")
            fixture.create("alpha", name=name)

    def run_project(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        root = fixtures.SCRIPT.parent.parent
        code = (
            "import importlib, pathlib, sys, tomllib; "
            f"root = pathlib.Path({str(root)!r}); sys.path.insert(0, str(root)); "
            "entry = tomllib.loads((root / 'pyproject.toml').read_text())['project']['scripts']['baton']; "
            "module, name = entry.split(':'); "
            "sys.exit(getattr(importlib.import_module(module), name)())"
        )
        result = subprocess.run([sys.executable, "-c", code, *args], cwd=cwd or self.new.root,
                                env=self.new.env, capture_output=True, text=True)
        return result

    def test_console_lists_and_selects_only_its_own_registry(self) -> None:
        result = self.run_project("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new-project", result.stdout)
        self.assertNotIn("old-project", result.stdout)
        for selector in ("p3", "new-project"):
            result = self.run_project("status", selector, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["name"], "new-project")
        result = self.run_project("status", "old-project", "--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_foreign_worktree_is_outside_this_framework(self) -> None:
        before = fixtures.state_file(self.old.projects).read_bytes()
        result = self.run_project("status", "--json", cwd=self.old.slots / "p3")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, "")
        result = self.run_project("path", "alpha", cwd=self.old.checkout("alpha", "old-project"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixtures.state_file(self.old.projects).read_bytes(), before)

    def test_mutations_and_repository_path_stay_in_this_registry(self) -> None:
        before = fixtures.state_file(self.old.projects).read_bytes()
        generation = fixtures.read_state(self.new.projects)["projects"]["p3"]["blackboard"]["generation"]
        result = self.run_project("blackboard", "post", "--selector", "p3", "--agent", "test",
                                  "--expected-generation", generation, "--text", "new-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new-only", self.new.cli("blackboard", "read", "--selector", "new-project").stdout)
        self.assertNotIn("new-only", self.old.cli("blackboard", "read", "--selector", "old-project").stdout)
        result = self.run_project("path", "alpha", cwd=self.new.slots / "p3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.new.checkout("alpha", "new-project"))
        self.assertEqual(fixtures.state_file(self.old.projects).read_bytes(), before)

    def test_foreign_names_do_not_block_create(self) -> None:
        self.new.ensure_slot("p4")
        before = fixtures.state_file(self.old.projects).read_bytes()
        result = self.run_project("create", "old-project", "--repos", "alpha", cwd=self.new.slots / "p4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.run_project("status", "p4", "--json").stdout)["name"], "old-project")
        self.assertEqual(fixtures.state_file(self.old.projects).read_bytes(), before)

    def test_foreign_names_do_not_block_rename(self) -> None:
        before = fixtures.state_file(self.old.projects).read_bytes()
        result = self.run_project("rename", "old-project", "new-project")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixtures.state_file(self.old.projects).read_bytes(), before)


class ResumeCommandTest(unittest.TestCase):
    def test_generated_resume_commands_use_stable_project_names(self) -> None:
        manager = object.__new__(StatusMixin)
        manager.slots = Path("/unused/slots")
        project = {"slot": "p3", "name": "example"}
        for state, pending, expected in (
            ("cleaning", {}, "baton cleanup example"),
            ("renaming", {"pending_rename": {"name": "renamed"}}, "baton rename renamed example"),
            ("branching", {"pending_member": {"action": "add", "repo": "alpha", "name": "api"}},
             "baton branch add alpha api --selector example"),
            ("retiring", {"pending_retirement": {"repo": "alpha", "member": "api"}},
             "baton branch cleanup alpha api --selector example"),
        ):
            with self.subTest(state=state):
                self.assertEqual(manager.resume_command({**project, "state": state, **pending}), expected)


if __name__ == "__main__":
    unittest.main()
