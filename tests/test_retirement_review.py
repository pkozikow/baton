"""Regression cases identified during the member-retirement safety review."""

import os
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import test_project_branches as branches
import test_project_retirement as retirement_tests
from baton import retirement


class RetirementReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = retirement_tests.ProjectRetirementTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(os.chdir, Path.cwd())

    def test_resume_does_not_delete_branch_checked_out_in_a_different_worktree(self) -> None:
        fixture = self.fixture
        first = fixture.init_members()
        tip = fixture.integrate("api")
        source = fixture.repos / "alpha"
        original_git = retirement.git

        def fail_ref_removal(repo: Path, *args: str, **kwargs: Any) -> str:
            if args[:3] == ("update-ref", "-d", "refs/heads/baton/example--api"):
                raise branches.ProjectError("injected ref removal failure")
            return original_git(repo, *args, **kwargs)

        with patch.dict(os.environ, fixture.env, clear=True):
            manager = branches.Manager()
            manager.load()
            with manager.command_lock("p3"):
                with patch.object(retirement, "git", side_effect=fail_ref_removal):
                    with self.assertRaisesRegex(branches.ProjectError, "injected ref removal failure"):
                        manager.retire_members("alpha", "api", "p3", False)
        self.assertFalse(first.exists())
        external = fixture.root / "external-worktree"
        fixture.git(source, "worktree", "add", str(external), "baton/example--api")
        before = fixture.snapshot()
        result = fixture.cleanup_member("api", ok=False)
        self.assertIn("worktree", result.stderr.lower())
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(fixture.git(source, "rev-parse", "refs/heads/baton/example--api"), tip)
        self.assertEqual(fixture.git(external, "rev-parse", "HEAD"), tip)
        fixture.git(source, "worktree", "remove", str(external))
        fixture.cleanup_member("api")
        fixture.assert_retired("api", first, tip)


if __name__ == "__main__":
    unittest.main()
