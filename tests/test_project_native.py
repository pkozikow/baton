import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

import test_project_manager as fixtures
import test_project_stacks as stack_fixtures

sys.path.insert(0, str(fixtures.SCRIPT.parent.parent))
from baton.errors import NativeOperationPending, ProjectError
from baton.native import integrated_stack, merged_pr, native_catalog


class NativeCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        fixtures.FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures.FIXTURES)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.catalog = self.directory / "gh-stack"
        self.repo = {"id": "alpha", "common_dir": str(self.directory)}
        self.stack = {"trunk": {"branch": "main"}, "branches": [{"branch": "pm/example"}]}

    def assert_rejected_without_writes(self, payload: Any) -> None:
        self.catalog.write_text(json.dumps(payload))
        before = self.catalog.read_bytes()
        files = set(self.directory.iterdir())
        with self.assertRaises(ProjectError):
            native_catalog(self.repo)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(set(self.directory.iterdir()), files)

    def test_rejects_malformed_catalog_schema_and_trunk(self) -> None:
        original = {"schemaVersion": 1, "stacks": [self.stack]}
        payloads = [{**original, "schemaVersion": value} for value in ("2", None, True, 2)]
        payloads.append({**original, "stacks": [{**original["stacks"][0], "trunk": None}]})
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_rejected_without_writes(payload)

    def test_absent_catalog_is_empty_without_creating_files(self) -> None:
        self.assertEqual(native_catalog(self.repo), [])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_supported_and_default_schema_preserve_catalog(self) -> None:
        for payload in ({"stacks": []}, {"stacks": [self.stack]},
                        {"schemaVersion": 1, "stacks": [self.stack]}):
            with self.subTest(payload=payload):
                self.catalog.write_text(json.dumps(payload))
                before = self.catalog.read_bytes()
                self.assertEqual(native_catalog(self.repo), payload["stacks"])
                self.assertEqual(self.catalog.read_bytes(), before)
                self.assertEqual(list(self.directory.iterdir()), [self.catalog])

    def test_pending_operations_block_catalog_reads_without_writes(self) -> None:
        self.catalog.write_text(json.dumps({"stacks": [self.stack]}))
        before = self.catalog.read_bytes()
        for kind, filename in (("rebase", "gh-stack-rebase-state"),
                               ("modify", "gh-stack-modify-state"),
                               ("migration", "gh-stack-migration")):
            with self.subTest(kind=kind):
                journal = self.directory / filename
                journal.write_text("Pending operation\n")
                with self.assertRaisesRegex(NativeOperationPending, kind):
                    native_catalog(self.repo)
                self.assertEqual(self.catalog.read_bytes(), before)
                self.assertEqual(journal.read_text(), "Pending operation\n")
                self.assertEqual(set(self.directory.iterdir()), {self.catalog, journal})
                journal.unlink()

    def test_rejects_malformed_catalog_and_branch_containers(self) -> None:
        for payload in (None, [], {}, {"stacks": None}, {"stacks": {}},
                        {"stacks": [None]}, {"stacks": [{"branches": []}]}):
            with self.subTest(payload=payload):
                self.assert_rejected_without_writes(payload)
        for trunk in ({}, {"branch": ""}, {"branch": 1}):
            with self.subTest(trunk=trunk):
                self.assert_rejected_without_writes({"stacks": [{**self.stack, "trunk": trunk}]})
        for branches in (None, {}, [], [None], [{}], [{"branch": 1}]):
            with self.subTest(branches=branches):
                self.assert_rejected_without_writes({"stacks": [{**self.stack, "branches": branches}]})
        for version in (0, -1, 1.0):
            with self.subTest(version=version):
                self.assert_rejected_without_writes({"schemaVersion": version, "stacks": [self.stack]})


