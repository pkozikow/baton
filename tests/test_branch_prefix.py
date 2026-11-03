import json
from pathlib import Path
import tempfile
import unittest

import test_project_manager as fixtures
from baton.configuration import configured_branch_prefix, effective_paths
from baton.manifest import branch_suffix, subplan_directory


class BranchPrefixConfigurationTest(unittest.TestCase):
    def test_prefix_validation_and_normalization(self) -> None:
        self.assertEqual(configured_branch_prefix({}), "baton")
        for value, expected in (("team", "team"), ("team/", "team"), ("team/feature/", "team/feature")):
            with self.subTest(value=value):
                self.assertEqual(configured_branch_prefix({"branch_prefix": value}), expected)
        for value in (None, 1, "", "/", "/team", "team//feature", "team/../escape", "team.lock", "team space"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "branch_prefix"):
                    configured_branch_prefix({"branch_prefix": value})

    def test_suffixes_strip_only_recorded_prefix(self) -> None:
        self.assertEqual(branch_suffix("team/feature/example--api", "team/feature"), "example--api")
        self.assertEqual(branch_suffix("other/team/example", "team"), "other/team/example")
        self.assertEqual(branch_suffix("pm/example"), "pm/example")
        self.assertEqual(branch_suffix("baton/example"), "example")
        self.assertEqual(subplan_directory(Path("artifacts"), "repo", "team/feature/example", "team/feature"),
                         Path("artifacts/branches/repo/example"))



class BranchPrefixIntegrationTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create

    def configure(self, prefix: str) -> dict:
        return json.loads(self.cli("config", "set", "--branch-prefix", prefix).stdout)

    def test_custom_prefix_survives_config_changes_and_scopes_subplans(self) -> None:
        self.make_repo("alpha")
        self.make_repo("beta")
        configured = self.configure("team/feature/")
        self.assertEqual(configured["branch_prefix"], "team/feature")
        self.create("alpha")
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "team/feature/example")
        self.configure("different")
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        self.cli("add", "beta", cwd=self.slots / "p3")
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3")
        child = self.checkout("alpha", "example--docs")
        status = json.loads(self.cli("status", "p3", "--json").stdout)
        entry = next(entry for entry in status["entries"] if entry["member"] == "docs")
        directory = fixtures.artifact_path(self.projects, self.artifacts)
        plan = directory / "branches/alpha/example--docs/plan.md"
        self.assertEqual(entry["branch"], "team/feature/example--docs")
        self.assertEqual(entry["path"], str(child))
        self.assertEqual(entry["subplan_path"], str(plan))
        self.assertEqual(self.git(self.checkout("beta"), "branch", "--show-current"), "team/feature/example")
        plan.parent.mkdir(parents=True)
        plan.write_text("# Docs plan\n")
        self.cli("artifacts", "commit", "--selector", "p3", "--no-push", "-m", "Save own plan", str(plan), cwd=child)
        sibling = directory / "branches/alpha/example--api/plan.md"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("# Sibling plan\n")
        refused = self.cli("artifacts", "commit", "--selector", "p3", "--no-push", "-m", "Wrong scope", str(sibling),
                           cwd=child, ok=False)
        self.assertIn("subplan", refused.stderr)
        self.cli("cleanup", "p3")
        self.assertFalse(child.exists())


    def test_invalid_prefix_is_not_saved(self) -> None:
        self.configure("team")
        failed = self.cli("config", "set", "--branch-prefix", "../escape", ok=False)
        self.assertIn("branch_prefix", failed.stderr)
        self.assertEqual(json.loads(self.cli("config", "show").stdout)["branch_prefix"], "team")
