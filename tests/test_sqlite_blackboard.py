import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from blackboard import BlackboardError
from baton.blackboard_sqlite import SQLiteBlackboard
import test_project_manager as fixtures
from test_project_manager import artifact_path, read_state


class SQLiteStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        fixtures.FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures.FIXTURES)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = SQLiteBlackboard(self.root, "example", "generation", "uuid")
        self.board.initialize()

    def test_sealed_context_is_frozen_in_export_journal_and_manifest(self) -> None:
        context = {"uuid": "uuid", "generation": "generation", "owner": {"provider": "codex", "session_id": "owner"},
                   "master_path": "saved/plan.md", "entries": [{"repo": "alpha", "member": "worker"}]}
        self.board.freeze()
        destination = self.root / "context-export"
        first = self.board.export(destination, replace_sealed=True, context=context)
        (destination / "manifest.json").unlink()
        (destination / "overview.md").write_text("corrupted")
        retry = self.board.export(destination, replace_sealed=True, context={**context, "owner": None})
        self.assertEqual(retry["context"], context)
        self.assertEqual(retry["logical_sha256"], first["logical_sha256"])
        overview = (destination / "overview.md").read_text()
        for value in ("Owner provider: codex", "session: owner", "Repository: alpha", "saved/plan.md", "not recorded"):
            self.assertIn(value, overview)
        with self.assertRaisesRegex(BlackboardError, "context UUID/generation"):
            self.board.export(self.root / "foreign-context", context={**context, "uuid": "other"})

    def test_orphan_sidecars_refuse_initialization_before_journal_change(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            directory = self.root / suffix[1:]
            directory.mkdir()
            sidecar = directory / ("blackboard.sqlite" + suffix)
            sidecar.write_bytes(b"orphan evidence")
            board = SQLiteBlackboard(directory, "example", "generation", "uuid")
            with self.assertRaisesRegex(BlackboardError, "sidecars"):
                board.initialize()
            self.assertFalse(board.path.exists())
            self.assertEqual(sidecar.read_bytes(), b"orphan evidence")
            with board._locked(read_only=True) as handle:
                self.assertIsNone(board._marker(handle))

    def test_sealed_retry_cannot_overwrite_different_logical_snapshot(self) -> None:
        self.board.freeze()
        destination = self.root / "sealed-retry"
        self.board.export(destination)
        original = (destination / "manifest.json").read_bytes()
        other = SQLiteBlackboard(self.root / "other", "example", "generation", "uuid")
        other.initialize(overview="different snapshot")
        other.freeze()
        with self.assertRaisesRegex(BlackboardError, "immutable"):
            other.export(destination, replace_sealed=True)
        self.assertEqual((destination / "manifest.json").read_bytes(), original)

    def test_missing_leaf_entry_is_detected_before_snapshot_publication(self) -> None:
        posted = self.board.post("worker", "must remain in current state")
        with contextlib.closing(self.board._connect()) as connection:
            connection.execute("DELETE FROM entries WHERE id=?", (posted["id"],))
            connection.commit()
        with self.assertRaisesRegex(BlackboardError, "entry IDs"):
            self.board.export(self.root / "invalid-export")
        self.assertFalse((self.root / "invalid-export" / "manifest.json").exists())

    def test_concurrent_exports_publish_one_consistent_snapshot_and_relative_output(self) -> None:
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        class Paused(SQLiteBlackboard):
            def render_snapshot(self, data: dict) -> tuple[str, str]:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("export test timed out")
                return super().render_snapshot(data)
        first = Paused(self.root, "example", "generation", "uuid")
        self.board.post("worker", "first snapshot")
        destination = self.root / "serialized"
        with ThreadPoolExecutor(max_workers=2) as pool:
            initial = pool.submit(first.export, destination)
            self.assertTrue(entered.wait(2))
            self.board.post("worker", "second snapshot")
            def second() -> dict:
                result = self.board.export(destination)
                second_done.set()
                return result
            later = pool.submit(second)
            self.assertFalse(second_done.wait(0.1))
            release.set()
            initial.result(timeout=5)
            final = later.result(timeout=5)
        with contextlib.closing(sqlite3.connect(destination / "board.sqlite")) as connection:
            count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        self.assertEqual(final["counts"]["entries"], count)
        self.assertEqual(count, 2)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.board.export(Path("relative"))
        finally:
            os.chdir(previous)
        self.assertTrue((self.root / "relative" / "manifest.json").exists())

    def test_tail_counts_current_posts_after_overview_and_entry_updates(self) -> None:
        first = self.board.post("worker", "one")
        self.board.post("worker", "two")
        self.board.update("worker", first["id"], 1, "one edited")
        self.board.edit("worker", self.board.read()["revision"], "overview edit")
        with self.assertRaisesRegex(BlackboardError, "--limit"):
            self.board.read(tail=1, limit=1)
        tail = self.board.read(tail=1)
        self.assertEqual(len(tail["entries"]), 1)
        self.assertEqual(tail["entries"][0]["body"], "two")
        with self.assertRaisesRegex(BlackboardError, "parent"):
            self.board.post("worker", "invalid reply", parent_id="")

    def test_initializing_journal_recovers_complete_and_schema_only_database(self) -> None:
        with self.board._locked() as handle:
            self.board._record(handle, {"project": "example", "generation": "generation", "status": "initializing"})
        self.board.initialize()
        self.board.post("worker", "after committed DB recovery")
        incomplete = SQLiteBlackboard(self.root / "partial", "partial", "partial-generation", "partial-uuid")
        with incomplete._locked(create=True) as handle:
            incomplete._record(handle, {"project": "partial", "generation": "partial-generation", "status": "initializing"})
            with contextlib.closing(incomplete._connect(create=True)) as connection:
                incomplete.ensure_schema(connection)
        incomplete.initialize()
        incomplete.post("worker", "after schema-only recovery")
        self.assertEqual(len(incomplete.read()["entries"]), 1)

    def test_nonobject_cursor_manifest_and_unrelated_export_symlink(self) -> None:
        with self.assertRaises(BlackboardError):
            self.board.read(since="")
        for value in ([], "value", None, 1):
            token = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
            with self.subTest(value=value), self.assertRaises(BlackboardError):
                self.board.read(since=token)
        destination = self.root / "malformed"
        destination.mkdir()
        (destination / "manifest.json").write_text("[]")
        with self.assertRaisesRegex(BlackboardError, "object"):
            self.board.export(destination)
        safe = self.root / "safe"
        safe.mkdir()
        unrelated = self.root / "private"
        unrelated.write_text("unrelated")
        unrelated.chmod(0)
        (safe / "unrelated").symlink_to(unrelated)
        self.board.export(safe)
        self.assertTrue((safe / "unrelated").is_symlink())

    def test_reader_excludes_rename_and_pending_wal_survives(self) -> None:
        with contextlib.closing(self.board._connect()) as held:
            held.execute("PRAGMA wal_autocheckpoint=0")
            held.execute("SELECT * FROM metadata").fetchall()
            self.board.post("a", "WAL evidence")
            self.assertGreater(Path(str(self.board.path) + "-wal").stat().st_size, 0)
            self.board.export(self.root / "wal-export")
            self.assertIn("WAL evidence", (self.root / "wal-export" / "history.md").read_text())
        started, completed = threading.Event(), threading.Event()
        def rename() -> None:
            started.set()
            self.board.rename("renamed")
            completed.set()
        with self.board._locked(read_only=True):
            with contextlib.closing(self.board._connect(readonly=True)) as reader:
                reader.execute("BEGIN")
                self.board._metadata(reader)
                thread = threading.Thread(target=rename)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(completed.wait(0.1))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(completed.is_set())
        self.assertIn("WAL evidence", self.board.read()["text"])
        self.assertEqual(self.board.read()["project"], "renamed")
        directory = self.board.directory
        inode = (directory / "blackboard-lock.md").stat().st_ino
        sequence = self.board.read()["sequence"]
        self.board.project = "example"
        self.board.rename("renamed")
        self.assertEqual(self.board.read()["project"], "renamed")
        self.board.project = "example"
        with self.board._locked() as handle:
            self.board._record(handle, {"project": "example", "generation": "generation", "status": "renaming", "new_project": "renamed"})
        with self.assertRaisesRegex(BlackboardError, "different blackboard rename"):
            self.board.rename("another")
        self.board.rename("renamed")
        self.assertEqual(self.board.read()["sequence"], sequence)
        self.assertEqual(self.board.directory, directory)
        self.assertEqual((directory / "blackboard-lock.md").stat().st_ino, inode)

    def test_rename_rejects_invalid_source_without_changing_journal(self) -> None:
        for name, sql, error in (
            ("schema", "PRAGMA user_version=999", "Unsupported blackboard schema"),
            ("sealed", "UPDATE metadata SET value='sealed' WHERE key='status'", "Sealed board"),
        ):
            with self.subTest(name=name):
                board = SQLiteBlackboard(self.root / name, "example", "generation", "uuid")
                board.initialize()
                lock = board.directory / "blackboard-lock.md"
                before = lock.read_bytes()
                with contextlib.closing(board._connect()) as connection:
                    connection.execute(sql)
                    connection.commit()
                with self.assertRaisesRegex(BlackboardError, error):
                    board.rename("renamed")
                self.assertEqual(lock.read_bytes(), before)

    def test_concurrent_posts_update_conflict_and_freeze(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            posts = list(pool.map(lambda number: self.board.post("worker", str(number)), range(12)))
        self.assertEqual(len({post["id"] for post in posts}), 12)
        def update(number: int) -> bool:
            try:
                self.board.update("worker", posts[0]["id"], 1, str(number))
                return True
            except BlackboardError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(update, range(2))), 1)
        def race(number: int) -> bool:
            try:
                if number == 0:
                    self.board.freeze()
                else:
                    self.board.post("worker", "racing")
                return True
            except BlackboardError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(race, range(4)))
        frozen = self.board.read()
        self.assertEqual(frozen["status"], "sealed")
        with self.assertRaises(BlackboardError):
            self.board.post("worker", "late")

    def test_export_destination_identity_sealed_and_sidecar_protection(self) -> None:
        destination = self.root / "export"
        self.board.export(destination)
        manifest = json.loads((destination / "manifest.json").read_text())
        manifest["uuid"] = "foreign"
        (destination / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BlackboardError, "another UUID"):
            self.board.export(destination)
        sidecars = self.root / "sidecars"
        sidecars.mkdir()
        (sidecars / "board.sqlite-wal").symlink_to(self.board.path)
        with self.assertRaises(BlackboardError):
            self.board.export(sidecars)
        self.board.freeze()
        sealed = self.root / "sealed"
        self.board.export(sealed)
        before = (sealed / "manifest.json").read_bytes()
        self.board.export(sealed)
        self.assertEqual((sealed / "manifest.json").read_bytes(), before)

    def test_unsupported_export_format_is_not_rewritten(self) -> None:
        destination = self.root / "unsupported-export"
        self.board.export(destination)
        path = destination / "manifest.json"
        original = json.loads(path.read_text())
        for field in ("schema_version", "export_version"):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: 1}))
                before = path.read_bytes()
                with self.assertRaisesRegex(BlackboardError, "Unsupported"):
                    self.board.export(destination)
                self.assertEqual(path.read_bytes(), before)

    def test_updates_threads_audit_and_rollback(self) -> None:
        first = self.board.post("parent", "# arbitrary\n<!-- frame -->", kind="decision")
        reply = self.board.post("child", "reply", parent_id=first["id"])
        self.board.update("parent", first["id"], 1, "changed", kind="finding")
        with self.assertRaisesRegex(BlackboardError, "stale"):
            self.board.update("parent", first["id"], 1, "lost")
        with self.assertRaises(BlackboardError):
            self.board.post("child", "lost", parent_id="missing")
        with self.assertRaises(RuntimeError):
            with self.board.transaction("test") as connection:
                self.board.append_event(connection, "discard", "x", None, {}, "test")
                raise RuntimeError()
        snapshot = self.board.read()
        self.assertEqual(len(snapshot["entries"]), 2)
        self.assertEqual(snapshot["entries"][1]["parent_id"], first["id"])
        self.assertEqual(snapshot["entries"][0]["version"], 2)
        self.assertEqual(len(snapshot["events"]), 4)
        self.assertEqual(reply["version"], 1)

    def test_settlement_kind_is_reserved_and_rendered_only_when_valid(self) -> None:
        from baton.blackboard_sqlite import parse_settlement, settlement_body
        from baton.tasks import Tasks
        task = Tasks(self.board).create("parent", {"title": "Settle"})
        with self.assertRaisesRegex(BlackboardError, "reserved"):
            self.board.post("worker", "done", kind="settlement")
        body = settlement_body(task["id"], "worker", "succeeded", "All green", ["a.py"], "report.md", 3)
        malformed = ("legacy free text", body.replace('"v": 1', '"v": 2'), body.replace('"files": ["a.py"]', '"files": "a.py"'),
                     " " + body)
        with self.board.transaction("worker") as connection:
            entries = [self.board.new_entry(author, text, "all", "settlement", None, task["id"])
                       for author, text in (("worker", body), ("other", body), *(("worker", text) for text in malformed))]
            for entry in entries:
                self.board.insert_entry(connection, entry)
        valid, *legacy = self.board.read()["entries"]
        self.assertEqual(parse_settlement(valid)["outcome"], "succeeded")
        self.assertEqual([parse_settlement(entry) for entry in legacy], [None] * len(legacy))
        text = self.board.read()["text"]
        self.assertEqual(text.count("Settlement: succeeded"), 1)
        self.assertIn(f"Settlement: succeeded (task {task['id']}, version 3)\n\nAll green\n\nFiles: a.py\n\nReport: report.md", text)
        for raw in malformed:
            self.assertIn(raw, text)
        for entry, kind, replacement in ((valid, None, "edited"), (valid, "note", body), (legacy[1], None, body)):
            with self.subTest(kind=kind), self.assertRaisesRegex(BlackboardError, "settlement"):
                self.board.update("worker", entry["id"], 1, replacement, kind=kind)
        note = self.board.post("worker", "plain", task_id=task["id"])
        with self.assertRaisesRegex(BlackboardError, "settlement"):
            self.board.update("worker", note["id"], 1, body, kind="settlement")
        self.assertEqual(self.board.update("worker", legacy[1]["id"], 1, "rewritten", kind="note")["version"], 2)
        self.assertEqual(self.board.update("other", legacy[0]["id"], 1, "still legacy")["version"], 2)
        self.board.export(self.root / "settled")
        for name, raw in (("overview.md", "still legacy"), ("history.md", "legacy free text")):
            exported = (self.root / "settled" / name).read_text()
            self.assertEqual(exported.count("Settlement: succeeded"), 1)
            self.assertIn(raw, exported)

    def test_wait_returns_write_from_second_process(self) -> None:
        cursor = self.board.read()["cursor"]
        script = """import sys, time
from pathlib import Path
from baton.blackboard_sqlite import SQLiteBlackboard
time.sleep(0.3)
SQLiteBlackboard(Path(sys.argv[1]), 'example', 'generation', 'uuid').post('worker', 'from another process', 'parent', kind='question')
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root)])
        self.addCleanup(process.wait)
        result = self.board.wait(since=cursor, to="parent", kinds=["question"], timeout=10, poll_interval=0.05)
        self.assertFalse(result["timed_out"])
        self.assertEqual([entry["body"] for entry in result["entries"]], ["from another process"])
        self.assertEqual(result["sequence"], result["entries"][0]["sequence"])
        self.assertEqual(process.wait(timeout=10), 0)

    def test_wait_cursor_advances_on_timeout_and_never_reports_updates(self) -> None:
        cursor = self.board.read(to="parent")["cursor"]
        before = self.board.post("worker", "posted before the first wait", "parent", kind="question")
        skipped = self.board.post("worker", "unrelated", "other")
        found = self.board.wait(since=cursor, kinds=["question", "settlement"], timeout=0)
        self.assertEqual([entry["id"] for entry in found["entries"]], [before["id"]])
        self.assertEqual(self.board.decode_cursor(found["cursor"])["to"], None)
        timed_out = self.board.wait(since=found["cursor"], to="parent", timeout=0)
        self.assertTrue(timed_out["timed_out"])
        self.board.post("worker", "also unrelated", "other")
        later = self.board.wait(since=timed_out["cursor"], to="parent", timeout=0.2, poll_interval=0.05)
        self.assertEqual((later["timed_out"], later["sequence"]), (True, self.board.read()["sequence"]))
        self.board.update("worker", before["id"], 1, "edited question")
        self.board.update("worker", skipped["id"], 1, "edited unrelated")
        self.assertEqual(self.board.wait(since=later["cursor"], timeout=0)["entries"], [])
        broadcast = self.board.post("parent", "broadcast", kind="decision")
        result = self.board.wait(since=later["cursor"], to="worker", kinds=["decision"], timeout=0)
        self.assertEqual([entry["id"] for entry in result["entries"]], [broadcast["id"]])
        for options in ({}, {"since": cursor, "reply_to": before["id"]}, {"reply_to": before["id"], "to": "x"},
                        {"since": cursor, "timeout": -1}, {"since": cursor, "poll_interval": 0}, {"since": "bogus"},
                        {"since": self.board.encode_cursor(10**6, None)}, {"reply_to": "missing"}):
            with self.subTest(options=options), self.assertRaises(BlackboardError):
                self.board.wait(**{"timeout": 0, **options})

    def test_ask_round_trip_and_resume_after_timeout(self) -> None:
        cursor = self.board.read()["cursor"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            asked = pool.submit(self.board.ask, "worker", "parent", "Which schema?", timeout=10, poll_interval=0.05)
            question = self.board.wait(since=cursor, kinds=["question"], timeout=10, poll_interval=0.05)["entries"][0]
            self.assertEqual((question["author"], question["recipient"], question["body"]), ("worker", "parent", "Which schema?"))
            self.board.post("parent", "Schema 2", "worker", kind="answer", parent_id=question["id"])
            answered = asked.result(timeout=10)
        self.assertEqual((answered["timed_out"], answered["question"]["id"]), (False, question["id"]))
        self.assertEqual([entry["body"] for entry in answered["entries"]], ["Schema 2"])
        pending = self.board.ask("worker", "parent", "Still there?", timeout=0)
        self.assertTrue(pending["timed_out"])
        self.board.post("parent", "Yes", "worker", kind="answer", parent_id=pending["question"]["id"])
        resumed = self.board.wait(reply_to=pending["question"]["id"], timeout=0)
        self.assertEqual([entry["body"] for entry in resumed["entries"]], ["Yes"])
        sequence = self.board.read()["sequence"]
        with self.assertRaises(BlackboardError):
            self.board.ask("worker", "parent", "invalid", timeout=-1)
        self.assertEqual(self.board.read()["sequence"], sequence)

    def test_new_boards_index_replies_and_events_while_unindexed_boards_still_work(self) -> None:
        with contextlib.closing(sqlite3.connect(self.board.path)) as connection:
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
            self.assertLessEqual({"entry_parent", "event_entity"}, indexes)
            plan = " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN SELECT id FROM entries WHERE parent_id=?", ("x",)))
            self.assertIn("entry_parent", plan)
            # Boards initialized before these indexes existed lack them but remain schema 2.
            connection.executescript("DROP INDEX entry_parent; DROP INDEX event_entity;")
        question = self.board.post("worker", "Ready?", "parent", kind="question")
        self.board.post("parent", "Yes", "worker", kind="answer", parent_id=question["id"])
        self.assertEqual([entry["body"] for entry in self.board.wait(reply_to=question["id"], timeout=0)["entries"]], ["Yes"])
        self.assertEqual({agent["agent"]: agent["unanswered_questions"] for agent in self.board.agents()["agents"]},
                         {"parent": [], "worker": []})

    def test_wait_refuses_frozen_board_and_stale_generation(self) -> None:
        cursor = self.board.read()["cursor"]
        question = self.board.post("worker", "question", kind="question")
        stale = SQLiteBlackboard(self.root, "example", "other-generation", "uuid")
        with self.assertRaisesRegex(BlackboardError, "cursor"):
            stale.wait(since=cursor, timeout=0)
        with self.assertRaisesRegex(BlackboardError, "generation"):
            stale.wait(reply_to=question["id"], timeout=0)
        self.board.freeze()
        with self.assertRaisesRegex(BlackboardError, "frozen"):
            self.board.wait(since=cursor, timeout=0)

    def test_agents_verdicts_are_advisory_and_ignore_malformed_settlements(self) -> None:
        from baton.tasks import Tasks
        tasks = Tasks(self.board)
        def claimed(actor: str) -> dict:
            task = tasks.create("parent", {"title": actor})
            return tasks.mutate(actor, task["id"], 1, "claim")
        active = claimed("active")
        self.board.ask("asker", "parent", "Blocked?", timeout=0)
        answered = self.board.ask("answered", "parent", "Answered?", timeout=0)["question"]
        self.board.post("parent", "Yes", "answered", kind="answer", parent_id=answered["id"])
        settled = claimed("settler")
        tasks.mutate("settler", settled["id"], 2, "settle", {"outcome": "succeeded", "summary": "Done"})
        rerouted = claimed("rerouted")
        rerouted = tasks.mutate("rerouted", rerouted["id"], 2, "settle", {"outcome": "failed", "summary": "Failed"})
        tasks.mutate("parent", rerouted["id"], rerouted["version"], "assign", {"assignee": "rerouted"})
        with self.board.transaction("legacy") as connection:
            self.board.insert_entry(connection, self.board.new_entry("legacy", "free-text settlement", "all", "settlement", None, active["id"]))
        report = {agent["agent"]: agent for agent in self.board.agents()["agents"]}
        self.assertNotIn("framework", report)
        self.assertEqual({name: agent["verdict"] for name, agent in report.items()},
                         {"active": "active", "asker": "waiting", "answered": "active", "parent": "active",
                          "settler": "settled", "rerouted": "active", "legacy": "active"})
        self.assertEqual([task["id"] for task in report["active"]["in_progress"]], [active["id"]])
        self.assertEqual(report["settler"]["settlements"][0]["task_id"], settled["id"])
        self.assertEqual((report["rerouted"]["settlements"], report["legacy"]["settlements"]), ([], []))
        self.assertEqual(report["asker"]["unanswered_questions"][0]["to"], "parent")
        self.assertEqual(report["legacy"]["last_write"]["kind"], "settlement")
        quiet = {agent["agent"]: agent["verdict"] for agent in self.board.agents(quiet_after=0)["agents"]}
        self.assertEqual((quiet["active"], quiet["asker"], quiet["settler"]), ("quiet", "waiting", "settled"))
        self.assertTrue({"waiting", "settled", "active", "quiet"} >= set(quiet.values()))
        with self.assertRaises(BlackboardError):
            self.board.agents(quiet_after=-1)

    def test_overview_edit_keeps_entries_and_versions(self) -> None:
        self.board.post("a", "keep me")
        before = self.board.read()
        self.board.edit("a", before["revision"], "New overview")
        self.assertIn("keep me", self.board.read()["text"])
        self.assertEqual(self.board.read()["overview"]["version"], 2)
        with self.assertRaises(BlackboardError):
            self.board.edit("a", before["revision"], "stale")

    def test_cursor_edits_filters_and_forward_page(self) -> None:
        start = self.board.read(to="worker")["cursor"]
        first = self.board.post("a", "private", "worker")
        self.board.post("a", "skip", "other")
        self.board.post("a", "broadcast")
        page = self.board.read(to="worker", since=start, limit=1)
        self.assertEqual(len(page["events"]), 1)
        rest = self.board.read(to="worker", since=page["cursor"], limit=1)
        self.assertIn("broadcast", rest["text"])
        self.board.update("a", first["id"], 1, "edited")
        edited = self.board.read(to="worker", since=rest["cursor"])
        self.assertIn("edited", edited["text"])
        self.assertEqual(len(edited["events"]), 1)
        with self.assertRaises(BlackboardError):
            self.board.read(to="other", since=start)
        with self.assertRaises(BlackboardError):
            self.board.read(since=start, tail=1)

    def test_snapshot_standalone_complete_and_frozen(self) -> None:
        message = self.board.post("a", "before")
        self.board.update("a", message["id"], 1, "after")
        self.board.freeze()
        with self.assertRaises(BlackboardError):
            self.board.post("a", "late")
        first, second = self.root / "one", self.root / "two"
        manifest = self.board.export(first)
        self.board.export(second)
        for name in ("overview.md", "history.md"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        self.assertIn("before", (first / "history.md").read_text())
        self.assertIn("after", (first / "overview.md").read_text())
        self.assertEqual(manifest["status"], "sealed")
        self.assertFalse((first / "board.sqlite-wal").exists())
        with contextlib.closing(sqlite3.connect((first / "board.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_unsupported_schema_refuses_read_export_freeze_and_mutation(self) -> None:
        for version in (1, 999):
            with self.board._connect() as connection:
                connection.execute(f"PRAGMA user_version={version}")
            for action in (self.board.read, lambda: self.board.post("a", "x"), self.board.freeze, lambda: self.board.export(self.root / "export")):
                with self.subTest(version=version, action=action), self.assertRaisesRegex(BlackboardError, "Unsupported"):
                    action()

    def test_foreign_missing_symlink_and_hardlink_refusal(self) -> None:
        foreign = SQLiteBlackboard(self.root, "example", "generation", "foreign")
        with self.assertRaisesRegex(BlackboardError, "UUID"):
            foreign.read()
        self.board.path.rename(self.root / "saved")
        with self.assertRaises(BlackboardError):
            self.board.read()
        self.assertFalse(self.board.path.exists())
        self.board.path.symlink_to(self.root / "saved")
        with self.assertRaises(BlackboardError):
            self.board.read()
        self.board.path.unlink()
        os.link(self.root / "saved", self.board.path)
        with self.assertRaises(BlackboardError):
            self.board.read()


class SQLiteCLITest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    update_state = fixtures.ProjectManagerTest.update_state
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot

    def test_default_sqlite_cleanup_commits_complete_archive(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        self.assertEqual(project["blackboard"]["backend"], "sqlite")
        self.cli("blackboard", "post", "--selector", "example", "--expected-generation", project["blackboard"]["generation"], "--agent", "worker", "--text", "preserve")
        self.cli("cleanup", "example")
        archived = read_state(self.projects)["archive"][-1]
        archive = archived["coordination_archive"]
        directory = Path(archive["directory"])
        self.assertIn("preserve", (directory / "history.md").read_text())
        self.assertTrue(archive["commit"])
        self.assertIn("coordination/board.sqlite", self.git(self.artifacts, "ls-tree", "-r", "--name-only", archive["commit"]))

    def archive_retry(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        generation = project["blackboard"]["generation"]
        directory = artifact_path(self.projects, self.artifacts)
        self.cli("blackboard", "post", "--selector", "example", "--expected-generation", generation, "--agent", "worker", "--text", "durable source")
        lock = self.artifacts / ".git" / "index.lock"
        lock.write_text("test commit contention")
        failed = self.cli("cleanup", "example", ok=False)
        self.assertIn("index.lock", failed.stderr)
        self.assertTrue(self.checkout("alpha").exists())
        pending = read_state(self.projects)["projects"]["p3"]
        self.assertEqual(pending["state"], "cleaning")
        self.assertNotIn("commit", pending["coordination_archive"])
        history = (directory / "coordination" / "history.md").read_text()
        sequence = pending["coordination_archive"]["sequence"]
        context = pending["coordination_archive"]["context"]
        self.assertEqual(context["uuid"], project["uuid"])
        self.assertEqual(context["generation"], generation)
        (directory / "coordination" / "history.md").unlink()
        lock.unlink()
        self.cli("cleanup", "example")
        archived = read_state(self.projects)["archive"][-1]
        self.assertEqual(archived["coordination_archive"]["sequence"], sequence)
        self.assertEqual(archived["coordination_archive"]["context"], context)
        self.assertEqual((directory / "coordination" / "history.md").read_text(), history)

    def test_sqlite_local_commit_failure_blocks_cleanup_and_recovers(self) -> None:
        self.archive_retry()

    def test_archived_retro_uses_markdown_without_database(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        self.cli("cleanup", "example")
        archived = read_state(self.projects)["archive"][-1]
        directory = Path(archived["coordination_archive"]["directory"])
        database = directory / "board.sqlite"
        database.rename(directory / "saved.sqlite")
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from baton.manager import Manager
manager = Manager()
manager.load()
print(manager.retro_prompt(manager.artifact_project(sys.argv[2])))
"""
        result = self.run_command([sys.executable, "-c", script, str(fixtures.SCRIPT.parent.parent), project["uuid"]], self.root)
        self.assertIn(str(directory / "overview.md"), result.stdout)
        self.assertIn(str(directory / "history.md"), result.stdout)
        self.assertIn("Snapshot sequence:", result.stdout)
        self.assertFalse(database.exists())

    def test_normalized_archive_destination_and_missing_checkpoint_checksum_refuse(self) -> None:
        from baton.artifacts import ArtifactsMixin
        from baton.errors import ProjectError
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        directory = Path(project["blackboard"]["directory"])
        result = self.cli("blackboard", "export", "--selector", "example", "--expected-generation", project["blackboard"]["generation"],
                          "--output", str(directory / "alias" / ".." / "coordination"), ok=False)
        self.assertIn("Final coordination", result.stderr)
        self.cli("cleanup", "example")
        archive = read_state(self.projects)["archive"][-1]["coordination_archive"]
        path = str((Path(archive["directory"]) / "manifest.json").relative_to(self.artifacts))
        with self.assertRaisesRegex(ProjectError, "missing a required checksum"):
            ArtifactsMixin.verify_archive_commit(self.artifacts, archive["commit"], [path], {})

    def test_cleanup_remote_failure_keeps_verified_local_archive(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        origin = self.root / "artifact-origin.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(origin))
        self.git(self.artifacts, "remote", "add", "origin", str(origin))
        self.git(self.artifacts, "push", "-u", "origin", "main")
        self.git(self.artifacts, "remote", "set-head", "origin", "main")
        published = self.git(origin, "rev-parse", "main")
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'test remote unavailable' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(origin / "hooks")
        result = self.cli("cleanup", "example")
        self.assertIn("saved locally", result.stderr)
        self.assertIn("test remote unavailable", result.stderr)
        archive = read_state(self.projects)["archive"][-1]["coordination_archive"]
        self.assertEqual(self.git(origin, "rev-parse", "main"), published)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), archive["commit"])
        self.assertFalse(self.checkout("alpha").exists())
        self.assertTrue((Path(archive["directory"]) / "history.md").exists())

    def test_sqlite_initialization_resumes_after_registry_save_interruption(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.update_state(lambda project: project["blackboard"].update(initialized=False))
        self.cli("blackboard", "init", "--selector", "example")
        self.assertTrue(read_state(self.projects)["projects"]["p3"]["blackboard"]["initialized"])
        self.assertEqual(json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)["backend"], "sqlite")

    def test_process_death_during_sqlite_initialization_can_retry_and_post(self) -> None:
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from baton.blackboard_sqlite import SQLiteBlackboard
class Interrupted(SQLiteBlackboard):
    @staticmethod
    def ensure_schema(connection):
        SQLiteBlackboard.ensure_schema(connection)
        if sys.argv[3] == 'schema':
            os._exit(71)
    def _record(self, handle, marker):
        if marker['status'] == 'active' and sys.argv[3] == 'committed':
            os._exit(71)
        super()._record(handle, marker)