class PRProofFixture(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    make_repo = fixtures.ProjectManagerTest.make_repo
    fake_github_cli = fixtures.ProjectManagerTest.fake_github_cli
    commit_file = stack_fixtures.ProjectStacksTest.commit_file

    branch = "pm/example"
    target = "refs/remotes/origin/main"
    fields = "state,headRefName,headRefOid,baseRefName,mergeCommit"

    def setUp(self) -> None:
        fixtures.FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures.FIXTURES)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repos = self.root / "repos"
        self.repos.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GIT_", "GH_", "GITHUB_"))}
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_COUNT="4",
            GIT_CONFIG_KEY_0="user.name", GIT_CONFIG_VALUE_0="Project Manager Test",
            GIT_CONFIG_KEY_1="user.email", GIT_CONFIG_VALUE_1="project-test@example.invalid",
            GIT_CONFIG_KEY_2="commit.gpgsign", GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="core.hooksPath", GIT_CONFIG_VALUE_3="/dev/null",
        )
        self.source = self.make_repo("alpha", remote=False)
        url = "https://github.com/test/alpha.git"
        self.git(self.source, "config", f"url.{self.source}.insteadOf", url)
        self.git(self.source, "remote", "add", "origin", url)
        self.git(self.source, "switch", "-c", self.branch)
        self.feature_tip = self.commit_file(self.source, "feature.txt", "Root feature\n")

    def squash_into(self, destination: str, branch: str) -> str:
        self.git(self.source, "switch", destination)
        self.git(self.source, "merge", "--squash", branch)
        self.git(self.source, "commit", "-m", f"Squash {branch} into {destination}")
        return self.git(self.source, "rev-parse", "HEAD")

    def pr(self, branch: str, head: str, base: str, merge: str) -> dict[str, Any]:
        return {"state": "MERGED", "headRefName": branch, "headRefOid": head,
                "baseRefName": base, "mergeCommit": {"oid": merge}}

    def assert_query(self, marker: Path, branches: list[str], *, base: bool = False) -> None:
        expected = []
        for branch in sorted(branches):
            expected.extend(["pr", "list", "--repo", "github.com/test/alpha", "--state", "merged",
                             "--head", branch])
            if base:
                expected.extend(["--base", "main"])
            expected.extend(["--limit", "100", "--json", self.fields])
        self.assertEqual(marker.read_text().splitlines(), expected)


class StandaloneMergedPRTest(PRProofFixture):
    def setUp(self) -> None:
        super().setUp()
        main_tip = self.squash_into("main", self.branch)
        self.git(self.source, "update-ref", self.target, main_tip)
        self.merged = self.pr(self.branch, self.feature_tip, "main", main_tip)
        self.git(self.source, "merge-base", "--is-ancestor", self.feature_tip, self.target, ok=False)

    def proof(self) -> bool:
        marker = self.root / "github-invocations"
        marker.unlink(missing_ok=True)
        with patch.dict(os.environ, self.env, clear=True):
            result = merged_pr(self.source, self.branch, self.git(self.source, "rev-parse", self.branch), self.target)
        self.assert_query(marker, [self.branch], base=True)
        return result

    def test_matching_squash_pr_proves_integration(self) -> None:
        marker = self.fake_github_cli([self.merged])
        self.assertTrue(self.proof())
        self.assert_query(marker, [self.branch], base=True)

    def test_squash_merge_requires_matching_pr_and_integrated_merge_commit(self) -> None:
        for field, value in (
            ("state", "CLOSED"), ("headRefName", "pm/other"), ("headRefOid", "0" * 40),
            ("baseRefName", "release"), ("mergeCommit", None), ("mergeCommit", {}),
            ("mergeCommit", {"oid": "0" * 40}),
            ("mergeCommit", {"oid": self.feature_tip}),
        ):
            with self.subTest(field=field, value=value):
                marker = self.fake_github_cli([{**self.merged, field: value}])
                marker.unlink(missing_ok=True)
                self.assertFalse(self.proof())
                self.assert_query(marker, [self.branch], base=True)

    def test_github_cli_failures_and_malformed_responses_cannot_prove_integration(self) -> None:
        for response, exit_code in (([self.merged], 1), ([], 0), ({"error": "unavailable"}, 0), ([None], 0)):
            with self.subTest(response=response, exit_code=exit_code):
                self.fake_github_cli(response, exit_code)
                self.assertFalse(self.proof())
        self.fake_github_cli([self.merged])
        (self.root / "github-response.json").write_text("not valid JSON")
        self.assertFalse(self.proof())

    def test_squash_pr_does_not_cover_new_local_commits(self) -> None:
        self.fake_github_cli([self.merged])
        self.git(self.source, "switch", self.branch)
        self.commit_file(self.source, "follow-up.txt", "Unmerged follow-up\n")
        self.assertFalse(self.proof())


