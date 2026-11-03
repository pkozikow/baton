import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_project_manager import artifact_path

import test_project_manager as fixtures

sys.path.insert(0, str(fixtures.SCRIPT.parent.parent))
from baton.manager import Manager
from baton.errors import ProjectError
from baton.manifest import subplan_directory


class ProjectManifestTest(unittest.TestCase):
    def test_subplan_directory_removes_only_leading_personal_branch_prefix(self) -> None:
        directory = Path("projects/example")
        for branch, suffix in (("baton/example", "example"), ("baton/example--api", "example--api"),
                               ("pm/example", "pm/example"), ("pm/example--api", "pm/example--api"),
                               ("team/pm/example", "team/pm/example")):
            with self.subTest(branch=branch):
                self.assertEqual(subplan_directory(directory, "alpha", branch),
                                 directory / "branches" / "alpha" / suffix)


class ProjectRegistryTest(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    spawn = fixtures.ProjectManagerTest.spawn
    held = fixtures.ProjectManagerTest.held
    wait_for = fixtures.ProjectManagerTest.wait_for
    finish = fixtures.ProjectManagerTest.finish

    def setUp(self) -> None:
        fixtures.FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures.FIXTURES)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(BATON_REPO=str(self.projects), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git(self.root, "init", "-b", "main", str(self.projects))
        with patch.dict(os.environ, self.env, clear=True):
            self.manager = Manager()
        self.project = {"slot": "p3", "name": "original", "uuid": "original-id", "state": "active", "repos": [], "artifacts_path": str(self.root / "artifacts")}
        self.directory = self.projects / ".git" / "baton"
        self.manager.build_layout(self.directory / "v2", {"projects": {"p3": self.project}})

    def test_load_without_registry_is_read_only(self) -> None:
        shutil.rmtree(self.directory)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ProjectError, "Baton is not initialized"):
                self.manager.load()
        self.assertFalse(self.directory.exists())

    def test_load_and_list_resolve_registry_twice(self) -> None:
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        with patch.dict(os.environ, self.env, clear=True):
            self.manager.load()
        self.assertEqual(self.manager.state["projects"], {"p3": self.project})
        self.assertEqual(trace.read_text().count("rev-parse --path-format=absolute --git-common-dir"), 2)
        trace.write_text("")
        self.assertIn("p3: original [active]", self.cli("list").stdout)
        self.assertEqual(trace.read_text().count("rev-parse --path-format=absolute --git-common-dir"), 2)

    def test_properties_and_repeated_load_follow_replacement_git_directory(self) -> None:
        replacement = self.root / "replacement"
        self.git(self.root, "init", "-b", "main", str(replacement))
        replacement_directory = replacement / ".git" / "baton"
        replacement_project = {**self.project, "slot": "p4", "name": "replacement", "uuid": "replacement-id"}
        self.manager.build_layout(replacement_directory / "v2", {"projects": {"p4": replacement_project}})
        with patch.dict(os.environ, self.env, clear=True):
            self.manager.load()
            self.assertEqual(self.manager.state_dir, self.directory)
            self.assertEqual(self.manager.layout, self.directory / "v2")
            (self.projects / ".git").rename(self.root / "original.git")
            (self.projects / ".git").write_text(f"gitdir: {replacement / '.git'}\n")
            self.assertEqual(self.manager.state_dir, replacement_directory)
            self.assertEqual(self.manager.layout, replacement_directory / "v2")
            self.manager.load()
        self.assertEqual(self.manager.state["projects"], {"p4": replacement_project})

    def test_reserve_and_archive_resolve_registry_after_locking(self) -> None:
        trace = self.root / "git-trace"
        with patch.dict(os.environ, self.env, clear=True):
            self.manager.load()
            with self.manager.slot_lock("p3"):
                with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
                    self.manager.reserve({("name", "original")})
                self.assertEqual(trace.read_text().count("rev-parse --path-format=absolute --git-common-dir"), 2)
                reservation = self.directory / "v2" / "reservations" / "p3.json"
                self.assertEqual(json.loads(reservation.read_text())["claims"], [["name", "original"]])
                trace.write_text("")
                with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
                    self.manager.archive_project(self.project)
                self.assertEqual(trace.read_text().count("rev-parse --path-format=absolute --git-common-dir"), 2)
        self.assertFalse((self.directory / "v2" / "projects" / "p3.json").exists())
        self.assertEqual(self.manager.state["projects"], {})
        archived = next((self.directory / "v2" / "archive").glob("*.json"))
        self.assertEqual(json.loads(archived.read_text()), self.project)


    def test_registry_writes_reject_replacement_while_waiting_for_lock(self) -> None:
        replacement = self.root / "replacement"
        self.git(self.root, "init", "-b", "main", str(replacement))
        replacement_directory = replacement / ".git" / "baton"
        replacement_project = {**self.project, "name": "replacement", "uuid": "replacement-id"}
        self.manager.build_layout(replacement_directory / "v2", {"projects": {"p3": replacement_project}})
        original_files = {path.relative_to(self.directory): path.read_bytes()
                          for path in (self.directory / "v2").rglob("*.json")}
        replacement_files = {path.relative_to(replacement_directory): path.read_bytes()
                             for path in (replacement_directory / "v2").rglob("*.json")}
        actions = {"save": "manager.save()",
                   "reserve": "manager.reserve({('name', 'original')})",
                   "archive": "manager.archive_project(manager.state['projects']['p3'])"}
        for name, action in actions.items():
            with self.subTest(action=name):
                script = (f"import sys; sys.path.insert(0, {str(fixtures.SCRIPT.parent.parent)!r})\n"
                          "from baton.manager import Manager\n"
                          "manager = Manager()\nmanager.load()\n"
                          f"with manager.slot_lock('p3'):\n    {action}\n")
                log = self.root / f"{name}.stderr"
                with self.held(replacement_directory / "lock"):
                    with self.held(self.directory / "lock"):
                        with log.open("w") as stderr, (self.root / f"{name}.stdout").open("w") as stdout:
                            process = subprocess.Popen([sys.executable, "-c", script], cwd=self.root,
                                                       env=self.env, stdout=stdout, stderr=stderr, text=True)
                        self.addCleanup(lambda process=process: process.poll() is None
                                        and (process.kill(), process.wait()))
                        self.wait_for(log, "waiting for the global project registry lock")
                        (self.projects / ".git").rename(self.root / "original.git")
                        (self.projects / ".git").write_text(f"gitdir: {replacement / '.git'}\n")
                    code, stdout_text, stderr_text = self.finish(process, name)
                self.assertNotEqual(code, 0, stderr_text)
                self.assertIn("registry changed while waiting for its lock", stderr_text)
                self.assertEqual(stdout_text, "")
                original_directory = self.root / "original.git" / "baton"
                self.assertEqual({path.relative_to(original_directory): path.read_bytes()
                                  for path in (original_directory / "v2").rglob("*.json")}, original_files)
                self.assertEqual({path.relative_to(replacement_directory): path.read_bytes()
                                  for path in (replacement_directory / "v2").rglob("*.json")}, replacement_files)
                (self.projects / ".git").unlink()
                (self.root / "original.git").rename(self.projects / ".git")


class GenericDefaultsTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot

    def default_environment(self) -> None:
        self.env = {key: value for key, value in self.env.items() if not key.startswith("BATON_")}
        self.env.pop("XDG_CONFIG_HOME", None)
        self.env["HOME"] = str(self.root / "home")
        self.env["BATON_REPOS_ROOT"] = str(self.repos)
        self.env["BATON_ALLOW_UNTRUSTED_WRITES"] = "1"
        self.projects = self.root / "home" / "dev" / "baton-projects"
        self.slots = self.root / "home" / "dev" / "baton-wt"
        self.worktrees = self.root / "home" / "dev" / "baton-worktrees"
        self.artifacts = self.root / "home" / "dev" / "baton-artifacts" / "projects"

    def test_init_with_default_paths_creates_usable_artifacts_repository(self) -> None:
        self.default_environment()
        self.cli("init")
        self.assertTrue(self.worktrees.is_dir())
        self.cli("init")
        self.make_repo("alpha")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3")
        plan = artifact_path(self.projects, self.artifacts, "example") / "plan.md"
        plan.write_text("# Example plan\n")
        self.cli("artifacts", "commit", "--selector", "example", "--no-push", "-m", "Save plan", str(plan))
        self.assertEqual(self.git(self.artifacts, "show", f"HEAD:projects/{artifact_path(self.projects, self.artifacts).name}/plan.md"), "# Example plan")
        self.assertEqual(self.git(self.worktrees / "alpha" / "example", "branch", "--show-current"), "baton/example")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git(self.artifacts, "remote"), "")

    def test_init_refuses_nonempty_default_artifact_directory(self) -> None:
        self.default_environment()
        self.artifacts.parent.mkdir(parents=True)
        existing = self.artifacts.parent / "existing.txt"
        existing.write_text("Do not adopt\n")
        result = self.cli("init", ok=False)
        self.assertIn("Refusing to adopt artifact directory", result.stderr)
        self.assertEqual(existing.read_text(), "Do not adopt\n")
        self.assertFalse((self.artifacts.parent / ".git").exists())

    def test_init_refuses_foreign_default_artifact_repository(self) -> None:
        self.default_environment()
        self.git(self.root, "init", "-b", "main", str(self.artifacts.parent))
        result = self.cli("init", ok=False)
        self.assertIn("Refusing to adopt artifact directory", result.stderr)
        self.assertEqual(self.git(self.artifacts.parent, "ls-files"), "")
        self.assertFalse(self.artifacts.exists())


class PersistentPathsTest(unittest.TestCase):
    def test_persistent_paths_and_environment_precedence(self) -> None:
        fixtures.FIXTURES.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=fixtures.FIXTURES) as temporary:
            root = Path(temporary)
            options = {"projects_repo": "repo", "slots_root": "slots", "worktrees_root": "worktrees",
                       "repos_root": "sources", "artifacts_root": "artifacts"}
            paths = {key: str(root / suffix) for key, suffix in options.items()}
            config = root / "config.json"
            config.write_text(json.dumps({"paths": paths}))
            environment = {"HOME": str(root), "BATON_CONFIG_FILE": str(config)}
            with patch.dict(os.environ, environment, clear=True):
                manager = Manager()
                for key, attribute in (("projects_repo", "repo"), ("slots_root", "slots"),
                                       ("worktrees_root", "worktrees"), ("repos_root", "sources"),
                                       ("artifacts_root", "artifacts")):
                    self.assertEqual(getattr(manager, attribute), Path(paths[key]))
                self.assertTrue(manager.artifacts_configured)
                manager.initialize_artifacts()
                self.assertFalse(manager.artifacts.exists())
                os.environ["BATON_REPO"] = str(root / "environment-repo")
                os.environ["BATON_ARTIFACTS_ROOT"] = str(root / "environment-artifacts")
                manager = Manager()
                self.assertEqual(manager.repo, root / "environment-repo")
                self.assertEqual(manager.artifacts, root / "environment-artifacts")
                self.assertEqual(manager.slots, Path(paths["slots_root"]))
