"""Offline compatibility checks against the installed native gh stack CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from typing import Any

import test_project_manager as fixtures


@unittest.skipUnless(shutil.which("gh"), "Native gh stack CLI is not installed")
class NativeRetirementCompatibilityTest(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    make_repo = fixtures.ProjectManagerTest.make_repo

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
            GIT_CONFIG_KEY_0="user.name", GIT_CONFIG_VALUE_0="Native Retirement Test",
            GIT_CONFIG_KEY_1="user.email", GIT_CONFIG_VALUE_1="retirement@example.invalid",
            GIT_CONFIG_KEY_2="commit.gpgsign", GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="core.hooksPath", GIT_CONFIG_VALUE_3="/dev/null",
            GH_HOST="127.0.0.1:9", GH_TOKEN="offline-test-token",
        )
        version = subprocess.run(["gh", "stack", "--version"], env=self.env,
                                 text=True, capture_output=True, timeout=10)
        if version.returncode:
            self.skipTest("Native gh stack CLI is not available")

    def native(self, path: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run_command(["gh", "stack", *args], path, ok=ok)

    def make_stack(self, name: str) -> tuple[Path, dict[str, Path]]:
        source = self.make_repo(name, remote=False)
        paths: dict[str, Path] = {}
        parent = "main"
        for branch in ("bottom", "middle", "top"):
            path = self.root / name / branch
            self.git(source, "worktree", "add", "-b", branch, str(path), parent)
            (path / f"{branch}.txt").write_text(f"{branch}\n")
            self.git(path, "add", f"{branch}.txt")
            self.git(path, "commit", "-m", branch)
            paths[branch] = path
            parent = branch
        self.native(paths["top"], "init", "--base", "main", "bottom", "middle", "top")
        return source, paths

    def retire(self, source: Path, paths: dict[str, Path], branch: str,
               *, delete_ref: bool) -> None:
        catalog = source / ".git" / "gh-stack"
        data = json.loads(catalog.read_text())
        member = next(item for item in data["stacks"][0]["branches"] if item["branch"] == branch)
        member["pullRequest"] = {"merged": True}
        catalog.write_text(json.dumps(data))
        self.git(source, "worktree", "remove", str(paths[branch]))
        if delete_ref:
            self.git(source, "update-ref", "-d", f"refs/heads/{branch}")

    def catalog(self, source: Path) -> dict[str, Any]:
        return json.loads((source / ".git" / "gh-stack").read_text())

    def test_missing_merged_members_have_native_position_constraints(self) -> None:
        for branch in ("bottom", "middle", "top"):
            for delete_ref in (False, True):
                with self.subTest(branch=branch, delete_ref=delete_ref):
                    source, paths = self.make_stack(f"{branch}-{delete_ref}")
                    self.retire(source, paths, branch, delete_ref=delete_ref)
                    active = paths["middle"] if branch == "top" else paths["top"]
                    view = json.loads(self.native(active, "view", "--json").stdout)
                    self.assertEqual([item["name"] for item in view["branches"]],
                                     ["bottom", "middle", "top"])
                    self.assertTrue(next(item for item in view["branches"]
                                         if item["name"] == branch)["isMerged"])
                    if branch == "bottom" and delete_ref:
                        result = self.native(active, "rebase", "--no-trunk", ok=False)
                        self.assertIn("valid commit 'bottom'", result.stderr)
                        self.assertFalse((source / ".git" / "gh-stack-rebase-state").exists())
                    else:
                        self.native(active, "rebase", "--no-trunk")
                    if branch == "top":
                        result = self.native(active, "add", "next", ok=False)
                        self.assertIn("only add branches to the top", result.stderr)
                    else:
                        self.native(active, "add", "next")
                        self.assertEqual([item["branch"] for item in self.catalog(source)["stacks"][0]["branches"]],
                                         ["bottom", "middle", "top", "next"])
                    self.assertFalse(paths[branch].exists())
                    if delete_ref:
                        self.git(source, "show-ref", "--verify", f"refs/heads/{branch}", ok=False)

    def test_local_unstack_preserves_sibling_group_with_missing_merged_root(self) -> None:
        source, paths = self.make_stack("groups")
        sibling = self.root / "sibling"
        self.git(source, "worktree", "add", "-b", "sibling", str(sibling), "main")
        self.native(sibling, "init", "--base", "main", "sibling")
        sibling_record = self.catalog(source)["stacks"][1]
        self.retire(source, paths, "bottom", delete_ref=True)
        self.native(paths["top"], "unstack", "--local")
        self.assertEqual(self.catalog(source)["stacks"], [sibling_record])
        self.assertTrue(sibling.exists())
        self.git(source, "show-ref", "--verify", "refs/heads/sibling")

    def test_normal_rebase_supports_missing_merged_root_after_local_default_fetch(self) -> None:
        source, paths = self.make_stack("default-fetch")
        self.git(source, "merge", "--ff-only", "bottom")
        origin = self.root / "origin.git"
        self.git(source, "clone", "--bare", str(source), str(origin))
        self.git(source, "remote", "add", "origin", origin.as_uri())
        self.git(source, "config", "branch.main.remote", "origin")
        self.git(source, "config", "branch.main.merge", "refs/heads/main")
        self.retire(source, paths, "bottom", delete_ref=True)
        result = self.native(paths["top"], "rebase", "--no-trunk", ok=False)
        self.assertIn("valid commit 'bottom'", result.stderr)
        self.native(paths["top"], "rebase")
        self.git(source, "show-ref", "--verify", "refs/heads/bottom", ok=False)
        self.assertFalse(paths["bottom"].exists())
        self.assertEqual([item["branch"] for item in self.catalog(source)["stacks"][0]["branches"]],
                         ["bottom", "middle", "top"])
        self.git(paths["middle"], "merge-base", "--is-ancestor", "main", "HEAD")


if __name__ == "__main__":
    unittest.main()
