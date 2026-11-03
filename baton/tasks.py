"""Project-local task state and dependencies in the coordination transaction."""

import contextlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from blackboard import BlackboardError
from .blackboard_sqlite import OUTCOMES, SETTLEMENT, current_settlement, is_held, latest_settlements, settlement_body, settlement_report

STATES = ("open", "in_progress", "done", "cancelled")
FIELDS = ("title", "description", "assignee", "priority", "repo", "member", "plan")


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL CHECK(length(trim(title))>0),
        description TEXT NOT NULL,assignee TEXT,priority INTEGER NOT NULL,repo TEXT,member TEXT,plan TEXT,
        state TEXT NOT NULL CHECK(state IN ('open','in_progress','done','cancelled')),
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>0),
        CHECK(state!='in_progress' OR assignee IS NOT NULL))""")
    connection.execute("""CREATE TABLE task_dependencies(task_id TEXT NOT NULL REFERENCES tasks(id),
        prerequisite_id TEXT NOT NULL REFERENCES tasks(id),PRIMARY KEY(task_id,prerequisite_id),
        CHECK(task_id!=prerequisite_id))""")
    connection.execute("CREATE INDEX task_state_priority ON tasks(state,priority,created_at,id)")
    connection.execute("CREATE INDEX task_prerequisite ON task_dependencies(prerequisite_id)")


def task_data(connection: sqlite3.Connection) -> dict[str, Any]:
    return {"tasks": [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY priority,created_at,id")],
            "task_dependencies": [dict(row) for row in connection.execute("SELECT * FROM task_dependencies ORDER BY task_id,prerequisite_id")]}


def group_dependencies(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for edge in edges:
        result.setdefault(edge["task_id"], []).append(edge["prerequisite_id"])
    return result


def validate_tasks(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    latest = {event["entity_id"]: json.loads(event["after_json"]) for event in data["events"] if event["kind"] == "task"}
    rows = {task["id"]: task for task in data["tasks"]}
    edges = data["task_dependencies"]
    if set(latest) != set(rows):
        raise BlackboardError("Tasks do not match durable audit history.")
    dependencies = group_dependencies(edges)
    for identifier, task in rows.items():
        prerequisites = sorted(dependencies.get(identifier, []))
        if latest[identifier] != {**task, "prerequisites": prerequisites}:
            raise BlackboardError("Task/dependencies do not match durable audit history.")
    # Kahn's traversal checks the loaded DAG without materializing its transitive closure.
    incoming = {identifier: 0 for identifier in rows}
    for edge in edges:
        if edge["task_id"] not in rows or edge["prerequisite_id"] not in rows:
            raise BlackboardError("Task dependencies reference a missing task.")
        incoming[edge["prerequisite_id"]] += 1
    pending = [identifier for identifier, count in incoming.items() if count == 0]
    visited = 0
    while pending:
        identifier = pending.pop()
        visited += 1
        for prerequisite in dependencies.get(identifier, []):
            incoming[prerequisite] -= 1
            if incoming[prerequisite] == 0:
                pending.append(prerequisite)
    if visited != len(rows):
        raise BlackboardError("Task dependencies contain a cycle.")
    for entry in data["entries"]:
        if entry["task_id"] is not None and entry["task_id"] not in rows:
            raise BlackboardError("Entry references a missing task.")


class Tasks:
    def __init__(self, board: Any, project: dict[str, Any] | None = None) -> None:
        self.board = board
        self.project = project

    def _row(self, connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise BlackboardError("Task does not exist in this project.")
        return dict(row)

    @staticmethod
    def _edges(connection: sqlite3.Connection, identifier: str) -> list[str]:
        return [row[0] for row in connection.execute("SELECT prerequisite_id FROM task_dependencies WHERE task_id=? ORDER BY prerequisite_id", (identifier,))]

    def _snapshot(self, connection: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "prerequisites": self._edges(connection, row["id"])}

    def _fields(self, values: dict[str, Any]) -> None:
        if "title" in values and (not isinstance(values["title"], str) or not values["title"].strip()):
            raise BlackboardError("Task title must not be empty.")
        if "title" in values:
            self.board._single_line("title", values["title"])
        if "description" in values and not isinstance(values["description"], str):
            raise BlackboardError("Task description must be text.")
        if "priority" in values and (type(values["priority"]) is not int or not 0 <= values["priority"] <= 2**63 - 1):
            raise BlackboardError("Task priority must be an integer between 0 and 9223372036854775807.")
        for field in ("assignee", "repo", "member", "plan"):
            if field in values and values[field] is not None:
                self.board._single_line(field, values[field])

    def _references(self, after: dict[str, Any], changed: dict[str, Any]) -> None:
        if after["member"] is not None and after["repo"] is None:
            raise BlackboardError("Task member requires repository.")
        if self.project is None or not ("repo" in changed or ("member" in changed and after["member"] is not None)):
            return
        if after["repo"] is not None:
            repo = next((repo for repo in self.project["repos"] if repo["id"] == after["repo"]), None)
            if repo is None:
                raise BlackboardError("Task repository is not registered in this project.")
            if after["member"] is not None:
                from .manifest import project_entries, member_name
                if not any(member_name(entry) == after["member"] for entry in project_entries({"repos": [repo]})):
                    raise BlackboardError("Task member is not registered in this repository.")

    @staticmethod
    def _blockers(connection: sqlite3.Connection, identifier: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute("""SELECT t.id,t.state FROM task_dependencies d
            JOIN tasks t ON t.id=d.prerequisite_id WHERE d.task_id=? AND t.state!='done' ORDER BY t.id""", (identifier,))]

    def _settlement_values(self, values: dict[str, Any]) -> dict[str, Any]:
        values = {"files": [], "report": None, "to": "all", **values}
        if set(values) != {"outcome", "summary", "files", "report", "to"}:
            raise BlackboardError("Settlement requires an outcome and summary.")
        if values["outcome"] not in OUTCOMES:
            raise BlackboardError("Settlement outcome must be succeeded or failed.")
        if not isinstance(values["summary"], str) or not values["summary"].strip():
            raise BlackboardError("Settlement summary must not be empty.")
        if not isinstance(values["files"], list) or not all(isinstance(path, str) for path in values["files"]):
            raise BlackboardError("Settlement files must be a list of paths.")
        for label, value in [*(("file", path) for path in values["files"]), ("report", values["report"]), ("recipient", values["to"])]:
            if value is not None:
                self.board._single_line(label, value)
        return values

    @staticmethod
    def _current(connection: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any] | None:
        return current_settlement(task, latest_settlements(connection, task["id"]))

    def create(self, actor: str, values: dict[str, Any], guard: Any = None) -> dict[str, Any]:
        if set(values) - set(FIELDS):
            raise BlackboardError("Unknown task fields.")
        self._fields(values)
        now = self.board._timestamp()
        task = {"id": uuid4().hex, "title": "", "description": "", "assignee": None, "priority": 2,
                "repo": None, "member": None, "plan": None, **values, "state": "open", "created_at": now, "updated_at": now, "version": 1}
        self._fields(task)
        with self.board.transaction(actor, guard) as connection:
            self._references(task, values)
            connection.execute("""INSERT INTO tasks VALUES(:id,:title,:description,:assignee,:priority,:repo,:member,:plan,
                :state,:created_at,:updated_at,:version)""", task)
            sequence = self.board.append_event(connection, "task", task["id"], None, self._snapshot(connection, task), actor)
        return {**task, "sequence": sequence}

    def mutate(self, actor: str, identifier: str, expected_version: int, action: str,
               values: dict[str, Any] | None = None, guard: Any = None) -> dict[str, Any]:
        values = values or {}
        if action == "settle":
            values = self._settlement_values(values)
        else:
            self._fields(values)
        with self.board.transaction(actor, guard) as connection:
            before = self._snapshot(connection, self._row(connection, identifier))
            after = {key: value for key, value in before.items() if key != "prerequisites"}
            settlement = self._current(connection, before) if action in ("claim", "settle", "update") else None
            replay = {"agent": actor, "task_version": expected_version + 1, **{key: values.get(key) for key in ("outcome", "summary", "files", "report")}}
            if action == "settle" and settlement is not None and replay.items() <= settlement.items():
                # Replay of the settle that produced the current version, e.g. after a lost response.
                return {**after, "settlement": settlement, "replayed": True}
            if before["version"] != expected_version:
                raise BlackboardError("Task version is stale.")
            if action == "update":
                if set(values) - (set(FIELDS) - {"assignee"}):
                    raise BlackboardError("Update cannot change task state or assignment.")
                if is_held(settlement):
                    raise BlackboardError("Task is held after a failed settlement; assign or cancel it first.")
                after.update(values)
                self._references(after, values)
            elif action == "claim":
                if before["state"] != "open" or before["assignee"] not in (None, actor):
                    raise BlackboardError("Task is not open or is assigned to another agent.")
                if self._blockers(connection, identifier):
                    raise BlackboardError("Task has unfinished prerequisites.")
                if is_held(settlement):
                    raise BlackboardError("Task is held after a failed settlement; the parent must assign it before another claim.")
                after.update(state="in_progress", assignee=actor)
            elif action in ("release", "done", "settle"):
                if before["state"] != "in_progress" or before["assignee"] != actor:
                    raise BlackboardError("Task must be in progress and assigned to this agent.")
                succeeded = action == "done" or action == "settle" and values["outcome"] == "succeeded"
                if succeeded and self._blockers(connection, identifier):
                    raise BlackboardError("Task has unfinished prerequisites.")
                # A failed settlement reopens the task but keeps its assignee, holding it for the parent.
                after.update(state="done" if succeeded else "open")
                if action == "release":
                    after["assignee"] = None
            elif action in ("assign", "cancel"):
                if before["state"] not in ("open", "in_progress"):
                    raise BlackboardError("Task is terminal.")
                if action == "cancel":
                    after["state"] = "cancelled"
                else:
                    after.update(assignee=values.get("assignee"), state="open")
            else:
                raise BlackboardError("Unknown task action.")
            after.update(version=expected_version + 1, updated_at=self.board._timestamp())
            connection.execute("""UPDATE tasks SET title=:title,description=:description,assignee=:assignee,priority=:priority,
                repo=:repo,member=:member,plan=:plan,state=:state,updated_at=:updated_at,version=:version WHERE id=:id""", after)
            sequence = self.board.append_event(connection, "task", identifier, before, self._snapshot(connection, after), actor)
            if action == "settle":
                body = settlement_body(identifier, actor, values["outcome"], values["summary"], values["files"],
                                       values["report"], after["version"])
                entry = self.board.new_entry(actor, body, values["to"], SETTLEMENT, None, identifier)
                sequence = self.board.insert_entry(connection, entry)
                return {**after, "sequence": sequence, "settlement": settlement_report(entry, json.loads(body))}
        return {**after, "sequence": sequence}

    def dependency(self, actor: str, identifier: str, prerequisite: str, expected_version: int,
                   remove: bool = False, guard: Any = None) -> dict[str, Any]:
        with self.board.transaction(actor, guard) as connection:
            before = self._snapshot(connection, self._row(connection, identifier))
            self._row(connection, prerequisite)
            if before["version"] != expected_version:
                raise BlackboardError("Task version is stale.")
            if before["state"] != "open":
                raise BlackboardError("Release active task before changing dependencies; terminal edges are immutable.")
            if is_held(self._current(connection, before)):
                raise BlackboardError("Task is held after a failed settlement; assign or cancel it first.")
            exists = prerequisite in before["prerequisites"]
            if exists == (not remove):
                return {**before, "changed": False}
            if not remove:
                if identifier == prerequisite or connection.execute("""WITH RECURSIVE walk(id) AS (
                    SELECT ? UNION SELECT d.prerequisite_id FROM task_dependencies d JOIN walk ON d.task_id=walk.id)
                    SELECT 1 FROM walk WHERE id=?""", (prerequisite, identifier)).fetchone():
                    raise BlackboardError("Task dependency would create a cycle.")
                connection.execute("INSERT INTO task_dependencies VALUES(?,?)", (identifier, prerequisite))
            else:
                connection.execute("DELETE FROM task_dependencies WHERE task_id=? AND prerequisite_id=?", (identifier, prerequisite))
            after = {**before, "version": expected_version + 1, "updated_at": self.board._timestamp()}
            connection.execute("UPDATE tasks SET version=?,updated_at=? WHERE id=?", (after["version"], after["updated_at"], identifier))
            after = self._snapshot(connection, self._row(connection, identifier))
            sequence = self.board.append_event(connection, "task", identifier, before, after, actor)
        return {**after, "changed": True, "sequence": sequence}

    def creator(self, identifier: str) -> str:
        with self.board._locked(read_only=True) as handle:
            self.board._verified(handle, ("active", "finalized"))
            with contextlib.closing(self.board._connect(readonly=True)) as connection:
                self.board._metadata(connection)
                row = connection.execute("SELECT actor FROM events WHERE kind='task' AND entity_id=? AND before_json IS NULL", (identifier,)).fetchone()
        if row is None:
            raise BlackboardError("Task does not exist in this project.")
        return str(row[0])

    def read(self, identifier: str | None = None, *, actor: str | None = None, ready: bool = False,
             blocked: bool = False, held: bool = False, **filters: Any) -> dict[str, Any]:
        with self.board._locked(read_only=True) as handle:
            self.board._verified(handle, ("active", "finalized"))
            with contextlib.closing(self.board._connect(readonly=True)) as connection:
                connection.execute("BEGIN")
                self.board._metadata(connection)
                data = task_data(connection)
                settlements = latest_settlements(connection, identifier)
                by_id = {task["id"]: task for task in data["tasks"]}
                if identifier is not None and identifier not in by_id:
                    raise BlackboardError("Task does not exist in this project.")
                dependencies = group_dependencies(data["task_dependencies"])
                tasks = [by_id[identifier]] if identifier is not None else data["tasks"]
                result = []
                for task in tasks:
                    prerequisites = dependencies.get(task["id"], [])
                    blockers = [{"id": key, "state": by_id[key]["state"]} for key in prerequisites if by_id[key]["state"] != "done"]
                    eligible = actor is None or task["assignee"] in (None, actor)
                    settlement = current_settlement(task, settlements)
                    task_held = is_held(settlement)
                    is_ready = task["state"] == "open" and not blockers and not task_held
                    if any(value is not None and task.get(key) != value for key, value in filters.items()):
                        continue
                    if (ready and not (is_ready and eligible) or blocked and not (blockers and task["state"] in ("open", "in_progress"))
                            or held and not task_held):
                        continue
                    result.append({**task, "prerequisites": prerequisites, "blockers": blockers,
                                   "ready": is_ready, "assignment_eligible": eligible, "settlement": settlement, "held": task_held})
                return {"json_version": 1, "generation": self.board.generation, "tasks": result}
