import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import unittest

from test_project_manager import artifact_path

import test_project_stacks as fixtures


class NamedFirstStackTest(unittest.TestCase):
    setUp = fixtures.ProjectStacksTest.setUp
    run_command = fixtures.ProjectStacksTest.run_command
    git = fixtures.ProjectStacksTest.git
    cli = fixtures.ProjectStacksTest.cli
    make_repo = fixtures.ProjectStacksTest.make_repo
    checkout = fixtures.ProjectStacksTest.checkout
    create = fixtures.ProjectStacksTest.create
    native = fixtures.ProjectStacksTest.native
    manifest = fixtures.ProjectStacksTest.manifest
    layer = fixtures.ProjectStacksTest.layer
    commit_file = fixtures.ProjectStacksTest.commit_file
    add_stack_member = fixtures.ProjectStacksTest.add_stack_member

    def initialize(self, layer: str = "core", repo: str = "alpha") -> None:
        self.cli("branch", "init", repo, layer, cwd=self.slots / "p3")

    def state(self) -> dict:
        return fixtures.fixtures.read_state(self.projects)["projects"]["p3"]

    def assert_initialized(self, source: Path, default: str = "main") -> None:
        root = self.layer("core")
        entry = self.manifest()["repos"][0]
        self.assertEqual(entry["first_member"], "core")
        self.assertEqual(entry["branch"], "baton/example--core")
        self.assertEqual(entry["path"], str(root))
        self.assertEqual(entry.get("members", []), [])
        self.assertEqual(self.manifest()["branch"], "baton/example")
        self.assertEqual(self.manifest()["name"], "example")
        self.assertFalse(self.checkout("alpha").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertEqual(self.git(root, "branch", "--show-current"), "baton/example--core")
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/example")
        self.assertEqual({p.name for p in (self.slots / "p3" / "repos").iterdir()}, {"alpha--core"})
        self.assertEqual((self.slots / "p3" / "repos" / "alpha--core").resolve(), root)
        for args, cwd in ((("path", "p3", "alpha"), self.root),
                          (("path", "alpha", "--member", "core"), self.slots / "p3"),
                          (("path", "alpha"), root)):
            self.assertEqual(self.cli(*args, cwd=cwd).stdout.strip(), str(root))
        self.assertFalse((source / ".git" / "gh-stack").exists())
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")
        self.assertEqual(self.state()["state"], "active")
        self.assertNotIn("pending_member", self.state())

    def test_branch_init_reports_move_and_stale_subplan_directory(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        old = self.checkout("alpha")
        subplan = artifact_path(self.projects, self.artifacts) / "branches" / "alpha" / "example"
        subplan.mkdir(parents=True)
        (subplan / "plan.md").write_text("Root subplan\n")
        self.cli("artifacts", "commit", "--selector", "p3", "-m", "Add subplan", str(subplan / "plan.md"))
        result = self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        new = self.layer("api")
        self.assertIn(f"Moved {old} (baton/example) -> {new} (baton/example--api)", result.stdout)
        self.assertIn(f"subplan directory {subplan} still uses the old branch name", result.stderr)
        command = next(line.strip() for line in result.stderr.splitlines() if line.strip().startswith("git -C"))
        self.run_command(["bash", "-c", command.replace("&& baton artifacts", "&& " + shlex.quote(str(fixtures.fixtures.SCRIPT)) + " artifacts")], self.root)
        renamed = subplan.with_name("example--api")
        self.assertEqual((renamed / "plan.md").read_text(), "Root subplan\n")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Rename subplan for baton/example--api")

    def test_named_first_layer_preserves_commits_and_default_trunk(self) -> None:
        for default in ("main", "master"):
            with self.subTest(default=default):
                if default == "master":
                    self.setUp()
                source = self.make_repo("alpha", default=default)
                self.create("alpha")
                tip = self.commit_file(self.checkout("alpha"), "core.txt", "Existing first PR work\n")
                self.initialize()
                self.assert_initialized(source, default)
                self.assertEqual(self.git(self.layer("core"), "rev-parse", "HEAD"), tip)

    def test_named_initialization_is_idempotent_and_rejects_new_label(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        state = self.state()
        catalog = (source / ".git" / "gh-stack").read_bytes() if (source / ".git" / "gh-stack").exists() else None
        self.initialize()
        self.cli("branch", "init", "alpha", "other", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.state(), state)
        self.assertEqual((source / ".git" / "gh-stack").read_bytes() if (source / ".git" / "gh-stack").exists() else None, catalog)
        self.assert_initialized(source)

    def test_invalid_names_and_collisions_leave_original_checkout_untouched(self) -> None:
        source = self.make_repo("alpha")
        self.make_repo("alpha--link")
        self.create("alpha", "alpha--link")
        self.git(source, "branch", "baton/example--branch")
        self.layer("path").mkdir()
        (self.layer("path") / "keep").write_text("User data\n")
        self.layer("dangling").symlink_to(self.root / "not-created")
        state = self.state()
        before = (self.slots / "p3" / "project.json").read_bytes()
        for name in ("../outside", "core/api", "branch", "path", "dangling", "link"):
            with self.subTest(name=name):
                self.cli("branch", "init", "alpha", name, cwd=self.slots / "p3", ok=False)
                self.assertEqual(self.state(), state)
                self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before)
                self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")
                self.assertFalse((source / ".git" / "gh-stack").exists())
        self.assertEqual((self.layer("path") / "keep").read_text(), "User data\n")
        self.assertTrue(self.layer("dangling").is_symlink())

    def interrupt_after(self, executable: str, matches: str) -> str:
        actual = shutil.which(executable, path=self.env["PATH"])
        self.assertIsNotNone(actual)
        bindir = self.root / f"interrupt-{executable}"
        bindir.mkdir()
        wrapper = bindir / executable
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(str(actual))} \"$@\" || exit $?\n"
            f'case " $* " in *" {matches} "*) exit 1 ;; esac\n'
        )
        wrapper.chmod(0o755)
        original = self.env["PATH"]
        self.env["PATH"] = str(bindir) + os.pathsep + original
        return original


    def test_init_resumes_after_worktree_move_failure(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        original = self.interrupt_after("git", "worktree move")
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.assertIn("pending_member", self.state())
        self.assertTrue(self.layer("core").exists())
        self.env["PATH"] = original
        self.initialize()
        self.assert_initialized(source)

    def test_init_resumes_after_branch_rename_failure(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        original = self.interrupt_after("git", "branch -m")
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.assertIn("pending_member", self.state())
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example--core")
        self.env["PATH"] = original
        self.initialize()
        self.assert_initialized(source)

    def test_init_resumes_after_manifest_commit_failure(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.assertIn("pending_member", self.state())
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.initialize()
        self.assert_initialized(source)

    def test_empty_commit_and_reverted_tree_cannot_parent_a_layer(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        root = self.layer("core")
        for change in ("empty", "empty-commit", "reverted-tree"):
            with self.subTest(change=change):
                if change == "empty-commit":
                    self.git(root, "commit", "--allow-empty", "-m", "Not first PR work")
                if change == "reverted-tree":
                    self.commit_file(root, "temporary.txt", "Undone change\n")
                    self.git(root, "revert", "--no-edit", "HEAD")
                before_state = self.state()
                before_catalog = (source / ".git" / "gh-stack").read_bytes() if (source / ".git" / "gh-stack").exists() else None if (source / ".git" / "gh-stack").exists() else None
                result = self.add_stack_member( "alpha", "api", cwd=self.slots / "p3", ok=False)
                self.assertIn("empty", result.stderr.lower())
                self.assertEqual(self.state(), before_state)
                self.assertEqual((source / ".git" / "gh-stack").read_bytes() if (source / ".git" / "gh-stack").exists() else None, before_catalog)
                self.assertFalse(self.layer("api").exists())
                self.git(source, "show-ref", "--verify", "refs/heads/baton/example--api", ok=False)

    def test_all_named_layers_are_real_and_first_pr_targets_default(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        root = self.layer("core")
        self.commit_file(root, "core.txt", "Core implementation\n")
        self.add_stack_member( "alpha", "api", cwd=root)
        self.add_stack_member( "alpha", "ui", cwd=self.slots / "p3", ok=False)
        self.commit_file(self.layer("api"), "api.txt", "API implementation\n")
        self.add_stack_member( "alpha", "ui", cwd=self.slots / "p3")
        stack = self.native(self.layer("ui"), "view", "--json")
        self.assertEqual(stack["trunk"], "main")
        self.assertEqual([item["name"] for item in stack["branches"]],
                         ["baton/example--core", "baton/example--api", "baton/example--ui"])
        self.assertEqual([item["member"] for item in self.manifest()["repos"][0]["members"]], ["api", "ui"])
        self.git(source, "merge", "baton/example--ui")
        self.git(source, "push")
        self.cli("cleanup", "p3")
        for name in ("core", "api", "ui"):
            self.assertFalse(self.layer(name).exists())
            self.git(source, "show-ref", "--verify", f"refs/heads/baton/example--{name}", ok=False)

    def test_named_init_rejects_existing_native_registration_without_renaming(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.native(self.checkout("alpha"), "init", "--base", "main", "baton/example")
        before = (source / ".git" / "gh-stack").read_bytes()
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.assertEqual((source / ".git" / "gh-stack").read_bytes(), before)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertFalse(self.layer("core").exists())

    def test_named_init_rejects_dirty_checkout_before_mutating(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        notes = self.checkout("alpha") / "notes.txt"
        notes.write_text("Untracked user work\n")
        before = self.state()
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.state(), before)
        self.assertEqual(notes.read_text(), "Untracked user work\n")
        self.assertFalse(self.layer("core").exists())
        self.assertFalse((source / ".git" / "gh-stack").exists())

    def test_named_init_rejects_live_native_lock_before_mutating(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        before = self.state()
        with (source / ".git" / "gh-stack-operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
            self.assertIn("retry when idle", result.stderr)
            self.assertEqual(self.state(), before)
            self.assertTrue(self.checkout("alpha").exists())
            self.assertFalse(self.layer("core").exists())
            self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")
            self.assertFalse((source / ".git" / "gh-stack").exists())
        self.initialize()
        self.assert_initialized(source)

    def test_named_init_resume_rejects_live_native_lock_before_moving(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        original = self.interrupt_after("git", "branch -m")
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
        self.env["PATH"] = original
        before = self.state()
        with (source / ".git" / "gh-stack-operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3", ok=False)
            self.assertIn("retry when idle", result.stderr)
            self.assertEqual(self.state(), before)
            self.assertTrue(self.checkout("alpha").exists())
            self.assertFalse(self.layer("core").exists())
            self.assertEqual((self.slots / "p3" / "repos" / "alpha").resolve(), self.checkout("alpha"))
        self.initialize()
        self.assert_initialized(source)


    def test_native_merged_empty_top_does_not_block_new_work(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        self.commit_file(self.layer("core"), "core.txt", "First layer implementation\n")
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3")
        catalog = source / ".git" / "gh-stack"
        stack = json.loads(catalog.read_text())
        stack["stacks"][0]["branches"][-1]["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(stack))
        self.add_stack_member( "alpha", "ui", cwd=self.slots / "p3")
        self.assertTrue(self.layer("ui").exists())
        self.assertEqual([item["member"] for item in self.manifest()["repos"][0]["members"]], ["api", "ui"])

    def test_fully_merged_native_stack_rejects_add_before_mutation(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        self.commit_file(self.layer("core"), "core.txt", "First layer implementation\n")
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3")
        catalog = source / ".git" / "gh-stack"
        stack = json.loads(catalog.read_text())
        for entry in stack["stacks"][0]["branches"]:
            entry["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(stack))
        before_catalog = catalog.read_bytes()
        before_state = self.state()
        before_manifest = (self.slots / "p3" / "project.json").read_bytes()
        result = self.add_stack_member( "alpha", "ui", cwd=self.slots / "p3", ok=False)
        self.assertIn("merged", result.stderr.lower())
        self.assertRegex(result.stderr.lower(), r"clean ?up")
        self.assertEqual(self.state(), before_state)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before_manifest)
        self.assertFalse(self.layer("ui").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example--ui", ok=False)

    def test_native_add_merged_refresh_does_not_leave_pending_operation(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.initialize()
        self.commit_file(self.layer("core"), "core.txt", "First layer implementation\n")
        self.native(self.layer("core"), "init", "--base", "main", "baton/example--core")
        state = self.state()
        repo = state["repos"][0]
        repo["stack_root"] = repo["branch"]
        repo["stacks"] = [{"root": repo["branch"]}]
        fixtures.fixtures.state_file(self.projects).write_text(json.dumps(state))
        self.initialize()
        before_state = self.state()
        before_manifest = (self.slots / "p3" / "project.json").read_bytes()
        actual = shutil.which("gh", path=self.env["PATH"])
        self.assertIsNotNone(actual)
        catalog = source / ".git" / "gh-stack"
        merged = json.loads(catalog.read_text())
        for entry in merged["stacks"][0]["branches"]:
            entry["pullRequest"] = {"merged": True}
        payload = self.root / "merged-catalog.json"
        payload.write_text(json.dumps(merged))
        bindir = self.root / "merged-refresh-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "stack" ] && [ "$2" = "add" ]; then\n'
            f"  cp {shlex.quote(str(payload))} {shlex.quote(str(catalog))}\n"
            "fi\n"
            f"exec {shlex.quote(str(actual))} \"$@\"\n"
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        result = self.add_stack_member( "alpha", "api", cwd=self.slots / "p3", ok=False)
        self.assertIn("fully merged", result.stderr)
        self.assertEqual(self.state(), before_state)
        self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before_manifest)
        self.assertFalse(self.layer("api").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example--api", ok=False)
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha")

    def test_adding_repository_cannot_overwrite_first_layer_alias(self) -> None:
        source = self.make_repo("alpha")
        self.make_repo("alpha--core")
        self.create("alpha")
        self.initialize()
        before = self.state()
        self.cli("add", "alpha--core", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.state(), before)
        self.assert_initialized(source)
        self.assertFalse(self.checkout("alpha--core").exists())


if __name__ == "__main__":
    unittest.main()