Interrupted(Path(sys.argv[2]), 'interrupted', 'init-generation', 'init-uuid').initialize()
"""
        for checkpoint in ("schema", "committed"):
            with self.subTest(checkpoint=checkpoint):
                directory = self.root / checkpoint
                result = self.run_command([sys.executable, "-c", script, str(fixtures.SCRIPT.parent.parent), str(directory), checkpoint], self.root, ok=False)
                self.assertEqual(result.returncode, 71)
                board = SQLiteBlackboard(directory, "interrupted", "init-generation", "init-uuid")
                board.initialize()
                board.post("worker", "retry is writable")
                self.assertEqual(len(board.read()["entries"]), 1)

    def test_cleanup_genuinely_uninitialized_board_records_no_invented_history(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        directory = Path(project["blackboard"]["directory"])
        for name in ("blackboard.sqlite", "blackboard.sqlite-wal", "blackboard.sqlite-shm", "blackboard-lock.md"):
            (directory / name).unlink(missing_ok=True)
        self.update_state(lambda value: value["blackboard"].update(initialized=False))
        self.cli("cleanup", "example")
        archive = read_state(self.projects)["archive"][-1]["coordination_archive"]
        self.assertTrue(archive["uninitialized"])
        self.assertFalse((directory / "coordination").exists())

    def test_cleanup_recognizes_commit_before_commit_sha_was_saved(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        script = """