class IntegratedStackPRTest(PRProofFixture):
    child = "pm/example--api"

    def setUp(self) -> None:
        super().setUp()
        self.git(self.source, "switch", "-c", self.child)
        child_tip = self.commit_file(self.source, "api.txt", "Child feature\n")
        root_tip = self.squash_into(self.branch, self.child)
        main_tip = self.squash_into("main", self.branch)
        self.git(self.source, "update-ref", self.target, main_tip)
        self.responses = [self.pr(self.child, child_tip, self.branch, root_tip),
                          self.pr(self.branch, root_tip, "main", main_tip)]
        self.repo = {"source": str(self.source), "branch": self.branch, "base_ref": self.target,
                     "stack_root": self.branch, "stacks": [{"root": self.branch}],
                     "members": [{"branch": self.child, "stack_root": self.branch}]}
        for branch in (self.branch, self.child):
            self.git(self.source, "merge-base", "--is-ancestor", branch, self.target, ok=False)

    def proof(self, evidence: dict[str, dict[str, Any]] | None = None) -> set[str]:
        marker = self.root / "github-invocations"
        marker.unlink(missing_ok=True)
        with patch.dict(os.environ, self.env, clear=True):
            result = integrated_stack(self.repo, self.target, evidence=evidence)
        self.assert_query(marker, [self.branch, self.child])
        return result

    def test_squash_chain_proves_both_exact_branch_tips(self) -> None:
        marker = self.fake_github_cli(self.responses)
        self.assertEqual(self.proof(), {self.branch, self.child})
        self.assert_query(marker, [self.branch, self.child])

    def test_child_pr_requires_merged_state_and_integrated_merge_commit(self) -> None:
        for field, value in (("state", "OPEN"), ("headRefOid", "0" * 40),
                             ("mergeCommit", {"oid": self.responses[0]["headRefOid"]}),
                             ("mergeCommit", None)):
            with self.subTest(field=field, value=value):
                self.fake_github_cli([{**self.responses[0], field: value}, self.responses[1]])
                self.assertEqual(self.proof(), {self.branch})

    def test_per_head_query_ignores_prs_for_other_heads(self) -> None:
        self.fake_github_cli([{**self.responses[0], "headRefName": "fork-owner:" + self.child}, self.responses[1]])
        self.assertEqual(self.proof(), {self.branch})

    def test_merge_into_unintegrated_parent_does_not_allow_cleanup(self) -> None:
        self.fake_github_cli(self.responses[:1])
        self.assertEqual(self.proof(), set())

    def test_merged_pr_cycle_is_not_integration_to_default(self) -> None:
        self.fake_github_cli([self.responses[0], {**self.responses[1], "baseRefName": self.child,
                                                 "mergeCommit": {"oid": self.responses[0]["headRefOid"]}}])
        self.assertEqual(self.proof(), set())

    def test_chain_requires_parents_current_tip_to_be_integrated(self) -> None:
        self.fake_github_cli(self.responses)
        self.git(self.source, "switch", self.branch)
        self.commit_file(self.source, "parent-follow-up.txt", "Unmerged parent follow-up\n")
        self.assertEqual(self.proof(), set())

    def test_chain_does_not_cover_new_local_child_commits(self) -> None:
        self.fake_github_cli(self.responses)
        self.git(self.source, "switch", self.child)
        self.commit_file(self.source, "child-follow-up.txt", "Unmerged child follow-up\n")
        self.assertEqual(self.proof(), {self.branch})

    def test_chain_cannot_cross_native_stack_groups(self) -> None:
        self.fake_github_cli(self.responses)
        self.repo["members"][0]["stack_root"] = self.child
        self.repo["stacks"].append({"root": self.child})
        self.assertEqual(self.proof(), {self.branch})


