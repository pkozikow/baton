"""Review-loop validation must not prevent unrelated work in other slots."""

import json
from pathlib import Path
import sys
import unittest

import test_project_manager as fixtures
from test_review_runner import STUB


class FocusedReviewIsolationTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create
    checkout = fixtures.ProjectManagerTest.checkout

    def test_invalid_global_focus_settings_leave_both_slots_operational(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.create("alpha", slot="p4", name="second")
        config = self.root / "config.json"
        invalid_settings = (
            {"focused_reviews": ["Security issues, such as authorization gaps."]},
            None,
            [],
            {"unknown_review_setting": True},
            {"focused_review_models": {"security": {"codex": "strong"}}},
        )
        for index, settings in enumerate(invalid_settings):
            with self.subTest(settings=settings):
                config.write_text(json.dumps({"review_loop": settings}))
                saved = config.read_bytes()
                for slot, name in (("p3", "example"), ("p4", "second")):
                    status = json.loads(self.cli("status", name, "--json").stdout)
                    self.assertEqual(status["slot"], slot)
                    generation = status["blackboard"]["generation"]
                    self.cli("blackboard", "post", "--selector", name,
                             "--expected-generation", generation, "--agent", "test",
                             "--text", f"Still operational: {index}")
                    board = self.cli("blackboard", "read", "--selector", name,
                                     "--expected-generation", generation, "--json")
                    self.assertIn(f"Still operational: {index}", board.stdout)
                    directory = Path(status["master_path"]).parent
                    plan = directory / "plan.md"
                    plan.write_text(f"Plan version {index}\n")
                    self.cli("artifacts", "commit", "--selector", name,
                             "--no-push", "-m", f"Plan {index}", str(plan))
                    self.assertEqual(self.git(self.artifacts, "show",
                                              f"HEAD:{directory.name}/plan.md"),
                                     f"Plan version {index}")
                self.assertEqual(config.read_bytes(), saved)

    def test_explicit_project_controls_focus_model_with_explicit_base(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.create("alpha", slot="p4", name="second")
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "implementation_review_model": {"codex": "normal-codex"},
            "review_loop": {"focused_review_models": {"security": {"codex": "global-security"}}},
        }))
        self.cli("config", "set", "--selector", "example",
                 "--focused-review-model", "security.codex=project-security")
        worktree = self.checkout("alpha")
        (worktree / "change.txt").write_text("Reviewed change\n")
        self.git(worktree, "add", "change.txt")
        self.git(worktree, "commit", "-m", "Change")
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\n" + STUB)
        fake.chmod(0o755)
        scratch = self.root / "review-artifacts"
        scratch.mkdir()
        self.env.update(CODEX_BIN=str(fake), CODEX_HOME=str(scratch), REVIEW_TEST_ROOT=str(scratch),
                        TMPDIR=str(scratch), PEER_REVIEW_SKIP_FETCH="1", PEER_REVIEW_REVIEWERS="codex")
        saved = config.read_bytes()
        for project, expected in (("example", "project-security"), ("second", "global-security")):
            with self.subTest(project=project):
                result = self.run_command(["bash", str(fixtures.SCRIPT), "review", "run", "--kind=implementation",
                                           "--focus-area", "security", "--selector", project, "main"], worktree)
                self.assertIn("Focus area: security", result.stdout)
                calls = [json.loads(line) for line in (scratch / "codex.calls").read_text().splitlines()]
                args = calls[-1]["args"]
                self.assertEqual(args[args.index("--model") + 1], expected)
                artifacts = Path(result.stdout.splitlines()[-1].removeprefix("REVIEW_ARTIFACTS="))
                prompt = next(artifacts.glob("*.prompt.txt")).read_text()
                guidance = (fixtures.SCRIPT.parent.parent / "baton/review_focuses/security.md").read_text().rstrip("\n")
                self.assertEqual(prompt.count(guidance), 1)
        self.assertEqual(config.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