import os, sys
sys.path.insert(0, sys.argv[1])
from baton.manager import Manager
class Interrupted(Manager):
    def save(self):
        archive = self.state['projects'][self.held_slot].get('coordination_archive', {})
        if archive.get('commit'):
            os._exit(71)
        super().save()
manager = Interrupted()
manager.load()
with manager.command_lock('example'):
    manager.cleanup('example', False, [])
"""
        result = self.run_command([sys.executable, "-c", script, str(fixtures.SCRIPT.parent.parent)], self.root, ok=False)
        self.assertEqual(result.returncode, 71)
        pending = read_state(self.projects)["projects"]["p3"]["coordination_archive"]
        self.assertNotIn("commit", pending)
        committed = self.git(self.artifacts, "rev-parse", "HEAD")
        history = (Path(pending["directory"]) / "history.md").read_bytes()
        live = Path(pending["directory"]).parent / "blackboard.sqlite"
        live.rename(live.with_name(".blackboard-saved-live.tmp"))
        (Path(pending["directory"]) / "history.md").unlink()
        self.cli("cleanup", "example")
        archive = read_state(self.projects)["archive"][-1]["coordination_archive"]
        self.assertEqual(archive["commit"], committed)
        self.assertEqual(archive["logical_sha256"], pending["logical_sha256"])
        self.assertEqual((Path(archive["directory"]) / "history.md").read_bytes(), history)

    def test_process_death_before_export_manifest_can_resume_owned_publication(self) -> None:
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from baton.blackboard_sqlite import SQLiteBlackboard
class Stop(str):
    def encode(self, *args, **kwargs):
        os._exit(71)
class Interrupted(SQLiteBlackboard):
    def render_snapshot(self, data):
        overview, history = super().render_snapshot(data)
        return Stop(overview), history
board = Interrupted(Path(sys.argv[2]), 'export', 'export-generation', 'export-uuid')
board.initialize()
board.post('worker', 'interrupted export evidence')
board.export(Path(sys.argv[3]))
"""
        source, destination = self.root / "export-source", self.root / "export-output"
        result = self.run_command([sys.executable, "-c", script, str(fixtures.SCRIPT.parent.parent), str(source), str(destination)], self.root, ok=False)
        self.assertEqual(result.returncode, 71)
        self.assertTrue((destination / "board.sqlite").exists())
        self.assertFalse((destination / "manifest.json").exists())
        board = SQLiteBlackboard(source, "export", "export-generation", "export-uuid")
        manifest = board.export(destination)
        self.assertEqual(manifest["counts"]["entries"], 1)
        self.assertIn("interrupted export evidence", (destination / "history.md").read_text())

    def test_hot_destination_rollback_journal_is_never_published_over(self) -> None:
        source = SQLiteBlackboard(self.root / "source", "source", "generation", "uuid")
        source.initialize()
        destination = self.root / "destination"
        source.export(destination)
        script = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=DELETE')
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE overview SET body=?', ('x' * 200000,))
os._exit(71)
"""
        result = self.run_command([sys.executable, "-c", script, str(destination / "board.sqlite")], self.root, ok=False)
        self.assertEqual(result.returncode, 71)
        self.assertGreater((destination / "board.sqlite-journal").stat().st_size, 0)
        before = (destination / "board.sqlite").read_bytes()
        with self.assertRaisesRegex(BlackboardError, "sidecars"):
            source.export(destination)
        self.assertEqual((destination / "board.sqlite").read_bytes(), before)

    def test_ignored_final_evidence_refuses_before_cleaning_or_sealing(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        ignore = self.artifacts / ".gitignore"
        ignore.write_text(ignore.read_text() + "\n*.sqlite\n")
        failed = self.cli("cleanup", "example", ok=False)
        self.assertIn("final coordination evidence must not be ignored", failed.stderr)
        self.assertEqual(read_state(self.projects)["projects"]["p3"]["state"], "active")
        snapshot = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)
        self.assertEqual(snapshot["status"], "active")
        self.assertTrue(self.checkout("alpha").exists())

    def test_missing_coordination_ignore_rules_refuse_before_cleaning_or_sealing(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        (self.artifacts / ".gitignore").write_text("*/blackboard.sqlite*\n*/blackboard-lock.md\n")
        self.git(self.artifacts, "add", ".gitignore")
        self.git(self.artifacts, "commit", "-m", "Model old artifact ignore rules")
        failed = self.cli("cleanup", "example", ok=False)
        self.assertIn("before cleanup", failed.stderr)
        self.assertEqual(read_state(self.projects)["projects"]["p3"]["state"], "active")
        snapshot = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)
        self.assertEqual(snapshot["status"], "active")
        self.cli("blackboard", "post", "--selector", "example", "--expected-generation", project["blackboard"]["generation"], "--agent", "worker", "--text", "still writable")

    def test_retro_metadata_before_initialization_does_not_create_evidence(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = read_state(self.projects)["projects"]["p3"]
        directory = Path(project["blackboard"]["directory"])
        for name in ("blackboard.sqlite", "blackboard.sqlite-wal", "blackboard.sqlite-shm", "blackboard-lock.md"):
            (directory / name).unlink(missing_ok=True)
        self.update_state(lambda value: value["blackboard"].update(initialized=False))
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from baton.manager import Manager
manager = Manager()
manager.load()
print(manager.retro_prompt(manager.artifact_project(sys.argv[2])))
"""
        self.run_command([sys.executable, "-c", script, str(fixtures.SCRIPT.parent.parent), project["uuid"]], self.root)
        self.assertFalse((directory / "blackboard.sqlite").exists())
        self.assertFalse((directory / ".blackboard-export").exists())
