import fcntl
import json
from pathlib import Path
import subprocess
import time
import unittest
from typing import Any

from test_project_manager import artifact_path

import test_project_manager as fixtures


class ProjectBlackboardTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    replace_interactively = fixtures.ProjectManagerTest.replace_interactively
    update_state = fixtures.ProjectManagerTest.update_state

    def start_project(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")

    def board_dir(self) -> Path:
        return artifact_path(self.projects, self.artifacts, "example")

    def snapshot(self) -> dict[str, Any]:
        return json.loads(self.cli("blackboard", "read", "--json", "--selector", "p3").stdout)

    def generation(self) -> str:
        return json.loads(self.cli("status", "p3", "--json").stdout)["blackboard"]["generation"]

    def test_cli_filtered_reads_include_broadcasts_and_advance_cursor(self) -> None:
        self.start_project()
        generation = self.generation()
        before = self.snapshot()
        for recipient, body in (("child", "Private child note"), ("other", "Other note"),
                                ("all", "Shared contract")):
            self.cli("blackboard", "post", "--selector", "example", "--expected-generation", generation,
                     "--agent", "parent", "--to", recipient, "--text", body)
        args = ("blackboard", "read", "--selector", "example", "--expected-generation", generation,
                "--json", "--to", "child")
        selected = json.loads(self.cli(*args, "--since", before["cursor"]).stdout)
        self.assertIn("Private child note", selected["text"])
        self.assertIn("Shared contract", selected["text"])
        self.assertNotIn("Other note", selected["text"])
        self.assertEqual(selected["revision"], before["revision"])
        limited = json.loads(self.cli(*args, "--tail", "1").stdout)
        self.assertIn("Shared contract", limited["text"])
        self.assertNotIn("Private child note", limited["text"])
        self.assertEqual(json.loads(self.cli(*args, "--since", selected["cursor"]).stdout)["text"], "")
        self.assertEqual(json.loads(self.cli(*args, "--tail", "0").stdout)["text"], "")
        self.assertIn("nonnegative", self.cli(*args, "--tail", "-1", ok=False).stderr)

    def test_cli_filtered_read_revision_edits_only_the_overview(self) -> None:
        self.start_project()
        generation = self.generation()
        initial = self.snapshot()
        self.assertTrue(initial["overview"]["body"].startswith("# Project blackboard: example\n"))
        for heading in ("Goal", "Phase and status", "Owners and assignments", "Blockers and open questions", "Next steps", "Pointers"):
            self.assertIn(f"\n## {heading}\n", initial["overview"]["body"])
        self.cli("blackboard", "post", "--selector", "example", "--expected-generation", generation,
                 "--agent", "parent", "--to", "worker-a", "--text", "Selected note")
        self.cli("blackboard", "post", "--selector", "example", "--expected-generation", generation,
                 "--agent", "parent", "--to", "worker-c", "--text", "Keep worker-c's note")
        before = self.snapshot()
        for filters in (("--to", "worker-a"), ("--tail", "1"), ("--tail", "0"),
                        ("--since", initial["cursor"])):
            with self.subTest(filters=filters):
                selected = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json", *filters).stdout)
                self.assertEqual((selected["revision"], selected["overview"]), (before["revision"], before["overview"]))
        cheap = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json", "--tail", "0").stdout)
        self.assertEqual((cheap["text"], cheap["entries"]), ("", []))
        self.cli("blackboard", "edit", "--selector", "example", "--expected-generation", generation,
                 "--agent", "parent", "--expected-revision", cheap["revision"], "--text", "# Current state\n")
        edited = self.snapshot()
        self.assertEqual(edited["overview"], {"body": "# Current state\n", "version": before["overview"]["version"] + 1})
        self.assertEqual(edited["entries"], before["entries"])
        failed = self.cli("blackboard", "edit", "--selector", "example", "--expected-generation", generation,
                          "--agent", "parent", "--expected-revision", cheap["revision"], "--text", "# Stale\n", ok=False)
        self.assertIn("read --tail 0 --json", failed.stderr)
        self.assertEqual(self.snapshot(), edited)

    def post(self, text: str, agent: str = "parent") -> subprocess.CompletedProcess[str]:
        return self.cli("blackboard", "post", "--expected-generation", self.generation(), "--agent", agent,
                        "--text", text, cwd=self.slots / "p3")

    def process(self, *args: str) -> subprocess.Popen[str]:
        if args[0] in ("post", "edit") and "--expected-generation" not in args:
            args = (args[0], "--expected-generation", self.generation(), *args[1:])
        process = subprocess.Popen(
            ["bash", str(fixtures.SCRIPT), "blackboard", *args, "--selector", "p3"],
            cwd=self.root, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate()

    def test_creation_ignores_board_and_permanent_lock(self) -> None:
        self.start_project()
        directory = self.board_dir()
        for filename in ("blackboard.sqlite", "blackboard-lock.md"):
            self.assertTrue((directory / filename).is_file())
            self.assertEqual(self.git(self.artifacts, "check-ignore", f"{self.board_dir().name}/{filename}"), f"{self.board_dir().name}/{filename}")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        before = (directory / "blackboard-lock.md").stat().st_ino
        self.post("Assign catalog audit to worker-a")
        self.assertEqual((directory / "blackboard-lock.md").stat().st_ino, before)

    def test_post_content_sources_and_revision_checked_edit(self) -> None:
        self.start_project()
        self.post("Catalog shape is stable", agent="worker-a")
        note = self.root / "finding.txt"
        note.write_text("Use the shared parser\n")
        self.cli("blackboard", "post", "--expected-generation", self.generation(), "--agent", "worker-b", "--to", "worker-a",
                 "--file", str(note), "--selector", "example")
        process = self.process("post", "--agent", "worker-c")
        stdout, stderr = process.communicate("Shared tests cover the contract\n", timeout=15)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        before = self.snapshot()
        self.assertEqual(Path(before["path"]), self.board_dir() / "blackboard.sqlite")
        for text in ("worker-a", "worker-b", "Catalog shape is stable", "Use the shared parser", "Shared tests cover the contract"):
            self.assertIn(text, before["text"])
        replacement = "# Updated coordination\n"
        self.cli("blackboard", "edit", "--expected-generation", self.generation(), "--agent", "parent", "--expected-revision", before["revision"],
                 "--text", replacement, "--selector", "p3")
        edited = self.snapshot()
        self.assertEqual(edited["overview"]["body"], replacement)
        self.assertEqual(edited["entries"], before["entries"])
        failed = self.cli("blackboard", "edit", "--expected-generation", self.generation(), "--agent", "parent", "--expected-revision", before["revision"],
                          "--text", replacement + "Stale replacement", "--selector", "p3", ok=False)
        self.assertIn("revision", failed.stderr.lower())
        self.assertEqual(self.snapshot(), edited)

    def test_posts_continue_during_project_operations_but_not_cleanup(self) -> None:
        self.start_project()
        generation = self.generation()
        states = ("adding", "branching", "renaming")
        for state in states:
            with self.subTest(state=state):
                self.update_state(lambda project, state=state: project.update(state=state))
                self.cli("blackboard", "post", "--expected-generation", generation, "--agent", "worker",
                         "--text", f"Blocker reported during {state}", "--selector", "p3")
        self.update_state(lambda project: project.update(state="cleaning"))
        failed = self.cli("blackboard", "post", "--expected-generation", generation, "--agent", "worker",
                          "--text", "Too late", "--selector", "p3", ok=False)
        self.assertIn("being cleaned up", failed.stderr)
        self.update_state(lambda project: project.update(state="active"))
        text = self.snapshot()["text"]
        for state in states:
            self.assertIn(f"Blocker reported during {state}", text)
        self.assertNotIn("Too late", text)

    def test_generation_is_required_for_updates_and_fences_reads(self) -> None:
        self.start_project()
        for action, extra in (("post", ("--text", "Unfenced")),
                              ("edit", ("--expected-revision", "0" * 64, "--text", "Unfenced"))):
            with self.subTest(action=action):
                failed = self.cli("blackboard", action, "--agent", "worker", *extra, "--selector", "p3", ok=False)
                self.assertIn("--expected-generation", failed.stderr)
        self.assertNotIn("Unfenced", self.snapshot()["text"])
        stale = self.cli("blackboard", "read", "--expected-generation", "0" * 32, "--selector", "p3", ok=False)
        self.assertIn("generation changed", stale.stderr)
        self.assertEqual(stale.stdout, "")
        fenced = json.loads(self.cli("blackboard", "read", "--json", "--expected-generation", self.generation(),
                                     "--selector", "p3").stdout)
        self.assertEqual(fenced["generation"], self.generation())

    def test_edit_rejects_empty_and_unattributed_text(self) -> None:
        self.start_project()
        self.post("Keep this note")
        before = self.snapshot()
        revision = ("--expected-revision", before["revision"])
        for content in ("", "  \n\t\n"):
            with self.subTest(content=content):
                process = self.process("edit", "--agent", "parent", *revision)
                stdout, stderr = process.communicate(content, timeout=15)
                self.assertNotEqual(process.returncode, 0, stdout)
                self.assertIn("must not be empty", stderr)
        missing = self.cli("blackboard", "edit", "--expected-generation", self.generation(), *revision, "--text", before["text"], "--selector", "p3", ok=False)
        self.assertIn("--agent", missing.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_writer_honors_separate_document_lock(self) -> None:
        self.start_project()
        before = (self.board_dir() / "blackboard.sqlite").read_bytes()
        with (self.board_dir() / "blackboard-lock.md").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.process("post", "--agent", "worker", "--text", "Blocked until unlocked")
            time.sleep(0.25)
            self.assertIsNone(process.poll())
            self.assertEqual((self.board_dir() / "blackboard.sqlite").read_bytes(), before)
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertIn("Blocked until unlocked", self.snapshot()["text"])

    def test_stdin_is_collected_before_locking(self) -> None:
        self.start_project()
        process = self.process("post", "--agent", "worker")
        time.sleep(0.25)
        with (self.board_dir() / "blackboard-lock.md").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(process.poll())
            fcntl.flock(lock, fcntl.LOCK_UN)
        stdout, stderr = process.communicate("Input finished before update\n", timeout=15)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")

    def test_cleanup_preserves_coordination_and_commits_only_archive(self) -> None:
        self.start_project()
        self.post("Final design decision")
        lock = self.board_dir() / "blackboard-lock.md"
        inode, journal = lock.stat().st_ino, lock.read_bytes()
        unrelated = self.artifacts / "unrelated.md"
        unrelated.write_text("Unrelated staged changes\n")
        self.git(self.artifacts, "add", "unrelated.md")
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        self.cli("cleanup", "p3")
        self.assertIn("Final design decision", (self.board_dir() / "coordination" / "history.md").read_text())
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertTrue(lock.read_bytes().startswith(journal))
        self.assertIn(b'"status": "finalized"', lock.read_bytes())
        self.assertNotEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.artifacts, "diff", "--cached", "--name-only"), "unrelated.md")
        late = self.cli("blackboard", "post", "--expected-generation", "0" * 32, "--agent", "late-worker", "--text", "Too late", "--selector", "example", ok=False)
        self.assertIn("No active project", late.stderr)
        self.assertIn("Final design decision", (self.board_dir() / "coordination" / "history.md").read_text())

    def test_cli_ask_wait_resume_and_parent_wait_from_read_cursor(self) -> None:
        self.start_project()
        generation = self.generation()
        board = ("--selector", "example", "--expected-generation", generation)
        cursor = self.snapshot()["cursor"]
        asked_run = self.cli("blackboard", "ask", *board, "--agent", "worker", "--to", "parent",
                             "--text", "Proceed?", "--timeout", "0")
        asked = json.loads(asked_run.stdout)
        self.assertTrue(asked["timed_out"])
        question = asked["question"]["id"]
        uuid = json.loads(self.cli("status", "example", "--json").stdout)["uuid"]
        self.assertRegex(asked_run.stderr, rf"Question {question} posted; resume with: \S+ blackboard wait --selector {uuid} "
                                           rf"--expected-generation {generation} --reply-to {question}")
        parent = json.loads(self.cli("blackboard", "wait", *board, "--since", cursor, "--kind", "question,settlement",
                                     "--timeout", "0").stdout)
        self.assertEqual([entry["id"] for entry in parent["entries"]], [question])
        self.cli("blackboard", "post", *board, "--agent", "parent", "--to", "worker", "--kind", "answer",
                 "--reply-to", question, "--text", "Yes")
        resumed = json.loads(self.cli("blackboard", "wait", *board, "--reply-to", question, "--timeout", "0").stdout)
        self.assertEqual([entry["body"] for entry in resumed["entries"]], ["Yes"])
        later = json.loads(self.cli("blackboard", "wait", *board, "--since", parent["cursor"], "--kind", "question",
                                    "--timeout", "0").stdout)
        self.assertEqual((later["timed_out"], later["entries"]), (True, []))
        agents = json.loads(self.cli("blackboard", "agents", *board, "--quiet-after", "0").stdout)["agents"]
        self.assertEqual({agent["agent"]: agent["verdict"] for agent in agents}, {"parent": "quiet", "worker": "quiet"})
        self.assertIn("one of the arguments", self.cli("blackboard", "wait", *board, ok=False).stderr)
        self.assertIn("stale", self.cli("blackboard", "wait", "--selector", "example", "--expected-generation", "old",
                                        "--reply-to", question, ok=False).stderr)

    def test_settlements_survive_validation_sealing_and_sealed_reexport(self) -> None:
        from baton.blackboard_sqlite import SQLiteBlackboard
        self.start_project()
        status = json.loads(self.cli("status", "p3", "--json").stdout)
        generation = status["blackboard"]["generation"]
        common = ("--selector", "p3", "--expected-generation", generation, "--agent", "worker")
        task = json.loads(self.cli("task", "create", "--title", "Settled", *common).stdout)
        self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *common)
        self.cli("task", "settle", "--id", task["id"], "--expected-version", "2", "--outcome", "succeeded",
                 "--summary", "Valid settlement", *common)
        board = SQLiteBlackboard(self.board_dir(), "example", generation, status["uuid"])
        legacy = '{"baton": "settlement", "v": 0, "summary": "Legacy settlement"}'
        with board.transaction("legacy") as connection:
            board.insert_entry(connection, board.new_entry("legacy", legacy, "all", "settlement", None, task["id"]))
        self.cli("cleanup", "p3")
        archive = fixtures.read_state(self.projects)["archive"][-1]["coordination_archive"]
        directory = Path(archive["directory"])
        overview = (directory / "overview.md").read_text()
        self.assertEqual(overview.count("Settlement: succeeded"), 1)
        self.assertIn(legacy, overview)
        manifest = board.export(directory, replace_sealed=True)
        self.assertEqual(manifest["logical_sha256"], archive["logical_sha256"])
        self.assertEqual((directory / "overview.md").read_text(), overview)

    def test_cleanup_dry_run_and_decline_leave_board_active(self) -> None:
        self.start_project()
        self.post("Keep coordinating")
        before = self.snapshot()
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        self.cli("cleanup", "p3", "--dry-run")
        (self.checkout("alpha") / "untracked.md").write_text("Dirty work\n")
        self.replace_interactively(b"n\n", cleanup=True, ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)
        self.post("Still coordinating")

    def test_reusing_project_name_keeps_previous_board_separate(self) -> None:
        self.start_project()
        self.post("First generation decision")
        first = self.snapshot()
        old = self.board_dir()
        lock = old / "blackboard-lock.md"
        inode = lock.stat().st_ino
        self.cli("cleanup", "p3")
        self.create("alpha")
        self.assertNotEqual(self.board_dir(), old)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertIn("First generation decision", (old / "coordination" / "history.md").read_text())
        second = self.snapshot()
        self.assertNotIn("First generation decision", second["text"])
        self.cli("blackboard", "post", "--agent", "worker", "--text", "Second generation decision",
                 "--expected-generation", second["generation"], "--selector", "p3")
        stale = self.cli("blackboard", "post", "--agent", "worker", "--text", "Stale session",
                         "--expected-generation", first["generation"], "--selector", "p3", ok=False)
        self.assertIn("generation", stale.stderr.lower())
        self.assertNotIn("Stale session", self.snapshot()["text"])
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_rename_preserves_lock_inode_and_posts_using_new_project_name(self) -> None:
        self.start_project()
        self.post("Preserve the design across rename")
        inode = (self.board_dir() / "blackboard-lock.md").stat().st_ino
        self.cli("rename", "renamed", "p3")
        directory = artifact_path(self.projects, self.artifacts, "renamed")
        self.assertTrue(self.board_dir().exists())
        self.assertEqual((directory / "blackboard-lock.md").stat().st_ino, inode)
        self.cli("blackboard", "post", "--expected-generation", self.generation(), "--agent", "worker", "--text", "Continue after rename", "--selector", "renamed")
        content = json.loads(self.cli("blackboard", "read", "--json", "--selector", "renamed").stdout)
        self.assertEqual(content["project"], "renamed")
        self.assertEqual(Path(content["path"]).parent, directory)
        self.assertIn("Preserve the design across rename", content["text"])
        self.assertIn("Continue after rename", content["text"])
        self.cli("cleanup", "renamed")
        self.assertIn("Continue after rename", (directory / "coordination" / "history.md").read_text())

    def test_rename_retries_already_relocated_board_after_manifest_commit_failure(self) -> None:
        self.start_project()
        self.post("Resume rename without replacing the lock")
        generation = self.generation()
        inode = (self.board_dir() / "blackboard-lock.md").stat().st_ino
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.cli("rename", "renamed", "p3", ok=False)
        renamed = artifact_path(self.projects, self.artifacts, "renamed")
        self.assertTrue(self.board_dir().exists())
        self.assertEqual((renamed / "blackboard-lock.md").stat().st_ino, inode)
        self.cli("blackboard", "post", "--expected-generation", generation, "--agent", "worker",
                 "--text", "Blocked while the rename is interrupted", "--selector", "p3")
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.cli("rename", "renamed", "p3")
        self.cli("blackboard", "post", "--expected-generation", self.generation(), "--agent", "worker", "--text", "Rename recovered", "--selector", "renamed")
        board = self.cli("blackboard", "read", "--selector", "renamed").stdout
        self.assertIn("Rename recovered", board)
        self.assertIn("Blocked while the rename is interrupted", board)
        self.assertEqual((renamed / "blackboard-lock.md").stat().st_ino, inode)

    def test_explicit_old_generation_is_rejected_by_cli_after_project_reuse(self) -> None:
        self.start_project()
        generation = self.snapshot()["generation"]
        self.cli("cleanup", "p3")
        self.create("alpha")
        before = self.snapshot()
        failed = self.cli("blackboard", "post", "--agent", "stale-worker", "--text", "Stale session",
                          "--expected-generation", generation, "--selector", "p3", ok=False)
        self.assertIn("generation", failed.stderr.lower())
        self.assertEqual(self.snapshot(), before)

    def test_queued_stdin_writer_cannot_write_to_reused_project(self) -> None:
        self.start_project()
        generation = self.snapshot()["generation"]
        process = self.process("post", "--agent", "queued-worker", "--expected-generation", generation)
        time.sleep(0.25)
        self.assertIsNone(process.poll())
        self.cli("cleanup", "p3")
        self.create("alpha")
        before = self.snapshot()
        stdout, stderr = process.communicate("Old queued input\n", timeout=15)
        self.assertNotEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertIn("generation", stderr.lower())
        self.assertEqual(self.snapshot(), before)

    def test_tracked_previous_board_is_not_adopted_on_name_reuse(self) -> None:
        self.start_project()
        original = self.board_dir()
        self.cli("cleanup", "p3")
        self.git(self.artifacts, "add", "--force", str(original / "blackboard.sqlite"))
        self.git(self.artifacts, "commit", "-m", "Track a previous live board")
        self.create("alpha")
        self.assertNotEqual(self.board_dir(), original)
        self.assertTrue((original / "blackboard.sqlite").exists())
        self.post("A fresh project never adopts the tracked board")
