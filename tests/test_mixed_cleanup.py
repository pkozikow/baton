import json
from pathlib import Path
import shlex
import shutil
import unittest

from test_project_manager import artifact_path

import test_project_manager as fixtures
import test_project_stacks as stack_fixtures


class MixedCleanupTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    layer = stack_fixtures.ProjectStacksTest.layer
    manifest = stack_fixtures.ProjectStacksTest.manifest
    commit_file = stack_fixtures.ProjectStacksTest.commit_file
    fake_pull_requests = stack_fixtures.ProjectStacksTest.fake_pull_requests

    def mixed_project(self) -> Path:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "first", cwd=self.slots / "p3")
        self.commit_file(self.layer("first"), "first.txt", "First root\n")
        self.cli("branch", "add", "alpha", "second", cwd=self.slots / "p3")
        self.commit_file(self.layer("second"), "second.txt", "Second root\n")
        for root in ("first", "second"):
            child = root + "-child"
            self.cli("branch", "add", "alpha", child, "--after", root, cwd=self.slots / "p3")
            self.commit_file(self.layer(child), child + ".txt", child + " work\n")
        self.cli("branch", "add", "alpha", "standalone", cwd=self.slots / "p3")
        self.commit_file(self.layer("standalone"), "standalone.txt", "Independent work\n")
        return source

    def integrate_members(self, source: Path) -> None:
        for name in ("first-child", "second-child", "standalone"):
            self.git(source, "merge", "--no-edit", "baton/example--" + name)
        self.git(source, "push")

    def test_cleanup_unstacks_all_groups_and_removes_independent_members(self) -> None:
        source = self.mixed_project()
        catalog = source / ".git" / "gh-stack"
        self.assertEqual(len(json.loads(catalog.read_text())["stacks"]), 2)
        self.integrate_members(source)
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.cli("cleanup", "p3")
        self.assertEqual(trace.read_text().count("git fetch origin refs/heads/main:refs/remotes/origin/main"), 1)
        self.assertEqual(json.loads(catalog.read_text())["stacks"], [])
        for name in ("first", "first-child", "second", "second-child", "standalone"):
            self.assertFalse(self.layer(name).exists())
            self.git(source, "show-ref", "--verify", "refs/heads/baton/example--" + name, ok=False)

    def test_status_distinguishes_independent_members_from_multiple_stacks(self) -> None:
        self.mixed_project()
        output = self.cli("status", "p3").stdout
        self.assertIn("\n    git: ## baton/example--standalone\n", output)
        self.assertNotIn("\n## ", output)
        log = self.git(self.slots / "p3", "log", "--format=%s").splitlines()
        self.assertIn("Record project example: add baton/example--standalone", log)
        self.assertIn("Record project example", log)
        self.assertIn("native stack baton/example--first; PR base: main", output)
        self.assertIn("native stack baton/example--second; PR base: baton/example--second", output)
        self.assertIn("standalone; independent; PR base: main", output)

    def test_native_rebase_journal_leaves_independent_members_reliable(self) -> None:
        source = self.mixed_project()
        (source / ".git" / "gh-stack-rebase-state").write_text("{}")
        # A native rebase checks out other branches in member worktrees.
        self.git(self.layer("first-child"), "switch", "--detach")
        data = json.loads(self.cli("status", "--json", "p3").stdout)
        by_member = {entry["member"]: entry for entry in data["entries"]}
        self.assertEqual((by_member["standalone"]["base_reliable"], by_member["standalone"]["needs_rebase"]), (True, False))
        for name in ("first", "first-child", "second", "second-child"):
            self.assertFalse(by_member[name]["base_reliable"])
            self.assertIsNone(by_member[name]["needs_rebase"])
            self.assertIn("native gh stack rebase", by_member[name]["base_error"])
        failed = self.cli("branch", "add", "alpha", "extra", cwd=self.slots / "p3", ok=False)
        self.assertIn("native gh stack rebase", failed.stderr)
        self.assertNotIn("Unexpected branch", failed.stderr)

    def test_unmerged_stack_children_list_own_commits_and_members_can_be_allowed(self) -> None:
        source = self.mixed_project()
        self.git(source, "merge", "--no-edit", "baton/example--standalone")
        self.git(source, "push")
        failed = self.cli("cleanup", "p3", "--dry-run", ok=False).stderr
        child = failed.split("(baton/example--first-child versus", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Own commits on top of baton/example--first:", child)
        self.assertIn("Add first-child.txt", child)
        self.assertNotIn("Add first.txt", child)
        unknown = self.cli("cleanup", "p3", "--dry-run", "--allow-unmerged", "alpha:missing", ok=False)
        self.assertIn("Unknown member in --allow-unmerged: alpha:missing", unknown.stderr)
        allow = [arg for name in ("first", "first-child", "second") for arg in ("--allow-unmerged", "alpha:" + name)]
        partial = self.cli("cleanup", "p3", "--dry-run", *allow, ok=False).stderr
        self.assertIn("(baton/example--second-child versus", partial)
        self.assertNotIn("(baton/example--first-child versus", partial)
        self.cli("cleanup", "p3", "--dry-run", *allow, "--allow-unmerged", "alpha:second-child")
        empty = self.cli("cleanup", "p3", "--dry-run", "--allow-unmerged", "alpha:", ok=False)
        self.assertIn("Empty member in --allow-unmerged: alpha:", empty.stderr)
        # Without a terminal, the sibling that still needs proof stops the real cleanup before any change.
        refused = self.cli("cleanup", "p3", *allow, ok=False)
        self.assertIn("Interactive confirmation is required", refused.stderr)
        self.assertTrue(self.layer("first-child").exists())
        tips = {name: self.git(source, "rev-parse", "baton/example--" + name)
                for name in ("first", "first-child", "second", "second-child")}
        self.cli("cleanup", "p3", *allow, "--allow-unmerged", "alpha:second-child")
        archived = set(self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/").splitlines())
        self.assertLessEqual(set(tips.values()), archived)
        self.assertFalse(self.layer("first-child").exists())

    def test_cleanup_flags_ignored_notes_until_they_are_archived(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        (source / ".git" / "info" / "exclude").write_text("/notes\n")
        notes = self.checkout("alpha") / "notes" / "shared" / "research"
        notes.mkdir(parents=True)
        (notes / "design.md").write_text("Findings\n")
        failed = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("1 unarchived ignored note files", failed.stderr)
        self.assertIn("notes/shared/research/design.md", failed.stderr)
        archive = artifact_path(self.projects, self.artifacts) / "notes" / "shared" / "research"
        archive.mkdir(parents=True)
        (archive / "design.md").write_text("Older findings\n")
        self.assertIn("unarchived", self.cli("cleanup", "p3", "--dry-run", ok=False).stderr)
        shutil.copy(notes / "design.md", archive / "design.md")
        self.cli("cleanup", "p3", "--dry-run")
        # A symlinked notes directory lives elsewhere, so removing the worktree does not delete its files.
        shutil.rmtree(self.checkout("alpha") / "notes")
        (self.checkout("alpha") / "notes").symlink_to(notes, target_is_directory=True)
        self.cli("cleanup", "p3", "--dry-run")

    def test_cleanup_checks_configured_note_directories_and_rejects_escaping_paths(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        (source / ".git" / "info" / "exclude").write_text("/research/\n")
        directory = self.checkout("alpha") / "research"
        directory.mkdir()
        (directory / "finding.md").write_text("Research evidence\n")
        self.env["BATON_NOTE_DIRS"] = "research"
        self.assertEqual(json.loads(self.cli("status", "example", "--json").stdout)["note_directories"], ["research"])
        failed = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("research/finding.md", failed.stderr)
        archive = artifact_path(self.projects, self.artifacts) / "research"
        archive.mkdir(parents=True)
        shutil.copy(directory / "finding.md", archive / "finding.md")
        self.cli("cleanup", "p3", "--dry-run")
        for name in ("../outside", ".git", "notes/.git", "notes/.GIT"):
            with self.subTest(name=name):
                self.env["BATON_NOTE_DIRS"] = name
                failed = self.cli("cleanup", "p3", "--dry-run", ok=False)
                message = "worktree-relative note directories" if name == "../outside" else "outside Git metadata"
                self.assertIn(message, failed.stderr)
                self.assertTrue(directory.exists())

    def test_status_rejects_note_symlinks_into_git_metadata(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.env["BATON_NOTE_DIRS"] = "research"
        for root in (self.checkout("alpha"), artifact_path(self.projects, self.artifacts)):
            with self.subTest(root=root):
                notes = root / "research"
                notes.symlink_to(source / ".git", target_is_directory=True)
                failed = self.cli("status", "example", "--json", ok=False)
                self.assertIn("outside Git metadata", failed.stderr)
                notes.unlink()

    def test_default_notes_guard_preserves_thoughts_for_cleanup_and_retirement(self) -> None:
        self.env.pop("BATON_NOTE_DIRS", None)
        source = self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "first", cwd=self.slots / "p3")
        (source / ".git" / "info" / "exclude").write_text("/notes/\n/thoughts/\n")
        directory = self.layer("first")
        for name in ("notes", "thoughts"):
            (directory / name).mkdir()
            (directory / name / "research.md").write_text(name + " evidence\n")
        status = json.loads(self.cli("status", "example", "--json").stdout)
        self.assertEqual(status["note_directories"], ["notes", "thoughts"])
        for command in (("cleanup", "p3", "--dry-run"),
                        ("branch", "cleanup", "alpha", "first", "--selector", "p3", "--dry-run")):
            failed = self.cli(*command, ok=False)
            self.assertIn("notes/research.md", failed.stderr)
            self.assertIn("thoughts/research.md", failed.stderr)
            self.assertTrue(directory.is_dir())
        archive = artifact_path(self.projects, self.artifacts)
        for name in ("notes", "thoughts"):
            (archive / name).mkdir(parents=True)
            shutil.copy(directory / name / "research.md", archive / name / "research.md")
        self.cli("cleanup", "p3", "--dry-run")
        self.cli("branch", "cleanup", "alpha", "first", "--selector", "p3", "--dry-run")

    def test_notes_guard_covers_content_patterns_and_honors_wrap_skip_list(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        (source / ".git" / "info" / "exclude").write_text("/notes/*\n")
        notes = self.checkout("alpha") / "notes"
        notes.mkdir()
        (notes / "relevant.md").write_text("Keep\n")
        (notes / "index.md").write_text("Generated index\n")
        failed = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("2 unarchived ignored note files", failed.stderr)
        archive = artifact_path(self.projects, self.artifacts)
        (archive / "notes").mkdir(parents=True)
        shutil.copy(notes / "relevant.md", archive / "notes" / "relevant.md")
        (archive / "archive-skipped.txt").write_text("notes/index.md\n")
        self.cli("cleanup", "p3", "--dry-run")

    def test_cleanup_resumes_after_unstacking_only_one_group(self) -> None:
        source = self.mixed_project()
        self.integrate_members(source)
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        self.assertIsNotNone(actual_gh)
        bindir = self.root / "interrupt-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(str(actual_gh))} \"$@\" || exit $?\n"
            'if [ "$1" = "stack" ] && [ "$2" = "unstack" ]; then exit 1; fi\n'
        )
        wrapper.chmod(0o755)
        original_path = self.env["PATH"]
        self.env["PATH"] = str(bindir) + ":" + original_path
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(len(json.loads((source / ".git" / "gh-stack").read_text())["stacks"]), 1)
        self.assertTrue(self.layer("first").exists())
        self.assertTrue(self.layer("second-child").exists())
        self.env["PATH"] = original_path
        self.cli("cleanup", "p3")
        self.assertFalse(self.layer("second-child").exists())

    def test_independent_sibling_cannot_prove_integration_through_another_sibling(self) -> None:
        source = self.make_repo("alpha")
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        self.create("alpha")
        self.cli("branch", "init", "alpha", "first", cwd=self.slots / "p3")
        self.commit_file(self.layer("first"), "first.txt", "First work\n")
        self.cli("branch", "add", "alpha", "second", cwd=self.slots / "p3")
        second_tip = self.commit_file(self.layer("second"), "second.txt", "Second work\n")
        self.git(self.layer("first"), "merge", "--squash", "baton/example--second")
        self.git(self.layer("first"), "commit", "-m", "Squash sibling into first")
        first_tip = self.git(self.layer("first"), "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example--first")
        self.git(source, "commit", "-m", "Squash first into main")
        self.git(source, "push")
        marker = self.fake_pull_requests([
            {"state": "MERGED", "headRefName": "baton/example--second", "headRefOid": second_tip,
             "baseRefName": "baton/example--first", "mergeCommit": {"oid": first_tip}},
            {"state": "MERGED", "headRefName": "baton/example--first", "headRefOid": first_tip,
             "baseRefName": "main", "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")}},
        ])
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("baton/example--second", result.stderr)
        self.assertEqual(marker.read_text().splitlines().count("list"), 2)
        self.assertTrue(self.layer("first").exists())
        self.assertTrue(self.layer("second").exists())

    def test_rename_rejects_multiple_independent_members_without_mutation(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "first", cwd=self.slots / "p3")
        self.cli("branch", "add", "alpha", "second", cwd=self.slots / "p3")
        before = self.manifest()
        result = self.cli("rename", "renamed", "p3", ok=False)
        self.assertIn("Multi-branch", result.stderr)
        self.assertEqual(self.manifest(), before)

    def test_cleanup_cannot_prove_a_native_group_through_a_different_group(self) -> None:
        source = self.mixed_project()
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        responses = []
        for root in ("first", "second"):
            child = root + "-child"
            child_tip = self.git(self.layer(child), "rev-parse", "HEAD")
            self.git(self.layer(root), "merge", "--squash", "baton/example--" + child)
            self.git(self.layer(root), "commit", "-m", "Squash child into " + root)
            responses.append({"state": "MERGED", "headRefName": "baton/example--" + child,
                              "headRefOid": child_tip, "baseRefName": "baton/example--" + root,
                              "mergeCommit": {"oid": self.git(self.layer(root), "rev-parse", "HEAD")}})
        second_tip = self.git(self.layer("second"), "rev-parse", "HEAD")
        self.git(self.layer("first"), "merge", "--squash", "baton/example--second")
        self.git(self.layer("first"), "commit", "-m", "Squash separate group into first")
        first_tip = self.git(self.layer("first"), "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example--first")
        self.git(source, "commit", "-m", "Squash first group into main")
        self.git(source, "merge", "--no-edit", "baton/example--standalone")
        self.git(source, "push")
        responses.extend([
            {"state": "MERGED", "headRefName": "baton/example--second", "headRefOid": second_tip,
             "baseRefName": "baton/example--first", "mergeCommit": {"oid": first_tip}},
            {"state": "MERGED", "headRefName": "baton/example--first", "headRefOid": first_tip,
             "baseRefName": "main", "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")}},
        ])
        self.fake_pull_requests(responses)
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("baton/example--second", result.stderr)
        self.assertTrue(self.layer("first").exists())
        self.assertTrue(self.layer("second-child").exists())


if __name__ == "__main__":
    unittest.main()
