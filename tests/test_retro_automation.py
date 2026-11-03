from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from baton.retro_automation import (DRAFT_BEGIN, DRAFT_END, archive_scan, create_draft_workspace,
                                    execute_retro, existing_result, output_excerpt, state_directory)
from baton.errors import ProjectError

import test_project_retro as fixtures
import test_project_manager as manager_fixtures
import provider_fixtures
from baton.providers import PROVIDERS, session_variables


class DraftWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        tempfile.tempdir = str(self.root)

    def test_private_unique_workspaces_use_system_temp_root(self) -> None:
        first, second = create_draft_workspace(), create_draft_workspace()
        self.assertEqual(first.parent, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)

    def test_temp_root_inside_git_is_rejected_before_creating_workspace(self) -> None:
        metadata = self.root / ".git"
        metadata.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(metadata, target_is_directory=True)
        for root in (metadata, alias):
            with self.subTest(root=root):
                tempfile.tempdir = str(root)
                with self.assertRaisesRegex(ProjectError, "outside Git metadata"):
                    create_draft_workspace()
        self.assertEqual(list(metadata.iterdir()), [])

    def test_only_old_failed_workspaces_are_pruned(self) -> None:
        stale, active, recent = (self.root / ("baton-retro-" + label) for label in ("stale", "active", "recent"))
        for directory in (stale, active, recent):
            directory.mkdir()
        for directory in (stale, recent):
            (directory / ".failed").touch()
        old = time.time() - 8 * 24 * 60 * 60
        os.utime(stale / ".failed", (old, old))
        os.utime(active, (old, old))
        alias = self.root / "baton-retro-alias"
        alias.symlink_to(active, target_is_directory=True)
        create_draft_workspace()
        self.assertFalse(stale.exists())
        self.assertTrue(active.is_dir())
        self.assertTrue(recent.is_dir())
        self.assertTrue(alias.is_symlink())


class ArchiveScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = self.root / "registry"
        for name in ("archive", "projects"):
            (self.layout / name).mkdir(parents=True)
        self.now = datetime(2026, 10, 16, tzinfo=timezone.utc)

    @staticmethod
    def artifact_directory(project: dict) -> Path:
        return Path(project["artifacts_path"])

    def archive(self, name: str, *, age: int = 1, owner: bool = True, **extra: object) -> dict:
        project_id = uuid.uuid4().hex
        output = self.root / f"{name}-{project_id[:8]}"
        output.mkdir()
        project = {"uuid": project_id, "name": name, "state": "archived",
                   "completed_at": (self.now - timedelta(days=age)).isoformat(),
                   "owner": {"provider": "codex", "session_id": str(uuid.uuid4())} if owner else None,
                   "artifacts_path": str(output), **extra}
        (self.layout / "archive" / f"{project_id}.json").write_text(json.dumps(project))
        return project

    def scan(self) -> dict:
        return archive_scan(self.layout, self.artifact_directory, "3d", now=self.now)

    def test_filters_completion_not_creation_or_mtime_and_orders_deterministically(self) -> None:
        older = self.archive("older", age=2)
        newer = self.archive("newer", age=1)
        self.archive("too-old", age=4)
        self.archive("future", age=-1)
        self.archive("unknown", completed_at="")
        self.archive("naive", completed_at="2026-10-16T00:00:00")
        self.archive("unowned", owner=False)
        report = self.scan()
        self.assertEqual([entry["uuid"] for entry in report["candidates"]], [older["uuid"], newer["uuid"]])
        self.assertEqual(len(report["skipped"]), 5)
        self.assertEqual(report["errors"], [])

    def test_manual_retros_require_project_uuid(self) -> None:
        new = self.archive("new")
        Path(new["artifacts_path"], "retro.md").write_text(f"Project UUID: {new['uuid']}")
        old = self.archive("old")
        legacy = self.root / "legacy"
        legacy.mkdir()
        old["artifacts_path"] = str(legacy)
        (legacy / "retro.md").write_text("Unidentified old retrospective")
        (self.layout / "archive" / f"{old['uuid']}.json").write_text(json.dumps(old))
        (self.layout / "projects" / "p1.json").write_text(json.dumps({**old, "uuid": uuid.uuid4().hex}))
        report = self.scan()
        self.assertEqual(report["candidates"], [])
        self.assertEqual({entry["status"] for entry in report["skipped"]}, {"complete", "blocked"})
        (legacy / "retro.md").write_text(f"Retrospective UUID: {old['uuid']}")
        self.assertTrue(all(entry["status"] == "complete" for entry in self.scan()["skipped"]))


    def test_duplicate_uuid_and_shared_artifact_paths_are_blocked(self) -> None:
        project = self.archive("duplicate")
        (self.layout / "archive" / "duplicate-copy.json").write_text(json.dumps(project))
        report = self.scan()
        self.assertEqual(report["candidates"], [])
        self.assertEqual(len(report["errors"]), 1)
        (self.layout / "archive" / "duplicate-copy.json").unlink()
        (self.layout / "projects" / "p1.json").write_text(json.dumps({**project, "uuid": uuid.uuid4().hex}))
        report = self.scan()
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["skipped"][0]["status"], "blocked")

    def test_malformed_artifact_paths_are_isolated_from_valid_candidates(self) -> None:
        valid = self.archive("valid")
        for index, value in enumerate((123, None, "relative/path", "", ["not-a-path"], "bad\x00path")):
            with self.subTest(value=value):
                self.archive(f"invalid-{index}", artifacts_path=value)
        # Resolving a self-referential symlink must also be caught per manifest.
        loop = self.root / "loop"
        loop.symlink_to(loop)
        self.archive("invalid-loop", artifacts_path=str(loop))
        report = self.scan()
        self.assertEqual([item["uuid"] for item in report["candidates"]], [valid["uuid"]])
        self.assertEqual(len(report["errors"]), 7)
        self.assertTrue(all(item["path"] and item["error"] for item in report["errors"]))

    def test_explicit_conflicting_project_uuid_blocks_manual_retro(self) -> None:
        new = self.archive("new")
        old = self.archive("old")
        legacy = self.root / "legacy"
        legacy.mkdir()
        old["artifacts_path"] = str(legacy)
        (self.layout / "archive" / f"{old['uuid']}.json").write_text(json.dumps(old))
        other = str(uuid.uuid4())
        for project in (new, old):
            Path(project["artifacts_path"], "retro.md").write_text(
                f"# Retrospective\n\n- **Project UUID:** `{other}`\nOwner session: {project['uuid']}\n")
        report = self.scan()
        self.assertEqual(report["candidates"], [])
        self.assertTrue(all(item["status"] == "blocked" for item in report["skipped"]))
        self.assertTrue(all("different project UUID" in item["reason"] for item in report["skipped"]))
        # Ordinary session/evidence UUIDs are not project identity declarations.
        for project in (new, old):
            Path(project["artifacts_path"], "retro.md").write_text(f"Owner session: {other}\n")
        self.assertTrue(all(item["status"] == "blocked" for item in self.scan()["skipped"]))

    def test_invalid_manifest_reported_without_losing_other_candidates(self) -> None:
        self.archive("valid")
        (self.layout / "archive" / "bad.json").write_text("{")
        report = self.scan()
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(len(report["errors"]), 1)


class RetroRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = {"uuid": uuid.uuid4().hex}
        self.output = self.root / "retro.md"
        self.layout = self.root / "layout"

    def execute(self, program: str, *, timeout: int = 2) -> dict:
        return execute_retro(self.layout, self.project, self.output, [sys.executable, "-c", program],
                             self.root, "Explicit test prompt", timeout=timeout)

    def execute_private(self, transcript: str, *, file_text: str | None = None,
                        symlink: bool = False, exit_code: int = 0, stderr: str = "") -> tuple[dict, Path]:
        workspace = self.root / ("private-" + uuid.uuid4().hex)
        workspace.mkdir()
        draft = workspace / "retro.md"
        if symlink:
            draft.symlink_to(self.root / "missing-target")
        transcript_path = workspace / "transcript.txt"
        transcript_path.write_text(transcript)
        program = (f"import sys; from pathlib import Path; sys.stderr.write({stderr!r}); sys.stderr.flush(); "
                   f"print(Path({str(transcript_path)!r}).read_text()); ")
        if file_text is not None:
            program += f"Path({str(draft)!r}).write_text({file_text!r}); "
        program += f"raise SystemExit({exit_code})"
        result = execute_retro(self.layout, self.project, self.output, [sys.executable, "-c", program],
                               workspace, "test", draft=draft,
                               publish=lambda source: self.output.write_bytes(source.read_bytes()),
                               managed_workspace=True)
        return result, workspace

    def test_private_draft_does_not_grant_ownership_of_caller_working_directory(self) -> None:
        sentinel = self.root / "keep.txt"
        sentinel.write_text("keep")
        draft = self.root / "draft.md"
        text = f"Project UUID: {self.project['uuid']}\n"
        program = f"from pathlib import Path; Path({str(draft)!r}).write_text({text!r})"
        result = execute_retro(self.layout, self.project, self.output, [sys.executable, "-c", program],
                               self.root, "test", draft=draft,
                               publish=lambda source: self.output.write_bytes(source.read_bytes()))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertTrue(draft.is_file())

    def test_missing_private_draft_is_recovered_and_published(self) -> None:
        text = f"# Retrospective\nProject UUID: {self.project['uuid']}\n"
        result, workspace = self.execute_private(f"{DRAFT_BEGIN}\n{text}{DRAFT_END}")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered_from_output"])
        self.assertEqual(self.output.read_text(), text)
        self.assertFalse(workspace.exists())

    def test_recovery_reads_stdout_only(self) -> None:
        # codex exec echoes the prompt (which quotes the markers) and repeats the final message on stderr.
        text = f"# Retrospective\nProject UUID: {self.project['uuid']}\n"
        echo = (f"user\n{DRAFT_BEGIN}\n<complete retrospective Markdown>\n{DRAFT_END}\n"
                f"codex\n{DRAFT_BEGIN}\n{text}{DRAFT_END}\n")
        result, _ = self.execute_private(f"{DRAFT_BEGIN}\n{text}{DRAFT_END}", stderr=echo)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.output.read_text(), text)
        self.assertIn("<complete retrospective Markdown>", Path(result["stderr_log"]).read_text())

    def test_recovery_rejects_conflicting_uuid_and_retains_failed_workspace(self) -> None:
        text = f"Project UUID: {uuid.uuid4()}\nOwner session: {self.project['uuid']}\n"
        result, workspace = self.execute_private(f"{DRAFT_BEGIN}\n{text}{DRAFT_END}")
        self.assertEqual(result["status"], "failed")
        self.assertIn("consistently", result["error"])
        self.assertTrue((workspace / ".failed").is_file())
        self.assertFalse(self.output.exists())

    def test_recovery_rejects_incomplete_duplicate_and_reversed_markers(self) -> None:
        for transcript in (f"{DRAFT_BEGIN}\n{self.project['uuid']}",
                           f"{DRAFT_BEGIN}\n{DRAFT_END}\n{DRAFT_BEGIN}\n{DRAFT_END}",
                           f"{DRAFT_END}\n{self.project['uuid']}\n{DRAFT_BEGIN}"):
            with self.subTest(transcript=transcript):
                result, _ = self.execute_private(transcript)
                self.assertEqual(result["status"], "failed")
                self.assertIn("markers", result["error"])
                self.assertFalse(self.output.exists())

    def test_recovery_does_not_replace_invalid_file_symlink_or_failed_process(self) -> None:
        transcript = f"{DRAFT_BEGIN}\n{self.project['uuid']}\n{DRAFT_END}"
        for options in ({"file_text": "partial"}, {"symlink": True}, {"exit_code": 4}):
            with self.subTest(options=options):
                result, _ = self.execute_private(transcript, **options)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.output.exists())

    def test_recovery_rejects_empty_unidentified_and_oversized_output(self) -> None:
        for body in ("", "# No project identity\n", "x" * (2 * 1024 * 1024)):
            with self.subTest(length=len(body)):
                result, _ = self.execute_private(f"{DRAFT_BEGIN}\n{body}\n{DRAFT_END}")
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.output.exists())

    def test_missing_draft_failure_includes_bounded_output_excerpt(self) -> None:
        result, _ = self.execute_private("Permission denied\n" + "x" * 3000)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Permission denied", result["output_excerpt"])
        self.assertLessEqual(len(result["output_excerpt"]), 1000)

    def test_failure_excerpt_includes_end_of_stderr(self) -> None:
        result, _ = self.execute_private("", stderr="banner\n" * 2000 + "Error: no rollout found\n", exit_code=1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Error: no rollout found", result["output_excerpt"])
        self.assertIn("exited with code 1", result["error"])
        self.assertLessEqual(len(result["output_excerpt"]), 1000)

    def test_failure_excerpt_keeps_stderr_tail_after_long_stdout(self) -> None:
        stdout = "".join(f"{'o' * 150} line {index}\n" for index in range(20))
        stderr = "".join(f"{'e' * 150} banner {index}\n" for index in range(20)) + "Error: not logged in\n"
        result, _ = self.execute_private(stdout, stderr=stderr, exit_code=1)
        excerpt = result["output_excerpt"]
        self.assertLessEqual(len(excerpt), 1000)
        self.assertTrue(excerpt.startswith("o" * 150 + " line 0"), excerpt)
        self.assertTrue(excerpt.endswith("Error: not logged in"), excerpt)
        self.assertIn("\nstderr:\n", excerpt)

    def test_excerpt_parts_are_independent(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "stderr.log").write_text("Error: denied\n")
        self.assertEqual(output_excerpt(root / "missing.log", root / "stderr.log"), "stderr:\nError: denied")
        (root / "stdout.log").write_text("x" * 3000)
        self.assertEqual(output_excerpt(root / "stdout.log", root / "missing.log"), "x" * 1000)

    def test_success_records_uuid_completion_and_cannot_double_launch(self) -> None:
        program = f"from pathlib import Path; Path({str(self.output)!r}).write_text({self.project['uuid']!r})"
        result = self.execute(program)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["uuid"], self.project["uuid"])
        self.assertEqual(self.execute("raise Exception('must not launch')")["status"], "complete")
        self.assertEqual(result["attempt"], 1)

    def test_conflicting_draft_project_marker_is_not_success(self) -> None:
        text = f"Project UUID: {uuid.uuid4()}\nOwner session: {self.project['uuid']}\n"
        result = self.execute(f"from pathlib import Path; Path({str(self.output)!r}).write_text({text!r})")
        self.assertEqual(result["status"], "failed")
        self.assertIn("consistently", result["error"])

    def test_missing_or_invalid_output_is_failure_and_safe_failure_can_retry(self) -> None:
        self.assertEqual(self.execute("pass")["status"], "failed")
        self.assertEqual(self.execute("raise SystemExit(4)")["attempt"], 2)
        self.assertEqual(self.execute("import pathlib; pathlib.Path('retro.md').write_text('partial')")["status"], "failed")
        self.assertEqual(self.execute("raise Exception('must not overwrite')")["status"], "blocked")
        self.assertEqual(self.output.read_text(), "partial")

    def test_symlink_final_is_never_followed(self) -> None:
        target = self.root / "unrelated"
        target.write_text("keep")
        self.output.symlink_to(target)
        self.assertEqual(self.execute("raise Exception('must not execute')")["status"], "blocked")
        self.assertEqual(target.read_text(), "keep")

    def test_state_uuid_mismatch_blocks_launch(self) -> None:
        directory = state_directory(self.layout, self.project)
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"uuid": uuid.uuid4().hex, "status": "completed"}))
        self.assertEqual(self.execute("raise Exception('must not execute')")["status"], "blocked")

    def test_timeout_kills_group_and_reports_retryable_failure(self) -> None:
        result = self.execute("import time; time.sleep(30)", timeout=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 124)
        self.assertIn("exceeded", result["error"])
        self.assertEqual(self.execute("pass")["attempt"], 2)

    def test_caller_crash_does_not_release_supervisor_lock_or_duplicate_launch(self) -> None:
        script = self.root / "run.py"
        script.write_text("from pathlib import Path\nfrom baton.retro_automation import execute_retro\n"
                          f"execute_retro(Path({str(self.layout)!r}), {self.project!r}, Path({str(self.output)!r}), "
                          f"[{sys.executable!r}, '-c', \"import time; time.sleep(2)\"], Path({str(self.root)!r}), 'test', timeout=5)\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        process = subprocess.Popen([sys.executable, str(script)], env=env)
        state = state_directory(self.layout, self.project) / "state.json"
        try:
            for _ in range(100):
                if state.exists() and json.loads(state.read_text()).get("process_group"):
                    break
                time.sleep(.02)
            self.assertTrue(state.exists())
            process.kill()
            process.wait()
            self.assertEqual(self.execute("raise Exception('duplicate')")["status"], "running")
            time.sleep(2.2)
            self.assertEqual(json.loads(state.read_text())["status"], "failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    def test_supervisor_crash_keeps_live_harness_group_fenced(self) -> None:
        script = self.root / "run.py"
        script.write_text("from pathlib import Path\nfrom baton.retro_automation import execute_retro\n"
                          f"execute_retro(Path({str(self.layout)!r}), {self.project!r}, Path({str(self.output)!r}), "
                          f"[{sys.executable!r}, '-c', \"import time; time.sleep(30)\"], Path({str(self.root)!r}), 'test', timeout=40)\n")
        process = subprocess.Popen([sys.executable, str(script)], env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
        state_path = state_directory(self.layout, self.project) / "state.json"
        group = None
        try:
            for _ in range(100):
                if state_path.exists():
                    state = json.loads(state_path.read_text())
                    if state.get("process_group"):
                        group = state["process_group"]
                        break
                time.sleep(.02)
            self.assertIsNotNone(group)
            os.kill(state["pid"], signal.SIGKILL)
            process.wait(timeout=3)
            self.assertEqual(self.execute("raise Exception('duplicate')")["status"], "running")
        finally:
            if group:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
                process.wait()



class ArchivedRetroIntegrationTest(unittest.TestCase):
    setUp = fixtures.ProjectRetroTest.setUp
    run_command = fixtures.ProjectRetroTest.run_command
    git = fixtures.ProjectRetroTest.git
    cli = fixtures.ProjectRetroTest.cli
    make_repo = fixtures.ProjectRetroTest.make_repo
    checkout = fixtures.ProjectRetroTest.checkout
    create = fixtures.ProjectRetroTest.create
    project = fixtures.ProjectRetroTest.project
    owner = fixtures.ProjectRetroTest.owner
    claim = fixtures.ProjectRetroTest.claim
    start_project = fixtures.ProjectRetroTest.start_project
    extra_cli = fixtures.ProjectRetroTest.extra_cli

    def archive(self, provider: str = "codex") -> dict:
        self.start_project()
        self.extra_cli("session", "claim", "--provider", provider, "--session-id", fixtures.fixtures.SESSION_ID,
                       "--selector", "example")
        project = self.project()
        project.update(state="archived", completed_at=datetime.now(timezone.utc).isoformat())
        layout = self.projects / ".git/baton/v2"
        (layout / "archive" / f"000001-{project['uuid']}.json").write_text(json.dumps(project))
        manager_fixtures.state_file(self.projects).unlink()
        self.git(self.slots / "p3", "switch", "p3")
        return project

    def saved_evidence(self, project: dict) -> None:
        directory = Path(project["artifacts_path"])
        (directory / "plan.md").write_text("# Plan\n")
        (directory / "notes").mkdir(exist_ok=True)
        (directory / "notes" / "finding.md").write_text("Finding\n")
        (directory / "validation").mkdir(exist_ok=True)
        (directory / "validation" / "report.md").write_text("# Validation\nAcceptance evidence\n")
        for name in ("blackboard.sqlite-wal", "blackboard.sqlite-shm", ".snapshot-1.sqlite", ".blackboard-x.tmp"):
            (directory / name).write_text("live")
        (directory / "outside-link").symlink_to(self.root, target_is_directory=True)
        self.git(self.artifacts, "add", "--", str(directory))
        self.git(self.artifacts, "commit", "-m", "Save evidence")

    def assert_evidence_copied(self, invocation: dict) -> None:
        files = set(invocation["files"])
        for name in ("identity.json", "evidence/plan.md", "evidence/notes/finding.md", "evidence/validation/report.md"):
            self.assertIn(name, files)
        self.assertFalse([name for name in files if "sqlite" in name or name.endswith((".tmp", ".lock", "lock.md"))
                          or "outside-link" in name], files)

    def fake_fork(self, *, valid: bool = True) -> Path:
        directory = self.root / "bin"
        directory.mkdir(exist_ok=True)
        log = self.root / "fork.jsonl"
        variables = sorted(session_variables() | {provider_fixtures.EXTRA.session_env})
        script = "#!" + sys.executable + "\n" + f"SESSION_VARIABLES = {variables!r}\n" + '''import json, os, re, sys
from pathlib import Path
stdin = sys.stdin.read()
# Argv-delivered prompts arrive as the longest argument with an empty stdin.
prompt = stdin or max(sys.argv[1:], key=len)
evidence = Path(re.search(r"Project artifacts directory \\(evidence\\): (.+)", prompt).group(1))
files = sorted(str(path.relative_to(os.getcwd())) for path in Path(os.getcwd()).rglob("*") if path.is_file())
with open(os.environ["FORK_LOG"], "a") as log:
    snapshot = json.loads(Path(re.search(r"Identity snapshot: (.+)", prompt).group(1)).read_text())
    log.write(json.dumps({"name": Path(sys.argv[0]).name, "args": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                          "stdin": bool(stdin), "identity": snapshot, "evidence": str(evidence), "files": files,
                          "session_env": sorted(name for name in SESSION_VARIABLES if name in os.environ)}) + "\\n")
identity = re.search(r"Project UUID: (.+)", prompt).group(1)
assert Path(os.getcwd()) in evidence.parents
text = "# Retrospective\\n\\nProject UUID: " + (identity if os.environ.get("VALID_FORK") == "1" else "wrong")
print("BATON_RETRO_DRAFT_BEGIN\\n" + text + "\\nBATON_RETRO_DRAFT_END")
'''
        for name in {entry.binary for entry in PROVIDERS.values()} | {provider_fixtures.EXTRA.binary}:
            command = directory / name
            command.write_text(script)
            command.chmod(0o755)
        temporary = self.root / "tmp"
        temporary.mkdir(exist_ok=True)
        self.env["TMPDIR"] = str(temporary)
        self.env.update(PATH=str(directory) + ":" + self.env["PATH"], FORK_LOG=str(log), VALID_FORK="1" if valid else "0")
        return log

    def test_pending_dry_run_does_not_launch_or_modify_registry_and_ignores_active_slot(self) -> None:
        project = self.archive()
        log = self.fake_fork()
        layout = self.projects / ".git/baton/v2"
        before = {str(path): path.read_bytes() for path in layout.glob("*/*.json")}
        result = json.loads(self.cli("retro", "pending", "--dry-run", "--json").stdout)
        self.assertEqual([entry["uuid"] for entry in result["candidates"]], [project["uuid"]])
        self.assertFalse(log.exists())
        self.assertFalse((layout / "retros").exists())
        self.assertEqual(before, {str(path): path.read_bytes() for path in layout.glob("*/*.json")})

    def test_headless_archived_fork_uses_native_session_fork_and_parent_publishes_and_commits(self) -> None:
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                if provider == "claude":
                    # Fresh fixture since one archived project should produce one invocation.
                    self.temp.cleanup()
                    self.setUp()
                project = self.archive(provider)
                self.saved_evidence(project)
                log = self.fake_fork()
                result = json.loads(self.cli("retro", "pending", "--json").stdout)
                self.assertEqual(result["runs"][0]["status"], "completed")
                self.assertEqual(result["runs"][0]["read_confinement"], PROVIDERS[provider].read_confinement)
                invocation = json.loads(log.read_text())
                args = invocation["args"]
                self.assertTrue(invocation["stdin"])
                if provider == "codex":
                    self.assertEqual(args, ["exec", "--config", 'approval_policy="never"', "-C", invocation["cwd"],
                                            "-s", "read-only", "fork", "--skip-git-repo-check",
                                            project["owner"]["session_id"], "-"])
                else:
                    self.assertEqual(args, ["--print", "--resume", project["owner"]["session_id"], "--fork-session",
                                            "--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk",
                                            "--permission-prompts", "none", "--tools", "Read,Glob,Grep"])
                self.assertNotIn(project["artifacts_path"], " ".join(args))
                self.assertTrue(invocation["evidence"].startswith(invocation["cwd"]))
                self.assertNotIn(f"Project artifacts directory (evidence): {project['artifacts_path']}", invocation["prompt"])
                # The frozen manifest and identity snapshot name the copies, never the live artifacts.
                self.assertNotIn(project["artifacts_path"], invocation["prompt"])
                self.assertNotIn(project["artifacts_path"], json.dumps(invocation["identity"]))
                for key in ("artifacts_path", "retro_path"):
                    self.assertTrue(invocation["identity"][key].startswith(invocation["evidence"]), invocation["identity"])
                self.assertEqual(invocation["identity"]["manifest"]["artifacts_path"], invocation["evidence"])
                self.assert_evidence_copied(invocation)
                self.assertIn("inherited owner history", invocation["prompt"])
                workspace = Path(invocation["cwd"])
                self.assertEqual(workspace.parent, (self.root / "tmp").resolve())
                self.assertNotIn(".git", workspace.parts)
                self.assertFalse(workspace.exists())
                self.assertEqual(invocation["identity"]["uuid"], project["uuid"])
                self.assertEqual(invocation["identity"]["owner"], project["owner"])
                self.assertIn(project["owner"]["session_id"], args)
                self.assertNotEqual(Path(invocation["cwd"]), self.slots / "p3")
                output = Path(project["blackboard"]["directory"]) / "retro.md"
                self.assertIn(project["uuid"], output.read_text())
                self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
                self.assertIn("retrospective", self.git(self.artifacts, "log", "-1", "--format=%s"))
                second = json.loads(self.cli("retro", "pending", "--json").stdout)
                self.assertEqual(second["candidates"], [])
                self.assertEqual(len(log.read_text().splitlines()), 1)
                snapshot = json.loads(self.cli("retro", "show", "--selector", project["uuid"], "--json").stdout)
                self.assertEqual(snapshot["uuid"], project["uuid"])
                self.assertTrue(snapshot["archived"])

    def test_non_fork_providers_run_fresh_unattended_sessions_with_their_prompt_delivery(self) -> None:
        cases = (("agy", "argv"), ("cursor", "argv"), ("extra", "stdin"), ("extra", "argv"))
        for index, (provider, delivery) in enumerate(cases):
            with self.subTest(provider=provider, delivery=delivery):
                if index:
                    self.temp.cleanup()
                    self.setUp()
                project = self.archive(provider)
                self.saved_evidence(project)
                log = self.fake_fork()
                self.env["EXTRA_UNATTENDED"] = delivery
                for variable in session_variables() | {provider_fixtures.EXTRA.session_env}:
                    self.env[variable] = project["owner"]["session_id"]
                run = json.loads(self.extra_cli("retro", "pending", "--json").stdout)["runs"][0]
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["provider"], provider)
                self.assertEqual(run["read_confinement"], (PROVIDERS.get(provider) or provider_fixtures.EXTRA).read_confinement)
                invocation = json.loads(log.read_text())
                self.assertEqual(invocation["stdin"], delivery == "stdin")
                self.assertEqual(invocation["session_env"], [])
                options = invocation["args"][:-1] if delivery == "argv" else invocation["args"]
                self.assertNotIn(project["owner"]["session_id"], options)
                self.assertFalse([value for value in options if value.startswith("--add-dir")])
                if provider == "agy":
                    self.assertEqual(invocation["args"][0], "--print")
                    self.assertEqual(invocation["args"][-1], "--sandbox")
                    self.assertIn("Never run shell commands", invocation["args"][1])
                if provider == "cursor":
                    self.assertEqual(options, ["-p", "--mode", "ask", "--sandbox", "enabled", "--trust",
                                               "--workspace", invocation["cwd"]])
                self.assert_evidence_copied(invocation)
                preamble = invocation["prompt"].partition("Read and follow the complete skill")[0]
                for value in ("fresh retrospective session", "no owner history", "saved evidence"):
                    self.assertIn(value, preamble)
                for value in ("inherited", "Git history", "fork of the project's owner session"):
                    self.assertNotIn(value, preamble)
                self.assertIn(project["uuid"], Path(project["artifacts_path"], "retro.md").read_text())
                self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_unsupported_unattended_provider_is_blocked_and_pending_continues(self) -> None:
        blocked = self.archive("extra")
        layout = self.projects / ".git/baton/v2"
        second_id = uuid.uuid4().hex
        directory = self.artifacts / f"second-{second_id[:8]}"
        directory.mkdir()
        second = {key: value for key, value in blocked.items() if key not in ("blackboard", "coordination_archive")}
        second.update(uuid=second_id, name="second", artifacts_path=str(directory),
                      owner=dict(blocked["owner"], provider="codex"))
        # The blocked project is selected first, so the second run shows that pending continues past it.
        blocked["completed_at"] = (datetime.fromisoformat(blocked["completed_at"]) - timedelta(minutes=1)).isoformat()
        (layout / "archive" / f"000001-{blocked['uuid']}.json").write_text(json.dumps(blocked))
        (layout / "archive" / f"000002-{second_id}.json").write_text(json.dumps(second))
        log = self.fake_fork()
        self.env["EXTRA_UNATTENDED"] = "unsupported"
        report = json.loads(self.extra_cli("retro", "pending", "--json", ok=False).stdout)
        self.assertEqual([(run["uuid"], run["status"]) for run in report["runs"]],
                         [(blocked["uuid"], "blocked"), (second_id, "completed")], report)
        self.assertIn("unsupported", report["runs"][0]["error"])
        self.assertEqual([json.loads(line)["name"] for line in log.read_text().splitlines()], ["codex"])
        self.assertFalse(Path(blocked["artifacts_path"], "retro.md").exists())
        self.assertTrue((directory / "retro.md").is_file())
        self.assertEqual(list((self.root / "tmp").glob("baton-retro-*")), [])
        text = self.extra_cli("retro", "pending", ok=False).stdout
        self.assertIn(f"{blocked['uuid']}: blocked", text)
        self.assertIn("unsupported", text)

    def test_unsupported_provider_is_blocked_before_evidence_is_exported_or_copied(self) -> None:
        self.start_project()
        self.extra_cli("session", "claim", "--provider", "extra", "--session-id", fixtures.fixtures.SESSION_ID,
                       "--selector", "example")
        project = self.project()
        # Exporting this board would fail, so a blocked result proves no export was attempted.
        export = Path(project["artifacts_path"]) / ".blackboard-export"
        export.mkdir()
        (export / "overview.md").write_text("Unrecognized evidence")
        log = self.fake_fork()
        self.env["EXTRA_UNATTENDED"] = "unsupported"
        result = json.loads(self.extra_cli("retro", "--selector", project["uuid"], "--non-interactive", ok=False).stdout)
        self.assertEqual(result["status"], "blocked", result)
        self.assertFalse(log.exists())
        self.assertEqual((export / "overview.md").read_text(), "Unrecognized evidence")
        self.assertEqual(list((self.root / "tmp").glob("baton-retro-*")), [])

    def test_archived_interactive_retro_for_non_fork_provider_starts_fresh_session(self) -> None:
        project = self.archive("cursor")
        log = fixtures.fixtures.ProjectSessionsTest.fake_agents(self)
        self.cli("retro", "--selector", project["uuid"])
        invocation = json.loads(log.read_text())
        directory = Path(project["artifacts_path"])
        self.assertEqual(invocation["provider"], "agent")
        self.assertEqual(Path(invocation["cwd"]).resolve(), directory.resolve())
        self.assertEqual(len(invocation["args"]), 1)
        preamble = invocation["args"][0].partition("Read and follow the complete skill")[0]
        self.assertIn("fresh retrospective session", preamble)
        self.assertIn("Use already saved plans, notes, and board in the artifact directory", preamble)
        self.assertNotIn("inherited", preamble)

    def test_stdout_only_draft_is_published_by_trusted_launcher(self) -> None:
        project = self.archive("claude")
        self.fake_fork()
        result = json.loads(self.cli("retro", "pending", "--json").stdout)["runs"][0]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered_from_output"])
        self.assertIn(project["uuid"], Path(project["artifacts_path"], "retro.md").read_text())
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_active_unattended_fork_uses_supplied_identity(self) -> None:
        self.start_project()
        self.claim(provider="claude")
        project = self.project()
        log = self.fake_fork()
        self.cli("retro", "--selector", project["uuid"], "--non-interactive")
        invocation = json.loads(log.read_text())
        self.assertFalse(invocation["identity"]["archived"])
        self.assertNotIn(f"Verify identity with baton retro show --selector {project['uuid']}", invocation["prompt"])
        self.assertIn("Read the supplied identity.json", invocation["prompt"])

    def test_prompt_preparation_failure_retains_workspace_for_bounded_cleanup(self) -> None:
        self.start_project()
        self.claim()
        project = self.project()
        export = Path(project["artifacts_path"]) / ".blackboard-export"
        export.mkdir()
        (export / "overview.md").write_text("Unrecognized evidence")
        log = self.fake_fork()
        result = self.cli("retro", "--selector", project["uuid"], "--non-interactive", ok=False)
        self.assertIn("unrecognized evidence", result.stderr)
        self.assertFalse(log.exists())
        workspaces = list((self.root / "tmp").glob("baton-retro-*"))
        self.assertEqual(len(workspaces), 1)
        self.assertTrue((workspaces[0] / ".failed").is_file())

    def test_failed_artifact_commit_retries_publication_without_launching_another_fork(self) -> None:
        project = self.archive()
        log = self.fake_fork()
        self.git(self.artifacts, "switch", "-c", "unrelated-branch")
        first = json.loads(self.cli("retro", "pending", "--json", ok=False).stdout)
        self.assertEqual(first["runs"][0]["status"], "publication-failed")
        output = Path(project["blackboard"]["directory"]) / "retro.md"
        before = output.read_bytes()
        retry = json.loads(self.cli("retro", "pending", "--json", ok=False).stdout)
        self.assertEqual(retry["runs"][0]["status"], "publication-failed")
        workspaces = list((self.root / "tmp").glob("baton-retro-*"))
        self.assertEqual(len(workspaces), 2)
        self.assertTrue(all((workspace / ".failed").is_file() for workspace in workspaces))
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.git(self.artifacts, "switch", "main")
        second = json.loads(self.cli("retro", "pending", "--json").stdout)
        self.assertEqual(second["runs"][0]["status"], "completed")
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_deleted_unpublished_artifact_relaunches_instead_of_retrying_missing_commit(self) -> None:
        project = self.archive()
        log = self.fake_fork()
        self.git(self.artifacts, "switch", "-c", "unrelated-branch")
        first = json.loads(self.cli("retro", "pending", "--json", ok=False).stdout)
        self.assertEqual(first["runs"][0]["status"], "publication-failed")
        output = Path(project["blackboard"]["directory"]) / "retro.md"
        output.unlink()
        self.git(self.artifacts, "switch", "main")
        second = json.loads(self.cli("retro", "pending", "--json").stdout)
        self.assertEqual(second["runs"][0]["status"], "completed")
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertTrue(output.is_file())


    def test_malformed_archive_does_not_break_explicit_retro(self) -> None:
        project = self.archive()
        log = self.fake_fork()
        layout = self.projects / ".git/baton/v2"
        for name, text in (("bad", "{"), ("scalar", "[]"), ("missing", "{}")):
            (layout / "archive" / f"{name}.json").write_text(text)
        result = self.cli("retro", "--selector", project["uuid"], "--non-interactive")
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        self.assertIn("ignoring invalid archive", result.stderr)
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_misordered_pending_cannot_launch_interactive_fork(self) -> None:
        project = self.archive()
        log = self.fake_fork()
        result = self.cli("retro", "--selector", project["uuid"], "pending", ok=False)
        self.assertIn("place pending before its options", result.stderr)
        self.assertFalse(log.exists())

    def test_invalid_draft_stays_private_and_next_run_retries(self) -> None:
        project = self.archive()
        log = self.fake_fork(valid=False)
        output = Path(project["blackboard"]["directory"]) / "retro.md"
        first = self.cli("retro", "pending", "--json", ok=False)
        self.assertEqual(json.loads(first.stdout)["runs"][0]["status"], "failed")
        self.assertFalse(output.exists())
        self.env["VALID_FORK"] = "1"
        self.cli("retro", "pending", "--json")
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
