import contextlib
import fcntl
import json
import os
import pty
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import Any, Callable, Iterator

from baton.providers import session_variables
from baton.storage_names import registry_directory


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "baton"
FIXTURES = Path(os.environ.get("BATON_TEST_ROOT", str(
    Path(__file__).resolve().parents[1] / "experiments" / ".baton-tests")))


def state_file(projects: Path, slot: str = "p3") -> Path:
    return registry_directory(projects / ".git") / "v2" / "projects" / f"{slot}.json"


def read_state(projects: Path) -> dict[str, Any]:
    """Read active and archived registry records for assertions."""
    layout = registry_directory(projects / ".git") / "v2"
    return {"projects": {path.stem: json.loads(path.read_text()) for path in sorted((layout / "projects").glob("*.json"))},
            "archive": [json.loads(path.read_text()) for path in sorted((layout / "archive").glob("*.json"))]}


def artifact_path(projects: Path, artifacts: Path, name: str = "example") -> Path:
    state = read_state(projects)
    matches = [p for p in (*state["archive"], *state["projects"].values())
               if name == p["name"] or name in p.get("previous_names", [])]
    if not matches:
        return artifacts / name
    project = matches[-1]
    return Path(project.get("artifacts_path") or project.get("blackboard", {}).get("directory") or artifacts / name)


class ProjectManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=FIXTURES)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repos = self.root / "repos"
        self.repos.mkdir()
        self.projects = self.root / "projects"
        self.slots = self.root / "project-wt"
        self.worktrees = self.root / "worktrees"
        self.artifacts = self.root / "artifacts"
        self.env = os.environ.copy()
        self.env.update(
            BATON_ALLOW_UNTRUSTED_WRITES="1",
            BATON_CONFIG_FILE=str(self.root / "config.json"),
            BATON_REPO=str(self.projects),
            BATON_SLOTS_ROOT=str(self.slots),
            BATON_WORKTREES_ROOT=str(self.worktrees),
            BATON_REPOS_ROOT=str(self.repos),
            BATON_ARTIFACTS_ROOT=str(self.artifacts),
            GIT_CONFIG_COUNT="4",
            GIT_CONFIG_KEY_0="user.name",
            GIT_CONFIG_VALUE_0="Project Manager Test",
            GIT_CONFIG_KEY_1="user.email",
            GIT_CONFIG_VALUE_1="project-test@example.invalid",
            GIT_CONFIG_KEY_2="commit.gpgsign",
            GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="core.hooksPath",
            GIT_CONFIG_VALUE_3="/dev/null",
        )
        # The suite may itself run inside an agent session; never let its identity reach claims.
        for variable in session_variables() | {"EXTRA_SESSION_ID"}:
            self.env.pop(variable, None)
        self.git(self.root, "init", "-b", "main", str(self.artifacts))
        (self.artifacts / ".gitignore").write_text(
            "*/.blackboard-*.tmp\n*/blackboard.sqlite*\n*/.blackboard-*/\nblackboard-lock.md\n.blackboard-*.tmp\n.snapshot-*.sqlite*\n"
        )
        self.git(self.artifacts, "add", ".gitignore")
        self.git(self.artifacts, "commit", "-m", "Initialize artifact repository")
        if self._testMethodName == "test_init_is_local_only_and_idempotent":
            self.cli("init")
        else:
            # Most tests exercise project operations, so they need one real slot rather than all ten.
            self.git(self.root, "init", "-b", "main", str(self.projects))
            (self.projects / "README.md").write_text("# Local projects\n")
            self.git(self.projects, "add", "README.md")
            self.git(self.projects, "commit", "-m", "Initialize test coordinator")
            (self.projects / ".git" / "info" / "exclude").write_text("/repos/\n")
            layout = self.projects / ".git" / "baton" / "v2"
            for name in ("projects", "archive", "reservations"):
                (layout / name).mkdir(parents=True)
            ProjectManagerTest.ensure_slot(self, "p3")

    def ensure_slot(self, slot: str) -> Path:
        path = self.slots / slot
        if not path.exists():
            self.slots.mkdir(exist_ok=True)
            self.git(self.projects, "worktree", "add", "-b", slot, str(path), "main")
        return path

    def run_command(self, args: list[str], cwd: Path, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=cwd, env=self.env, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, f"{args}\n{result.stdout}\n{result.stderr}")
        else:
            self.assertNotEqual(result.returncode, 0, f"Unexpected success: {args}\n{result.stdout}")
        return result

    def git(self, repo: Path, *args: str, ok: bool = True) -> str:
        return self.run_command(["git", "-C", str(repo), *args], self.root, ok=ok).stdout.strip()

    def cli(self, *args: str, cwd: Path | None = None, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run_command(["bash", str(SCRIPT), *args], cwd or self.root, ok=ok)

    def make_repo(self, name: str, default: str = "main", remote: bool = True) -> Path:
        repo = self.repos / name
        self.git(self.root, "init", "-b", default, str(repo))
        (repo / "README.md").write_text(f"# {name}\n")
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-m", "Initial commit")
        if remote:
            origin = self.root / f"{name}.git"
            self.git(self.root, "init", "--bare", "-b", default, str(origin))
            self.git(repo, "remote", "add", "origin", str(origin))
            self.git(repo, "push", "-u", "origin", default)
            self.git(repo, "remote", "set-head", "origin", "-a")
        return repo

    def checkout(self, repo: str, project: str = "example") -> Path:
        return self.worktrees / repo / project

    def create(self, *repos: str, slot: str = "p3", name: str = "example") -> None:
        ProjectManagerTest.ensure_slot(self, slot)
        self.cli("create", name, "--repos", *repos, cwd=self.slots / slot)

    def replace_interactively(self, reply: bytes, name: str = "next", repo: str = "alpha", ok: bool = True, cleanup: bool = False) -> subprocess.CompletedProcess[str]:
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(
                ["bash", str(SCRIPT), *(["cleanup"] if cleanup else ["create", name, "-r", repo])],
                cwd=self.slots / "p3", env=self.env, stdin=slave,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            os.write(master, reply)
            stdout, stderr = process.communicate(timeout=30)
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            self.assertEqual(result.returncode == 0, ok, f"{stdout}\n{stderr}")
            return result
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def test_confirmed_replacement_cleans_and_creates(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        result = self.replace_interactively(b"y\n")
        self.assertIn("[y/N]", result.stderr)
        self.assertFalse(self.checkout("alpha").exists())
        self.assertTrue(self.checkout("alpha", "next").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/next")

    def test_declined_or_eof_replacement_preserves_project(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        state = state_file(self.projects)
        before = state.read_bytes()
        for reply in (b"n\n", b"\n", b"\x04"):
            with self.subTest(reply=reply):
                self.replace_interactively(reply, ok=False)
                self.assertEqual(state.read_bytes(), before)
                self.assertTrue(self.checkout("alpha").exists())

    def test_replacement_keeps_cleanup_safety_checks(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        notes = self.checkout("alpha") / "notes.txt"
        notes.write_text("preserve me")
        self.replace_interactively(b"y\nn\n", ok=False)
        self.assertEqual(notes.read_text(), "preserve me")
        notes.unlink()
        self.git(self.checkout("alpha"), "commit", "--allow-empty", "-m", "Unmerged work")
        self.replace_interactively(b"y\nn\n", ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertFalse(self.checkout("alpha", "next").exists())

    def test_invalid_replacement_repo_preserves_old_project(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.replace_interactively(b"y\n", repo="missing", ok=False)
        self.replace_interactively(b"y\n", repo=str(self.checkout("alpha")), ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/example")

    def test_cleanup_confirmation_discards_dirty_files_and_archives_commits(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        checkout = self.checkout("alpha")
        self.git(checkout, "commit", "--allow-empty", "-m", "Unmerged change")
        sha = self.git(checkout, "rev-parse", "HEAD")
        (checkout / "notes.txt").write_text("untracked")
        (checkout / "README.md").write_text("dirty")
        result = self.replace_interactively(b"y\n", cleanup=True)
        self.assertIn("notes.txt", result.stderr)
        self.assertIn("README.md", result.stderr)
        self.assertIn("Unmerged change", result.stderr)
        self.assertIn("permanently discarded", result.stderr)
        self.assertFalse(checkout.exists())
        self.assertIn(sha, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))

    def test_cleanup_decline_and_dry_run_preserve_project(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        notes = self.checkout("alpha") / "notes.txt"
        notes.write_text("keep")
        state = state_file(self.projects)
        before = state.read_bytes()
        for reply in (b"n\n", b"\n", b"\x04"):
            self.replace_interactively(reply, cleanup=True, ok=False)
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(notes.read_text(), "keep")
        result = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("notes.txt", result.stderr)
        self.assertNotIn("[y/N]", result.stderr)
        self.assertEqual(state.read_bytes(), before)

    def test_confirmation_does_not_override_nested_repository(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        nested = self.checkout("alpha") / "nested"
        self.git(self.root, "init", str(nested))
        result = self.replace_interactively(b"y\n", cleanup=True, ok=False)
        self.assertIn("Nested repository", result.stderr)
        self.assertNotIn("[y/N]", result.stderr)
        self.assertTrue(nested.exists())

    def test_cleanup_confirms_and_stashes_dirty_coordinator(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        coordinator = self.slots / "p3"
        (coordinator / "README.md").write_text("Staged coordinator change\n")
        self.git(coordinator, "add", "README.md")
        notes = coordinator / "trace-compatibility"
        notes.mkdir()
        (notes / "notes.md").write_text("Untracked notes\n")
        self.cli("cleanup", "p3", ok=False)
        self.cli("cleanup", "p3", "--dry-run", ok=False)
        for reply in (b"n\n", b"\n", b"\x04"):
            self.replace_interactively(reply, cleanup=True, ok=False)
            self.assertTrue((notes / "notes.md").exists())
            self.assertTrue(self.checkout("alpha").exists())
        result = self.replace_interactively(b"y\n", cleanup=True)
        self.assertIn("trace-compatibility/notes.md", result.stderr)
        self.assertIn("Preserved coordinator changes", result.stdout)
        self.assertFalse(notes.exists())
        self.assertFalse(self.checkout("alpha").exists())
        self.assertEqual(self.git(coordinator, "show", "stash@{0}:README.md"), "Staged coordinator change")
        self.assertEqual(self.git(coordinator, "show", "stash@{0}^3:trace-compatibility/notes.md"), "Untracked notes")

    def test_replacement_can_confirm_dirty_coordinator(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        coordinator = self.slots / "p3"
        (coordinator / "notes.txt").write_text("Save before replacement\n")
        self.replace_interactively(b"y\nn\n", ok=False)
        self.assertTrue((coordinator / "notes.txt").exists())
        self.replace_interactively(b"y\ny\n")
        self.assertTrue(self.checkout("alpha", "next").exists())
        self.assertEqual(self.git(coordinator, "show", "stash@{0}^3:notes.txt"), "Save before replacement")

    def test_replacement_can_reuse_same_project_name(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.replace_interactively(b"y\n", name="example")
        self.assertTrue(self.checkout("alpha").exists())
        state = read_state(self.projects)
        self.assertEqual(len(state["archive"]), 1)
        self.assertEqual(state["projects"]["p3"]["state"], "active")

    def test_piped_yes_cannot_replace_project(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        result = subprocess.run(
            ["bash", str(SCRIPT), "create", "next", "-r", "alpha"],
            cwd=self.slots / "p3", env=self.env, input="y\n", capture_output=True, text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive confirmation", result.stderr)
        self.assertTrue(self.checkout("alpha").exists())

    def test_init_is_local_only_and_idempotent(self) -> None:
        self.cli("init")
        self.assertEqual(self.git(self.projects, "ls-files"), "README.md")
        self.assertEqual(self.git(self.projects, "remote"), "")
        worktrees = self.git(self.projects, "worktree", "list", "--porcelain")
        self.assertEqual(sum(line.startswith("worktree ") for line in worktrees.splitlines()), 11)
        for number in range(1, 11):
            self.assertEqual(self.git(self.slots / f"p{number}", "branch", "--show-current"), f"p{number}")

    def test_lightweight_fixture_uses_isolated_real_worktrees(self) -> None:
        self.assertEqual([path.name for path in self.slots.iterdir()], ["p3"])
        self.assertEqual(read_state(self.projects), {"projects": {}, "archive": []})
        self.assertEqual(self.git(self.projects, "remote"), "")
        slot = self.ensure_slot("p4")
        self.assertEqual(self.ensure_slot("p4"), slot)
        self.assertEqual(self.git(slot, "branch", "--show-current"), "p4")
        self.assertEqual(Path(self.git(slot, "rev-parse", "--git-common-dir")).resolve(),
                         self.projects / ".git")
        (slot / "repos").mkdir()
        (slot / "repos" / "ignored").write_text("Ignored coordinator link directory\n")
        self.assertEqual(self.git(slot, "status", "--porcelain"), "")
        self.git(slot, "commit", "--allow-empty", "-m", "Independent slot commit")
        self.assertNotEqual(self.git(slot, "rev-parse", "HEAD"),
                            self.git(self.slots / "p3", "rev-parse", "HEAD"))

    def test_create_with_short_repos_option(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta", default="master")
        self.cli("create", "example", "-r", "alpha", "Beta", cwd=self.slots / "p3")
        for name in ("alpha", "Beta"):
            self.assertEqual(self.git(self.checkout(name), "branch", "--show-current"), "baton/example")

    def test_repository_names_create_add_path_and_cleanup(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.make_repo("gamma")
        self.create("alpha")
        self.cli("add", "Beta", "gamma", cwd=self.slots / "p3")
        manifest = json.loads((self.slots / "p3" / "project.json").read_text())
        self.assertEqual([repo["id"] for repo in manifest["repos"]], ["alpha", "Beta", "gamma"])
        for alias, canonical in (("alpha", "alpha"), ("Beta", "Beta"), ("gamma", "gamma")):
            self.assertEqual(self.cli("path", "p3", alias).stdout.strip(), str(self.checkout(canonical)))
            self.git(self.checkout(canonical), "commit", "--allow-empty", "-m", "Unmerged work")
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha", "--allow-unmerged", "Beta", "--allow-unmerged", "gamma")
        self.assertFalse(self.checkout("alpha").exists())
        self.assertFalse(self.checkout("Beta").exists())
        self.assertFalse(self.checkout("gamma").exists())

    def test_duplicate_repository_is_rejected(self) -> None:
        self.make_repo("alpha")
        self.cli("create", "example", "-r", "alpha", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertFalse(self.checkout("alpha").exists())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")

    def test_explicit_repository_path_keeps_name(self) -> None:
        source = self.make_repo("alpha")
        self.create(str(source))
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual(self.git(source, "branch", "--show-current"), "main")

    def test_create_multiple_repos_and_infer_from_subdirectory(self) -> None:
        first = self.make_repo("alpha")
        second = self.make_repo("Beta", default="master")
        self.create("alpha", "Beta")
        for name, source in (("alpha", first), ("Beta", second)):
            checkout = self.checkout(name)
            self.assertEqual(self.git(checkout, "branch", "--show-current"), "baton/example")
            self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.git(source, "rev-parse", "HEAD"))
            self.assertEqual((self.slots / "p3" / "repos" / name).resolve(), checkout)
            self.assertEqual(self.git(checkout, "config", "--get", "branch.baton/example.merge", ok=False), "")
        self.assertEqual(self.git(first, "branch", "--show-current"), "main")
        self.assertEqual(self.git(second, "branch", "--show-current"), "master")
        self.assertIn("example", self.cli("list").stdout)
        self.assertIn("alpha", self.cli("status", cwd=self.checkout("alpha")).stdout)
        self.assertEqual(self.cli("path", "p3", "alpha").stdout.strip(), str(self.checkout("alpha")))
        (self.checkout("alpha") / "subdir").mkdir()
        self.assertIn("example", self.cli("status", cwd=self.checkout("alpha") / "subdir").stdout)

    def test_rename_moves_repos_and_preserves_artifacts_and_dirty_files(self) -> None:
        sources = {name: self.make_repo(name) for name in ("alpha", "Beta")}
        self.create("alpha", "Beta")
        artifacts = artifact_path(self.projects, self.artifacts)
        (artifacts / "notes").mkdir(parents=True)
        (artifacts / "plan.md").write_text("Project plan\n")
        (artifacts / "retro.md").write_text("Project retrospective\n")
        (artifacts / "notes" / "notes.md").write_text("Supporting notes\n")
        checkout = self.checkout("alpha")
        (checkout / "README.md").write_text("Staged change\n")
        self.git(checkout, "add", "README.md")
        (checkout / "README.md").write_text("Unstaged change\n")
        (checkout / "notes.txt").write_text("Untracked change\n")
        before_status = self.git(checkout, "status", "--porcelain")
        tips = {name: self.git(self.checkout(name), "rev-parse", "HEAD") for name in sources}

        self.cli("rename", "renamed", cwd=self.slots / "p3")

        for name, source in sources.items():
            renamed = self.checkout(name, "renamed")
            self.assertFalse(self.checkout(name).exists())
            self.assertEqual(self.git(renamed, "branch", "--show-current"), "baton/renamed")
            self.assertEqual(self.git(renamed, "rev-parse", "HEAD"), tips[name])
            self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
            self.assertEqual((self.slots / "p3" / "repos" / name).resolve(), renamed)
            self.assertEqual(self.cli("path", "renamed", name).stdout.strip(), str(renamed))
        self.assertEqual(self.git(self.checkout("alpha", "renamed"), "status", "--porcelain"), before_status)
        self.assertEqual((self.checkout("alpha", "renamed") / "notes.txt").read_text(), "Untracked change\n")
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/renamed")
        self.git(self.projects, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        manifest = json.loads((self.slots / "p3" / "project.json").read_text())
        self.assertEqual(manifest["name"], "renamed")
        for repo in manifest["repos"]:
            self.assertEqual(repo["branch"], "baton/renamed")
            self.assertEqual(repo["path"], str(self.checkout(repo["id"], "renamed")))
        self.assertTrue(artifacts.exists())
        for relative, content in (("plan.md", "Project plan\n"), ("retro.md", "Project retrospective\n"), ("notes/notes.md", "Supporting notes\n")):
            self.assertEqual((artifacts / relative).read_text(), content)
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["name"], "renamed")
        self.assertEqual(state["projects"]["p3"]["state"], "active")
        self.assertNotIn("pending_rename", state["projects"]["p3"])

    def test_rename_rejects_collisions_before_mutating_any_repo(self) -> None:
        self.make_repo("alpha")
        beta = self.make_repo("Beta")
        self.create("alpha", "Beta")
        artifacts = artifact_path(self.projects, self.artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "plan.md").write_text("Keep this plan\n")
        state = state_file(self.projects)
        before = state.read_bytes()
        self.git(beta, "branch", "baton/branch-conflict")
        self.git(self.projects, "branch", "baton/coordinator-conflict")
        path_conflict = self.checkout("Beta", "path-conflict")
        path_conflict.mkdir()
        (path_conflict / "keep.txt").write_text("Unrelated\n")
        for name in ("branch-conflict", "coordinator-conflict", "path-conflict"):
            with self.subTest(name=name):
                self.cli("rename", name, "p3", ok=False)
                self.assertEqual(state.read_bytes(), before)
                for repo in ("alpha", "Beta"):
                    self.assertEqual(self.git(self.checkout(repo), "branch", "--show-current"), "baton/example")
                    self.assertFalse(self.checkout("alpha", name).exists())
                self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/example")
                self.assertEqual((artifacts / "plan.md").read_text(), "Keep this plan\n")
        self.assertEqual((path_conflict / "keep.txt").read_text(), "Unrelated\n")

    def test_rename_rejects_invalid_names_and_nonactive_projects(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        state = state_file(self.projects)
        before = state.read_bytes()
        for name in ("../outside", "a/b", "--unsafe", "p1", "p3", "p10"):
            with self.subTest(name=name):
                self.cli("rename", name, "p3", ok=False)
                self.assertEqual(state.read_bytes(), before)
        self.update_state(lambda project: project.update(state="cleaning"))
        self.cli("rename", "renamed", "p3", ok=False)
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")

    def test_rename_resumes_after_worktree_move_failure(self) -> None:
        self.make_repo("alpha")
        beta = self.make_repo("Beta")
        self.create("alpha", "Beta")
        artifact_path(self.projects, self.artifacts).mkdir(parents=True, exist_ok=True)
        (artifact_path(self.projects, self.artifacts) / "plan.md").write_text("Keep plan\n")
        (self.checkout("Beta") / "notes.txt").write_text("Keep local work\n")
        self.git(beta, "worktree", "lock", str(self.checkout("Beta")))
        self.cli("rename", "renamed", "p3", ok=False)
        self.assertTrue(self.checkout("alpha", "renamed").exists())
        self.assertFalse(self.checkout("alpha").exists())
        self.assertTrue(self.checkout("Beta").exists())
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "renaming")
        self.assertIn("pending_rename", state["projects"]["p3"])
        self.cli("rename", "different-name", "p3", ok=False)
        self.make_repo("gamma")
        self.ensure_slot("p4")
        conflict = self.cli("create", "renamed", "-r", "gamma", cwd=self.slots / "p4", ok=False)
        self.assertIn("Project already active", conflict.stderr)
        self.assertFalse(self.checkout("gamma", "renamed").exists())
        self.git(beta, "worktree", "unlock", str(self.checkout("Beta")))
        self.cli("rename", "renamed", cwd=self.slots / "p3")

        for repo in ("alpha", "Beta"):
            self.assertFalse(self.checkout(repo).exists())
            self.assertEqual(self.git(self.checkout(repo, "renamed"), "branch", "--show-current"), "baton/renamed")
            self.assertEqual((self.slots / "p3" / "repos" / repo).resolve(), self.checkout(repo, "renamed"))
        self.assertEqual((self.checkout("Beta", "renamed") / "notes.txt").read_text(), "Keep local work\n")
        self.assertEqual((artifact_path(self.projects, self.artifacts, "renamed") / "plan.md").read_text(), "Keep plan\n")
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "active")
        self.assertNotIn("pending_rename", state["projects"]["p3"])

    def test_rename_resumes_after_manifest_commit_failure(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        artifact_path(self.projects, self.artifacts).mkdir(parents=True, exist_ok=True)
        (artifact_path(self.projects, self.artifacts) / "plan.md").write_text("Keep plan\n")
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.cli("rename", "renamed", "p3", ok=False)
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "renaming")
        self.assertTrue(self.checkout("alpha", "renamed").exists())
        self.assertEqual((artifact_path(self.projects, self.artifacts, "renamed") / "plan.md").read_text(), "Keep plan\n")
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.cli("rename", "renamed", "p3")
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")
        manifest = json.loads((self.slots / "p3" / "project.json").read_text())
        self.assertEqual(manifest["name"], "renamed")
        self.assertNotIn("pending_rename", manifest)
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "active")
        self.assertNotIn("pending_rename", state["projects"]["p3"])

    def test_cleanup_after_rename_uses_new_names_and_preserves_artifacts(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        artifact_path(self.projects, self.artifacts).mkdir(parents=True, exist_ok=True)
        (artifact_path(self.projects, self.artifacts) / "plan.md").write_text("Keep plan\n")
        self.cli("rename", "renamed", "example")
        self.cli("cleanup", "renamed")
        self.assertFalse(self.checkout("alpha", "renamed").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/renamed", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")
        self.assertEqual((artifact_path(self.projects, self.artifacts, "renamed") / "plan.md").read_text(), "Keep plan\n")
        state = read_state(self.projects)
        self.assertEqual(state["archive"][-1]["name"], "renamed")

    def status_json(self, *args: str, cwd: Path | None = None) -> dict[str, Any]:
        result = self.cli("status", "--json", *args, cwd=cwd)
        return json.loads(result.stdout)

    def test_status_json_reports_entries_and_current_entry(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta", default="master")
        self.create("alpha", "Beta")
        data = self.status_json(cwd=self.checkout("Beta"))
        self.assertEqual(set(data), {"name", "slot", "coordinator", "branch", "state", "blackboard", "uuid", "owner",
                                 "master_path", "helper_path", "note_directories", "pending_operation", "current_entry", "entries", "retired_entries", "errors",
                                 "worker_model", "resolved_worker_model", "resolved_worker_effort", "worker_model_guidance",
                                     "provider_guidance"})
        self.assertIsNone(data["owner"])
        self.assertRegex(data["uuid"], r"^[0-9a-f]{32}$")
        self.assertEqual(data["retired_entries"], [])
        self.assertEqual((data["name"], data["slot"], data["state"]), ("example", "p3", "active"))
        self.assertEqual(data["coordinator"], str(self.slots / "p3"))
        self.assertEqual(data["master_path"], str(artifact_path(self.projects, self.artifacts, "example") / "plan.md"))
        self.assertEqual(Path(data["helper_path"]).name, "baton")
        self.assertTrue(os.access(data["helper_path"], os.X_OK))
        self.assertIsNone(data["pending_operation"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["blackboard"]["path"], str(artifact_path(self.projects, self.artifacts, "example") / "blackboard.sqlite"))
        self.assertEqual(data["blackboard"]["backend"], "sqlite")
        self.assertRegex(data["blackboard"]["generation"], r"^[0-9a-f]{32}$")
        alpha, beta = data["entries"]
        self.assertEqual(alpha, {
            "repo": "alpha", "member": None, "link": "alpha", "branch": "baton/example",
            "path": str(self.checkout("alpha")), "workflow": "independent", "stack_root": None,
            "subplan_path": str(artifact_path(self.projects, self.artifacts, "example") / "branches" / "alpha" / "example" / "plan.md"),
            "pr_base": "main", "base_ref": "refs/remotes/origin/main", "base_reliable": True,
            "base_error": None, "needs_rebase": False, "step": "ready",
        })
        self.assertEqual((beta["pr_base"], beta["base_ref"]), ("master", "refs/remotes/origin/master"))
        self.assertEqual(data["current_entry"], beta)
        (self.checkout("Beta") / "subdir").mkdir()
        self.assertEqual(self.status_json(cwd=self.checkout("Beta") / "subdir")["current_entry"], beta)
        self.assertIsNone(self.status_json(cwd=self.slots / "p3")["current_entry"])
        self.assertIsNone(self.status_json("example")["current_entry"])

    def test_status_json_outside_project_exits_3_with_empty_stdout(self) -> None:
        for cwd, args in ((self.root, ()), (self.slots / "p3", ()), (self.root, ("missing",))):
            with self.subTest(cwd=cwd, args=args):
                result = self.cli("status", "--json", *args, cwd=cwd, ok=False)
                self.assertEqual((result.returncode, result.stdout), (3, ""))
                self.assertIn("No active project", result.stderr)
        self.env["BATON_REPO"] = str(self.root / "uninitialized")
        result = self.cli("status", "--json", ok=False)
        self.assertEqual((result.returncode, result.stdout), (3, ""))

    def test_status_reports_pending_operation_and_unreliable_base(self) -> None:
        source = self.make_repo("alpha")
        self.make_repo("Beta")
        self.create("alpha")
        self.update_state(lambda project: project.update(state="adding", pending_add=["Beta", "my repo"]))
        data = self.status_json("p3")
        self.assertEqual(data["state"], "adding")
        command = f"cd {shlex.quote(str(self.slots / 'p3'))} && baton add Beta 'my repo'"
        self.assertEqual(data["pending_operation"], {"operation": "adding", "command": command})
        self.assertIn(f"resume with: {command}", self.cli("status", "p3").stdout)
        # A corrupt catalog can hide stack membership, so even an independent entry is unreliable.
        (source / ".git" / "gh-stack").write_text('{"stacks": 1}')
        result = self.cli("status", "--json", "p3")
        self.assertIn("base unreliable", result.stderr)
        data = json.loads(result.stdout)
        entry = data["entries"][0]
        self.assertFalse(entry["base_reliable"])
        self.assertIsNone(entry["needs_rebase"])
        self.assertIn("Invalid native stack catalog", entry["base_error"])
        self.assertEqual(data["errors"], [entry["base_error"]])
        self.assertEqual(entry["pr_base"], "main")
        self.assertIn("PR base: main (unreliable)", self.cli("status", "p3").stdout)

    def manager_dir(self) -> Path:
        return registry_directory(self.projects / ".git")

    def spawn(self, *args: str, cwd: Path, name: str, stdin: Any = subprocess.DEVNULL) -> tuple[subprocess.Popen[str], Path]:
        log = self.root / f"{name}.stderr"
        with log.open("w") as stderr, (self.root / f"{name}.stdout").open("w") as stdout:
            process = subprocess.Popen(["bash", str(SCRIPT), *args], cwd=cwd, env=self.env, stdin=stdin,
                                       stdout=stdout, stderr=stderr, text=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        return process, log

    def wait_for(self, log: Path, text: str, timeout: float = 30) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            content = log.read_text()
            if text in content:
                return content
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {text!r} in {log}:\n{log.read_text()}")

    def finish(self, process: subprocess.Popen[str], name: str) -> tuple[int, str, str]:
        code = process.wait(timeout=60)
        return code, (self.root / f"{name}.stdout").read_text(), (self.root / f"{name}.stderr").read_text()

    @contextlib.contextmanager
    def held(self, path: Path, content: bytes | None = None) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if content is not None:
                handle.truncate(0)
                handle.write(content)
                handle.flush()
            yield

    def test_interrupted_fresh_init_finishes_on_next_command(self) -> None:
        directory = self.manager_dir()
        shutil.rmtree(directory / "v2")
        (directory / "v2.tmp" / "projects").mkdir(parents=True)
        self.assertIn("p1: idle", self.cli("list").stdout)
        self.assertTrue((directory / "v2" / "reservations").is_dir())
        self.assertFalse((directory / "v2.tmp").exists())

    def test_cleanup_prompt_blocks_only_its_own_slot(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.make_repo("gamma")
        self.create("alpha")
        self.create("Beta", slot="p4", name="other")
        (self.checkout("alpha") / "notes.txt").write_text("dirty")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        cleanup, cleanup_log = self.spawn("cleanup", cwd=self.slots / "p3", name="cleanup", stdin=slave)
        self.wait_for(cleanup_log, "[y/N]")
        self.cli("branch", "add", "Beta", "docs", cwd=self.slots / "p4")
        self.assertTrue(self.checkout("Beta", "other--docs").exists())
        add, add_log = self.spawn("add", "gamma", cwd=self.slots / "p3", name="add")
        message = self.wait_for(add_log, "waiting for the slot p3 lock held by")
        self.assertIn(f"pid {cleanup.pid} (baton cleanup, slot p3, since ", message)
        time.sleep(0.3)
        self.assertIsNone(add.poll())
        os.write(master, b"n\n")
        self.assertNotEqual(self.finish(cleanup, "cleanup")[0], 0)
        code, _, stderr = self.finish(add, "add")
        self.assertEqual(code, 0, stderr)
        self.assertTrue(self.checkout("gamma").exists())
        self.assertEqual((self.checkout("alpha") / "notes.txt").read_text(), "dirty")

    def test_concurrent_slots_persist_and_share_source_fetches(self) -> None:
        for name in ("alpha", "Beta", "gamma", "shared"):
            self.make_repo(name)
        self.create("alpha")
        self.create("Beta", slot="p4", name="other")
        first = self.spawn("add", "gamma", "shared", cwd=self.slots / "p3", name="first")[0]
        second = self.spawn("add", "shared", cwd=self.slots / "p4", name="second")[0]
        for process, name in ((first, "first"), (second, "second")):
            code, _, stderr = self.finish(process, name)
            self.assertEqual(code, 0, stderr)
        state = read_state(self.projects)["projects"]
        self.assertEqual([r["id"] for r in state["p3"]["repos"]], ["alpha", "gamma", "shared"])
        self.assertEqual([r["id"] for r in state["p4"]["repos"]], ["Beta", "shared"])
        self.assertTrue(self.checkout("shared").exists())
        self.assertTrue(self.checkout("shared", "other").exists())

    def test_racing_slots_claim_a_name_or_member_branch_once(self) -> None:
        self.make_repo("alpha")
        self.ensure_slot("p4")
        racers = [self.spawn("create", "same", "-r", "alpha", cwd=self.slots / slot, name=slot)[0] for slot in ("p3", "p4")]
        codes = [self.finish(process, slot)[0] for process, slot in zip(racers, ("p3", "p4"))]
        self.assertEqual(sorted(code == 0 for code in codes), [False, True])
        winner, loser = ("p3", "p4") if codes[0] == 0 else ("p4", "p3")
        self.assertEqual(list(read_state(self.projects)["projects"]), [winner])
        self.assertEqual(self.git(self.slots / loser, "branch", "--show-current"), loser)
        self.cli("cleanup", winner)

        self.create("alpha", slot="p3", name="foo--bar")
        self.create("alpha", slot="p4", name="foo")
        racers = [self.spawn("branch", "add", "alpha", member, cwd=self.slots / slot, name=f"{slot}-member")[0]
                  for slot, member in (("p3", "baz"), ("p4", "bar--baz"))]
        codes = [self.finish(process, f"{slot}-member")[0] for process, slot in zip(racers, ("p3", "p4"))]
        self.assertEqual(sorted(code == 0 for code in codes), [False, True])
        state = read_state(self.projects)["projects"]
        self.assertTrue(all(project["state"] == "active" and "pending_member" not in project for project in state.values()))
        owners = [slot for slot in ("p3", "p4") if any(m["branch"] == "baton/foo--bar--baz"
                                                        for m in state[slot]["repos"][0].get("members", []))]
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.git(self.checkout("alpha", "foo--bar--baz"), "branch", "--show-current"), "baton/foo--bar--baz")

    def test_reservation_blocks_other_slots_only_while_its_slot_lock_is_held(self) -> None:
        self.make_repo("alpha")
        reservation = self.manager_dir() / "v2" / "reservations" / "p4.json"
        reservation.write_text(json.dumps({"slot": "p4", "pid": 1, "claims": [["name", "taken"]]}))
        with self.held(self.manager_dir() / "locks" / "p4.lock"):
            result = self.cli("create", "taken", "-r", "alpha", cwd=self.slots / "p3", ok=False)
            self.assertIn("Project already active or a former name of one: taken (claimed by a running command in slot p4)",
                          result.stderr)
            self.assertTrue(reservation.exists())
        self.create("alpha", name="taken")
        self.assertFalse(reservation.exists())
        self.assertEqual(os.listdir(reservation.parent), [])

    def test_lock_wait_reports_garbled_and_stale_holders(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.create("alpha")
        finished = subprocess.Popen(["true"])
        finished.wait()
        stale = json.dumps({"pid": finished.pid, "command": "baton cleanup", "slot": "p3", "started": "then"}).encode()
        huge = json.dumps({"pid": 10**30, "command": "baton cleanup"}).encode()
        for content, expected in ((b"\x00garbled{", "held by an unknown holder"), (b"", "held by an unknown holder"),
                                  (huge, "held by an unknown holder"),
                                  (stale, f"pid {finished.pid} (baton cleanup, slot p3, since then); "
                                          "that process is gone, so the lock may be stale")):
            with self.subTest(content=content):
                lock = self.manager_dir() / "locks" / "p3.lock"
                with self.held(lock, content):
                    process, log = self.spawn("status", "p3", cwd=self.root, name="reader")
                    self.assertEqual(self.finish(process, "reader")[0], 0)
                    process, log = self.spawn("blackboard", "init", "--selector", "p3", cwd=self.root, name="writer")
                    self.wait_for(log, expected)
                    self.assertIsNone(process.poll())
                    inode = lock.stat().st_ino
                code, _, stderr = self.finish(process, "writer")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(lock.stat().st_ino, inode)

    def test_selector_command_aborts_when_its_project_changes_while_waiting(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        path = state_file(self.projects)
        original = path.read_bytes()

        def renamed() -> None:
            path.write_text(json.dumps(dict(json.loads(original), name="elsewhere")))

        def replaced() -> None:
            path.write_text(json.dumps(dict(json.loads(original), uuid="0" * 32)))

        for change in (renamed, replaced, path.unlink):
            with self.subTest(change=change.__name__):
                with self.held(self.manager_dir() / "locks" / "p3.lock"):
                    process, log = self.spawn("cleanup", "example", cwd=self.root, name="cleanup")
                    self.wait_for(log, "waiting for the slot p3 lock")
                    change()
                code, _, stderr = self.finish(process, "cleanup")
                self.assertNotEqual(code, 0)
                self.assertIn("changed while waiting for its lock; rerun", stderr)
                path.write_bytes(original)
                self.assertTrue(self.checkout("alpha").exists())

    def test_cleanup_reports_its_own_stash_when_another_slot_stashes(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.ensure_slot("p4")
        coordinator, other = self.slots / "p3", self.slots / "p4"
        (coordinator / "notes.md").write_text("Coordinator notes\n")
        (other / "intruder.md").write_text("Other slot notes\n")
        real_git = shutil.which("git", path=self.env["PATH"])
        assert real_git is not None
        bindir = self.root / "git-bin"
        bindir.mkdir()
        wrapper = bindir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(real_git)} \"$@\"\n"
            "status=$?\n"
            'case " $* " in *" stash push "*"baton cleanup"*)\n'
            f"  {shlex.quote(real_git)} -C {shlex.quote(str(other))} stash push --include-untracked -m intruder >/dev/null;;\n"
            "esac\n"
            "exit $status\n"
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        uuid = json.loads(state_file(self.projects).read_text())["uuid"]
        result = self.replace_interactively(b"y\n", cleanup=True)
        reported = result.stdout.split("stash ", 1)[1].split()[0]
        entries = dict(line.split(" ", 1)[::-1] for line in
                       self.git(coordinator, "stash", "list", "--format=%H %gs").splitlines())
        self.assertEqual(reported, entries[f"On baton/example: baton cleanup example {uuid}"])
        self.assertNotEqual(reported, self.git(coordinator, "rev-parse", "refs/stash"))
        self.assertEqual(self.git(coordinator, "show", f"{reported}^3:notes.md"), "Coordinator notes")
        self.assertFalse((coordinator / "notes.md").exists())
        self.assertFalse((other / "intruder.md").exists())

    def test_former_names_resolve_after_rename_and_stay_claimed(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.ensure_slot("p4")
        self.create("alpha")
        generation = json.loads(state_file(self.projects).read_text())["blackboard"]["generation"]
        self.cli("rename", "renamed", "example")
        self.cli("rename", "final", "renamed")
        self.assertEqual(json.loads(state_file(self.projects).read_text())["previous_names"], ["example", "renamed"])
        for alias in ("example", "renamed"):
            with self.subTest(alias=alias):
                self.cli("blackboard", "post", "--selector", alias, "--agent", "worker",
                         "--expected-generation", generation, "--text", f"Posted via {alias}")
                self.assertEqual(self.status_json(alias)["name"], "final")
                self.assertEqual(self.cli("path", alias, "alpha").stdout.strip(), str(self.checkout("alpha", "final")))
                result = self.cli("create", alias, "-r", "Beta", cwd=self.slots / "p4", ok=False)
                self.assertIn(f"former name of one: {alias}", result.stderr)
        self.assertIn("Posted via renamed", self.cli("blackboard", "read", "--selector", "final").stdout)
        stale = self.cli("blackboard", "post", "--selector", "example", "--agent", "worker",
                         "--expected-generation", "0" * 32, "--text", "Stale", ok=False)
        self.assertIn("generation changed", stale.stderr)
        self.assertFalse(self.checkout("Beta", "example").exists())
        self.cli("rename", "example", "final")
        self.assertEqual(json.loads(state_file(self.projects).read_text())["previous_names"], ["renamed", "final"])
        self.cli("cleanup", "example")
        self.create("Beta", slot="p4", name="renamed")

    def test_ambiguous_former_name_is_an_error(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.create("alpha")
        self.create("Beta", slot="p4", name="other")
        self.update_state(lambda project: project.update(previous_names=["shared"]))
        path = state_file(self.projects, "p4")
        path.write_text(json.dumps(dict(json.loads(path.read_text()), previous_names=["shared"])))
        result = self.cli("status", "shared", ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Former project name shared matches several active projects: example (p3), other (p4)", result.stderr)
        self.assertEqual(self.status_json("example")["name"], "example")

    def test_init_preserves_existing_unborn_repository(self) -> None:
        existing = self.root / "unborn"
        self.git(self.root, "init", "-b", "main", str(existing))
        readme = existing / "README.md"
        readme.write_text("Existing notes\n")
        self.git(existing, "add", "README.md")
        self.env["BATON_REPO"] = str(existing)
        self.cli("init", ok=False)
        self.assertEqual(readme.read_text(), "Existing notes\n")
        self.assertEqual(self.git(existing, "diff", "--cached", "--name-only"), "README.md")
        self.git(existing, "rev-parse", "--verify", "HEAD", ok=False)

    def test_discovered_default_branch_supports_cleanup_dry_run(self) -> None:
        source = self.make_repo("alpha")
        self.git(source, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.create("alpha")
        self.cli("cleanup", "p3", "--dry-run")
        self.assertTrue(self.checkout("alpha").is_dir())

    def test_cleanup_fetches_cached_default_without_discovery(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.cli("cleanup", "p3")
        commands = trace.read_text()
        self.assertIn("git fetch origin refs/heads/main:refs/remotes/origin/main", commands)
        self.assertNotIn("git ls-remote", commands)

    def test_cleanup_rediscovers_missing_cached_default(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.git(source, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.cli("cleanup", "p3")
        self.assertEqual(self.git(source, "symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")

    def test_cleanup_rediscovers_deleted_remote_default(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.git(source, "push", "origin", "main:refs/heads/replacement")
        self.git(self.root / "alpha.git", "symbolic-ref", "HEAD", "refs/heads/replacement")
        self.git(source, "push", "origin", "--delete", "main")
        self.cli("cleanup", "p3")
        self.assertEqual(self.git(source, "symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/replacement")

    def test_creation_rejects_external_repos_symlink(self) -> None:
        self.make_repo("alpha")
        outside = self.root / "external-links"
        outside.mkdir()
        (self.slots / "p3" / "repos").symlink_to(outside, target_is_directory=True)
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.checkout("alpha").exists())

    def test_creation_preserves_ignored_manifest(self) -> None:
        self.make_repo("alpha")
        exclude = self.projects / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "\n/project.json\n")
        manifest = self.slots / "p3" / "project.json"
        manifest.write_text("Unrelated ignored notes\n")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(manifest.read_text(), "Unrelated ignored notes\n")
        self.assertFalse(self.checkout("alpha").exists())

    def test_create_requires_free_coordinator_slot(self) -> None:
        self.make_repo("alpha")
        self.cli("create", "example", "--repos", "alpha", ok=False)
        self.create("alpha")
        head = self.git(self.slots / "p3", "rev-parse", "HEAD")
        self.cli("create", "other", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "rev-parse", "HEAD"), head)
        self.assertFalse(self.checkout("alpha", "other").exists())

    def test_preflight_checks_every_repo_before_creation(self) -> None:
        repo = self.make_repo("alpha")
        self.cli("create", "example", "--repos", "alpha", "missing", cwd=self.slots / "p3", ok=False)
        self.assertFalse(self.checkout("alpha").exists())
        self.git(repo, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")

    def test_refuses_existing_branch_or_target_path(self) -> None:
        repo = self.make_repo("alpha")
        self.git(repo, "branch", "baton/example")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.git(repo, "branch", "-d", "baton/example")
        self.checkout("alpha").mkdir(parents=True)
        sentinel = self.checkout("alpha") / "keep.txt"
        sentinel.write_text("preserve")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_add_and_cleanup_preserve_main_checkouts_and_slot(self) -> None:
        alpha = self.make_repo("alpha")
        beta = self.make_repo("Beta")
        self.create("alpha")
        self.cli("add", "Beta", cwd=self.checkout("alpha"))
        self.assertTrue(self.checkout("Beta").is_dir())
        self.cli("cleanup", cwd=self.slots / "p3")
        for name, source in (("alpha", alpha), ("Beta", beta)):
            self.assertFalse(self.checkout(name).exists())
            self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
            self.assertTrue((source / "README.md").is_file())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")
        self.assertEqual(self.git(self.projects, "ls-files"), "README.md")
        self.create("alpha", name="next")

    def test_cleanup_dry_run_does_not_change_files(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        state = state_file(self.projects)
        before = state.read_bytes()
        self.cli("cleanup", "p3", "--dry-run")
        self.assertEqual(state.read_bytes(), before)
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/example")

    def test_dirty_repo_blocks_cleanup_of_all_repos(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.create("alpha", "Beta")
        dirty = self.checkout("Beta") / "notes.txt"
        dirty.write_text("important untracked content")
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual(dirty.read_text(), "important untracked content")

    def test_ignored_files_are_removed_with_worktree(self) -> None:
        source = self.make_repo("alpha")
        (source / ".gitignore").write_text(".env\n.venv/\n")
        self.git(source, "add", ".gitignore")
        self.git(source, "commit", "-m", "Ignore local environment")
        self.git(source, "push")
        self.create("alpha")
        secret = self.checkout("alpha") / ".env"
        secret.write_text("KEEP_ME=1\n")
        venv = self.checkout("alpha") / ".venv"
        venv.mkdir()
        (venv / "generated").write_text("disposable")
        self.cli("cleanup", "p3", "--dry-run")
        self.assertTrue(secret.exists())
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("alpha").exists())

    def test_ignored_coordinator_files_do_not_block_cleanup(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        with (self.projects / ".git" / "info" / "exclude").open("a") as handle:
            handle.write("\nlocal-cache\n")
        cache = self.slots / "p3" / "local-cache"
        cache.write_text("cache")
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("alpha").exists())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")

    def test_cleanup_rechecks_files_after_other_repository_fetch(self) -> None:
        self.make_repo("alpha")
        beta = self.make_repo("Beta")
        self.create("alpha", "Beta")
        late_file = self.checkout("alpha") / "late-file"
        upload = self.root / "upload-pack.sh"
        upload.write_text(f"#!/bin/sh\nprintf 'keep' > {shlex.quote(str(late_file))}\nexec git-upload-pack \"$@\"\n")
        self.git(beta, "config", "remote.origin.uploadpack", f"sh {shlex.quote(str(upload))}")
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(late_file.read_text(), "keep")
        self.assertTrue(self.checkout("Beta").is_dir())

    def test_unmerged_commit_blocks_cleanup_until_integrated(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        checkout = self.checkout("alpha")
        self.git(checkout, "commit", "--allow-empty", "-m", "Unmerged work")
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(checkout.is_dir())
        self.git(source, "merge", "--ff-only", "baton/example")
        self.git(source, "push")
        self.cli("cleanup", "p3")
        self.assertFalse(checkout.exists())

    def fake_github_cli(self, response: Any, exit_code: int = 0) -> Path:
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        payload = self.root / "github-response.json"
        payload.write_text(json.dumps(response))
        marker = self.root / "github-invocations"
        executable = bindir / "gh"
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" >> {shlex.quote(str(marker))}\n"
            f"cat {shlex.quote(str(payload))}\n"
            f"exit {exit_code}\n"
        )
        executable.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        return marker

    def make_squash_merged_project(self) -> tuple[Path, dict[str, Any]]:
        source = self.make_repo("alpha")
        github_url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", github_url)
        self.git(source, "remote", "set-url", "origin", github_url)
        self.create("alpha")
        checkout = self.checkout("alpha")
        (checkout / "feature.txt").write_text("Merged feature\n")
        self.git(checkout, "add", "feature.txt")
        self.git(checkout, "commit", "-m", "Feature branch work")
        head = self.git(checkout, "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example")
        self.git(source, "commit", "-m", "Squash merge feature (#1)")
        self.git(source, "push")
        self.git(source, "merge-base", "--is-ancestor", head, "origin/main", ok=False)
        return source, {
            "state": "MERGED", "headRefName": "baton/example", "headRefOid": head,
            "baseRefName": "main", "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")},
        }

    def test_squash_merged_pr_allows_cleanup_and_retains_recovery_ref(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.git(source, "update-ref", "refs/remotes/origin/main", "main^")
        marker = self.fake_github_cli([merged])
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("alpha").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertIn(merged["headRefOid"], self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        self.assertEqual(marker.read_text().splitlines(), [
            "pr", "list", "--repo", "github.com/test/alpha", "--state", "merged",
            "--head", "baton/example", "--base", "main", "--limit", "100", "--json",
            "state,headRefName,headRefOid,baseRefName,mergeCommit",
        ])

    def test_squash_merged_pr_dry_run_fetches_before_checking_integration(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.git(source, "update-ref", "refs/remotes/origin/main", "main^")
        self.fake_github_cli([merged])
        state = state_file(self.projects)
        registry = state.parents[2]
        before_registry = {str(path.relative_to(registry)): path.read_bytes()
                           for path in registry.rglob("*") if path.is_file()}
        manifest = self.slots / "p3" / "project.json"
        before = (manifest.read_bytes(), self.git(source, "worktree", "list", "--porcelain"),
                  self.git(source, "rev-parse", "main"))
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES", None)
        self.cli("cleanup", "p3", "--dry-run")
        self.assertEqual(self.git(source, "rev-parse", "origin/main"), merged["mergeCommit"]["oid"])
        self.assertEqual(self.git(source, "rev-parse", "baton/example"), merged["headRefOid"])
        self.assertEqual(trace.read_text().count("git fetch origin refs/heads/main:refs/remotes/origin/main"), 1)
        self.assertEqual({str(path.relative_to(registry)): path.read_bytes()
                          for path in registry.rglob("*") if path.is_file()}, before_registry)
        self.assertEqual((manifest.read_bytes(), self.git(source, "worktree", "list", "--porcelain"),
                          self.git(source, "rev-parse", "main")), before)
        self.assertTrue(self.checkout("alpha").is_dir())

    def test_cleanup_dry_run_fetch_failure_preserves_project(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.git(source, "update-ref", "refs/remotes/origin/main", "main^")
        marker = self.fake_github_cli([merged])
        (self.root / "alpha.git").rename(self.root / "unavailable.git")
        state = state_file(self.projects)
        before = (state.read_bytes(), self.git(source, "rev-parse", "origin/main"))
        result = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertNotIn("Unmerged commits", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual((state.read_bytes(), self.git(source, "rev-parse", "origin/main")), before)
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual(self.git(source, "rev-parse", "baton/example"), merged["headRefOid"])

    def test_squash_merged_pr_does_not_cover_additional_local_commits(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.fake_github_cli([merged])
        checkout = self.checkout("alpha")
        (checkout / "feature.txt").write_text("Additional unmerged work\n")
        self.git(checkout, "commit", "-am", "Follow-up work")
        tip = self.git(checkout, "rev-parse", "HEAD")
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(checkout.is_dir())
        self.assertEqual(self.git(source, "rev-parse", "baton/example"), tip)

    def test_squash_merge_requires_matching_pr_and_integrated_merge_commit(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.fake_github_cli([{**merged, "mergeCommit": {"oid": merged["headRefOid"]}}])
        state = state_file(self.projects)
        manifest = self.slots / "p3" / "project.json"
        before = (state.read_bytes(), manifest.read_bytes(),
                  self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                  self.git(source, "worktree", "list", "--porcelain"))
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual((state.read_bytes(), manifest.read_bytes(),
                          self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                          self.git(source, "worktree", "list", "--porcelain")), before)

    def test_resumed_squash_cleanup_rechecks_new_commits(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.fake_github_cli([merged])
        checkout = self.checkout("alpha")
        self.git(source, "worktree", "lock", str(checkout))
        self.cli("cleanup", "p3", ok=False)
        self.assertIn(merged["headRefOid"], self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        (checkout / "feature.txt").write_text("Work after interrupted cleanup\n")
        self.git(checkout, "commit", "-am", "Follow-up after interruption")
        self.git(source, "worktree", "unlock", str(checkout))
        tip = self.git(checkout, "rev-parse", "HEAD")
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(checkout.is_dir())
        self.assertEqual(self.git(source, "rev-parse", "baton/example"), tip)

    def test_github_cli_failures_and_malformed_responses_preserve_project(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.fake_github_cli([merged], exit_code=1)
        state = state_file(self.projects)
        manifest = self.slots / "p3" / "project.json"
        before = (state.read_bytes(), manifest.read_bytes(),
                  self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                  self.git(source, "worktree", "list", "--porcelain"))
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual((state.read_bytes(), manifest.read_bytes(),
                          self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                          self.git(source, "worktree", "list", "--porcelain")), before)

    def test_normal_ancestry_does_not_query_github(self) -> None:
        source, _ = self.make_squash_merged_project()
        self.git(source, "merge", "--no-edit", "baton/example")
        self.git(source, "push")
        marker = self.fake_github_cli([], exit_code=1)
        self.cli("cleanup", "p3")
        self.assertFalse(marker.exists())
        self.assertFalse(self.checkout("alpha").exists())

    def test_allow_unmerged_does_not_query_github(self) -> None:
        self.make_squash_merged_project()
        marker = self.fake_github_cli([], exit_code=1)
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha")
        self.assertFalse(marker.exists())
        self.assertFalse(self.checkout("alpha").exists())

    def test_non_github_origin_does_not_query_github(self) -> None:
        source, merged = self.make_squash_merged_project()
        self.git(source, "remote", "set-url", "origin", str(self.root / "alpha.git"))
        marker = self.fake_github_cli([merged])
        self.cli("cleanup", "p3", ok=False)
        self.assertFalse(marker.exists())
        self.assertTrue(self.checkout("alpha").is_dir())

    def test_changed_branch_blocks_cleanup(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.git(self.checkout("alpha"), "checkout", "-b", "unrelated")
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "unrelated")

    def test_nested_worktree_blocks_cleanup(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        nested = self.checkout("alpha") / "nested"
        self.git(self.checkout("alpha"), "worktree", "add", "-b", "nested-test", str(nested))
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(nested.is_dir())

    def test_ignored_nested_dependency_does_not_block_cleanup(self) -> None:
        source = self.make_repo("alpha")
        (source / ".gitignore").write_text(".venv/\n")
        self.git(source, "add", ".gitignore")
        self.git(source, "commit", "-m", "Ignore virtual environment")
        self.git(source, "push")
        self.create("alpha")
        nested = self.checkout("alpha") / ".venv/lib/python3.13/site-packages/temporalio/bridge/sdk-core"
        self.git(self.root, "init", str(nested))
        (nested / "dependency.txt").write_text("ignored dependency")
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.cli("cleanup", "p3", "--dry-run")
        dependency_checks = [line for line in trace.read_text().splitlines()
                             if "git check-ignore" in line and ".venv" in line]
        self.assertEqual(dependency_checks, [])
        self.assertTrue(nested.exists())
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("alpha").exists())

    def test_nested_scan_respects_ignore_exceptions(self) -> None:
        source = self.make_repo("alpha")
        (source / ".gitignore").write_text("cache/*\n!cache/keep/\n")
        self.git(source, "add", ".gitignore")
        self.git(source, "commit", "-m", "Keep selected cache directory visible")
        self.git(source, "push")
        self.create("alpha")
        ignored = self.checkout("alpha") / "cache/discard/dependency"
        kept = self.checkout("alpha") / "cache/keep/nested"
        self.git(self.root, "init", str(ignored))
        self.git(self.root, "init", str(kept))
        result = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn(f"Nested repository in {self.checkout('alpha')}: {kept}", result.stderr)
        self.assertTrue(kept.exists())

    def test_unrelated_worktrees_survive_cleanup(self) -> None:
        source = self.make_repo("alpha")
        unrelated = self.root / "unrelated"
        self.git(source, "worktree", "add", "-b", "unrelated", str(unrelated))
        self.create("alpha")
        self.cli("cleanup", "p3")
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(self.git(unrelated, "branch", "--show-current"), "unrelated")

    def test_local_only_repo(self) -> None:
        self.make_repo("local", remote=False)
        self.create("local")
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("local").exists())

    def test_invalid_project_name(self) -> None:
        self.make_repo("alpha")
        for name in ("../outside", "a/b", "--unsafe", "p1", "p3", "p10"):
            self.cli("create", name, "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")

    def update_state(self, change: Callable[[dict[str, Any]], None]) -> None:
        state_path = state_file(self.projects)
        project = json.loads(state_path.read_text())
        change(project)
        state_path.write_text(json.dumps(project))

    def test_resumed_creation_does_not_commit_unrelated_staged_files(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.update_state(lambda project: project.update(state="creating"))
        notes = self.slots / "p3" / "notes.txt"
        notes.write_text("unfinished notes")
        self.git(self.slots / "p3", "add", "notes.txt")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.git(self.slots / "p3", "diff", "--cached", "--name-only"), "notes.txt")
        self.assertEqual(notes.read_text(), "unfinished notes")

    def test_resumed_creation_rejects_incomplete_checkout(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")

        def interrupted(project: dict[str, Any]) -> None:
            project["state"] = "creating"
            project["repos"][0]["step"] = "creating"
            project["pending_manifest"] = (self.slots / "p3" / "project.json").read_text()

        self.update_state(interrupted)
        (self.checkout("alpha") / "README.md").unlink()
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        state = read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["repos"][0]["step"], "creating")

    def test_creation_refreshes_remote_default_branch(self) -> None:
        source = self.make_repo("alpha")
        self.git(source, "checkout", "-b", "new-default")
        self.git(source, "commit", "--allow-empty", "-m", "New default")
        tip = self.git(source, "rev-parse", "HEAD")
        self.git(source, "push", "origin", "new-default")
        self.git(self.root / "alpha.git", "symbolic-ref", "HEAD", "refs/heads/new-default")
        self.create("alpha")
        self.assertEqual(self.git(self.checkout("alpha"), "rev-parse", "HEAD"), tip)

    def test_resumed_cleanup_preserves_branch_checked_out_elsewhere(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.git(source, "worktree", "remove", str(self.checkout("alpha")))
        other = self.root / "other-checkout"
        self.git(source, "worktree", "add", str(other), "baton/example")

        def interrupted(project: dict[str, Any]) -> None:
            project["state"] = "cleaning"
            project["repos"][0]["step"] = "removing"

        self.update_state(interrupted)
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.git(other, "branch", "--show-current"), "baton/example")
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example")

    def test_allow_unmerged_retains_recoverable_ref(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.git(self.checkout("alpha"), "commit", "--allow-empty", "-m", "Squash merged work")
        tip = self.git(self.checkout("alpha"), "rev-parse", "HEAD")
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha")
        self.assertFalse(self.checkout("alpha").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertIn(tip, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))

    def test_allow_unmerged_does_not_discard_dirty_files(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        notes = self.checkout("alpha") / "notes.txt"
        notes.write_text("still working")
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha", ok=False)
        self.assertEqual(notes.read_text(), "still working")

    def test_resumed_cleanup_preserves_previous_archived_tip(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        checkout = self.checkout("alpha")
        self.git(checkout, "commit", "--allow-empty", "-m", "Original work")
        original = self.git(checkout, "rev-parse", "HEAD")
        self.git(source, "worktree", "lock", str(checkout))
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha", ok=False)
        refs = self.git(source, "for-each-ref", "--format=%(refname)", "refs/project-archive/").splitlines()
        self.assertEqual(len(refs), 1)
        self.git(checkout, "commit", "--amend", "--allow-empty", "-m", "Revised work")
        revised = self.git(checkout, "rev-parse", "HEAD")
        self.git(source, "worktree", "unlock", str(checkout))
        self.cli("cleanup", "p3")
        self.assertEqual(self.git(source, "rev-parse", refs[0]), original)
        self.assertIn(revised, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))


if __name__ == "__main__":
    unittest.main()
