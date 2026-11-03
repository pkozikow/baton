import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

import test_project_branches as fixtures

sys.path.insert(0, str(fixtures.fixtures.SCRIPT.parent.parent))
from baton.configuration import read_config
from baton.manager import Manager


class ProjectAliasesTest(unittest.TestCase):
    setUp = fixtures.ProjectBranchesTest.setUp
    run_command = fixtures.ProjectBranchesTest.run_command
    git = fixtures.ProjectBranchesTest.git
    cli = fixtures.ProjectBranchesTest.cli
    make_repo = fixtures.ProjectBranchesTest.make_repo
    checkout = fixtures.ProjectBranchesTest.checkout
    create = fixtures.ProjectBranchesTest.create
    native = fixtures.ProjectBranchesTest.native
    commit_file = fixtures.ProjectBranchesTest.commit_file
    member = fixtures.ProjectBranchesTest.member
    init_members = fixtures.ProjectBranchesTest.init_members
    add_member = fixtures.ProjectBranchesTest.add_member

    def configure(self, *arguments: str) -> dict:
        return json.loads(self.cli("config", "set", *arguments).stdout)

    def test_alias_create_add_path_handoff_branch_and_cleanup(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.configure("--repo-alias", "api=alpha", "--repo-alias", "web=Beta")
        self.create("api")
        self.cli("add", "web", cwd=self.slots / "p3")
        self.assertEqual(self.cli("path", "p3", "web").stdout.strip(), str(self.checkout("Beta")))
        self.cli("branch", "init", "api", "first", cwd=self.slots / "p3")
        first = self.member("first", "api")
        handoff = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "api",
                           "--member", "first").stdout
        self.assertIn("Repository: alpha", handoff)
        self.assertIn(str(first), handoff)
        self.commit_file(first, "api.txt", "Implementation\n")
        self.cli("cleanup", "p3", "--allow-unmerged", "api:first")
        self.assertFalse(first.exists())
        self.assertFalse(self.checkout("Beta").exists())

    def test_stack_alias_is_resolved_once_and_canonical_manifest_names_are_retained(self) -> None:
        self.make_repo("Beta")
        self.make_repo("gamma")
        self.configure("--repo-alias", "api=Beta", "--repo-alias", "Beta=gamma")
        self.create("api")
        self.assertEqual(self.cli("path", "p3", "api").stdout.strip(), str(self.checkout("Beta")))
        self.commit_file(self.checkout("Beta"), "api.txt", "Stable implementation\n")
        self.cli("branch", "add", "api", "tests", "--after", "baton/example", cwd=self.slots / "p3")
        child = self.member("tests", "api")
        self.assertTrue(child.exists())
        self.assertIn("baton/example--tests", self.cli("stack", "status", "api", cwd=child).stdout)
        status = json.loads(self.cli("status", "example", "--json").stdout)
        self.assertEqual({entry["repo"] for entry in status["entries"]}, {"Beta"})
        # Beta is itself an alias key, so the refresh line must replay the alias that selected it.
        handoff = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "api", "--member", "tests").stdout
        refresh = next(line.removeprefix("Refresh instructions: ") for line in handoff.splitlines()
                       if line.startswith("Refresh instructions: "))
        self.assertIn("--repo api --member tests", refresh)
        self.assertEqual(self.run_command(shlex.split(refresh), self.root).stdout, handoff)

    def test_branch_retirement_and_prune_accept_alias(self) -> None:
        source = self.make_repo("alpha")
        self.configure("--repo-alias", "api=alpha")
        self.create("api")
        self.cli("branch", "init", "api", "first", cwd=self.slots / "p3")
        first = self.member("first", "api")
        self.commit_file(first, "first.txt", "Merged implementation\n")
        self.git(source, "merge", "--no-edit", "baton/example--first")
        self.git(source, "push")
        self.cli("branch", "prune", "api", "--selector", "example", "--dry-run")
        self.cli("branch", "cleanup", "api", "first", "--selector", "example")
        self.assertFalse(first.exists())

    def test_updated_and_removed_aliases_apply_to_later_commands(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta")
        self.configure("--repo-alias", "api=alpha")
        self.create("api")
        self.cli("add", "Beta", cwd=self.slots / "p3")
        self.assertEqual(self.cli("path", "p3", "api").stdout.strip(), str(self.checkout("alpha")))
        self.configure("--repo-alias", "api=Beta")
        self.assertEqual(self.cli("path", "p3", "api").stdout.strip(), str(self.checkout("Beta")))
        self.configure("--remove-repo-alias", "api")
        self.assertIn("not part of project: api", self.cli("path", "p3", "api", ok=False).stderr)
        self.assertEqual(self.cli("path", "p3", "alpha").stdout.strip(), str(self.checkout("alpha")))
        self.assertEqual(json.loads(self.cli("config", "show").stdout)["repo_aliases"], {})

    def test_explicit_source_paths_bypass_aliases_and_duplicates_are_rejected(self) -> None:
        source = self.make_repo("alpha")
        self.make_repo("Beta")
        self.configure("--repo-alias", "alpha=Beta")
        self.create(str(source))
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertFalse(self.checkout("Beta").exists())
        self.configure("--repo-alias", "api=alpha")
        failed = self.cli("add", "api", cwd=self.slots / "p3", ok=False)
        self.assertIn("Duplicate repository", failed.stderr)

    def test_aliases_are_per_manager_snapshots_and_config_changes_preserve_roots(self) -> None:
        configured = self.configure("--repo-alias", "api=alpha", "--worktrees-root", str(self.worktrees))
        self.assertEqual(configured["repo_aliases"], {"api": "alpha"})
        with patch.dict(os.environ, self.env, clear=True):
            first = Manager()
        self.configure("--repo-alias", "api=Beta")
        with patch.dict(os.environ, self.env, clear=True):
            second = Manager()
        self.configure("--remove-repo-alias", "api")
        with patch.dict(os.environ, self.env, clear=True):
            third = Manager()
            self.assertEqual(read_config()["paths"]["worktrees_root"], str(self.worktrees))
        self.assertEqual(first.resolve_repo("api"), "alpha")
        self.assertEqual(second.resolve_repo("api"), "Beta")
        self.assertEqual(third.resolve_repo("api"), "api")

    def test_invalid_alias_configuration_does_not_replace_saved_settings(self) -> None:
        self.configure("--repo-alias", "api=alpha")
        config = Path(self.env["BATON_CONFIG_FILE"])
        original = config.read_bytes()
        for arguments in (("--repo-alias", "api"), ("--repo-alias", "../api=alpha"),
                          ("--repo-alias", "api=../alpha"), ("--repo-alias", "api=a=b"),
                          ("--remove-repo-alias", "../api"),
                          ("--repo-alias", "api=Beta", "--remove-repo-alias", "api")):
            with self.subTest(arguments=arguments):
                self.cli("config", "set", *arguments, ok=False)
                self.assertEqual(config.read_bytes(), original)
        for invalid in ([], {"api": []}, {"../api": "alpha"}, {"api": "/alpha"}):
            with self.subTest(invalid=invalid):
                config.write_text(json.dumps({"repo_aliases": invalid}))
                failed = self.cli("config", "show", ok=False)
                self.assertIn("repo_aliases must map simple repository names", failed.stderr)
        config.write_bytes(original)

    def test_status_reports_invalid_config_without_traceback_or_fallback(self) -> None:
        Path(self.env["BATON_CONFIG_FILE"]).write_text("invalid json")
        failed = self.cli("status", "--json", ok=False)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("baton:", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)
