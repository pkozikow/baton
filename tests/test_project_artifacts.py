from pathlib import Path
import shutil
import subprocess
import unittest

from test_project_manager import artifact_path

import test_project_manager as fixtures


class ProjectArtifactsTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    held = fixtures.ProjectManagerTest.held
    manager_dir = fixtures.ProjectManagerTest.manager_dir

    def start_project(self) -> Path:
        self.make_repo("alpha")
        self.create("alpha")
        return artifact_path(self.projects, self.artifacts, "example")

    def commit(self, *args: str, cwd: Path | None = None, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.cli("artifacts", "commit", *args, cwd=cwd or self.artifacts, ok=ok)

    def head_files(self) -> list[str]:
        return self.git(self.artifacts, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()

    def staged(self) -> list[str]:
        return self.git(self.artifacts, "diff", "--cached", "--name-only").splitlines()

    def test_creation_rejects_artifact_roots_inside_git_metadata(self) -> None:
        self.make_repo("alpha")
        alias = self.root / "metadata-alias"
        alias.symlink_to(self.artifacts / ".git", target_is_directory=True)
        for directory in (self.artifacts / ".git/projects", self.artifacts / "future/.git/projects", alias / "projects"):
            with self.subTest(directory=directory):
                self.env["BATON_ARTIFACTS_ROOT"] = str(directory)
                failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
                self.assertIn("outside Git metadata", failed.stderr)
                self.assertFalse(directory.exists())

    def test_creation_explains_an_artifact_root_outside_any_checkout(self) -> None:
        self.make_repo("alpha")
        directory = self.root / "plain" / "projects"
        directory.mkdir(parents=True)
        # The fixtures live inside this repository's checkout, which would otherwise adopt the root.
        self.env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.env["BATON_ARTIFACTS_ROOT"] = str(directory)
        failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertIn(f"Project artifacts directory {directory}/", failed.stderr)
        self.assertIn("is not inside a Git checkout", failed.stderr)
        self.assertIn("paths.artifacts_root", failed.stderr)

    def test_commits_new_and_modified_files(self) -> None:
        directory = self.start_project()
        (directory / "plan.md").write_text("Plan v1\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn(f"{artifact_path(self.projects, self.artifacts).name}/plan.md", result.stdout)
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/plan.md"])
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")
        (directory / "plan.md").write_text("Plan v2\n")
        (directory / "branches" / "api").mkdir(parents=True)
        (directory / "branches" / "api" / "plan.md").write_text("Subplan\n")
        # The project resolves from a code worktree; paths are relative to the caller's directory.
        self.commit("-m", "Update plans", str(directory / "plan.md"), str(directory / "branches"),
                    cwd=self.checkout("alpha"))
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/branches/api/plan.md", f"{artifact_path(self.projects, self.artifacts).name}/plan.md"])
        self.assertEqual(self.git(self.artifacts, "show", f"HEAD:{artifact_path(self.projects, self.artifacts).name}/plan.md"), "Plan v2")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_malformed_archive_does_not_block_active_commit_or_creation(self) -> None:
        directory = self.start_project()
        layout = self.projects / ".git/baton/v2"
        (layout / "archive" / "bad.json").write_text("{")
        (directory / "plan.md").write_text("Plan")
        self.commit("--selector", "example", "-m", "Add plan", str(directory / "plan.md"))
        self.create("alpha", name="second", slot="p4")

    def test_artifact_diff_accepts_equivalent_symlinked_checkout_path(self) -> None:
        from baton.manager import Manager
        directory = self.start_project()
        (directory / "plan.md").write_text("Plan")
        self.commit("--selector", "example", "-m", "Add plan", str(directory / "plan.md"))
        alias = self.root / "artifact-alias"
        alias.symlink_to(self.artifacts, target_is_directory=True)
        manager = Manager.__new__(Manager)
        self.assertEqual(manager.non_artifact_paths(self.artifacts, "HEAD~", "HEAD",
                                                    artifacts_root=alias / directory.name), [])

    def test_rejects_paths_outside_the_project_directory(self) -> None:
        directory = self.start_project()
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        (self.artifacts / "other").mkdir()
        (self.artifacts / "other" / "plan.md").write_text("Another project\n")
        (self.artifacts / "unrelated.md").write_text("Unrelated\n")
        (directory / "escape.md").symlink_to(self.artifacts / "unrelated.md")
        (directory / "plan.md").write_text("Plan\n")
        for path in ("other/plan.md", "unrelated.md", f"{artifact_path(self.projects, self.artifacts).name}/../unrelated.md", f"{artifact_path(self.projects, self.artifacts).name}/escape.md", "."):
            with self.subTest(path=path):
                failed = self.commit("--selector", "example", "-m", "Escape", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", path, ok=False)
                self.assertIn("outside", failed.stderr)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        self.assertEqual(self.staged(), [])

    def test_refuses_a_symlinked_project_directory(self) -> None:
        directory = self.start_project()
        elsewhere = self.root / "elsewhere"
        directory.rename(elsewhere)
        directory.symlink_to(elsewhere, target_is_directory=True)
        (elsewhere / "plan.md").write_text("Plan\n")
        failed = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", ok=False)
        self.assertIn("Unsafe project artifacts directory", failed.stderr)
        self.assertEqual(self.staged(), [])

    def test_refuses_conflicted_paths(self) -> None:
        directory = self.start_project()
        (directory / "plan.md").write_text("Base\n")
        self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.git(self.artifacts, "switch", "-q", "-c", "other")
        (directory / "plan.md").write_text("Other\n")
        self.git(self.artifacts, "commit", "-qam", "Other")
        self.git(self.artifacts, "switch", "-q", "main")
        (directory / "plan.md").write_text("Main\n")
        self.git(self.artifacts, "commit", "-qam", "Main")
        self.run_command(["git", "merge", "other"], self.artifacts, ok=False)
        failed = self.commit("--selector", "example", "-m", "Resolve", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", ok=False)
        self.assertIn("Resolve the merge conflicts", failed.stderr)
        self.assertTrue(self.git(self.artifacts, "ls-files", "-u", "--", f"{artifact_path(self.projects, self.artifacts).name}/plan.md"))

    def test_paths_are_literal_not_globs(self) -> None:
        directory = self.start_project()
        (directory / "a.md").write_text("Not requested\n")
        (directory / "[ab].md").write_text("Requested\n")
        self.commit("--selector", "example", "-m", "Add literal", f"{artifact_path(self.projects, self.artifacts).name}/[ab].md")
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/[ab].md"])
        self.assertIn(f"?? {artifact_path(self.projects, self.artifacts).name}/a.md", self.git(self.artifacts, "status", "--porcelain"))

    def test_unrelated_staged_files_and_hooks_stay_out_of_the_commit(self) -> None:
        directory = self.start_project()
        (self.artifacts / "unrelated.md").write_text("Unrelated staged changes\n")
        self.git(self.artifacts, "add", "unrelated.md")
        private = self.artifacts / "private.md"
        private.write_text("Unrelated private work\n")
        hooks = self.root / "artifact-hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nif [ -f private.md ]; then git add private.md; fi\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        (directory / "retro.md").write_text("Retro\n")
        self.commit("--selector", "p3", "-m", "Add retro", f"{artifact_path(self.projects, self.artifacts).name}/retro.md")
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/retro.md"])
        self.assertEqual(self.staged(), ["unrelated.md"])
        self.assertEqual(self.git(self.artifacts, "ls-files", "--", "private.md"), "")

    def test_failed_or_empty_commit_unstages_only_its_own_paths(self) -> None:
        directory = self.start_project()
        (directory / "plan.md").write_text("Committed plan\n")
        self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        (directory / "notes.md").write_text("Staged by the user\n")
        (self.artifacts / "unrelated.md").write_text("Unrelated\n")
        self.git(self.artifacts, "add", f"{artifact_path(self.projects, self.artifacts).name}/notes.md", "unrelated.md")
        (directory / "notes.md").write_text("Unstaged edit\n")
        (directory / "new.md").write_text("New subplan\n")
        failed = self.commit("--selector", "example", "-m", "", f"{artifact_path(self.projects, self.artifacts).name}/new.md", f"{artifact_path(self.projects, self.artifacts).name}/notes.md", ok=False)
        self.assertIn("restored the index for its paths", failed.stderr)
        self.assertEqual(self.staged(), [f"{artifact_path(self.projects, self.artifacts).name}/notes.md", "unrelated.md"])
        self.assertEqual(self.git(self.artifacts, "show", f":{artifact_path(self.projects, self.artifacts).name}/notes.md"), "Staged by the user")
        self.assertIn(f"?? {artifact_path(self.projects, self.artifacts).name}/new.md", self.git(self.artifacts, "status", "--porcelain"))
        empty = self.commit("--selector", "example", "-m", "No changes", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Nothing to commit", empty.stdout)
        self.assertEqual(empty.stderr, "")
        ignored = self.commit("--selector", "example", "-m", "Board", f"{artifact_path(self.projects, self.artifacts).name}/blackboard.sqlite", ok=False)
        self.assertIn("ignored", ignored.stderr)
        self.assertEqual(self.staged(), [f"{artifact_path(self.projects, self.artifacts).name}/notes.md", "unrelated.md"])
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)

    def test_branch_checkouts_cannot_commit_other_branches_subplans(self) -> None:
        directory = self.start_project()
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3")
        docs = self.checkout("alpha", "example--docs")
        own = directory / "branches" / "alpha" / "example--docs" / "plan.md"
        other = directory / "branches" / "alpha" / "example" / "plan.md"
        for path in (own, other, directory / "plan.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Plan\n")
        failed = self.commit("-m", "Not mine", str(own), str(other), cwd=docs, ok=False)
        self.assertIn("is another branch's subplan", failed.stderr)
        self.assertEqual(self.staged(), [])
        self.commit("-m", "Docs subplan and master", str(own), str(directory / "plan.md"), cwd=docs)
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/branches/alpha/example--docs/plan.md", f"{artifact_path(self.projects, self.artifacts).name}/plan.md"])
        self.commit("-m", "Root subplan", str(other), cwd=self.slots / "p3")
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/branches/alpha/example/plan.md"])

    def test_reused_name_keeps_old_plans_in_a_separate_directory(self) -> None:
        directory = self.start_project()
        (directory / "plan.md").write_text("Old plan\n")
        self.commit("--selector", "example", "-m", "Add plan", str(directory / "plan.md"))
        self.cli("cleanup", "p3")
        self.create("alpha")
        fresh = artifact_path(self.projects, self.artifacts)
        self.assertNotEqual(fresh, directory)
        self.assertFalse((fresh / "plan.md").exists())
        self.assertEqual((directory / "plan.md").read_text(), "Old plan\n")

    def test_refuses_when_checkout_is_not_on_its_default_branch(self) -> None:
        directory = self.start_project()
        self.git(self.artifacts, "switch", "-c", "feature")
        (directory / "plan.md").write_text("Plan\n")
        failed = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", ok=False)
        self.assertIn("only on main", failed.stderr)
        self.assertEqual(self.staged(), [])
        self.assertEqual(self.git(self.artifacts, "rev-list", "--count", "HEAD"), "1")

    def add_origin(self) -> tuple[Path, Path]:
        origin = self.root / "artifacts.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(origin))
        self.git(self.artifacts, "remote", "add", "origin", str(origin))
        self.git(self.artifacts, "push", "-u", "origin", "main")
        self.git(self.artifacts, "remote", "set-head", "origin", "main")
        other = self.root / "artifacts-other"
        self.git(self.root, "clone", str(origin), str(other))
        return origin, other

    def push_elsewhere(self, other: Path, filename: str) -> str:
        self.git(other, "pull", "--ff-only")
        (other / filename).write_text("Pushed from another checkout\n")
        self.git(other, "add", filename)
        self.git(other, "commit", "-m", f"Add {filename}")
        self.git(other, "push")
        return self.git(other, "rev-parse", "HEAD")

    def test_pushes_and_fast_forwards_the_shared_checkout(self) -> None:
        directory = self.start_project()
        origin, other = self.add_origin()
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Pushed 1 artifact commit", result.stdout)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))
        upstream = self.push_elsewhere(other, "upstream.md")
        (directory / "plan.md").write_text("Plan v2\n")
        self.commit("--selector", "example", "-m", "Update plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), upstream)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_divergence_keeps_the_commit_local_without_rewriting_history(self) -> None:
        directory = self.start_project()
        origin, other = self.add_origin()
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", "--no-push")
        self.assertNotIn("Pushed", result.stdout)
        upstream = self.push_elsewhere(other, "upstream.md")
        local = self.git(self.artifacts, "rev-parse", "HEAD")
        (directory / "retro.md").write_text("Retro\n")
        diverged = self.commit("--selector", "example", "-m", "Add retro", f"{artifact_path(self.projects, self.artifacts).name}/retro.md")
        self.assertIn("diverged", diverged.stderr)
        self.assertIn("ask the user", diverged.stderr)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), local)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add retro")
        self.assertEqual(self.git(origin, "rev-parse", "main"), upstream)

    def test_remote_failure_keeps_commit_local_and_retry_publishes_without_reconciliation(self) -> None:
        directory = self.start_project()
        origin, _ = self.add_origin()
        self.git(self.artifacts, "remote", "set-url", "origin", str(self.root / "missing.git"))
        trace = self.root / "artifact-network.trace"
        self.env["GIT_TRACE"] = str(trace)
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Fetch failed; sync skipped", result.stderr)
        self.assertIn("artifact commits are saved locally", result.stderr)
        self.assertIn("fetch failed after", result.stderr)
        self.assertIn("Push skipped because fetch failed", result.stderr)
        self.assertNotIn("push failed after", result.stderr)
        self.assertIn(" fetch ", trace.read_text())
        self.assertNotIn(" push ", trace.read_text())
        self.assertIn("does not appear to be a git repository", result.stderr)
        self.assertNotIn("ask the user", result.stderr)
        self.assertNotIn("reconciled", result.stderr)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")
        self.git(self.artifacts, "remote", "set-url", "origin", str(origin))
        retried = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Nothing to commit", retried.stdout)
        self.assertIn("Pushed 1 artifact commit", retried.stdout)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))

    def test_push_rejection_preserves_full_error_and_does_not_claim_divergence(self) -> None:
        directory = self.start_project()
        origin, _ = self.add_origin()
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'Publication temporarily unavailable' >&2\n"
                        "echo 'Try again after maintenance' >&2\nexit 1\n")
        hook.chmod(0o755)
        (directory / "plan.md").write_text("Plan\n")
        # Command-scoped test config normally disables hooks in every Git process.
        self.env["GIT_CONFIG_VALUE_3"] = str(origin / "hooks")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Publication temporarily unavailable", result.stderr)
        self.assertIn("Try again after maintenance", result.stderr)
        self.assertNotIn("reconciled", result.stderr)
        self.assertNotIn("diverged", result.stderr)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")

    def test_failed_fast_forward_still_commits_locally(self) -> None:
        directory = self.start_project()
        origin, other = self.add_origin()
        upstream = self.push_elsewhere(other, "shared.md")
        (self.artifacts / "shared.md").write_text("Another agent's uncommitted file\n")
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Cannot fast-forward", result.stderr)
        self.assertIn("diverged", result.stderr)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")
        self.assertEqual((self.artifacts / "shared.md").read_text(), "Another agent's uncommitted file\n")
        self.assertEqual(self.git(origin, "rev-parse", "main"), upstream)

    def test_unknown_paths_fail_but_committed_deletions_are_a_no_op(self) -> None:
        directory = self.start_project()
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        failed = self.commit("--selector", "example", "-m", "Typo", f"{artifact_path(self.projects, self.artifacts).name}/pln.md", ok=False)
        self.assertIn(f"do not exist and have no history: {artifact_path(self.projects, self.artifacts).name}/pln.md", failed.stderr)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        origin, _ = self.add_origin()
        (directory / "plan.md").write_text("Plan\n")
        self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", "--no-push")
        (directory / "plan.md").unlink()
        self.commit("--selector", "example", "-m", "Drop plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md", "--no-push")
        self.assertNotEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))
        # Retrying the already committed deletion publishes what an earlier failed push left behind.
        retried = self.commit("--selector", "example", "-m", "Drop plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("Nothing to commit", retried.stdout)
        self.assertIn("Pushed 2 artifact commits", retried.stdout)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))

    def nest_artifacts(self) -> None:
        # Like artifact-repo: artifacts live in projects/ of a checkout that also holds the live framework code.
        checkout = self.root / "artifact-repo"
        self.git(self.root, "init", "-b", "main", str(checkout))
        self.artifacts = checkout / "projects"
        self.artifacts.mkdir()
        (self.artifacts / ".gitignore").write_text("*/.blackboard-*.tmp\n*/blackboard.sqlite*\n*/.blackboard-*/\nblackboard-lock.md\n.blackboard-*.tmp\n.snapshot-*.sqlite*\n")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-m", "Initialize artifact-repo")
        self.env["BATON_ARTIFACTS_ROOT"] = str(self.artifacts)

    def test_fast_forwards_and_pushes_only_artifact_changes(self) -> None:
        self.nest_artifacts()
        directory = self.start_project()
        origin, other = self.add_origin()
        upstream = self.push_elsewhere(other, "projects/upstream.md")
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertNotIn("warning:", result.stderr)
        self.assertIn("fetch completed after", result.stderr)
        self.assertIn("push completed after", result.stderr)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), upstream)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))

        tool = self.push_elsewhere(other, "tool.py")
        (directory / "plan.md").write_text("Plan v2\n")
        result = self.commit("--selector", "example", "-m", "Update plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("tool.py", result.stderr)
        self.assertIn("would deploy them", result.stderr)
        self.assertFalse((self.artifacts.parent / "tool.py").exists())
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Update plan")
        self.assertEqual(self.git(origin, "rev-parse", "main"), tool)

    def test_does_not_publish_local_non_artifact_commits(self) -> None:
        self.nest_artifacts()
        directory = self.start_project()
        origin, _ = self.add_origin()
        published = self.git(origin, "rev-parse", "main")
        checkout = self.artifacts.parent
        (checkout / "tool.py").write_text("Another agent's merged work\n")
        self.git(checkout, "add", "tool.py")
        self.git(checkout, "commit", "-m", "Add tool")
        (directory / "plan.md").write_text("Plan\n")
        result = self.commit("--selector", "example", "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md")
        self.assertIn("unpushed changes outside", result.stderr)
        self.assertIn("tool.py", result.stderr)
        self.assertNotIn("Pushed", result.stdout)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")
        self.assertEqual(self.git(origin, "rev-parse", "main"), published)

    def test_archived_project_with_changed_config_does_not_publish_unrelated_old_checkout_commits(self) -> None:
        self.nest_artifacts()
        directory = self.start_project()
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        origin, _ = self.add_origin()
        self.cli("cleanup", "p3")
        published = self.git(origin, "rev-parse", "main")
        self.env["BATON_ARTIFACTS_ROOT"] = str(self.root / "future-artifacts" / "projects")
        checkout = self.artifacts.parent
        (checkout / "tool.py").write_text("Another agent's unpublished code\n")
        self.git(checkout, "add", "tool.py")
        self.git(checkout, "commit", "-m", "Add unrelated tool")
        (directory / "retro.md").write_text("Retrospective\n")
        result = self.commit("--selector", project["uuid"], "-m", "Add retrospective", str(directory / "retro.md"))
        self.assertIn(f"unpushed changes outside {self.artifacts}", result.stderr)
        self.assertIn("tool.py", result.stderr)
        self.assertNotIn("Pushed", result.stdout)
        self.assertEqual(self.git(origin, "rev-parse", "main"), published)
        self.assertEqual(self.git(checkout, "log", "-1", "--format=%s"), "Add retrospective")
        self.assertEqual(self.git(checkout, "show", f"HEAD:projects/{directory.name}/retro.md"), "Retrospective")

    def test_archived_project_with_changed_config_syncs_only_its_original_artifact_root(self) -> None:
        self.nest_artifacts()
        directory = self.start_project()
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        origin, other = self.add_origin()
        self.cli("cleanup", "p3")
        self.env["BATON_ARTIFACTS_ROOT"] = str(self.root / "future-artifacts" / "projects")
        upstream = self.push_elsewhere(other, "projects/upstream.md")
        (directory / "retro.md").write_text("Retrospective v1\n")
        result = self.commit("--selector", project["uuid"], "-m", "Add retrospective", str(directory / "retro.md"))
        self.assertNotIn("warning:", result.stderr)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), upstream)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))
        code = self.push_elsewhere(other, "tool.py")
        (directory / "retro.md").write_text("Retrospective v2\n")
        result = self.commit("--selector", project["uuid"], "-m", "Update retrospective", str(directory / "retro.md"))
        self.assertIn("would deploy them", result.stderr)
        self.assertIn("tool.py", result.stderr)
        self.assertFalse((self.artifacts.parent / "tool.py").exists())
        self.assertEqual(self.git(origin, "rev-parse", "main"), code)
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Update retrospective")

    def test_does_not_wait_for_the_project_slot_lock(self) -> None:
        directory = self.start_project()
        (directory / "plan.md").write_text("Plan\n")
        with self.held(self.manager_dir() / "locks" / "p3.lock"):
            result = subprocess.run(["bash", str(fixtures.SCRIPT), "artifacts", "commit", "--selector", "example",
                                     "-m", "Add plan", f"{artifact_path(self.projects, self.artifacts).name}/plan.md"], cwd=self.artifacts, env=self.env,
                                    text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head_files(), [f"{artifact_path(self.projects, self.artifacts).name}/plan.md"])


if __name__ == "__main__":
    unittest.main()
