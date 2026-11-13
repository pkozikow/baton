"""Implementation reviews as the review skills run them: `baton review run --kind implementation`."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
BATON = ROOT / "bin/baton"
sys.path.insert(0, str(ROOT / "tests"))
import test_project_manager as fixtures  # noqa: E402

FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["REVIEW_TEST_CALLS"], "a") as handle:
    handle.write(json.dumps(args) + "\\n")
assert "Changes to review:" in sys.stdin.read()
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text("## Summary\\nTest review\\n")
'''


def install_fake_codex(directory: Path, env: dict[str, str]) -> Path:
    """Points the runner at a fake Codex that records calls; other reviewers are absent."""
    fake = directory / "fake-codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    calls = directory / "calls.jsonl"
    env.update(CODEX_BIN=str(fake), CLAUDE_BIN="/nonexistent/claude", AGY_BIN="/nonexistent/agy",
               CODEX_HOME=str(directory), PEER_REVIEW_SKIP_FETCH="1", REVIEW_TEST_CALLS=str(calls))
    return calls


def artifacts_dir(test: unittest.TestCase, result: subprocess.CompletedProcess[str]) -> Path:
    """Returns the run directory announced by the machine-readable last stdout line."""
    lines = result.stdout.splitlines()
    test.assertTrue(lines and lines[-1].startswith("REVIEW_ARTIFACTS="), result.stdout)
    run_dir = Path(lines[-1].removeprefix("REVIEW_ARTIFACTS="))
    test.assertIn(f"Artifacts: {run_dir}", result.stderr)
    test.assertTrue(run_dir.is_dir())
    test.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
    test.assertTrue((run_dir / "run.json").is_file())
    return run_dir


class PeerReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        fixtures = ROOT / "experiments/.peer-review-tests"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.config = self.repo / "config"
        self.config.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("PEER_REVIEW_", "CODEX_", "GIT_"))
        }
        # Keeps the default-base lookup away from real project state: nothing is initialized here.
        unused = self.repo / "no-project-manager"
        for key in ("BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT",
                    "BATON_REPOS_ROOT", "BATON_ARTIFACTS_ROOT"):
            self.env[key] = str(unused / key.lower())
        self.project_config = self.repo / "project-config.json"
        self.env["BATON_CONFIG_FILE"] = str(self.project_config)
        self.calls = install_fake_codex(self.config, self.env)
        self.tmp = self.repo / "tmp"
        self.tmp.mkdir()
        self.env["TMPDIR"] = str(self.tmp)
        self.env.update(
            GIT_CONFIG_COUNT="4",
            GIT_CONFIG_KEY_0="user.name", GIT_CONFIG_VALUE_0="Test",
            GIT_CONFIG_KEY_1="user.email", GIT_CONFIG_VALUE_1="test@example.invalid",
            GIT_CONFIG_KEY_2="commit.gpgsign", GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="core.hooksPath", GIT_CONFIG_VALUE_3="/dev/null",
        )
        self.branch = "peer-review-test-" + self.repo.name
        self.make_branch(self.repo, self.branch, "change.txt")

    def make_branch(self, repo: Path, branch: str, filename: str) -> None:
        self.git("init", "-b", "main", cwd=repo)
        self.git("commit", "--allow-empty", "-m", "Base", cwd=repo)
        self.git("switch", "-c", branch, cwd=repo)
        (repo / filename).write_text("change\n")
        self.git("add", filename, cwd=repo)
        self.git("commit", "-m", "Change", cwd=repo)

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True, capture_output=True)

    def review(self, model: str, reasoning: str, *base: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([str(BATON), "review", "run", "--kind=implementation", *(base or ("main",))],
                                cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        args = calls[0]
        if model == "default":
            self.assertNotIn("--model", args)
        else:
            self.assertEqual(args[args.index("--model") + 1], model)
        self.assertEqual(args[args.index("--config") + 1], f'model_reasoning_effort="{reasoning}"')
        output = Path(args[args.index("--output-last-message") + 1])
        self.assertEqual(output.name, f"{self.branch}-review-codex-{model}.md")
        self.assertTrue(output.read_text())
        self.assertNotIn("Codex agent", result.stdout)
        run_dir = artifacts_dir(self, result)
        self.assertEqual(output.parent, run_dir)
        self.assertTrue(run_dir.name.startswith(f"peer-review.{self.repo.name}-{self.branch}."), run_dir)
        self.assertEqual(run_dir.parent, self.tmp)
        self.assertIn(f"Review file: {output}", result.stderr)
        self.assertIn(f"Diff: {run_dir / (self.branch + '.diff')}", result.stdout)
        self.calls.unlink()
        return result

    def test_default_runs_codex_once_with_fallbacks(self) -> None:
        self.review("default", "high")

    def test_prunes_this_kinds_runs_older_than_a_week(self) -> None:
        old, recent, other = (self.tmp / name for name in ("peer-review.x.AAAAAA", "peer-review.y.BBBBBB",
                                                           "plan-review.z.CCCCCC"))
        for directory in (old, recent, other):
            directory.mkdir()
            (directory / "review.md").write_text("Review\n")
        week_ago = time.time() - 8 * 24 * 3600
        for directory in (old, other):
            os.utime(directory, (week_ago, week_ago))
        self.review("default", "high")
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())

    def test_default_ignores_harness_model_and_keeps_reasoning(self) -> None:
        (self.config / "config.toml").write_text('model = "config-model"\nmodel_reasoning_effort = "medium"\n')
        self.env["PEER_REVIEW_REVIEWERS"] = "codex"
        self.review("default", "medium")

    def test_ignores_model_environment_and_keeps_reasoning_override(self) -> None:
        (self.config / "config.toml").write_text('model = "config-model"\nmodel_reasoning_effort = "medium"\n')
        self.env.update(CODEX_MODEL="env-model", CODEX_REASONING_EFFORT="low")
        self.review("default", "low")

    def test_configured_model_overrides_model_environment(self) -> None:
        self.env.update(CODEX_MODEL="env-model", CODEX_REASONING_EFFORT="low",
                        PEER_REVIEW_CODEX_MODEL="old-env-override", PEER_REVIEW_CODEX_REASONING="medium")
        self.project_config.write_text(json.dumps({
            "implementation_review_model": {"codex": "explicit-model"}}))
        self.review("explicit-model", "medium")

    def test_default_base_outside_framework_uses_origin_head(self) -> None:
        self.git("update-ref", "refs/remotes/origin/trunk", "main")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        result = self.review("default", "high", "--")
        self.assertIn("Base ref: origin/trunk (default branch)", result.stderr)

    def test_extra_prompt_environment_is_rejected(self) -> None:
        self.env["PEER_REVIEW_EXTRA_PROMPT"] = "focus"
        result = subprocess.run([str(BATON), "review", "run", "--kind=implementation", "main"], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("PEER_REVIEW_EXTRA_PROMPT is no longer supported. Use --extra-prompt instead.", result.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_concurrent_runs_of_one_branch_in_different_repos_use_distinct_dirs(self) -> None:
        # Same repo basename too: uniqueness must come from the run directory, not the label.
        repos = [self.repo / parent / "repo" for parent in ("one", "two")]
        for index, repo in enumerate(repos):
            repo.mkdir(parents=True)
            self.make_branch(repo, "baton/shared", f"change-{index}.txt")
        procs = [subprocess.Popen([str(BATON), "review", "run", "--kind=implementation", "main"], cwd=repo,
                                  env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for repo in repos]
        results = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, stderr)
            results.append(subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr))
        run_dirs = [artifacts_dir(self, result) for result in results]
        self.assertNotEqual(run_dirs[0], run_dirs[1])
        ids = [json.loads((run_dir / "run.json").read_text())["id"] for run_dir in run_dirs]
        self.assertNotEqual(ids[0], ids[1])
        for index, run_dir in enumerate(run_dirs):
            self.assertTrue(run_dir.name.startswith("peer-review.repo-baton-shared."), run_dir)
            diff = (run_dir / "baton-shared.diff").read_text()
            self.assertIn(f"change-{index}.txt", diff)
            self.assertNotIn(f"change-{1 - index}.txt", diff)
            self.assertTrue((run_dir / "diffs" / f"change-{index}.txt.diff").is_file())
            self.assertTrue((run_dir / "baton-shared-review-codex-default.md").read_text())
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)


class FrameworkBaseTest(unittest.TestCase):
    """Default base inside a project, using a disposable project manager."""

    def setUp(self) -> None:
        self.fixture = fixtures.ProjectManagerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.name = "review" + uuid.uuid4().hex[:8]
        env = self.fixture.env
        for key in [key for key in env if key.startswith(("PEER_REVIEW_", "CODEX_"))]:
            del env[key]
        self.calls = install_fake_codex(self.fixture.root, env)
        tmp = self.fixture.root / "review-tmp"
        tmp.mkdir()
        env["TMPDIR"] = str(tmp)
        self.source = self.fixture.make_repo("alpha")
        self.fixture.create("alpha", name=self.name)
        self.root = self.fixture.checkout("alpha", self.name)
        self.commit(self.root, "root.txt")
        self.fixture.cli("branch", "add", "alpha", "api", "--after", f"baton/{self.name}",
                         cwd=self.fixture.slots / "p3")
        self.child = self.fixture.checkout("alpha", f"{self.name}--api")
        self.commit(self.child, "api.txt")

    def commit(self, checkout: Path, filename: str) -> None:
        (checkout / filename).write_text(filename + "\n")
        self.fixture.git(checkout, "add", filename)
        self.fixture.git(checkout, "commit", "-m", f"Add {filename}")

    def review(self, checkout: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.fixture.run_command([str(BATON), "review", "run", "--kind=implementation", *args], checkout, ok=ok)

    def test_stack_child_reviews_against_predecessor(self) -> None:
        result = self.review(self.child)
        self.assertIn(f"Base ref: refs/heads/baton/{self.name} (expected PR base", result.stderr)
        run_dir = artifacts_dir(self, result)
        diff = (run_dir / f"baton-{self.name}-api.diff").read_text()
        self.assertIn("api.txt", diff)
        self.assertNotIn("root.txt", diff)
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["project"]["name"], self.name)

    def test_stack_root_reviews_against_default_branch(self) -> None:
        result = self.review(self.root)
        self.assertIn("Base ref: refs/remotes/origin/main (expected PR base", result.stderr)
        self.assertIn("root.txt", (artifacts_dir(self, result) / f"baton-{self.name}.diff").read_text())

    def test_stale_stack_child_stops_unless_allowed(self) -> None:
        self.commit(self.root, "later.txt")
        result = self.review(self.child, ok=False)
        self.assertIn(f"Error: this stack child is behind its predecessor refs/heads/baton/{self.name}; the parent "
                      "must run gh stack rebase before this review (or pass --allow-stale-base).", result.stderr)
        self.assertNotIn("REVIEW_ARTIFACTS=", result.stdout)
        self.assertFalse(self.calls.exists())
        result = self.review(self.child, "--allow-stale-base")
        self.assertIn("Warning: reviewing a stack child that is behind its predecessor", result.stderr)
        self.assertIn(f"Base ref: refs/heads/baton/{self.name} (expected PR base", result.stderr)

    def test_unreliable_base_stops_with_reason(self) -> None:
        (self.source / ".git" / "gh-stack-rebase-state").write_text("{}")
        result = self.review(self.child, ok=False)
        self.assertIn("unreliable", result.stderr)
        self.assertIn("native gh stack rebase", result.stderr)
        self.assertNotIn("Base ref:", result.stderr)
        self.assertFalse(self.calls.exists())
        # An explicit base skips the project lookup.
        self.review(self.child, "main")


if __name__ == "__main__":
    unittest.main()
