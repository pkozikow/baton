"""Review run records: `baton review save`, `list` and `show`, and interrupted-run detection."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

from baton.providers import process_start
from baton.review_records import effective_status
import test_project_manager as fixtures
from test_review_runner import BATON, STUB, ReviewRunnerFixture


class RunStatusTest(ReviewRunnerFixture):
    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen([str(BATON), "review", "run", "--kind=plan", *args, str(self.plan)], cwd=self.root,
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for_run_dir(self) -> Path:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            records = list(self.tmp.glob("*/run.json"))
            if records and self.all_calls():
                return records[0].parent
            time.sleep(0.05)
        self.fail("the run did not start")

    def show(self, run: str, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([str(BATON), "review", "show", run, *args], cwd=self.root, env=self.env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_background_run_is_confirmed_finished_through_show(self) -> None:
        self.env.update(STUB_CLAUDE="hang", PLAN_REVIEW_TIMEOUT_SECS="2")
        process = self.start("--reviewers=claude")
        try:
            run_dir = self.wait_for_run_dir()
            record = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(self.show(record["id"], "--json").stdout.count('"status": "running"'), 2)
            self.assertIn(f"Review run {record['id']} (plan): running", self.show(str(run_dir)).stdout)
        finally:
            process.communicate(timeout=30)
        shown = json.loads(self.show(record["id"], "--json").stdout)
        self.assertEqual((shown["status"], shown["recorded_status"]), ("failed", "failed"))
        self.assertEqual(shown["reviewers"][0]["failure"], "timeout")
        self.assertIn(": failed\n", self.show(str(run_dir / "run.json")).stdout)

    def test_killed_run_is_shown_as_interrupted_with_its_sessions_and_live_reviewers(self) -> None:
        self.env.update(STUB_CLAUDE="hang", STUB_CODEX="hang", STUB_AGY="hang")
        process = self.start("--reviewers=codex,claude,agy")
        run_dir = self.wait_for_run_dir()
        # Each attempt is recorded while it runs, with its process group and session as soon as it is known.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            attempts = {entry["provider"]: entry.get("attempts") or []
                        for entry in json.loads((run_dir / "run.json").read_text())["reviewers"]}
            if all(attempts[name] and attempts[name][0].get("session_id") for name in ("codex", "claude", "agy")):
                break
            time.sleep(0.1)
        groups = {name: self.calls(name)[0]["pid"] for name in ("codex", "claude", "agy")}
        self.assertEqual({name: (attempts[name][0]["session_id"], attempts[name][0]["process_group"],
                                 attempts[name][0]["status"]) for name in groups},
                         {"codex": (f"codex-session-{groups['codex']}", groups["codex"], None),
                          "claude": (attempts["claude"][0]["session_id"], groups["claude"], None),
                          "agy": (f"agy-conversation-{groups['agy']}", groups["agy"], None)})
        process.send_signal(signal.SIGKILL)
        process.communicate(timeout=10)
        try:
            record = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(record["status"], "running")
            output = self.show(str(run_dir)).stdout.splitlines()
            self.assertIn(f"Review run {record['id']} (plan): interrupted (recorded as running; runner pid "
                          f"{record['pid']} is gone)", output)
            work = run_dir / "golden-plan-review-agy.work"
            self.assertTrue(any(f"cwd temporary {work} (still present)" in line for line in output), output)
            for name, group in groups.items():
                self.assertIn(f"Warning: {name} reviewer process group {group} is still running after its runner "
                              f"stopped; stop it with: kill -TERM -{group}", output)
                session = attempts[name][0]["session_id"]
                self.assertTrue(any(line.startswith(f"    attempt 1: {name} session {session}, ") and "unfinished" in line
                                    for line in output), output)
            shown = json.loads(self.show(record["id"], "--json").stdout)
            self.assertEqual((shown["status"], shown["recorded_status"]), ("interrupted", "running"))
            self.assertEqual([entry["status"] for entry in shown["reviewers"]], ["interrupted"] * 3)
            self.assertEqual(sorted((group["process_group"], group["confirmed"]) for group in shown["live_process_groups"]),
                             sorted((group, True) for group in groups.values()))
            # A group whose leader is not the process Baton started (a reused ID) is only reported as uncertain.
            reused = json.loads((run_dir / "run.json").read_text())
            for entry in reused["reviewers"]:
                for attempt in entry["attempts"]:
                    attempt["process_group_start"] = "Thu Jan  1 00:00:00 1970"
            (run_dir / "run.json").write_text(json.dumps(reused))
            output = self.show(str(run_dir)).stdout
            self.assertNotIn("kill -TERM", output)
            self.assertIn(f"Warning: process group {groups['claude']} of the claude reviewer may still exist, but its "
                          f"leader is not the reviewer Baton started; check it with `pgrep -lg {groups['claude']}`",
                          output)
        finally:
            for group in groups.values():
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "still running" in self.show(str(run_dir)).stdout:
            time.sleep(0.1)
        self.assertNotIn("still running", self.show(str(run_dir)).stdout)

    def test_reused_pid_is_not_mistaken_for_the_runner(self) -> None:
        own = os.getpid()
        self.assertEqual(effective_status({"status": "running", "pid": own, "pid_start": process_start(own)}), "running")
        self.assertEqual(effective_status({"status": "running", "pid": own, "pid_start": "Thu Jan  1 00:00:00 1970"}),
                         "interrupted")
        self.assertEqual(effective_status({"status": "running", "pid": None}), "interrupted")
        self.assertEqual(effective_status({"status": "completed", "pid": 1}), "completed")

    def test_show_lists_sessions_and_marks_missing_files(self) -> None:
        self.env["CLAUDE_CODE_SESSION_ID"] = "launcher"
        result = self.plan_review(reviewers="codex,claude")
        record = self.record(result)
        claude = next(entry for entry in record["reviewers"] if entry["provider"] == "claude")
        Path(claude["log"]).unlink()
        output = self.show(record["id"]).stdout.splitlines()
        self.assertIn(f"    log: {claude['log']} (missing)", output)
        self.assertIn(f"    review: {claude['review']}", output)
        self.assertIn("Launched from: claude session launcher", output)
        self.assertIn("Reviewers: 2 of 2 produced a review", output)
        self.assertIn(f"    attempt 1: claude session {claude['attempts'][0]['session_id']}, cwd {self.root}, exit 0 "
                      "[accepted]", output)
        self.assertIn(f"    attempt 1: codex session codex-session-{self.calls('codex')[0]['pid']}, cwd {self.root}, "
                      "exit 0, reasoning high [accepted]", output)
        self.assertTrue(output[-1].startswith("Reviewer sessions hold the untrusted diff or plan text"))

    def test_listing_ignores_symlinked_run_directories(self) -> None:
        record = self.record(self.plan_review(reviewers="claude"))
        (self.tmp / "plan-review.planted.x").symlink_to(Path(record["run_dir"]), target_is_directory=True)
        result = subprocess.run([str(BATON), "review", "list", "--json"], cwd=self.root, env=self.env, text=True,
                                capture_output=True)
        self.assertEqual([row["path"] for row in json.loads(result.stdout)], [record["run_dir"]])

    def test_unknown_run_and_unsaved_list_outside_a_project(self) -> None:
        result = subprocess.run([str(BATON), "review", "show", "nope"], cwd=self.root, env=self.env, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No review run found for nope.", result.stderr)
        first = self.record(self.plan_review(reviewers="claude"))
        second = self.record(self.plan_review(reviewers="codex"))
        result = subprocess.run([str(BATON), "review", "list", "--json"], cwd=self.root, env=self.env, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([(row["id"], row["coverage"], row["status"], row["saved"]) for row in rows],
                         [(first["id"], "1 of 1", "completed", False), (second["id"], "1 of 1", "completed", False)])


class SaveTest(unittest.TestCase):
    """`baton review save` inside and outside a project, with a disposable project manager."""

    def setUp(self) -> None:
        self.fixture = fixtures.ProjectManagerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        env = self.fixture.env
        root = self.fixture.root
        self.tmp = root / "review-tmp"
        self.tmp.mkdir()
        stub = root / "fake-claude"
        stub.write_text(STUB)
        stub.chmod(0o755)
        env.update(TMPDIR=str(self.tmp), CLAUDE_BIN=str(stub), REVIEW_TEST_ROOT=str(root),
                   PLAN_REVIEW_REVIEWERS="claude", PEER_REVIEW_REVIEWERS="claude", PEER_REVIEW_SKIP_FETCH="1")
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        self.checkout = self.fixture.checkout("alpha")
        self.directory = fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts)
        (self.directory / "plan.md").write_text("# Plan\nChange alpha.\n")

    def baton(self, *args: str, cwd: Path | None = None, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.fixture.run_command([str(BATON), *args], cwd or self.checkout, ok=ok)

    def run_dir(self, result: subprocess.CompletedProcess[str]) -> Path:
        return Path(result.stdout.splitlines()[-1].removeprefix("REVIEW_ARTIFACTS="))

    def test_save_copies_the_record_and_reviews_and_commits_them(self) -> None:
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan"))
        record = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(record["project"]["name"], "example")
        self.assertEqual(record["plan"], str(self.directory / "plan.md"))
        self.assertEqual(record["repositories"], [str(self.checkout.resolve())])
        (run_dir / f"{self.directory.name}-plan-review-claude.md.failed").write_text("earlier failure\n")
        result = self.baton("review", "save", str(run_dir))
        saved = self.directory / "reviews" / record["id"]
        self.assertIn(f"Saved review run {record['id']} to {saved}", result.stdout)
        stem = f"{self.directory.name}-plan-review-claude"
        self.assertEqual(sorted(path.name for path in saved.iterdir()), [f"{stem}.md", f"{stem}.md.failed", "run.json"])
        self.assertEqual(json.loads((saved / "run.json").read_text()), record)
        tracked = self.fixture.git(self.fixture.artifacts, "ls-files", "--", str(saved))
        self.assertEqual(len(tracked.splitlines()), 3)
        self.assertEqual(self.fixture.git(self.fixture.artifacts, "status", "--porcelain", "--", str(saved)), "")
        # Saving again is a no-op; the saved copy survives the temporary run directory.
        self.assertIn("already saved", self.baton("review", "save", str(run_dir)).stdout)
        rows = json.loads(self.baton("review", "list", "--json").stdout)
        self.assertEqual([(row["id"], row["saved"], row["path"]) for row in rows], [(record["id"], True, str(saved))])
        for path in run_dir.iterdir():
            path.unlink()
        run_dir.rmdir()
        output = self.baton("review", "show", record["id"]).stdout
        self.assertIn(f"Record: {saved / 'run.json'} (saved)", output)
        self.assertIn(f"(missing; saved copy: {saved / (stem + '.md')})", output)
        self.assertEqual(json.loads(self.baton("review", "list", "--json", "--selector", "example",
                                               cwd=self.fixture.root).stdout)[0]["id"], record["id"])

    def test_save_refuses_a_running_run_and_another_projects_run(self) -> None:
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan"))
        record = json.loads((run_dir / "run.json").read_text())
        running = {**record, "status": "running", "pid": os.getpid(), "pid_start": process_start(os.getpid())}
        (run_dir / "run.json").write_text(json.dumps(running))
        result = self.baton("review", "save", str(run_dir), ok=False)
        self.assertIn(f"Review run {record['id']} is still running", result.stderr)
        self.assertFalse((self.directory / "reviews").exists())
        (run_dir / "run.json").write_text(json.dumps({**record, "project": {"name": "other", "uuid": "0" * 32}}))
        result = self.baton("review", "save", str(run_dir), ok=False)
        self.assertIn("belongs to project other", result.stderr)

    def test_save_rejects_a_record_whose_id_is_not_a_run_id(self) -> None:
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan"))
        record = json.loads((run_dir / "run.json").read_text())
        for forged in ("../escaped", "/tmp/escaped", "20261113T000000Z-plan-x/../../x"):
            with self.subTest(id=forged):
                (run_dir / "run.json").write_text(json.dumps({**record, "id": forged}))
                result = self.baton("review", "save", str(run_dir), ok=False)
                self.assertIn("Not a review run record", result.stderr)
        self.assertFalse((self.directory / "reviews").exists())
        self.assertFalse((self.directory / "escaped").exists())

    def test_refused_save_copies_nothing_and_a_leftover_copy_is_not_trusted(self) -> None:
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan"))
        record = json.loads((run_dir / "run.json").read_text())
        reviews = self.directory / "reviews"
        # An artifact checkout that cannot take the commit refuses the save before anything is copied.
        self.fixture.git(self.fixture.artifacts, "switch", "-q", "-c", "elsewhere")
        result = self.baton("review", "save", str(run_dir), ok=False)
        self.assertIn("Project artifacts are committed only on main", result.stderr)
        self.assertFalse(reviews.exists())
        self.fixture.git(self.fixture.artifacts, "switch", "-q", "main")
        # A copy left by a save whose commit failed is replaced and committed, not reported as saved.
        leftover = reviews / record["id"]
        leftover.mkdir(parents=True)
        (leftover / "run.json").write_text("{}")
        result = self.baton("review", "save", str(run_dir))
        self.assertNotIn("already saved", result.stdout)
        self.assertEqual(json.loads((leftover / "run.json").read_text()), record)
        self.assertTrue(self.fixture.git(self.fixture.artifacts, "ls-files", "--", str(leftover / "run.json")))

    def test_failed_save_leaves_nothing_and_empty_reviews_are_not_saved(self) -> None:
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan"))
        record = json.loads((run_dir / "run.json").read_text())
        empty = run_dir / f"{self.directory.name}-plan-review-agy.md"
        empty.write_text("")
        lock = Path(self.fixture.git(self.fixture.artifacts, "rev-parse", "--absolute-git-dir")) / "index.lock"
        lock.write_text("")
        try:
            result = self.baton("review", "save", str(run_dir), ok=False)
        finally:
            lock.unlink()
        self.assertIn("index.lock", result.stderr)
        self.assertFalse((self.directory / "reviews").exists())
        self.baton("review", "save", str(run_dir))
        saved = self.directory / "reviews" / record["id"]
        self.assertEqual(sorted(path.name for path in saved.iterdir()),
                         [f"{self.directory.name}-plan-review-claude.md", "run.json"])

    def test_save_outside_a_project_has_nothing_to_save(self) -> None:
        plan = self.fixture.root / "loose-plan.md"
        plan.write_text("# Plan\n")
        outside = self.fixture.root / "outside"
        outside.mkdir()
        run_dir = self.run_dir(self.baton("review", "run", "--kind=plan", str(plan), cwd=outside))
        self.assertIsNone(json.loads((run_dir / "run.json").read_text())["project"])
        result = self.fixture.run_command([str(BATON), "review", "save", str(run_dir)], outside, ok=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("Not inside a project", result.stderr)

    def test_implementation_run_records_the_project_base(self) -> None:
        (self.checkout / "change.txt").write_text("change\n")
        self.fixture.git(self.checkout, "add", "change.txt")
        self.fixture.git(self.checkout, "commit", "-m", "Change")
        run_dir = self.run_dir(self.baton("review", "run", "--kind=implementation"))
        record = json.loads((run_dir / "run.json").read_text())
        self.assertEqual((record["base_ref"], record["project"]["name"]), ("refs/remotes/origin/main", "example"))
        self.baton("review", "save", str(run_dir))
        self.assertTrue((self.directory / "reviews" / record["id"] / "run.json").is_file())


if __name__ == "__main__":
    unittest.main()
