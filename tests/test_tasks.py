import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from blackboard import BlackboardError
from baton.blackboard_sqlite import SQLiteBlackboard
from baton.tasks import Tasks
import test_sqlite_blackboard as storage
import test_project_manager as fixtures


class TaskTest(unittest.TestCase):
    setUp = storage.SQLiteStorageTest.setUp
    # Reuse the real SQLite fixture without rerunning inherited storage tests.
    def new(self, **values: object) -> dict:
        return Tasks(self.board).create("parent", {"title": "Test", **values})

    def change(self, task: dict, action: str, actor: str = "worker", **values: object) -> dict:
        return Tasks(self.board).mutate(actor, task["id"], task["version"], action, values)

    def test_lifecycle_assignment_and_terminal_metadata(self) -> None:
        task = self.new(description="Unicode λ\n# evidence", assignee="worker")
        with self.assertRaisesRegex(BlackboardError, "another"):
            self.change(task, "claim", actor="other")
        task = self.change(task, "claim")
        with self.assertRaisesRegex(BlackboardError, "version"):
            Tasks(self.board).mutate("worker", task["id"], 1, "done")
        task = self.change(task, "assign", actor="parent", assignee="other")
        self.assertEqual((task["state"], task["assignee"]), ("open", "other"))
        task = self.change(task, "claim", actor="other")
        task = self.change(task, "release", actor="other")
        self.assertIsNone(task["assignee"])
        task = self.change(task, "claim")
        task = self.change(task, "done")
        with self.assertRaisesRegex(BlackboardError, "terminal"):
            self.change(task, "cancel")
        task = self.change(task, "update", title="Final evidence")
        self.assertEqual(task["state"], "done")
        self.board.export(self.root / "export")
        self.assertIn("Final evidence", (self.root / "export/overview.md").read_text())

    def test_dependency_dag_cancelled_blocker_and_noop(self) -> None:
        tasks = Tasks(self.board)
        first, second, child = self.new(title="first"), self.new(title="second"), self.new(title="child")
        child = tasks.dependency("parent", child["id"], first["id"], 1)
        child = tasks.dependency("parent", child["id"], second["id"], child["version"])
        sequence = self.board.read()["sequence"]
        self.assertFalse(tasks.dependency("parent", child["id"], second["id"], child["version"])["changed"])
        self.assertEqual(self.board.read()["sequence"], sequence)
        with self.assertRaisesRegex(BlackboardError, "cycle"):
            tasks.dependency("parent", first["id"], child["id"], 1)
        first = self.change(self.change(first, "claim"), "done")
        second = self.change(second, "cancel")
        with self.assertRaisesRegex(BlackboardError, "prerequisites"):
            self.change(child, "claim")
        blocked = tasks.read(blocked=True)["tasks"]
        self.assertEqual(blocked[0]["blockers"], [{"id": second["id"], "state": "cancelled"}])
        child = tasks.dependency("parent", child["id"], second["id"], child["version"], remove=True)
        child = self.change(child, "claim")
        with self.assertRaisesRegex(BlackboardError, "Release"):
            tasks.dependency("parent", child["id"], first["id"], child["version"], remove=True)
        self.board.freeze()
        manifest = self.board.export(self.root / "sealed")
        self.assertEqual(manifest["counts"]["task_states"], {"open": 0, "in_progress": 1, "done": 1, "cancelled": 1})
        self.assertIn("cancelled", (self.root / "sealed/overview.md").read_text())
        with self.assertRaises(BlackboardError):
            self.change(child, "done")

    def test_claim_race_one_winner_complete_audit(self) -> None:
        task = self.new()
        script = """import sys
from pathlib import Path
from baton.blackboard_sqlite import SQLiteBlackboard
from baton.tasks import Tasks
from blackboard import BlackboardError
b=SQLiteBlackboard(Path(sys.argv[1]),'example','generation','uuid')
try: Tasks(b).mutate(sys.argv[3],sys.argv[2],1,'claim')
except BlackboardError: sys.exit(2)
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root), task["id"], actor]) for actor in ("a", "b")]
        self.assertEqual(sorted(process.wait(timeout=10) for process in processes), [0, 2])
        events = [event for event in self.board.read()["events"] if event["kind"] == "task"]
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[-1]["after_json"])["state"], "in_progress")

    def test_reference_validation_and_historical_edits(self) -> None:
        tasks = Tasks(self.board, {"repos": [{"id": "alpha", "first_member": "base", "members": [{"member": "child"}]}]})
        task = tasks.create("parent", {"title": "Refs", "repo": "alpha", "member": "base", "plan": "plan.md"})
        tasks.project = {"repos": []}
        task = tasks.mutate("parent", task["id"], 1, "update", {"description": "retain historical references"})
        self.assertEqual(task["member"], "base")
        cleared = tasks.mutate("parent", task["id"], task["version"], "update", {"member": None})
        self.assertEqual((cleared["repo"], cleared["member"]), ("alpha", None))
        with self.assertRaisesRegex(BlackboardError, "registered"):
            tasks.mutate("parent", task["id"], cleared["version"], "update", {"member": "new"})
        for values in ({"title": " "}, {"priority": -1}, {"assignee": "bad\nname"}, {"member": "child"}):
            with self.assertRaises(BlackboardError):
                tasks.create("parent", {"title": "Invalid", **values})

    def test_link_and_export_projection_integrity(self) -> None:
        task = self.new(title="Unresolved", description="## Markdown\nEvidence λ")
        self.board.post("worker", "linked", task_id=task["id"])
        with self.assertRaisesRegex(BlackboardError, "Task"):
            self.board.post("worker", "bad link", task_id="foreign")
        first = self.board.export(self.root / "one")
        second = self.board.export(self.root / "two")
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["logical_sha256"], second["logical_sha256"])
        for name in ("overview.md", "history.md"):
            self.assertEqual((self.root / "one" / name).read_bytes(), (self.root / "two" / name).read_bytes())
            self.assertIn("Evidence λ", (self.root / "one" / name).read_text())
        with self.board.transaction("corrupt") as connection:
            connection.execute("UPDATE tasks SET description='unaudited'")
        with self.assertRaisesRegex(BlackboardError, "audit"):
            self.board.export(self.root / "bad")

    def test_ready_assignment_and_query_order(self) -> None:
        first = self.new(title="low", priority=5)
        second = self.new(title="high", priority=0, assignee="other")
        tasks = Tasks(self.board)
        self.assertEqual([task["id"] for task in tasks.read(ready=True)["tasks"]], [second["id"], first["id"]])
        self.assertEqual([task["id"] for task in tasks.read(actor="worker", ready=True)["tasks"]], [first["id"]])
        self.assertFalse(tasks.read(second["id"], actor="worker")["tasks"][0]["assignment_eligible"])

    def test_reassignment_and_dependency_claim_races(self) -> None:
        for mode in ("assign", "dependency"):
            with self.subTest(mode=mode):
                task, prerequisite = self.new(), self.new()
                def claim() -> object:
                    try:
                        return self.change(task, "claim")
                    except BlackboardError:
                        return None
                def competing() -> object:
                    try:
                        if mode == "assign":
                            return self.change(task, "assign", actor="parent", assignee="other")
                        return Tasks(self.board).dependency("parent", task["id"], prerequisite["id"], 1)
                    except BlackboardError:
                        return None
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = [future.result() for future in (pool.submit(claim), pool.submit(competing))]
                self.assertEqual(sum(value is not None for value in results), 1)
                current = Tasks(self.board).read(task["id"])["tasks"][0]
                if current["state"] == "in_progress":
                    self.assertEqual((current["assignee"], current["prerequisites"]), ("worker", []))
                else:
                    self.assertTrue(current["blockers"] or current["assignee"] == "other")

    def test_prerequisite_done_claim_race_preserves_eligibility(self) -> None:
        parent, child = self.new(), self.new()
        child = Tasks(self.board).dependency("parent", child["id"], parent["id"], 1)
        parent = self.change(parent, "claim")
        def claim() -> object:
            try:
                return self.change(child, "claim")
            except BlackboardError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed, completed = pool.submit(claim), pool.submit(self.change, parent, "done")
            claimed.result()
            completed.result()
        current = Tasks(self.board).read(child["id"])["tasks"][0]
        self.assertFalse(current["blockers"])
        if current["state"] == "open":
            current = self.change(current, "claim")
        self.assertEqual(current["state"], "in_progress")

    def test_diamond_cycle_missing_and_terminal_edges(self) -> None:
        tasks = Tasks(self.board)
        root, left, right, bottom = [self.new() for _ in range(4)]
        left = tasks.dependency("parent", left["id"], root["id"], 1)
        right = tasks.dependency("parent", right["id"], root["id"], 1)
        bottom = tasks.dependency("parent", bottom["id"], left["id"], 1)
        bottom = tasks.dependency("parent", bottom["id"], right["id"], bottom["version"])
        for prerequisite in (root["id"], bottom["id"], "foreign"):
            with self.assertRaises(BlackboardError):
                tasks.dependency("parent", root["id"], prerequisite, 1)
        cancelled = self.change(bottom, "cancel")
        with self.assertRaisesRegex(BlackboardError, "terminal"):
            tasks.dependency("parent", cancelled["id"], left["id"], cancelled["version"], remove=True)
        self.board.export(self.root / "diamond")

    def test_failed_changes_rollback_state_and_events(self) -> None:
        task = self.new()
        before = self.board.read()
        @contextlib.contextmanager
        def reject() -> object:
            raise BlackboardError("stale generation")
            yield
        with self.assertRaisesRegex(BlackboardError, "stale"):
            Tasks(self.board).mutate("worker", task["id"], 1, "claim", guard=reject)
        with self.assertRaises(BlackboardError):
            Tasks(self.board).create("worker", {"title": "Invalid", "state": "done"})
        self.assertEqual(self.board.read(), before)
        other = SQLiteBlackboard(self.root / "other", "other", "gen", "uuid2")
        other.initialize()
        with self.assertRaisesRegex(BlackboardError, "does not exist"):
            Tasks(other).mutate("worker", task["id"], 1, "claim")

    def test_single_line_title_and_cleared_references(self) -> None:
        for title in ("first\n# injected heading", "first\rsecond"):
            with self.assertRaises(BlackboardError):
                self.new(title=title)
        tasks = Tasks(self.board, {"repos": [{"id": "alpha", "first_member": "base"}, {"id": "beta"}]})
        task = tasks.create("parent", {"title": "Move", "repo": "alpha", "member": "base", "plan": "plan.md"})
        task = tasks.mutate("parent", task["id"], 1, "update", {"repo": "beta", "member": None, "plan": None})
        self.assertEqual((task["repo"], task["member"], task["plan"]), ("beta", None, None))

    def test_priority_overflow_has_no_state_or_audit_effect(self) -> None:
        task = self.new(priority=2**63 - 1)
        before = self.board.read()
        with self.assertRaisesRegex(BlackboardError, "priority"):
            self.new(priority=2**63)
        with self.assertRaisesRegex(BlackboardError, "priority"):
            self.change(task, "update", priority=2**63)
        self.assertEqual(self.board.read(), before)

    def settle(self, task: dict, outcome: str, actor: str = "worker", summary: str = "Result", **values: object) -> dict:
        return self.change(task, "settle", actor=actor, outcome=outcome, summary=summary, **values)

    def test_settle_succeeded_and_failed_outcomes(self) -> None:
        task = self.settle(self.change(self.new(), "claim"), "succeeded", files=["a.py"], report="report.md", to="parent")
        self.assertEqual((task["state"], task["assignee"], task["version"]), ("done", "worker", 3))
        self.assertEqual({key: task["settlement"][key] for key in ("agent", "outcome", "files", "report", "task_version")},
                         {"agent": "worker", "outcome": "succeeded", "files": ["a.py"], "report": "report.md", "task_version": 3})
        entry = self.board.read()["entries"][-1]
        self.assertEqual((entry["id"], entry["kind"], entry["recipient"], entry["task_id"]),
                         (task["settlement"]["entry_id"], "settlement", "parent", task["id"]))
        self.assertEqual(Tasks(self.board).read(task["id"])["tasks"][0]["settlement"], task["settlement"])
        failed = self.settle(self.change(self.new(assignee="worker"), "claim"), "failed", summary="Blocked on API")
        self.assertEqual((failed["state"], failed["assignee"]), ("open", "worker"))
        shown = Tasks(self.board).read(failed["id"], actor="worker")["tasks"][0]
        self.assertEqual((shown["held"], shown["ready"], shown["settlement"]["outcome"]), (True, False, "failed"))
        for values in ({"outcome": "maybe", "summary": "x"}, {"outcome": "failed", "summary": " "}, {"outcome": "failed"},
                       {"outcome": "failed", "summary": "x", "files": ["a\nb"]}):
            with self.subTest(values=values), self.assertRaises(BlackboardError):
                Tasks(self.board).mutate("worker", failed["id"], failed["version"], "settle", values)

    def test_settle_is_once_per_claim_and_rejects_wrong_agent_or_version(self) -> None:
        claimed = self.change(self.new(), "claim")
        with self.assertRaisesRegex(BlackboardError, "assigned to this agent"):
            self.settle(claimed, "succeeded", actor="other")
        with self.assertRaisesRegex(BlackboardError, "stale"):
            Tasks(self.board).mutate("worker", claimed["id"], 1, "settle", {"outcome": "succeeded", "summary": "x"})
        settled = self.settle(claimed, "succeeded")
        sequence = self.board.read()["sequence"]
        replayed = self.settle(claimed, "succeeded")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["settlement"], settled["settlement"])
        self.assertEqual(self.board.read()["sequence"], sequence)
        for actor, outcome, summary in (("worker", "failed", "Result"), ("worker", "succeeded", "Other"), ("other", "succeeded", "Result")):
            with self.subTest(actor=actor, outcome=outcome, summary=summary), self.assertRaises(BlackboardError):
                self.settle(claimed, outcome, actor=actor, summary=summary)
        with self.assertRaises(BlackboardError):
            self.settle(settled, "succeeded")
        self.assertEqual(self.board.read()["sequence"], sequence)

    def test_failed_settlement_holds_task_until_assign(self) -> None:
        first = self.change(self.new(), "claim")
        failed = self.settle(first, "failed")
        prerequisite = self.new(title="Prerequisite")
        for attempt in (lambda: self.change(failed, "claim"), lambda: self.change(failed, "update", title="Bump"),
                        lambda: Tasks(self.board).dependency("worker", failed["id"], prerequisite["id"], failed["version"])):
            with self.assertRaisesRegex(BlackboardError, "held"):
                attempt()
        held = Tasks(self.board).read(failed["id"])["tasks"][0]
        self.assertEqual((held["version"], held["held"], held["settlement"]["outcome"]), (failed["version"], True, "failed"))
        rerouted = self.change(failed, "assign", actor="parent", assignee="worker")
        shown = Tasks(self.board).read(rerouted["id"])["tasks"][0]
        self.assertEqual((shown["held"], shown["ready"], shown["settlement"]), (False, True, None))
        reclaimed = self.change(rerouted, "claim")
        for outcome in ("failed", "succeeded"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(BlackboardError, "stale"):
                self.settle(first, outcome)
        self.assertIsNone(Tasks(self.board).read(reclaimed["id"])["tasks"][0]["settlement"])
        done = self.change(reclaimed, "done")
        self.assertEqual((done["state"], Tasks(self.board).read(done["id"])["tasks"][0]["settlement"]), ("done", None))
        succeeded = self.settle(self.change(self.new(), "claim"), "succeeded")
        self.change(succeeded, "update", actor="parent", title="Renamed")
        self.assertIsNone(Tasks(self.board).read(succeeded["id"])["tasks"][0]["settlement"])

    def test_held_query_lists_only_current_failed_settlements(self) -> None:
        held = self.settle(self.change(self.new(title="Held"), "claim"), "failed")
        retried = self.settle(self.change(self.new(title="Retried"), "claim"), "failed")
        self.change(retried, "assign", actor="parent", assignee="worker")
        superseded = self.settle(self.change(self.new(title="Superseded"), "claim"), "failed")
        self.change(superseded, "cancel", actor="parent")
        self.settle(self.change(self.new(title="Succeeded"), "claim"), "succeeded")
        self.change(self.new(title="Active"), "claim")
        self.new(title="Unsettled")
        self.assertEqual([task["id"] for task in Tasks(self.board).read(held=True)["tasks"]], [held["id"]])

    def test_export_renders_held_tasks_and_unanswered_questions(self) -> None:
        held = self.settle(self.change(self.new(title="Held"), "claim"), "failed")
        open_question = self.board.post("worker", "Which host routes Facebook?\nDetails follow", "parent", kind="question", task_id=held["id"])
        answered = self.board.post("worker", "Answered question", "parent", kind="question")
        self.board.post("parent", "Yes", "worker", kind="answer", parent_id=answered["id"])
        self_replied = self.board.post("worker", "Self-replied question", "parent", kind="question")
        self.board.post("worker", "Still blocked", "parent", parent_id=self_replied["id"])
        self.board.export(self.root / "one")
        time.sleep(1.1)
        self.board.export(self.root / "two")
        overview = (self.root / "one" / "overview.md").read_text()
        self.assertEqual(overview, (self.root / "two" / "overview.md").read_text())
        self.assertIn(f"ID: {held['id']} | state: open | held: yes", overview)
        unresolved = overview.split("## Unanswered questions\n\n", 1)[1]
        self.assertIn(f"- {open_question['id']} |", unresolved)
        self.assertIn(f"task: {held['id']} | Which host routes Facebook?\n", unresolved)
        self.assertNotIn("Details follow", unresolved)
        # Any reply, including the author's own, counts as answered, matching blackboard agents.
        self.assertNotIn("Answered question", unresolved)
        self.assertNotIn("Self-replied question", unresolved)

    def test_decision_task_blocks_dependents_until_settled(self) -> None:
        tasks = Tasks(self.board)
        decision, child = self.new(title="Decide storage"), self.new(title="Implement")
        child = tasks.dependency("parent", child["id"], decision["id"], 1)
        with self.assertRaisesRegex(BlackboardError, "prerequisites"):
            self.change(child, "claim")
        self.settle(self.change(decision, "claim", actor="decider"), "succeeded", actor="decider", summary="Use SQLite")
        self.assertEqual(self.change(child, "claim")["state"], "in_progress")


class TaskCLITest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create
    update_state = fixtures.ProjectManagerTest.update_state

    def test_real_task_cli_and_generation_guard(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        state = json.loads(self.cli("status", "p3", "--json").stdout)
        generation = state["blackboard"]["generation"]
        common = ("--selector", "p3", "--expected-generation", generation, "--agent", "worker")
        task = json.loads(self.cli("task", "create", "--title", "CLI task", "--repo", "alpha", *common).stdout)
        task = json.loads(self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *common).stdout)
        self.assertEqual(task["state"], "in_progress")
        self.cli("blackboard", "post", "--text", "linked CLI evidence", "--task", task["id"], *common)
        result = json.loads(self.cli("task", "show", "--id", task["id"], "--selector", "p3").stdout)
        self.assertEqual(result["tasks"][0]["id"], task["id"])
        bad = self.cli("task", "done", "--id", task["id"], "--expected-version", "2", "--selector", "p3", "--agent", "worker", "--expected-generation", "old", ok=False)
        self.assertIn("stale", bad.stderr)

    def test_settle_cli_reports_and_refuses_reserved_posts(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        generation = json.loads(self.cli("status", "p3", "--json").stdout)["blackboard"]["generation"]
        common = ("--selector", "p3", "--expected-generation", generation, "--agent", "worker")
        task = json.loads(self.cli("task", "create", "--title", "Settle", *common).stdout)
        self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *common)
        settle = ("task", "settle", "--id", task["id"], "--expected-version", "2", "--outcome", "succeeded",
                  "--summary", "Shipped", "--files", "a.py", "b.py", "--report", "report.md", *common)
        result = json.loads(self.cli(*settle).stdout)
        self.assertEqual((result["state"], result["settlement"]["files"]), ("done", ["a.py", "b.py"]))
        self.assertTrue(json.loads(self.cli(*settle).stdout)["replayed"])
        self.assertIn("stale", self.cli(*settle[:-len(common)], "--summary", "Changed", *common, ok=False).stderr)
        shown = json.loads(self.cli("task", "show", "--id", task["id"], "--selector", "p3").stdout)["tasks"][0]
        self.assertEqual(shown["settlement"]["summary"], "Shipped")
        self.assertIn("Settlement: succeeded", self.cli("blackboard", "read", "--selector", "p3").stdout)
        self.assertIn("reserved", self.cli("blackboard", "post", "--kind", "settlement", "--text", "x", *common, ok=False).stderr)

    def test_held_list_cli_and_exclusive_queries(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        generation = json.loads(self.cli("status", "p3", "--json").stdout)["blackboard"]["generation"]
        common = ("--selector", "p3", "--expected-generation", generation, "--agent", "worker")
        task = json.loads(self.cli("task", "create", "--title", "Held", *common).stdout)
        self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *common)
        self.cli("task", "settle", "--id", task["id"], "--expected-version", "2", "--outcome", "failed", "--summary", "Blocked", *common)
        self.cli("task", "create", "--title", "Other", *common)
        listed = json.loads(self.cli("task", "list", "--selector", "p3", "--held").stdout)["tasks"]
        self.assertEqual([(entry["id"], entry["held"]) for entry in listed], [(task["id"], True)])
        for other in ("--ready", "--blocked"):
            with self.subTest(other=other):
                self.assertIn("not allowed", self.cli("task", "list", "--selector", "p3", "--held", other, ok=False).stderr)

    def test_clear_flags_and_conflicting_assignments(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        generation = project["blackboard"]["generation"]
        common = ("--selector", "p3", "--expected-generation", generation, "--agent", "parent")
        task = json.loads(self.cli("task", "create", "--title", "Refs", "--repo", "alpha", "--plan", "plan.md", *common).stdout)
        task = json.loads(self.cli("task", "update", "--id", task["id"], "--expected-version", "1", "--clear-repo", "--clear-member", "--clear-plan", *common).stdout)
        self.assertEqual((task["repo"], task["member"], task["plan"]), (None, None, None))
        self.assertIn("Cannot supply and clear", self.cli("task", "update", "--id", task["id"], "--expected-version", "2", "--repo", "alpha", "--clear-repo", *common, ok=False).stderr)

    def test_live_reference_guard_uses_current_registry_membership(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.update_state(lambda current: current["repos"][0].update(first_member="base"))
        script = """import json
from pathlib import Path
from baton.manager import Manager
from baton.cli import task_project_guard
from baton.tasks import Tasks
from blackboard import BlackboardError
manager=Manager()
manager.load()
project=manager.project('p3')
board=manager.board_for(project)
tasks=Tasks(board,project)
record=manager.layout/'projects/p3.json'
current=json.loads(record.read_text())
current['repos'][0]['retirement_state']='retired'
record.write_text(json.dumps(current))
guard=lambda: task_project_guard(manager,project,board,board.generation,tasks)
try:
    tasks.create('parent',{'title':'Stale','repo':'alpha','member':'base'},guard)
except BlackboardError as error:
    assert 'not registered' in str(error), str(error)
else:
    raise AssertionError('Retired member accepted from stale manifest')
assert tasks.read()['tasks']==[]
print('Current registry membership enforced')
"""
        result = self.run_command([sys.executable, "-c", script], Path(fixtures.SCRIPT).parent.parent)
        self.assertIn("Current registry membership enforced", result.stdout)
