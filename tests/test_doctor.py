import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_project_manager as fixtures
from baton.doctor import path_checks


class DoctorTest(unittest.TestCase):
    setUpFixture = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    cli = fixtures.ProjectManagerTest.cli
    git = fixtures.ProjectManagerTest.git
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create

    def setUp(self) -> None:
        self.setUpFixture()
        self.cli("init")
        self.worktrees.mkdir(exist_ok=True)
        self.make_repo("alpha")
        self.config = {"repo_aliases": {"api": "alpha"}}

    def results(self, config: dict | None = None, env: dict | None = None) -> dict:
        return {row["name"]: row for row in path_checks(self.config if config is None else config,
                                                       self.env if env is None else env)}

    def doctor(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        for provider in ("CODEX", "CLAUDE", "AGY"):
            self.env[provider + "_BIN"] = sys.executable
        result = subprocess.run(["bash", str(fixtures.SCRIPT), "doctor", "--json", "--skip-smoke", *args],
                                env=self.env, cwd=self.root, text=True, capture_output=True)
        self.assertNotIn("Traceback", result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if data["ok"] else 1)
        return result, data

    def test_directory_roots_repositories_aliases_and_p_slots(self) -> None:
        rows = self.results()
        self.assertTrue(all(row["status"] == "pass" for row in rows.values()), rows)
        self.assertEqual({name for name in rows if name.startswith("slots.p")},
                         {f"slots.p{number}" for number in range(1, 11)})
        self.assertEqual({name for name in rows if name.startswith("slots.")},
                         {f"slots.p{number}" for number in range(1, 11)})
        self.assertEqual(rows["repos.alpha"]["status"], "pass")
        self.assertEqual(rows["paths.repos_root"]["status"], "pass")


    def test_unsupported_active_slot_has_actionable_diagnostic(self) -> None:
        self.create("alpha")
        source = fixtures.state_file(self.projects)
        project = json.loads(source.read_text())
        project["slot"] = "invalid"
        unsupported = fixtures.state_file(self.projects, "invalid")
        unsupported.write_text(json.dumps(project))
        source.unlink()
        before = unsupported.read_bytes()
        rows = self.results()
        self.assertEqual(rows["slots.invalid"]["status"], "fail")
        self.assertIn("p1 through p10", rows["slots.invalid"]["message"])
        self.assertEqual(unsupported.read_bytes(), before)

    def test_invalid_active_record_has_one_failure_diagnostic(self) -> None:
        self.create("alpha")
        path = fixtures.state_file(self.projects)
        original = json.loads(path.read_text())
        records = ((json.dumps(original | {"slot": "invalid"}), "does not match"),
                   (json.dumps(original | {"slot": "p4"}), "does not match"),
                   ("[]", "must be a JSON object"),
                   ("{", "Invalid active coordinator record"))
        for text, message in records:
            with self.subTest(text=text):
                path.write_text(text)
                rows = [row for row in path_checks(self.config, self.env) if row["name"] == "slots.p3"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "fail")
                self.assertIn(message, rows[0]["message"])
                self.assertEqual(path.read_text(), text)

    def test_artifacts_can_be_a_subdirectory_of_a_git_checkout(self) -> None:
        subdirectory = self.artifacts / "projects"
        subdirectory.mkdir()
        rows = self.results(env=self.env | {"BATON_ARTIFACTS_ROOT": str(subdirectory)})
        self.assertEqual(rows["paths.artifacts_root"]["status"], "pass")

    def test_missing_roots_and_non_git_artifacts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            rows = self.results(env=self.env | {"BATON_ARTIFACTS_ROOT": outside,
                                               "BATON_WORKTREES_ROOT": str(self.root / "absent")})
        self.assertEqual(rows["paths.artifacts_root"]["status"], "fail")
        self.assertEqual(rows["paths.worktrees_root"]["status"], "fail")

    def test_alias_target_must_be_a_checkout_root(self) -> None:
        (self.repos / "plain").mkdir()
        rows = self.results(config={"repo_aliases": {"bad": "plain", "missing": "absent"}})
        self.assertEqual(rows["repos.plain"]["status"], "fail")
        self.assertEqual(rows["repos.absent"]["status"], "fail")

    def test_slot_must_belong_to_configured_registry(self) -> None:
        self.git(self.projects, "worktree", "remove", str(self.slots / "p4"))
        self.git(self.root, "init", "-b", "main", str(self.slots / "p4"))
        rows = self.results()
        self.assertEqual(rows["slots.p4"]["status"], "fail")
        self.assertIn("different coordinator", rows["slots.p4"]["message"])

    def test_missing_registered_slot_is_detected(self) -> None:
        self.create("alpha")
        self.git(self.projects, "worktree", "move", str(self.slots / "p3"), str(self.root / "moved"))
        rows = self.results()
        self.assertEqual(rows["slots.p3"]["status"], "fail")

    def test_invalid_paths_aliases_and_environment_are_reported(self) -> None:
        rows = self.results(config={"paths": {"worktrees_root": 42}, "repo_aliases": []},
                            env=self.env | {"BATON_REPOS_ROOT": ""})
        self.assertEqual(rows["paths.config"]["status"], "fail")
        self.assertEqual(rows["paths.repo_aliases"]["status"], "fail")
        self.assertEqual(rows["paths.repos_root"]["status"], "fail")
        rows = self.results(config={"paths": {"worktrees_root": "bad\0path"}},
                            env={key: value for key, value in self.env.items() if key != "BATON_WORKTREES_ROOT"})
        self.assertEqual(rows["paths.worktrees_root"]["status"], "fail")
        self.assertEqual(rows["paths.projects_repo"]["status"], "pass")
        self.assertEqual(rows["slots.p1"]["status"], "pass")

    def test_invalid_repository_root_skips_dependent_aliases(self) -> None:
        rows = self.results(env=self.env | {"BATON_REPOS_ROOT": ""})
        self.assertEqual(rows["paths.repos_root"]["status"], "fail")
        self.assertEqual(rows["repos.alpha"]["status"], "skip")

    def test_model_failure_does_not_hide_path_diagnostics(self) -> None:
        config = Path(self.env["BATON_CONFIG_FILE"])
        config.write_text(json.dumps({"plan_review_model": {"codex": "strong"}}))
        result, data = self.doctor()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(row["name"] == "paths.projects_repo" and row["status"] == "pass" for row in data["checks"]))
        self.assertTrue(any(row["status"] == "fail" and "strong" in row["message"] for row in data["checks"]))

    def test_invalid_json_still_reports_version_and_blocked_checks(self) -> None:
        Path(self.env["BATON_CONFIG_FILE"]).write_text("{")
        result, data = self.doctor()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(row["name"] == "version" for row in data["checks"]))
        self.assertEqual(next(row for row in data["checks"] if row["name"] == "config")["status"], "fail")
        self.assertFalse(data["complete"])

    def test_timeout_must_be_positive(self) -> None:
        result = subprocess.run(["bash", str(fixtures.SCRIPT), "doctor", "--timeout", "0"],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive", result.stderr)
