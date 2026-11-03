"""Closed-PR retirement exercised through real isolated project and Git fixtures."""

import json
import os
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from baton import retirement
import test_project_branches as branches
import test_project_retirement as retirement_fixtures


class AbandonedRetirementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = retirement_fixtures.ProjectRetirementTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.env.update(GH_HOST="127.0.0.1:9", GH_TOKEN="offline-test-token")
        self.pr_marker: Path | None = None

    def github_remote(self) -> Path:
        source = self.f.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.f.git(source, "config", f"url.{self.f.root / 'alpha.git'}.insteadOf", url)
        self.f.git(source, "remote", "set-url", "origin", url)
        return source

    @staticmethod
    def pull_request(member: str, tip: str, state: str = "CLOSED", *, base: str = "main") -> dict[str, Any]:
        return {"state": state, "headRefName": "baton/example--" + member,
                "headRefOid": tip, "baseRefName": base, "mergeCommit": None}

    def responses(self, payload: Any, *, filter_head: bool = True) -> None:
        if self.pr_marker is None:
            self.pr_marker = self.f.fake_pull_requests(payload, filter_head=filter_head)
        else:
            (self.f.root / "pull-requests.json").write_text(json.dumps(payload))

    def assert_abandoned(self, member: str, path: Path, tip: str) -> None:
        self.f.assert_retired(member, path, tip)
        record = self.f.entry(member)["retirement"]
        self.assertEqual(record["outcome"], "abandoned")
        self.assertEqual(record["pull_requests"]["baton/example--" + member]["state"], "CLOSED")
        self.assertEqual(self.f.git(self.f.repos / "alpha", "rev-parse", record["archive_ref"]), tip)

    def native_group(self) -> dict[str, Path]:
        first = self.f.init_members()
        self.f.commit_file(first, "api.txt", "API\n")
        child = self.f.add_member("tests", after="api")
        self.f.commit_file(child, "tests.txt", "Tests\n")
        top = self.f.add_member("top", after="tests")
        self.f.commit_file(top, "top.txt", "Top\n")
        return {"api": first, "tests": child, "top": top}

    def test_exact_closed_unmerged_tip_is_archived_as_abandoned(self) -> None:
        first = self.f.init_members()
        tip = self.f.commit_file(first, "api.txt", "Abandoned API\n")
        docs = self.f.add_member("docs")
        source = self.github_remote()
        proof = self.pull_request("api", tip)
        self.responses([proof])
        self.f.git(source, "merge-base", "--is-ancestor", tip, "origin/main", ok=False)
        before = self.f.snapshot()
        self.f.cleanup_member("api", dry_run=True)
        self.assertEqual(self.f.snapshot(), before)
        self.f.cleanup_member("api")
        self.assert_abandoned("api", first, tip)
        self.assertEqual(self.f.entry("api")["retirement"]["pull_requests"]["baton/example--api"], proof)
        self.assertTrue(docs.exists())
        self.assertEqual(self.f.status()["errors"], [])
        self.assertIn("--state\nopen\n", self.pr_marker.read_text())
        self.assertIn("--state\nclosed\n", self.pr_marker.read_text())

    def test_open_pr_blocks_even_integrated_tip_with_an_exact_closed_proof(self) -> None:
        first = self.f.init_members()
        self.f.commit_file(first, "api.txt", "Integrated API\n")
        tip = self.f.integrate("api")
        self.github_remote()
        self.responses([self.pull_request("api", tip), self.pull_request("api", "f" * 40, "OPEN")])
        before = self.f.snapshot()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                result = self.f.cleanup_member("api", dry_run=dry_run, ok=False)
                self.assertIn("open", result.stderr.lower())
                self.assertNotIn("[y/N]", result.stderr)
                self.assertEqual(self.f.snapshot(), before)
                self.assertTrue(first.exists())
                self.assertNotIn("pending_retirement", self.f.project())

    def test_no_pr_or_nonmatching_closed_tip_cannot_authorize_abandonment(self) -> None:
        first = self.f.init_members()
        tip = self.f.commit_file(first, "api.txt", "Unmerged API\n")
        self.github_remote()
        cases = ([], [self.pull_request("api", "f" * 40)], [self.pull_request("other", tip)])
        for payload in cases:
            with self.subTest(payload=payload):
                self.responses(payload)
                before = self.f.snapshot()
                result = self.f.cleanup_member("api", ok=False)
                self.assertIn("integrated", result.stderr.lower())
                self.assertEqual(self.f.snapshot(), before)
                self.assertTrue(first.exists())

    def test_malformed_github_responses_block_even_integrated_members(self) -> None:
        first = self.f.init_members()
        self.f.commit_file(first, "api.txt", "Integrated API\n")
        tip = self.f.integrate("api")
        self.github_remote()
        for payload in ({"unexpected": "object"}, ["not a PR"], [{"state": "CLOSED"}],
                        [{**self.pull_request("api", tip), "state": "UNKNOWN"}]):
            with self.subTest(payload=payload):
                self.responses(payload, filter_head=False)
                before = self.f.snapshot()
                result = self.f.cleanup_member("api", ok=False)
                self.assertIn("verify", result.stderr.lower())
                self.assertEqual(self.f.snapshot(), before)
                self.assertTrue(first.exists())

    def test_github_query_failure_is_not_bypassed_by_git_integration(self) -> None:
        first = self.f.init_members()
        self.f.commit_file(first, "api.txt", "Integrated API\n")
        self.f.integrate("api")
        self.github_remote()
        self.responses([])
        (self.f.root / "bin" / "gh").write_text("#!/bin/sh\nprintf 'GitHub unavailable\\n' >&2\nexit 1\n")
        before = self.f.snapshot()
        result = self.f.cleanup_member("api", ok=False)
        self.assertIn("verify", result.stderr.lower())
        self.assertEqual(self.f.snapshot(), before)
        self.assertTrue(first.exists())

    def test_prune_retires_closed_member_and_skips_open_member(self) -> None:
        first = self.f.init_members()
        tip = self.f.commit_file(first, "api.txt", "Abandoned API\n")
        docs = self.f.add_member("docs")
        docs_tip = self.f.commit_file(docs, "docs.txt", "Open docs\n")
        source = self.github_remote()
        self.responses([self.pull_request("api", tip), self.pull_request("docs", docs_tip, "OPEN")])
        result = self.f.interactive("branch", "prune", "alpha", "--selector", "example", reply=b"y\n")
        self.assert_abandoned("api", first, tip)
        self.assertTrue(docs.exists())
        self.assertEqual(self.f.git(source, "rev-parse", "baton/example--docs"), docs_tip)
        self.assertIn("open", result.stderr.lower())
        self.assertEqual([entry["member"] for entry in self.f.status()["entries"]], ["docs"])

    def test_whole_abandoned_native_group_unstacks_without_changing_sibling_group(self) -> None:
        source, paths = branches.ProjectBranchesTest.two_groups(self.f)
        tips = {member: self.f.git(path, "rev-parse", "HEAD") for member, path in zip(
            ("api", "api-tests", "docs", "docs-tests", "tooling"), paths)}
        sibling = self.f.catalog(source)["stacks"][1]
        self.github_remote()
        self.responses([self.pull_request(member, tip, "CLOSED" if member in ("api", "api-tests") else "OPEN")
                        for member, tip in tips.items()])
        self.f.interactive("branch", "prune", "alpha", "--selector", "example", reply=b"y\n")
        self.assert_abandoned("api", paths[0], tips["api"])
        self.assert_abandoned("api-tests", paths[1], tips["api-tests"])
        self.assertEqual(self.f.catalog(source)["stacks"], [sibling])
        for member, path in zip(("docs", "docs-tests", "tooling"), paths[2:]):
            self.assertTrue(path.exists())
            self.assertEqual(self.f.git(path, "rev-parse", "HEAD"), tips[member])
        self.assertEqual(self.f.status()["errors"], [])

    def test_partial_abandoned_native_member_is_blocked_at_every_position(self) -> None:
        paths = self.native_group()
        self.github_remote()
        tips = {member: self.f.git(path, "rev-parse", "HEAD") for member, path in paths.items()}
        for selected in paths:
            with self.subTest(selected=selected):
                self.responses([self.pull_request(member, tip, "CLOSED" if member == selected else "OPEN")
                                for member, tip in tips.items()])
                before = self.f.snapshot()
                for dry_run in (True, False):
                    result = self.f.cleanup_member(selected, dry_run=dry_run, ok=False)
                    self.assertIn("whole group", result.stderr.lower())
                    self.assertEqual(self.f.snapshot(), before)
                for dry_run in (True, False):
                    result = self.f.cli("branch", "prune", "alpha", "--selector", "example",
                                        *(["--dry-run"] if dry_run else []))
                    self.assertIn("whole group", result.stderr.lower())
                    self.assertNotIn("[y/N]", result.stderr)
                    self.assertEqual(self.f.snapshot(), before)
                self.assertNotIn("pending_retirement", self.f.project())
                self.assertTrue(all(path.exists() for path in paths.values()))

    def test_prune_skips_unsafe_native_group_but_retires_unrelated_abandoned_member(self) -> None:
        paths = self.native_group()
        tooling = self.f.add_member("tooling")
        tooling_tip = self.f.commit_file(tooling, "tooling.txt", "Abandoned standalone work\n")
        source = self.github_remote()
        catalog_before = (source / ".git" / "gh-stack").read_bytes()
        tips = {member: self.f.git(path, "rev-parse", "HEAD") for member, path in paths.items()}
        self.responses([self.pull_request(member, tip, "CLOSED" if member == "api" else "OPEN")
                        for member, tip in tips.items()] + [self.pull_request("tooling", tooling_tip)])
        result = self.f.interactive("branch", "prune", "alpha", "--selector", "example", reply=b"y\n")
        self.assert_abandoned("tooling", tooling, tooling_tip)
        self.assertIn("whole group", result.stderr.lower())
        self.assertEqual((source / ".git" / "gh-stack").read_bytes(), catalog_before)
        self.assertTrue(all(path.exists() for path in paths.values()))
        self.assertEqual({entry["member"] for entry in self.f.status()["entries"]}, set(paths))

    def test_reopened_pr_blocks_resumed_retirement_before_destruction(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())
        first = self.f.init_members()
        tip = self.f.commit_file(first, "api.txt", "Abandoned API\n")
        source = self.github_remote()
        self.responses([self.pull_request("api", tip)])
        original_git = retirement.git

        def fail_removal(repo: Path, *args: str, **kwargs: Any) -> str:
            if args[:2] == ("worktree", "remove"):
                raise branches.ProjectError("injected removal interruption")
            return original_git(repo, *args, **kwargs)

        with patch.dict(os.environ, self.f.env, clear=True):
            manager = branches.Manager()
            manager.load()
            with manager.command_lock("p3"):
                with patch.object(retirement, "git", side_effect=fail_removal):
                    with self.assertRaisesRegex(branches.ProjectError, "injected removal interruption"):
                        manager.retire_members("alpha", "api", "p3", False)
        self.assertEqual(self.f.project()["state"], "retiring")
        self.assertTrue(first.exists())
        self.assertEqual(self.f.git(source, "rev-parse", "baton/example--api"), tip)
        self.responses([self.pull_request("api", tip, "OPEN")])
        before = self.f.snapshot()
        result = self.f.cleanup_member("api", ok=False)
        self.assertIn("open", result.stderr.lower())
        self.assertEqual(self.f.snapshot(), before)
        self.assertTrue(first.exists())
        self.assertEqual(self.f.git(source, "rev-parse", "baton/example--api"), tip)


if __name__ == "__main__":
    unittest.main()