class DefaultMergedStackPRTest(PRProofFixture):
    child = "pm/example--api"
    proof = IntegratedStackPRTest.proof

    def setUp(self) -> None:
        super().setUp()
        self.git(self.source, "switch", "-c", self.child)
        self.child_tip = self.commit_file(self.source, "api.txt", "Child feature\n")
        root_merge = self.squash_into("main", self.branch)
        child_merge = self.squash_into("main", self.child)
        latest = self.commit_file(self.source, "api.txt", "Later main change\n")
        self.git(self.source, "update-ref", self.target, latest)
        self.responses = [self.pr(self.child, self.child_tip, self.branch, child_merge),
                          self.pr(self.branch, self.feature_tip, "main", root_merge)]
        self.repo = {"source": str(self.source), "branch": self.branch, "base_ref": self.target,
                     "stack_root": self.branch, "stacks": [{"root": self.branch}],
                     "members": [{"branch": self.child, "stack_root": self.branch}]}
        self.git(self.source, "merge-base", "--is-ancestor", child_merge, self.branch, ok=False)
        for branch in (self.branch, self.child):
            self.git(self.source, "merge-base", "--is-ancestor", branch, self.target, ok=False)

    def test_default_merge_proves_child_despite_stale_parent_base_and_later_changes(self) -> None:
        self.fake_github_cli(self.responses)
        evidence: dict[str, dict[str, Any]] = {}
        self.assertEqual(self.proof(evidence), {self.branch, self.child})
        self.assertEqual(evidence, {pr["headRefName"]: pr for pr in self.responses})

    def test_default_merged_child_does_not_require_parents_current_tip_to_be_integrated(self) -> None:
        self.fake_github_cli(self.responses)
        self.git(self.source, "switch", self.branch)
        self.commit_file(self.source, "follow-up.txt", "Unmerged parent follow-up\n")
        self.assertEqual(self.proof(), {self.child})

    def test_default_merge_does_not_cover_new_local_child_commits(self) -> None:
        self.fake_github_cli(self.responses)
        self.git(self.source, "switch", self.child)
        self.commit_file(self.source, "follow-up.txt", "Unmerged child follow-up\n")
        self.assertEqual(self.proof(), {self.branch})

    def test_default_merge_requires_valid_exact_merged_pr_and_reachable_commit(self) -> None:
        for field, value in (
            ("state", "OPEN"), ("headRefOid", "0" * 40),
            ("headRefName", []), ("baseRefName", []), ("baseRefName", "release"),
            ("mergeCommit", None), ("mergeCommit", []), ("mergeCommit", "invalid"),
            ("mergeCommit", {}), ("mergeCommit", {"oid": "invalid"}),
            ("mergeCommit", {"oid": "0" * 40}),
            ("mergeCommit", {"oid": self.child_tip}),
        ):
            with self.subTest(field=field, value=value):
                self.fake_github_cli([{**self.responses[0], field: value}, self.responses[1]])
                self.assertEqual(self.proof(), {self.branch})

    def test_stale_base_cannot_cross_native_stack_groups(self) -> None:
        self.fake_github_cli(self.responses)
        self.repo["members"][0]["stack_root"] = self.child
        self.repo["stacks"].append({"root": self.child})
        self.assertEqual(self.proof(), {self.branch})


if __name__ == "__main__":
    unittest.main()
