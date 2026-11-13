"""Plan reviews as the review skills run them: `baton review run --kind plan`."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
REVIEW = [str(ROOT / "bin/baton"), "review", "run", "--kind=plan"]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_manager as fixtures  # noqa: E402
from test_peer_review import artifacts_dir  # noqa: E402
from baton.review_runner import plan_input  # noqa: E402


class PlanPeerReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        fixtures = ROOT / "experiments/.peer-review-tests"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.config = self.repo / "config"
        self.config.mkdir()
        self.slug = "plan-review-test-" + self.repo.name
        self.plan = self.repo / self.slug / "plan.md"
        self.plan.parent.mkdir()
        self.plan.write_text("# Test plan\nImplement a bounded change and verify it.\n")
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("PLAN_REVIEW_", "CODEX_"))
        }
        self.tmp = self.repo / "tmp"
        self.tmp.mkdir()
        self.env.update(CODEX_HOME=str(self.config), REVIEW_TEST_ROOT=str(self.repo),
                        PLAN_REVIEW_PROJECTS_ROOT=str(self.repo), TMPDIR=str(self.tmp),
                        # Fixtures live inside Baton's checkout; plan reviews must not take it as the repository.
                        GIT_CEILING_DIRECTORIES=str(self.repo.parent))
        # Keeps the default-plan lookup away from real project state: nothing is initialized here.
        for key in ("BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT",
                    "BATON_REPOS_ROOT", "BATON_ARTIFACTS_ROOT"):
            self.env[key] = str(self.repo / "no-project-manager" / key.lower())
        self.project_config = self.repo / "project-config.json"
        self.env["BATON_CONFIG_FILE"] = str(self.project_config)
        for reviewer in ("claude", "codex", "agy"):
            fake = self.repo / f"fake-{reviewer}"
            fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
reviewer = pathlib.Path(sys.argv[0]).name.removeprefix("fake-")
args = sys.argv[1:]
if args[-3:] == ["mcp", "list", "--json"]:
    print("[]")
    raise SystemExit(0)
with open(pathlib.Path(os.environ["REVIEW_TEST_ROOT"]) / (reviewer + ".jsonl"), "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if reviewer == "agy":
    message = json.loads(sys.stdin.read())
    assert message["event"] == "user" and "# Test plan" in message["message"]["content"]
    assert "--print" not in args
else:
    assert "# Test plan" in sys.stdin.read()
review = "## Summary\\nTest review\\n## Findings\\nNone\\n## Suggested Plan Changes\\nNone\\n## Verdict\\nReady\\n"
if reviewer == "codex":
    pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(review)
elif reviewer == "agy":
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": review}}), flush=True)
else:
    print(review)
''')
            fake.chmod(0o755)
            self.env[reviewer.upper() + "_BIN"] = str(fake)

    def review(self, model: str, reasoning: str, claude_model: str | None = None,
               cwd: Path | None = None, default_plan: bool = False, plan: Path | None = None,
               review_slug: str | None = None) -> Path:
        slug = review_slug or self.slug
        args = [] if default_plan else [str(plan or self.plan)]
        result = subprocess.run([*REVIEW, *args], cwd=cwd or self.repo,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("review failed", result.stderr)
        calls = {}
        for reviewer in ("claude", "codex", "agy"):
            log = self.repo / f"{reviewer}.jsonl"
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            log.unlink()
            self.assertEqual(len(entries), 1, reviewer)
            calls[reviewer] = entries[0]
        args = calls["codex"]
        if model == "default":
            self.assertNotIn("--model", args)
        else:
            self.assertEqual(args[args.index("--model") + 1], model)
        self.assertEqual(args[args.index("--config") + 1], f'model_reasoning_effort="{reasoning}"')
        output = Path(args[args.index("--output-last-message") + 1])
        self.assertEqual(output.name, f"{slug}-plan-review-codex-{model}.md")
        self.assertTrue(output.read_text())
        run_dir = artifacts_dir(self, result)
        self.assertEqual(output.parent, run_dir)
        self.assertEqual(run_dir.parent, self.tmp)
        for name in ("claude", f"codex-{model}", "agy"):
            review_file = run_dir / f"{slug}-plan-review-{name}.md"
            self.assertIn(f"Review file: {review_file}", result.stderr)
            self.assertIn(f"review: {review_file}", result.stdout)
        if claude_model is None:
            self.assertNotIn("--model", calls["claude"])
        else:
            args = calls["claude"]
            self.assertEqual(args[args.index("--model") + 1], claude_model)
        self.assertNotIn("--model", calls["agy"])
        return run_dir

    def test_invalid_review_loop_does_not_block_explicit_or_default_plan(self) -> None:
        self.env.pop("PLAN_REVIEW_PROJECTS_ROOT")
        self.env.pop("BATON_ARTIFACTS_ROOT")
        checkout = self.repo / "checkout"
        subprocess.run(["git", "init", "-q", "-b", f"baton/{self.slug}", str(checkout)], check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "-C", str(checkout),
                        "commit", "-q", "--allow-empty", "-m", "Base"], check=True)
        for settings in ({"focused_reviews": ["legacy prompt"]}, "malformed", {"unknown": True},
                         {"focused_review_models": {"security": {"cursor": "strong"}}}):
            self.project_config.write_text(json.dumps({"paths": {"artifacts_root": str(self.repo)},
                                                        "review_loop": settings}))
            with self.subTest(settings=settings):
                self.review("default", "high")
                self.review("default", "high", cwd=checkout, default_plan=True)

    def test_default_runs_each_reviewer_once_with_fallbacks(self) -> None:
        self.review("default", "high")

    def test_default_ignores_harness_model_and_keeps_root_reasoning(self) -> None:
        (self.config / "config.toml").write_text(
            'model = "config-model"\nmodel_reasoning_effort = "medium"\n'
            '[profiles.other]\nmodel = "profile-model"\nmodel_reasoning_effort = "low"\n'
        )
        self.review("default", "medium")

    def test_ignores_model_environment_and_keeps_reasoning_override(self) -> None:
        (self.config / "config.toml").write_text('model = "config-model"\nmodel_reasoning_effort = "medium"\n')
        self.env.update(CODEX_MODEL="env-model", CODEX_REASONING_EFFORT="low")
        self.review("default", "low")

    def test_configured_model_overrides_model_environment(self) -> None:
        (self.config / "config.toml").write_text('model = "config-model"\nmodel_reasoning_effort = "high"\n')
        self.env.update(CODEX_MODEL="env-model", CODEX_REASONING_EFFORT="low",
                        PLAN_REVIEW_CODEX_MODEL="old-env-override", PLAN_REVIEW_CODEX_REASONING="medium")
        self.project_config.write_text(json.dumps({
            "plan_review_model": {"codex": "explicit-model", "claude": "default", "agy": "default"}}))
        self.review("explicit-model", "medium")

    def test_claude_explicit_model(self) -> None:
        self.project_config.write_text(json.dumps({"plan_review_model": {"codex": "default", "claude": "explicit-claude", "agy": "default"}}))
        self.review("default", "high", "explicit-claude")

    def test_master_and_root_subplan_use_distinct_dirs(self) -> None:
        subplan = self.repo / self.slug / "branches" / "alpha" / self.slug / "plan.md"
        subplan.parent.mkdir(parents=True)
        subplan.write_text(self.plan.read_text())
        master_dir = self.review("default", "high")
        subplan_dir = self.review("default", "high", plan=subplan)
        self.assertNotEqual(master_dir, subplan_dir)
        self.assertTrue(master_dir.name.startswith(f"plan-review.{self.slug}-plan.md."), master_dir)
        self.assertTrue(subplan_dir.name.startswith(
            f"plan-review.{self.slug}-branches-alpha-{self.slug}-plan.md."), subplan_dir)

    def test_plan_outside_projects_root_uses_absolute_path_label(self) -> None:
        self.env["PLAN_REVIEW_PROJECTS_ROOT"] = str(self.repo / "elsewhere")
        run_dir = self.review("default", "high")
        label = run_dir.name.removeprefix("plan-review.").rsplit(".", 1)[0]
        self.assertTrue(label.endswith(f"{self.repo.name}-{self.slug}-plan.md"), label)

    def test_failed_run_still_reports_artifacts(self) -> None:
        for reviewer in ("claude", "codex", "agy"):
            self.env[reviewer.upper() + "_BIN"] = "/usr/bin/false"
        result = subprocess.run([*REVIEW, str(self.plan)], cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("All review jobs failed.", result.stderr)
        self.assertNotIn("Review file:", result.stderr)
        self.assertEqual(artifacts_dir(self, result).parent, self.tmp)

    def read_dirs(self, reviewer: str = "claude") -> list[list[str]]:
        log = self.repo / f"{reviewer}.jsonl"
        runs = [json.loads(line) for line in log.read_text().splitlines()]
        log.unlink()
        return [[args[index + 1] for index, value in enumerate(args) if value == "--add-dir"] for args in runs]

    def test_outside_framework_reads_the_checkout_and_otherwise_only_the_plan(self) -> None:
        checkout = self.repo / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        run_dir = self.review("default", "high", cwd=checkout)
        prompt = (run_dir / f"{self.slug}-plan-review.prompt.txt").read_text()
        self.assertTrue(prompt.endswith(f"\n- {checkout.resolve()}"), prompt)
        run_dir = self.review("default", "high")
        prompt = (run_dir / f"{self.slug}-plan-review.prompt.txt").read_text()
        self.assertNotIn("Do not inspect repository files", prompt)
        self.assertIn("No repository is available for inspection: review the plan text only, tag every finding "
                      "plan-only", prompt)

    def test_embedded_plan_does_not_expand_repository_read_scope(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        related = self.plan.parent / "related-master.md"
        self.plan.write_text(f"# Test plan\nRelated master: {related}\n")
        result = subprocess.run([*REVIEW, str(self.plan)], cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = artifacts_dir(self, result)
        prompt = (run_dir / f"{self.slug}-plan-review.prompt.txt").read_text()
        self.assertIn("never reopen the provided plan file", prompt)
        self.assertIn("only inside the repository roots explicitly listed below", prompt)
        self.assertIn("never follow artifact links", prompt)
        self.assertIn("record that inspection gap", prompt)
        self.assertNotIn(str(related), prompt)
        combined = (run_dir / f"{self.slug}-plan-review-input.txt").read_text()
        self.assertIn(str(related), combined)
        self.assertEqual(self.read_dirs(), [[str(self.repo.resolve())]])

    def test_missing_and_duplicate_repositories_are_skipped(self) -> None:
        present = self.repo / "present"
        present.mkdir()
        alias = self.repo / "alias"
        alias.symlink_to(present)
        status = {"name": self.slug, "master_path": str(self.plan), "current_entry": None, "entries": [
            {"path": str(present)}, {"path": str(self.repo / "removed")}, {"path": str(alias)}]}
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            review = plan_input(None, "", 420, lambda: status)
        self.assertIn(f"Warning: skipping repository that is not a directory: {self.repo / 'removed'}",
                      warnings.getvalue())
        self.assertIn(f"Warning: skipping duplicate repository: {present.resolve()}", warnings.getvalue())
        self.assertEqual(review.read_dirs, (present.resolve(),))
        self.assertEqual(review.record["plan"], str(self.plan))
        self.assertTrue(review.prompt.endswith(f"\n- {present.resolve()}"))

    def test_default_plan_outside_framework_uses_branch_suffix(self) -> None:
        checkout = self.repo / "checkout"
        subprocess.run(["git", "init", "-q", "-b", f"baton/{self.slug}", str(checkout)], check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "-C", str(checkout),
                        "commit", "-q", "--allow-empty", "-m", "Base"], check=True)
        self.review("default", "high", cwd=checkout, default_plan=True)

        config_file = self.repo / "project-config.json"
        config_file.write_text(json.dumps({"paths": {"artifacts_root": str(self.repo)}}))
        self.env["BATON_CONFIG_FILE"] = str(config_file)
        self.env.pop("PLAN_REVIEW_PROJECTS_ROOT")
        self.env.pop("BATON_ARTIFACTS_ROOT")
        self.review("default", "high", cwd=checkout, default_plan=True)

        self.env["BATON_ARTIFACTS_ROOT"] = str(self.repo)
        config_file.write_text(json.dumps({"paths": {"artifacts_root": str(self.repo / "wrong")}}))
        self.review("default", "high", cwd=checkout, default_plan=True)

    def test_default_plan_outside_framework_uses_configured_prefix(self) -> None:
        checkout = self.repo / "checkout"
        subprocess.run(["git", "init", "-q", "-b", f"team/feature/{self.slug}", str(checkout)], check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "-C", str(checkout),
                        "commit", "-q", "--allow-empty", "-m", "Base"], check=True)
        self.project_config.write_text(json.dumps({"branch_prefix": "team/feature"}))
        self.review("default", "high", cwd=checkout, default_plan=True)

    def test_default_plan_in_named_member_uses_project_name(self) -> None:
        fixture = fixtures.ProjectManagerTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.make_repo("alpha")
        fixture.create("alpha", name=self.slug)
        fixture.cli("branch", "init", "alpha", "first", cwd=fixture.slots / "p3")
        checkout = fixture.checkout("alpha", f"{self.slug}--first")
        self.assertEqual(fixture.git(checkout, "branch", "--show-current"), f"baton/{self.slug}--first")
        self.env.update({key: fixture.env[key] for key in (
            "BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT",
            "BATON_REPOS_ROOT", "BATON_ARTIFACTS_ROOT")})
        master = fixtures.artifact_path(fixture.projects, fixture.artifacts, self.slug) / "plan.md"
        master.write_text(self.plan.read_text())
        self.env["PLAN_REVIEW_PROJECTS_ROOT"] = str(fixture.artifacts)
        self.review("default", "high", cwd=checkout, default_plan=True, review_slug=master.parent.name)

    def test_default_plan_in_branch_of_multi_branch_project_is_its_subplan(self) -> None:
        fixture = fixtures.ProjectManagerTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.make_repo("alpha")
        fixture.create("alpha", name=self.slug)
        fixture.cli("branch", "add", "alpha", "docs", cwd=fixture.slots / "p3")
        directory = fixtures.artifact_path(fixture.projects, fixture.artifacts, self.slug)
        subplan = directory / "branches" / "alpha" / f"{self.slug}--docs" / "plan.md"
        subplan.parent.mkdir(parents=True)
        subplan.write_text("# Test plan\nDocs subplan.\n")
        self.env.update({key: fixture.env[key] for key in (
            "BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT",
            "BATON_REPOS_ROOT", "BATON_ARTIFACTS_ROOT")})
        (directory / "plan.md").write_text(self.plan.read_text())
        self.env["PLAN_REVIEW_PROJECTS_ROOT"] = str(fixture.artifacts)
        # The run directory label is the reviewed plan's path below the projects root.
        for cwd, label in ((fixture.checkout("alpha", f"{self.slug}--docs"),
                            f"{directory.name}-branches-alpha-{self.slug}-docs-plan.md"),
                           (fixture.slots / "p3", f"{directory.name}-plan.md")):
            with self.subTest(cwd=cwd):
                result = subprocess.run(REVIEW, cwd=cwd, env=self.env, text=True,
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(artifacts_dir(self, result).name.startswith(f"plan-review.{label}."))
        # The master plan and each subplan review may read every active worktree.
        worktrees = [str(fixture.checkout("alpha", name).resolve()) for name in (self.slug, f"{self.slug}--docs")]
        self.assertEqual(self.read_dirs(), [worktrees, worktrees])


if __name__ == "__main__":
    unittest.main()
