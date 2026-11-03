from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from baton.errors import ProjectError
from baton.manager import Manager
from baton.retro_summary import (scan_sources, run_summary, synthesize, validate_draft, digest,
                                 INPUT_BUDGET, existing_report, provenance_text, preview_summary)
from baton.providers import PROVIDERS, Provider, PROMPT_STDIN, session_variables
import test_project_manager as fixtures


class SummaryTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli

    def commit_retro(self, name: str, text: str = "# Retro\nA useful observation.\n", age: int = 1,
                     author_age: int | None = None) -> Path:
        path = self.artifacts / name / "retro.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        self.git(self.artifacts, "add", str(path))
        env = dict(self.env, GIT_COMMITTER_DATE=(self.now - timedelta(days=age)).isoformat(),
                   GIT_AUTHOR_DATE=(self.now - timedelta(days=author_age if author_age is not None else age)).isoformat())
        result = subprocess.run(["git", "-C", str(self.artifacts), "commit", "-qm", name], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    @property
    def now(self) -> datetime:
        return datetime(2026, 10, 28, 12, tzinfo=timezone.utc)

    def manager(self) -> Manager:
        return Manager()

    def environment(self):
        return patch.dict(os.environ, self.env | {"XDG_STATE_HOME": str(self.root / "state")}, clear=True)

    def test_snapshot_dates_dirty_untracked_and_committer_time(self) -> None:
        path = self.commit_retro("active", author_age=30)
        self.commit_retro("old", age=9)
        self.commit_retro("end", age=0)
        cutoff = self.commit_retro("cutoff", age=7)
        self.commit_retro("empty", "  ")
        path.write_text("dirty mutable contents")
        extra = self.artifacts / "untracked"
        extra.mkdir()
        (extra / "retro.md").write_text("untracked")
        result = scan_sources(self.artifacts, now=self.now)
        self.assertEqual([s["project"] for s in result["sources"]], ["active", "cutoff"])
        self.assertTrue(result["sources"][0]["dirty"])
        self.assertEqual(result["sources"][0]["sha256"], digest(b"# Retro\nA useful observation.\n"))
        self.assertEqual(len(result["excluded"]), 2)
        self.assertEqual(result["week"], "2026-W44")

    def test_pure_rename_keeps_date_but_edited_rename_refreshes(self) -> None:
        path = self.commit_retro("legacy", age=10)
        self.git(self.artifacts, "mv", "legacy", "renamed")
        self.git(self.artifacts, "commit", "-qm", "pure rename")
        self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"], [])
        self.git(self.artifacts, "mv", "renamed", "edited")
        (self.artifacts / "edited/retro.md").write_text("# Retro\nA useful observation.\nExtra edited conclusion.\n")
        self.git(self.artifacts, "add", ".")
        env = dict(self.env, GIT_COMMITTER_DATE=(self.now - timedelta(hours=1)).isoformat())
        subprocess.run(["git", "-C", str(self.artifacts), "commit", "-qm", "edited rename"], env=env, check=True)
        self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"][0]["project"], "edited")

    def test_symlinks_collision_nested_and_iso_year(self) -> None:
        self.commit_retro("real")
        (self.artifacts / "escape").symlink_to(self.artifacts / "real", target_is_directory=True)
        result = scan_sources(self.artifacts, now=self.now)
        self.assertEqual(len(result["sources"]), 1)
        self.assertIn("escape/retro.md", str(result["excluded"]))
        self.assertEqual(scan_sources(self.artifacts, now=datetime(2021, 1, 1, tzinfo=timezone.utc))["week"], "2020-W53")
        (self.artifacts / "retros").mkdir()
        (self.artifacts / "retros/retro.md").write_text("reserved project")
        with self.assertRaisesRegex(ProjectError, "collides"):
            scan_sources(self.artifacts, now=self.now)

    def test_citation_validation(self) -> None:
        self.commit_retro("one")
        manifest = scan_sources(self.artifacts, now=self.now)
        validate_draft("Example [one](../../one/retro.md)", manifest)
        for draft in ("", "No citations", "[bad](../../unknown/retro.md)", "[bad](/tmp/evidence.md)", "[bad](https://example.com)"):
            with self.subTest(draft=draft), self.assertRaises(ProjectError):
                validate_draft(draft, manifest)

    def test_zero_sources_publication_preserves_staged_work_and_reruns(self) -> None:
        (self.artifacts / "unrelated").write_text("staged")
        self.git(self.artifacts, "add", "unrelated")
        with self.environment():
            result = run_summary(self.manager(), "claude", 2, now=self.now)
            first = self.git(self.artifacts, "rev-parse", "HEAD")
            again = run_summary(self.manager(), "claude", 2, now=self.now + timedelta(hours=1))
        self.assertTrue(result["committed"])
        self.assertFalse(result["published"])
        self.assertEqual(first, self.git(self.artifacts, "rev-parse", "HEAD"))
        self.assertEqual(result["window_end"], again["window_end"])
        self.assertEqual(self.git(self.artifacts, "diff", "--cached", "--name-only"), "unrelated")
        self.assertIn("No retros in this window", Path(result["report"]).read_text())
        changed = self.git(self.artifacts, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
        self.assertEqual(changed, ["retros/2026-W44/report.md", "retros/2026-W44/sources.json"])

    def provider(self, code: str, name: str = "claude"):
        script = self.root / "fixture.py"
        script.write_text(code)
        class FixtureProvider(Provider):
            def exec_unattended(self, session_id, workspace, prompt):
                if session_id is not None:
                    raise AssertionError("must be fresh")
                return [sys.executable, str(script)], PROMPT_STDIN
        return patch.dict(PROVIDERS, {name: FixtureProvider(name, sys.executable)})

    def test_provider_sees_complete_committed_evidence_and_scrubbed_identity(self) -> None:
        path = self.commit_retro("one")
        path.write_text("DIRTY CONTENT MUST NOT BE COPIED")
        state = self.root / "private-state"
        state.mkdir()
        code = '''import os, pathlib, sys, json
prompt = sys.stdin.read()
assert 'Only one source exists' in prompt
assert '600–1000' in prompt
assert pathlib.Path('evidence/0.md').read_text() == '# Retro\\nA useful observation.\\n'
assert all(key not in os.environ for key in json.loads(os.environ['SESSION_KEYS']))
print('BATON_RETRO_DRAFT_BEGIN')
print('A supported example from [one](../../one/retro.md).')
print('BATON_RETRO_DRAFT_END')
'''
        with self.environment(), self.provider(code), patch.dict(os.environ, {key: "inherited" for key in session_variables()} | {"SESSION_KEYS": json.dumps(list(session_variables()))}):
            result = synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 3)
        self.assertIn("supported example", result)

    def test_budget_prevents_provider_launch(self) -> None:
        self.commit_retro("huge", "A" * INPUT_BUDGET)
        state = self.root / "state"
        state.mkdir()
        with self.environment(), self.provider("raise AssertionError('launched')"):
            with self.assertRaisesRegex(ProjectError, "byte input budget"):
                synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 2)
        self.assertFalse(list(state.glob("attempt-*.log")))

    def test_timeout_and_ambiguous_output(self) -> None:
        self.commit_retro("one")
        state = self.root / "state"
        state.mkdir()
        with self.environment(), self.provider("import time; time.sleep(30)"):
            with self.assertRaisesRegex(ProjectError, "exceeded"):
                synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 1)
        with self.environment(), self.provider("print('BATON_RETRO_DRAFT_BEGIN\\nBATON_RETRO_DRAFT_BEGIN\\ntext\\nBATON_RETRO_DRAFT_END')"):
            with self.assertRaisesRegex(ProjectError, "ambiguous"):
                synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 2)

    def test_partial_pair_recovers_verified_output_and_rejects_mismatch(self) -> None:
        with self.environment():
            manager = self.manager()
            original = manager.commit_artifact_paths
            def interrupted(*args, **kwargs):
                kwargs["prepare"]()
                (self.artifacts / "retros/2026-W44/sources.json").unlink()
                raise ProjectError("interrupted")
            manager.commit_artifact_paths = interrupted
            with self.assertRaisesRegex(ProjectError, "interrupted"):
                run_summary(manager, "claude", 2, now=self.now)
            manager.commit_artifact_paths = original
            result = run_summary(manager, "claude", 2, now=self.now)
            self.assertTrue(result["committed"])
            (self.artifacts / "retros/2026-W44/report.md").write_text("conflict")
            with self.assertRaisesRegex(ProjectError, "conflicts"):
                run_summary(manager, "claude", 2, now=self.now)

    def test_symlink_destination_never_overwritten(self) -> None:
        (self.artifacts / "retros").symlink_to(self.root, target_is_directory=True)
        with self.environment(), self.assertRaisesRegex(ProjectError, "Symlinked"):
            run_summary(self.manager(), "claude", 2, now=self.now)

    def test_unicode_and_tab_names_survive_history_and_rename(self) -> None:
        self.commit_retro("żółć\tproject", age=2)
        self.git(self.artifacts, "mv", "żółć\tproject", "日本語\tproject")
        self.git(self.artifacts, "commit", "-qm", "rename unicode")
        source = scan_sources(self.artifacts, now=self.now)["sources"][0]
        self.assertEqual(source["path"], "日本語\tproject/retro.md")
        self.assertEqual(source["timestamp"], (self.now - timedelta(days=2)).isoformat())
        self.assertIn("%09", source["link"])

    def test_mode_only_change_does_not_refresh_and_missing_copy_is_committed(self) -> None:
        path = self.commit_retro("mode", age=10)
        path.chmod(0o755)
        self.git(self.artifacts, "add", "mode/retro.md")
        self.git(self.artifacts, "commit", "-qm", "mode only")
        self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"], [])
        path = self.commit_retro("missing")
        path.unlink()
        selected = scan_sources(self.artifacts, now=self.now)["sources"]
        self.assertEqual(selected[0]["project"], "missing")
        self.assertTrue(selected[0]["dirty"])

    def test_reserved_committed_project_collision_even_with_deleted_working_file(self) -> None:
        path = self.commit_retro("retros")
        path.unlink()
        with self.assertRaisesRegex(ProjectError, "committed project evidence"):
            scan_sources(self.artifacts, now=self.now)

    def test_preview_uses_frozen_selection_without_writes(self) -> None:
        from baton.retro_summary import preview_summary
        with self.environment():
            result = run_summary(self.manager(), "claude", 2, now=self.now)
            self.commit_retro("late")
            before = {p: p.stat().st_mtime_ns for p in self.root.rglob("run.json")}
            preview = preview_summary(self.artifacts, now=self.now + timedelta(hours=2))
            after = {p: p.stat().st_mtime_ns for p in self.root.rglob("run.json")}
        self.assertEqual(preview["window_end"], result["window_end"])
        self.assertEqual(preview["sources"], [])
        self.assertTrue(preview["existing_report"])
        self.assertEqual(before, after)

    def test_session_manifest_has_no_absolute_checkout_paths(self) -> None:
        self.commit_retro("one")
        state = self.root / "state"
        state.mkdir()
        code = '''import pathlib, json, sys
sys.stdin.read()
manifest = json.loads(pathlib.Path('sources.json').read_text())
assert 'artifacts_root' not in manifest
assert 'report' not in manifest
assert manifest['sources'][0]['input'] == 'evidence/0.md'
print('BATON_RETRO_DRAFT_BEGIN')
print('Useful practice [one](../../one/retro.md).')
print('BATON_RETRO_DRAFT_END')
'''
        with self.environment(), self.provider(code):
            synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 2)

    def test_unknown_capacity_rejected_before_launch(self) -> None:
        self.commit_retro("one")
        state = self.root / "state"
        state.mkdir()
        with self.environment(), self.provider("raise AssertionError('launched')", "fixture"):
            with self.assertRaisesRegex(ProjectError, "safe input capacity"):
                synthesize(scan_sources(self.artifacts, now=self.now), "fixture", state, 2)
        self.assertFalse(list(state.glob("attempt-*")))

    def setup_remote(self) -> Path:
        remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(remote))
        self.git(self.artifacts, "remote", "add", "origin", str(remote))
        self.git(self.artifacts, "push", "-u", "origin", "main")
        self.git(self.artifacts, "remote", "set-head", "origin", "main")
        return remote

    def test_publication_retry_with_failed_fetch_is_never_reported_published(self) -> None:
        self.setup_remote()
        with self.environment():
            result = run_summary(self.manager(), "claude", 2, now=self.now)
            self.assertTrue(result["published"])
            manager = self.manager()
            manager.fetch_artifacts = lambda *args: False
            retried = run_summary(manager, "claude", 2, now=self.now)
            self.assertFalse(retried["published"])
            self.assertTrue(retried["committed"])

    def test_remote_same_week_report_wins_after_sync(self) -> None:
        from baton.retro_summary import render_report
        self.setup_remote()
        other = self.root / "other-host"
        self.git(self.root, "clone", str(self.root / "remote.git"), str(other))
        manifest = scan_sources(self.artifacts, now=self.now - timedelta(hours=1))
        text = render_report("No retros in this window.\n\nPublished by another host.", manifest)
        manifest["report_sha256"] = digest(text.encode())
        destination = other / "retros/2026-W44"
        destination.mkdir(parents=True)
        (destination / "report.md").write_text(text)
        (destination / "sources.json").write_text(provenance_text(manifest))
        self.git(other, "add", "retros")
        self.git(other, "commit", "-qm", "other host report")
        self.git(other, "push", "origin", "main")
        with self.environment():
            result = run_summary(self.manager(), "claude", 2, now=self.now)
        self.assertTrue(result["published"])
        self.assertEqual(Path(result["report"]), self.artifacts / "retros/2026-W44/report.md")
        self.assertEqual(Path(result["report"]).read_text(), text)
        self.assertEqual(result["window_end"], manifest["window_end"])
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "other host report")
        record = json.loads(next((self.root / "state").rglob("run.json")).read_text())
        self.assertEqual(record["report"], text)
        local = self.artifacts / "retros/2026-W44"
        for missing in ("sources.json", "report.md"):
            (local / missing).unlink()
            with self.environment():
                run_summary(self.manager(), "claude", 2, now=self.now)
            self.assertIsNotNone(existing_report(local))
            self.assertEqual((local / "report.md").read_text(), text)

    def test_provider_descendant_terminated_on_timeout(self) -> None:
        import time
        self.commit_retro("one")
        state = self.root / "state"
        state.mkdir()
        child_pid = self.root / "child.pid"
        code = f'''import os, pathlib, time
pid = os.fork()
if pid == 0:
    pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))
    time.sleep(30)
else:
    time.sleep(30)
'''
        with self.environment(), self.provider(code):
            with self.assertRaisesRegex(ProjectError, "exceeded"):
                synthesize(scan_sources(self.artifacts, now=self.now), "claude", state, 1)
        pid = int(child_pid.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            self.fail("provider descendant survived timeout")

    def test_caller_death_retains_supervisor_lock_until_timeout(self) -> None:
        import time
        self.commit_retro("one")
        marker = self.root / "model-started"
        code = f'''import pathlib,time
pathlib.Path({str(marker)!r}).write_text('started')
time.sleep(30)
'''
        with self.environment(), self.provider(code):
            caller = os.fork()
            if caller == 0:
                try:
                    run_summary(self.manager(), "claude", 2, now=self.now)
                except BaseException:
                    pass
                os._exit(0)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                os.kill(caller, signal.SIGKILL)
                os.waitpid(caller, 0)
                with self.assertRaisesRegex(ProjectError, "already running"):
                    run_summary(self.manager(), "claude", 2, now=self.now)
                deadline = time.monotonic() + 10
                state = self.root / "state"
                while time.monotonic() < deadline:
                    process = json.loads(next(state.rglob("process.json")).read_text())
                    if process["status"] == "finished":
                        break
                    time.sleep(.02)
                self.assertEqual(process["status"], "finished")
            finally:
                try:
                    os.kill(caller, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_committed_stage_interruption_retries_without_second_model_call(self) -> None:
        self.commit_retro("one")
        counter = self.root / "model-count"
        code = f'''import pathlib, sys
sys.stdin.read()
p = pathlib.Path({str(counter)!r})
p.write_text(p.read_text() + 'x' if p.exists() else 'x')
print('BATON_RETRO_DRAFT_BEGIN')
print('A useful practice [one](../../one/retro.md).')
print('BATON_RETRO_DRAFT_END')
'''
        with self.environment(), self.provider(code):
            manager = self.manager()
            original = manager.commit_artifact_paths
            def interrupted(*args, **kwargs):
                callback = kwargs["after_commit"]
                def checkpoint():
                    callback()
                    raise ProjectError("interrupted after local commit")
                kwargs["after_commit"] = checkpoint
                return original(*args, **kwargs)
            manager.commit_artifact_paths = interrupted
            with self.assertRaisesRegex(ProjectError, "interrupted after local commit"):
                run_summary(manager, "claude", 2, now=self.now)
            record = json.loads(next((self.root / "state").rglob("run.json")).read_text())
            self.assertEqual(record["stage"], "committed")
            manager.commit_artifact_paths = original
            result = run_summary(manager, "claude", 2, now=self.now)
            self.assertTrue(result["committed"])
        self.assertEqual(counter.read_text(), "x")

    def test_failed_analysis_freezes_snapshot_and_does_not_pick_later_sources(self) -> None:
        self.commit_retro("one")
        with self.environment(), self.provider("print('invalid output')"):
            with self.assertRaisesRegex(ProjectError, "no draft markers"):
                run_summary(self.manager(), "claude", 2, now=self.now)
        self.commit_retro("later")
        code = '''import json, pathlib, sys
sys.stdin.read()
m = json.loads(pathlib.Path('sources.json').read_text())
assert len(m['sources']) == 1
assert m['sources'][0]['project'] == 'one'
print('BATON_RETRO_DRAFT_BEGIN')
print('Useful practice [one](../../one/retro.md).')
print('BATON_RETRO_DRAFT_END')
'''
        with self.environment(), self.provider(code):
            result = run_summary(self.manager(), "claude", 2, now=self.now + timedelta(hours=1))
        self.assertEqual(result["window_end"], self.now.isoformat())
        self.assertEqual(len(result["sources"]), 1)


    def test_inline_citations_allow_project_names_and_prose_but_reject_other_forms(self) -> None:
        for name in ("my-evidence", "tmp", "private"):
            self.commit_retro(name)
        manifest = scan_sources(self.artifacts, now=self.now)
        for source in manifest["sources"]:
            validate_draft(f"[Example]({source['link']})\nUse Option<T>; compare a < b > c. Code used /tmp/example and `https://docs.example`.", manifest)
        known = "[Known](../../my-evidence/retro.md)"
        for extra in ("[bad]( ../../other/retro.md)", "[bad](\n../../other/retro.md )",
                      "[pixel]: //attacker.example/pixel.png\n![pixel]", "[alias]: ../../other/retro.md\n[alias]",
                      "[bad][ref]", "<img src='//attacker.example/pixel.png'>", "[bad](https://example.test)"):
            with self.subTest(extra=extra), self.assertRaises(ProjectError):
                validate_draft(known + "\n" + extra, manifest)

    def test_merge_resolution_dates_new_content(self) -> None:
        path = self.commit_retro("merged", "Base\n", age=12)
        base = self.git(self.artifacts, "rev-parse", "HEAD")
        self.commit_retro("merged", "Main parent\n", age=10)
        self.git(self.artifacts, "switch", "-c", "feature", base)
        self.commit_retro("merged", "Other parent\n", age=9)
        self.git(self.artifacts, "switch", "main")
        self.git(self.artifacts, "merge", "--no-ff", "--no-commit", "feature", ok=False)
        path.write_text("Resolved content unlike either parent\n")
        self.git(self.artifacts, "add", ".")
        changed = self.now - timedelta(hours=1)
        env = self.env | {"GIT_COMMITTER_DATE": changed.isoformat()}
        subprocess.run(["git", "-C", str(self.artifacts), "commit", "-qm", "resolution"], env=env, check=True)
        source = scan_sources(self.artifacts, now=self.now)["sources"][0]
        self.assertEqual(source["timestamp"], changed.isoformat())
        self.assertEqual(source["sha256"], digest(path.read_bytes()))

    def test_merge_adopting_unchanged_parent_blob_does_not_refresh_date(self) -> None:
        self.commit_retro("merged", "Base\n", age=12)
        base = self.git(self.artifacts, "rev-parse", "HEAD")
        (self.artifacts / "unrelated.txt").write_text("unrelated main work")
        self.git(self.artifacts, "add", ".")
        self.git(self.artifacts, "commit", "-qm", "main work")
        self.git(self.artifacts, "switch", "-c", "feature", base)
        self.commit_retro("merged", "Old feature content\n", age=10)
        self.git(self.artifacts, "switch", "main")
        env = self.env | {"GIT_COMMITTER_DATE": (self.now - timedelta(hours=1)).isoformat()}
        subprocess.run(["git", "-C", str(self.artifacts), "merge", "--no-ff", "-m", "adopt parent", "feature"],
                       env=env, check=True, capture_output=True)
        self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"], [])

    def test_oversized_and_expired_sources_are_never_loaded(self) -> None:
        self.commit_retro("old-large", "A" * (INPUT_BUDGET + 1), age=10)
        shim = self.root / "git-shim"
        shim.mkdir()
        binary = shutil.which("git")
        script = shim / "git"
        script.write_text("#!" + sys.executable + "\n" + f"import os,sys\nif 'cat-file' in sys.argv and 'blob' in sys.argv: raise RuntimeError('blob read before size/date gate')\nos.execv({binary!r}, [{binary!r}, *sys.argv[1:]])\n")
        script.chmod(0o755)
        with self.environment(), patch.dict(os.environ, {"PATH": str(shim) + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"], [])
        self.commit_retro("new-large", "B" * (INPUT_BUDGET + 1))
        with self.environment(), patch.dict(os.environ, {"PATH": str(shim) + os.pathsep + os.environ["PATH"]}):
            with self.assertRaisesRegex(ProjectError, "byte input budget"):
                scan_sources(self.artifacts, now=self.now)

    def test_persisted_provenance_omits_host_paths_and_preview_skips_history(self) -> None:
        with self.environment():
            result = run_summary(self.manager(), "claude", 2, now=self.now)
            destination = Path(result["report"]).parent
            provenance = (destination / "sources.json").read_text()
            self.assertNotIn(str(self.artifacts), provenance)
            self.assertNotIn('"artifacts_root"', provenance)
            self.assertNotIn('"report"', provenance)
            with patch("baton.retro_summary.scan_sources", side_effect=AssertionError("unnecessary discovery")):
                self.assertTrue(preview_summary(self.artifacts, now=self.now)["existing_report"])

    def test_unexpected_supervisor_error_preserves_diagnostics(self) -> None:
        with self.environment():
            manager = self.manager()
            def fail(*args, **kwargs):
                raise TypeError("specific unexpected failure")
            manager.commit_artifact_paths = fail
            with self.assertRaisesRegex(ProjectError, "TypeError: specific unexpected failure"):
                run_summary(manager, "claude", 2, now=self.now)
            diagnostic = next((self.root / "state").rglob("supervisor.log")).read_text()
            self.assertIn("Traceback", diagnostic)
            self.assertIn("specific unexpected failure", diagnostic)

    def test_supervisor_progress_survives_broken_caller_diagnostics(self) -> None:
        class ClosedCaller:
            def flush(self) -> None:
                pass
            def write(self, text: str) -> None:
                raise BrokenPipeError("caller went away")
        with self.environment(), patch.object(sys, "stderr", ClosedCaller()):
            result = run_summary(self.manager(), "claude", 2, now=self.now)
        self.assertTrue(result["committed"])


    def test_discarded_branch_version_cannot_refresh_surviving_main_blob(self) -> None:
        self.commit_retro("retained", "B\n", age=27)
        base = self.git(self.artifacts, "rev-parse", "HEAD")
        self.commit_retro("retained", "A\n", age=26)
        self.git(self.artifacts, "switch", "-c", "discarded", base)
        self.commit_retro("retained", "A\n", age=3)
        self.commit_retro("retained", "C\n", age=2)
        self.git(self.artifacts, "switch", "main")
        env = self.env | {"GIT_COMMITTER_DATE": (self.now - timedelta(days=1)).isoformat()}
        subprocess.run(["git", "-C", str(self.artifacts), "merge", "--no-ff", "-s", "ours", "-m", "discard side", "discarded"],
                       env=env, check=True, capture_output=True)
        self.assertEqual(scan_sources(self.artifacts, now=self.now)["sources"], [])

    def test_interrupt_exceptions_never_escape_the_forked_supervisor(self) -> None:
        for exception in (SystemExit(37), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__), self.environment():
                marker = self.root / "escaped-child"
                caller = os.getpid()
                try:
                    with patch("baton.retro_summary._run_summary", side_effect=exception):
                        with self.assertRaisesRegex(ProjectError, type(exception).__name__):
                            run_summary(self.manager(), "claude", 2, now=self.now)
                except BaseException:
                    if os.getpid() != caller:
                        marker.write_text("child escaped")
                        os._exit(0)
                    raise
                self.assertFalse(marker.exists())


    def test_container_references_and_external_autolinks_cannot_hide_citations(self) -> None:
        self.commit_retro("www.example")
        manifest = scan_sources(self.artifacts, now=self.now)
        known = "[www.example](../../www.example/retro.md)"
        for extra in ("> [other]: https://example.com\n[other]", "- [other]: https://example.com\n[other]",
                      "1. [other]: https://example.com\n[other]", "https://example.com", "www.example.com", "person@example.com",
                      "\\`\n> [other]: https://example.com\n\\`\n[other]",
                      "> ```\n[other]: https://example.com\n```\n[other]"):
            with self.subTest(extra=extra), self.assertRaises(ProjectError):
                validate_draft(known + "\n" + extra, manifest)
        for example in ("Use `https://example.com` or `person@example.com` as literal examples.",
                        "```text\n[other]: https://example.com\n```\n", "~~~text\nhttps://example.com\n~~~\n"):
            with self.subTest(example=example):
                validate_draft(known + "\n" + example, manifest)


    def test_html_blocks_cannot_disguise_active_content_as_fenced_code(self) -> None:
        self.commit_retro("one")
        manifest = scan_sources(self.artifacts, now=self.now)
        known = "[one](../../one/retro.md)"
        for container in ("<div>", "<div\n>", "</div>", "<widget>", "<widget\n>", ">> <div>", "- <div>", "<!--"):
            draft = known + "\n\n" + container + '\n```\n<img src="https://example.test/pixel">\n```\n</div>\n'
            with self.subTest(container=container), self.assertRaises(ProjectError):
                validate_draft(draft, manifest)
        validate_draft(known + '\n\n```html\n<div>\n<img src="https://example.test/pixel">\n</div>\n```\n', manifest)

    def test_root_commit_evidence_ignores_log_showroot_configuration(self) -> None:
        repository = self.root / "root-evidence"
        self.git(self.root, "init", "-b", "main", str(repository))
        project = repository / "project"
        project.mkdir()
        (project / "retro.md").write_text("Root retrospective evidence\n")
        self.git(repository, "add", ".")
        env = self.env | {"GIT_COMMITTER_DATE": (self.now - timedelta(days=1)).isoformat()}
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "root retro"], env=env, check=True)
        self.git(repository, "config", "log.showRoot", "false")
        sources = scan_sources(repository, now=self.now)["sources"]
        self.assertEqual([source["path"] for source in sources], ["project/retro.md"])
