import json
import os
from pathlib import Path
import pty
import subprocess
import unittest
from typing import Any
from unittest.mock import patch

from test_project_manager import artifact_path

import test_project_branches as branches
import test_project_manager as fixtures
from baton import retirement


class ProjectRetirementTest(unittest.TestCase):
    setUp = branches.ProjectBranchesTest.setUp
    run_command = branches.ProjectBranchesTest.run_command
    git = branches.ProjectBranchesTest.git
    cli = branches.ProjectBranchesTest.cli
    make_repo = branches.ProjectBranchesTest.make_repo
    checkout = branches.ProjectBranchesTest.checkout
    create = branches.ProjectBranchesTest.create
    native = branches.ProjectBranchesTest.native
    commit_file = branches.ProjectBranchesTest.commit_file
    member = branches.ProjectBranchesTest.member
    init_members = branches.ProjectBranchesTest.init_members
    add_member = branches.ProjectBranchesTest.add_member
    catalog = branches.ProjectBranchesTest.catalog
    fake_pull_requests = branches.ProjectBranchesTest.fake_pull_requests

    def project(self) -> dict[str, Any]:
        return json.loads(fixtures.state_file(self.projects).read_text())

    def entry(self, member: str) -> dict[str, Any]:
        repo = self.project()["repos"][0]
        return next(entry for entry in [repo, *repo.get("members", [])]
                    if entry.get("member") == member
                    or entry.get("first_member") == member)

    def status(self) -> dict[str, Any]:
        return json.loads(self.cli("status", "p3", "--json").stdout)

    def interactive(self, *args: str, reply: bytes, ok: bool = True) -> subprocess.CompletedProcess[str]:
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(["bash", str(fixtures.SCRIPT), *args],
                                       cwd=self.slots / "p3", env=self.env, stdin=slave,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

    def integrate(self, member: str) -> str:
        source = self.repos / "alpha"
        tip = self.git(self.member(member), "rev-parse", "HEAD")
        self.git(source, "merge", "--no-edit", "baton/example--" + member)
        self.git(source, "push")
        return tip

    def cleanup_member(self, member: str, *, dry_run: bool = False, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.cli("branch", "cleanup", "alpha", member, "--selector", "example",
                        *(["--dry-run"] if dry_run else []), ok=ok)

    def snapshot(self) -> tuple[Any, ...]:
        source = self.repos / "alpha"
        files = [fixtures.state_file(self.projects), self.slots / "p3" / "project.json"]
        files.extend(path for path in (artifact_path(self.projects, self.artifacts, "example")).rglob("*") if path.is_file())
        catalog = source / ".git" / "gh-stack"
        if catalog.exists():
            files.append(catalog)
        return ([(str(path), path.read_bytes()) for path in sorted(files)],
                self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                self.git(source, "worktree", "list", "--porcelain"),
                self.git(self.slots / "p3", "rev-parse", "HEAD"),
                sorted((path.name, os.readlink(path)) for path in (self.slots / "p3" / "repos").iterdir()))

    def assert_retired(self, member: str, path: Path, tip: str) -> None:
        self.assertFalse(path.exists())
        self.git(self.repos / "alpha", "show-ref", "--verify", "refs/heads/baton/example--" + member, ok=False)
        self.assertIn(tip, self.git(self.repos / "alpha", "for-each-ref", "--format=%(objectname)",
                                  "refs/project-archive/"))
        self.assertEqual(self.entry(member)["retirement_state"], "retired")
        self.assertEqual(self.project()["state"], "active")
        self.assertNotIn("pending_retirement", self.project())

    def test_plain_merge_retirement_preserves_project_board_plans_and_history(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        docs = self.add_member("docs")
        tip = self.integrate("api")
        artifact = artifact_path(self.projects, self.artifacts, "example")
        plan = artifact / "plan.md"
        plan.write_text("Project remains open\n")
        board = Path(self.status()["blackboard"]["path"])
        board_before = board.read_bytes()
        branch_before = self.git(self.slots / "p3", "branch", "--show-current")
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)
        self.assertTrue(docs.exists())
        self.assertEqual(plan.read_text(), "Project remains open\n")
        self.assertEqual(board.read_bytes(), board_before)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), branch_before)
        self.assertFalse((self.slots / "p3" / "repos" / "alpha").is_symlink())
        self.assertEqual({p.name for p in (self.slots / "p3" / "repos").iterdir()}, {"alpha--docs"})
        self.assertFalse((self.slots / "p3" / "repos" / "alpha--api").exists())
        self.assertEqual(self.cli("path", "p3", "alpha").stdout.strip(), str(docs))
        self.cli("path", "p3", "alpha", "--member", "api", ok=False)
        status = self.status()
        self.assertEqual([entry["member"] for entry in status["entries"]], ["docs"])
        self.assertEqual([entry["member"] for entry in status["retired_entries"]], ["api"])
        self.assertIn("retired", self.cli("status", "p3", "--all").stdout.lower())
        self.assertNotIn("baton/example--api", self.cli("status", "p3").stdout)
        self.assertEqual(fixtures.read_state(self.projects)["archive"], [])
        self.cli("blackboard", "read", "--selector", "example")
        after = self.snapshot()
        self.cleanup_member("baton/example--api")
        self.assertEqual(self.snapshot(), after)

    def test_squash_merge_requires_exact_local_tip(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        tip = self.commit_file(first, "api.txt", "API\n")
        self.git(source, "merge", "--squash", "baton/example--api")
        self.git(source, "commit", "-m", "Squash API")
        self.git(source, "push")
        self.fake_pull_requests([{"state": "MERGED", "headRefName": "baton/example--api",
                                  "headRefOid": tip, "baseRefName": "main",
                                  "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")}}])
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)

    def test_squash_pr_does_not_cover_advanced_local_tip(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        tip = self.commit_file(first, "api.txt", "API\n")
        self.git(source, "merge", "--squash", "baton/example--api")
        self.git(source, "commit", "-m", "Squash API")
        self.git(source, "push")
        self.fake_pull_requests([{"state": "MERGED", "headRefName": "baton/example--api",
                                  "headRefOid": tip, "baseRefName": "main",
                                  "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")}}])
        self.commit_file(first, "follow-up.txt", "Not covered by the merged PR\n")
        before = self.snapshot()
        self.interactive("branch", "cleanup", "alpha", "api", reply=b"y\n", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(first.exists())

    def test_additional_local_commit_is_not_overridable(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Merged API\n")
        self.integrate("api")
        self.commit_file(first, "follow-up.txt", "Unmerged follow-up\n")
        before = self.snapshot()
        result = self.interactive("branch", "cleanup", "alpha", "api", reply=b"y\n", ok=False)
        self.assertIn("integrated", result.stderr.lower())
        self.assertNotIn("[y/N]", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(first.exists())

    def test_dirty_member_needs_confirmation_and_decline_is_nonmutating(self) -> None:
        first = self.init_members()
        tip = self.integrate("api")
        (first / "dirty.txt").write_text("Uncommitted\n")
        before = self.snapshot()
        self.cleanup_member("api", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.cleanup_member("api", dry_run=True, ok=False)
        self.assertEqual(self.snapshot(), before)
        self.interactive("branch", "cleanup", "alpha", "api", reply=b"n\n", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((first / "dirty.txt").exists())
        self.interactive("branch", "cleanup", "alpha", "api", reply=b"y\n")
        self.assert_retired("api", first, tip)

    def test_dry_run_is_byte_stable_for_targeted_and_prune(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        self.add_member("docs")
        self.integrate("api")
        before = self.snapshot()
        self.cleanup_member("api", dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.cli("branch", "prune", "alpha", "--selector", "p3", "--dry-run")
        self.assertEqual(self.snapshot(), before)

    def test_all_retired_allows_fresh_member_and_eventual_project_cleanup(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        tip = self.integrate("api")
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)
        self.assertEqual(self.status()["entries"], [])
        self.assertFalse((self.slots / "p3" / "repos" / "alpha").exists())
        self.cli("path", "p3", "alpha", ok=False)
        self.cli("blackboard", "read", "--selector", "example")
        docs = self.add_member("docs")
        self.assertEqual(self.git(docs, "rev-parse", "HEAD"), self.git(self.repos / "alpha", "rev-parse", "main"))
        self.assertFalse((self.slots / "p3" / "repos" / "alpha").is_symlink())
        self.assertEqual({p.name for p in (self.slots / "p3" / "repos").iterdir()}, {"alpha--docs"})
        self.assertEqual([entry["member"] for entry in self.status()["entries"]], ["docs"])
        self.cli("cleanup", "p3")
        self.assertFalse(docs.exists())
        self.assertEqual(fixtures.read_state(self.projects)["archive"][-1]["name"], "example")

    def test_all_retired_project_can_be_cleaned_without_readding(self) -> None:
        self.init_members()
        self.integrate("api")
        self.cleanup_member("api")
        self.cli("cleanup", "p3")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})
        self.assertEqual(fixtures.read_state(self.projects)["archive"][-1]["name"], "example")

    def test_unarchived_ignored_notes_are_guarded_but_archived_notes_are_safe(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        (source / ".git" / "info" / "exclude").write_text("notes/\n")
        notes = first / "notes"
        notes.mkdir()
        (notes / "research.md").write_text("Keep this research\n")
        tip = self.integrate("api")
        before = self.snapshot()
        result = self.cleanup_member("api", ok=False)
        self.assertIn("unarchived", result.stderr.lower())
        self.interactive("branch", "cleanup", "alpha", "api", reply=b"n\n", ok=False)
        self.assertEqual(self.snapshot(), before)
        archived = artifact_path(self.projects, self.artifacts, "example") / "notes"
        archived.mkdir()
        (archived / "research.md").write_text("Keep this research\n")
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)
        self.assertEqual((archived / "research.md").read_text(), "Keep this research\n")

    def test_unarchived_notes_can_be_explicitly_discarded(self) -> None:
        first = self.init_members()
        (self.repos / "alpha" / ".git" / "info" / "exclude").write_text("notes/\n")
        (first / "notes").mkdir()
        (first / "notes" / "discard.md").write_text("Deliberately discarded\n")
        tip = self.integrate("api")
        result = self.interactive("branch", "cleanup", "alpha", "api", reply=b"y\n")
        self.assertIn("unarchived", result.stderr.lower())
        self.assert_retired("api", first, tip)

    def test_prune_skips_unarchived_notes_without_prompting(self) -> None:
        first = self.init_members()
        (self.repos / "alpha" / ".git" / "info" / "exclude").write_text("notes/\n")
        (first / "notes").mkdir()
        (first / "notes" / "keep.md").write_text("Keep the note\n")
        self.integrate("api")
        before = self.snapshot()
        result = self.cli("branch", "prune", "alpha", "--selector", "example")
        self.assertIn("unarchived", result.stderr.lower())
        self.assertNotIn("[y/N]", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((first / "notes" / "keep.md").exists())

    def test_prune_confirms_once_and_skips_dirty_and_unmerged_members(self) -> None:
        first = self.init_members()
        tip = self.commit_file(first, "api.txt", "API\n")
        docs = self.add_member("docs")
        self.commit_file(docs, "docs.txt", "Merged docs\n")
        self.integrate("docs")
        tooling = self.add_member("tooling")
        self.commit_file(tooling, "tooling.txt", "Unmerged tooling\n")
        self.integrate("api")
        (docs / "dirty.txt").write_text("Dirty docs\n")
        before = self.snapshot()
        self.interactive("branch", "prune", "alpha", reply=b"n\n", ok=False)
        self.assertEqual(self.snapshot(), before)
        result = self.interactive("branch", "prune", "alpha", reply=b"y\n")
        self.assertEqual(result.stderr.count("[y/N]"), 1)
        self.assertIn("api", result.stdout)
        self.assert_retired("api", first, tip)
        self.assertTrue((docs / "dirty.txt").exists())
        self.assertTrue(tooling.exists())
        before = self.snapshot()
        result = self.cli("branch", "prune", "--selector", "example")
        self.assertNotIn("[y/N]", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_native_group_is_untracked_only_after_every_member_retires(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        api_tip = self.commit_file(first, "api.txt", "API\n")
        child = self.add_member("tests", after="api")
        tests_tip = self.commit_file(child, "tests.txt", "Tests\n")
        self.integrate("tests")
        catalog_path = source / ".git" / "gh-stack"
        payload = json.loads(catalog_path.read_text())
        payload["stacks"][0]["branches"][0]["pullRequest"] = {"merged": True}
        catalog_path.write_text(json.dumps(payload))
        catalog = (source / ".git" / "gh-stack").read_bytes()
        self.cleanup_member("api")
        self.assert_retired("api", first, api_tip)
        self.assertEqual((source / ".git" / "gh-stack").read_bytes(), catalog)
        self.assertTrue(child.exists())
        self.assertTrue(self.project()["repos"][0]["stacks"])
        self.assertEqual(self.status()["errors"], [])
        self.cleanup_member("tests")
        self.assert_retired("tests", child, tests_tip)
        self.assertEqual(self.catalog(source)["stacks"], [])
        self.assertEqual(self.status()["errors"], [])

    def test_prune_without_repo_selects_safe_members_across_repositories(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        alpha_tip = self.integrate("api")
        beta = self.make_repo("beta")
        self.cli("add", "beta", cwd=self.slots / "p3")
        self.cli("branch", "init", "beta", "sdk", cwd=self.slots / "p3")
        sdk = self.member("sdk", "beta")
        beta_tip = self.commit_file(sdk, "sdk.txt", "SDK\n")
        self.git(beta, "merge", "--no-edit", "baton/example--sdk")
        self.git(beta, "push")
        result = self.interactive("branch", "prune", reply=b"y\n")
        self.assertEqual(result.stderr.count("[y/N]"), 1)
        self.assert_retired("api", first, alpha_tip)
        self.assertFalse(sdk.exists())
        self.assertIn(beta_tip, self.git(beta, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        self.assertEqual(self.status()["entries"], [])
        self.assertEqual({entry["repo"] for entry in self.status()["retired_entries"]}, {"alpha", "beta"})

    def test_locked_worktree_is_a_nonoverridable_preflight_failure(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        self.integrate("api")
        self.git(source, "worktree", "lock", str(first))
        before = self.snapshot()
        self.interactive("branch", "cleanup", "alpha", "api", reply=b"y\n", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(first.exists())

    def test_deletion_failure_journals_and_same_command_resumes(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())
        first = self.init_members()
        source = self.repos / "alpha"
        self.commit_file(first, "api.txt", "API\n")
        tip = self.integrate("api")
        original_git = retirement.git

        def fail_removal(repo: Path, *args: str, **kwargs: Any) -> str:
            if args[:2] == ("worktree", "remove"):
                raise branches.ProjectError("injected worktree removal failure")
            return original_git(repo, *args, **kwargs)

        with patch.dict(os.environ, self.env, clear=True):
            manager = branches.Manager()
            manager.load()
            with manager.command_lock("p3"):
                with patch.object(retirement, "git", side_effect=fail_removal):
                    with self.assertRaisesRegex(branches.ProjectError, "injected worktree removal failure"):
                        manager.retire_members("alpha", "api", "p3", False)
        project = self.project()
        self.assertEqual(project["state"], "retiring")
        self.assertTrue(project["pending_retirement"])
        self.assertIn(tip, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        self.assertTrue(first.exists())
        self.assertIn("branch cleanup", self.cli("status", "p3").stdout)
        before = self.snapshot()
        self.cleanup_member("api", dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.cli("branch", "prune", "alpha", "--selector", "example", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)

    def test_materialize_failure_resumes_after_worktree_and_branch_are_gone(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        tip = self.integrate("api")
        with patch.dict(os.environ, self.env, clear=True):
            manager = branches.Manager()
            manager.load()
            with manager.command_lock("p3"):
                with patch.object(manager, "materialize", side_effect=RuntimeError("injected materialize failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected materialize failure"):
                        manager.retire_members("alpha", "api", "p3", False)
        self.assertFalse(first.exists())
        self.assertEqual(self.project()["state"], "retiring")
        self.assertTrue(self.project()["pending_retirement"])
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)


    def test_unnamed_first_checkout_can_be_retired_by_full_branch(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        first = self.checkout("alpha")
        tip = self.commit_file(first, "api.txt", "Unnamed first member\n")
        self.git(source, "merge", "--no-edit", "baton/example")
        self.git(source, "push")
        self.cleanup_member("baton/example")
        self.assertFalse(first.exists())
        self.assertEqual(self.project()["repos"][0]["retirement_state"], "retired")
        self.assertEqual(self.status()["entries"], [])
        self.assertIsNone(self.status()["retired_entries"][0]["member"])
        self.assertIn(tip, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        before = self.snapshot()
        self.cleanup_member("baton/example")
        self.assertEqual(self.snapshot(), before)

    def test_cleanup_and_prune_accept_repository_name(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        first = self.member("api", "alpha")
        tip = self.commit_file(first, "api.txt", "Aliased repository\n")
        docs = self.add_member("docs", repo="alpha")
        self.git(source, "merge", "--no-edit", "baton/example--api")
        self.git(source, "push")
        self.cli("branch", "cleanup", "alpha", "api", "--selector", "example")
        self.assertFalse(first.exists())
        self.assertEqual(self.entry("api")["retirement_state"], "retired")
        self.assertIn(tip, self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/"))
        self.assertEqual(self.cli("path", "p3", "alpha").stdout.strip(), str(docs))
        self.cli("branch", "cleanup", "alpha", "baton/example--api", "--selector", "example")
        self.interactive("branch", "prune", "alpha", reply=b"y\n")
        self.assertFalse(docs.exists())
        self.assertEqual(self.status()["entries"], [])
        self.assertEqual({entry["repo"] for entry in self.status()["retired_entries"]}, {"alpha"})

    def test_full_cleanup_after_native_root_retirement_uses_live_descendant(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        tip = self.commit_file(first, "api.txt", "API\n")
        child = self.add_member("tests", after="api")
        self.commit_file(child, "tests.txt", "Tests\n")
        self.integrate("tests")
        catalog = source / ".git" / "gh-stack"
        payload = json.loads(catalog.read_text())
        payload["stacks"][0]["branches"][0]["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(payload))
        self.cleanup_member("api")
        self.assert_retired("api", first, tip)
        self.assertTrue(child.exists())
        self.cli("cleanup", "p3")
        self.assertFalse(child.exists())
        self.assertEqual(self.catalog(source)["stacks"], [])
        archived = fixtures.read_state(self.projects)["archive"][-1]
        self.assertEqual(archived["repos"][0]["retirement_state"], "retired")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_retired_native_top_rejects_after_surviving_predecessor_without_mutation(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        self.commit_file(first, "api.txt", "API\n")
        top = self.add_member("tests", after="api")
        tip = self.commit_file(top, "tests.txt", "Tests\n")
        self.integrate("tests")
        catalog = source / ".git" / "gh-stack"
        payload = json.loads(catalog.read_text())
        payload["stacks"][0]["branches"][-1]["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(payload))
        self.cleanup_member("tests")
        self.assert_retired("tests", top, tip)
        before = self.snapshot()
        result = self.cli("branch", "add", "alpha", "more", "--after", "api",
                          cwd=self.slots / "p3", ok=False)
        self.assertIn("retired", result.stderr.lower())
        self.assertIn("top", result.stderr.lower())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.checkout("alpha", "example--more").exists())

    def test_squash_child_merged_to_default_with_stale_parent_base_can_retire(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        parent_tip = self.commit_file(first, "api.txt", "API\n")
        child = self.add_member("tests", after="api")
        child_tip = self.commit_file(child, "tests.txt", "Tests\n")
        merges = []
        for member in ("api", "tests"):
            self.git(source, "merge", "--squash", "baton/example--" + member)
            self.git(source, "commit", "-m", "Squash " + member + " into main")
            merges.append(self.git(source, "rev-parse", "HEAD"))
        self.commit_file(source, "tests.txt", "Later main change to the same file\n")
        self.git(source, "push")
        self.git(source, "merge-base", "--is-ancestor", merges[1], "baton/example--api", ok=False)
        for tip in (parent_tip, child_tip):
            self.git(source, "merge-base", "--is-ancestor", tip, "origin/main", ok=False)
        catalog = source / ".git" / "gh-stack"
        payload = json.loads(catalog.read_text())
        for branch in payload["stacks"][0]["branches"]:
            branch["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(payload))
        responses = [
            {"state": "MERGED", "headRefName": "baton/example--tests", "headRefOid": child_tip,
             "baseRefName": "baton/example--api", "mergeCommit": {"oid": merges[1]}},
            {"state": "MERGED", "headRefName": "baton/example--api", "headRefOid": parent_tip,
             "baseRefName": "main", "mergeCommit": {"oid": merges[0]}},
        ]
        self.fake_pull_requests(responses)
        before = self.snapshot()
        self.cleanup_member("tests", dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.cleanup_member("api")
        self.assert_retired("api", first, parent_tip)
        self.cleanup_member("tests")
        self.assert_retired("tests", child, child_tip)
        proof = self.entry("tests")["retirement"]["pull_requests"]["baton/example--tests"]
        self.assertEqual(proof, responses[0])

    def test_squash_child_retirement_uses_parent_archived_by_previous_invocation(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        self.commit_file(first, "api.txt", "API\n")
        child = self.add_member("tests", after="api")
        child_tip = self.commit_file(child, "tests.txt", "Tests\n")
        self.git(first, "merge", "--squash", "baton/example--tests")
        self.git(first, "commit", "-m", "Squash tests into API")
        parent_tip = self.git(first, "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example--api")
        self.git(source, "commit", "-m", "Squash API into main")
        self.git(source, "push")
        main_tip = self.git(source, "rev-parse", "HEAD")
        catalog = source / ".git" / "gh-stack"
        payload = json.loads(catalog.read_text())
        payload["stacks"][0]["branches"][0]["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(payload))
        self.fake_pull_requests([
            {"state": "MERGED", "headRefName": "baton/example--tests", "headRefOid": child_tip,
             "baseRefName": "baton/example--api", "mergeCommit": {"oid": parent_tip}},
            {"state": "MERGED", "headRefName": "baton/example--api", "headRefOid": parent_tip,
             "baseRefName": "main", "mergeCommit": {"oid": main_tip}},
        ])
        self.git(source, "merge-base", "--is-ancestor", child_tip, "origin/main", ok=False)
        self.cleanup_member("api")
        self.assert_retired("api", first, parent_tip)
        self.assertTrue(child.exists())
        self.cleanup_member("tests")
        self.assert_retired("tests", child, child_tip)
        self.assertIn("baton/example--api", self.entry("tests")["retirement"]["pull_requests"])

    def test_dry_run_preserves_mixed_native_groups_and_independent_member_byte_for_byte(self) -> None:
        source, _paths = branches.ProjectBranchesTest.two_groups(self)
        for member in ("api-tests", "docs-tests", "tooling"):
            self.integrate(member)
        catalog = source / ".git" / "gh-stack"
        payload = json.loads(catalog.read_text())
        payload["stacks"][0]["branches"][0]["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(payload))
        self.cleanup_member("api")
        before = self.snapshot()
        self.cleanup_member("api-tests", dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.cli("branch", "prune", "alpha", "--selector", "example", "--dry-run")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.catalog(source)["stacks"]), 2)


if __name__ == "__main__":
    unittest.main()
