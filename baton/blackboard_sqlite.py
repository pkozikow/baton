"""Transactional project notes and portable, deterministic evidence snapshots."""

import base64
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator
from uuid import uuid4

from .manifest import render_coordination_context

from blackboard import Blackboard, BlackboardError, revision_of

SCHEMA_VERSION = 2
EXPORT_VERSION = 2
SETTLEMENT = "settlement"
OUTCOMES = ("succeeded", "failed")
CURSOR_ERROR = "Invalid/foreign blackboard cursor; read again without --since."
SETTLEMENT_FIELDS = {"baton", "v", "task", "agent", "outcome", "summary", "files", "report", "task_version"}


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def settlement_body(task: str, agent: str, outcome: str, summary: str, files: list[str],
                    report: str | None, task_version: int) -> str:
    return encoded({"baton": SETTLEMENT, "v": 1, "task": task, "agent": agent, "outcome": outcome,
                    "summary": summary, "files": files, "report": report, "task_version": task_version})


def parse_settlement(entry: dict[str, Any]) -> dict[str, Any] | None:
    # Only the exact canonical shape written by task settle counts; legacy or
    # malformed settlement-kind entries remain plain entries everywhere.
    if entry.get("kind") != SETTLEMENT:
        return None
    try:
        body = json.loads(entry["body"])
    except (TypeError, ValueError):
        return None
    if (not isinstance(body, dict) or set(body) != SETTLEMENT_FIELDS or body["baton"] != SETTLEMENT
            or type(body["v"]) is not int or body["v"] != 1 or body["outcome"] not in OUTCOMES
            or not isinstance(body["summary"], str) or not body["summary"].strip()
            or not isinstance(body["files"], list) or not all(isinstance(path, str) and path for path in body["files"])
            or not (body["report"] is None or isinstance(body["report"], str) and body["report"])
            or type(body["task_version"]) is not int or body["task_version"] < 1
            or body["task"] != entry.get("task_id") or body["agent"] != entry.get("author")
            or entry["body"] != encoded(body)):
        return None
    return body


def settlement_report(entry: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return {"entry_id": entry["id"], "created_at": entry["created_at"],
            **{key: body[key] for key in ("agent", "outcome", "summary", "files", "report", "task_version")}}


def current_settlement(task: dict[str, Any], settlements: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    # A settlement describes a task only while it produced the task's current version.
    settlement = settlements.get(task["id"])
    return settlement if settlement is not None and settlement["task_version"] == task["version"] else None


def is_held(settlement: dict[str, Any] | None) -> bool:
    # A current failed settlement holds the task: claim, update and dependency changes
    # are refused, so only the parent's assign or cancel moves it past that version.
    return settlement is not None and settlement["outcome"] == "failed"


def settlements_by_task(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # Entries must be in creation order: the last valid settlement per task wins.
    result = {}
    for entry in entries:
        body = parse_settlement(entry)
        if body is not None:
            result[entry["task_id"]] = settlement_report(entry, body)
    return result


def latest_settlements(connection: sqlite3.Connection, task_id: str | None = None) -> dict[str, dict[str, Any]]:
    query = "SELECT * FROM entries WHERE kind=?" + (" AND task_id=?" if task_id is not None else "") + " ORDER BY rowid"
    return settlements_by_task(dict(row) for row in connection.execute(query, (SETTLEMENT,) if task_id is None else (SETTLEMENT, task_id)))


def initial_overview(project: str) -> str:
    return (f"# Project blackboard: {project}\n\n"
            "Current-state summary, maintained by the parent. Replace this text at each major handoff; keep it "
            "short and link the master plan rather than copying it.\n\n"
            "## Goal\n\nOne or two sentences.\n\n"
            "## Phase and status\n\nCurrent phase, latest accepted result and candidate SHA.\n\n"
            "## Owners and assignments\n\nAgent → task ID → scope.\n\n"
            "## Blockers and open questions\n\nEntry or task IDs awaiting an answer, retry or decision; \"none\" when clear.\n\n"
            "## Next steps\n\nWhat happens next and what gates it.\n\n"
            "## Pointers\n\nMaster plan, decisions and reports.\n")


def entry_text(entry: dict[str, Any]) -> str:
    body = parse_settlement(entry)
    if body is None:
        return entry["body"]
    text = f"Settlement: {body['outcome']} (task {body['task']}, version {body['task_version']})\n\n{body['summary']}"
    if body["files"]:
        text += "\n\nFiles: " + ", ".join(body["files"])
    if body["report"]:
        text += f"\n\nReport: {body['report']}"
    return text


class SQLiteBlackboard(Blackboard):
    def __init__(self, directory: Path, project: str, generation: str, project_uuid: str) -> None:
        super().__init__(directory, project, generation)
        self.project_uuid = project_uuid
        self._database_name = "blackboard.sqlite"

    @property
    def path(self) -> Path:
        return self.directory / self._database_name

    def _safe_database(self, missing: bool = False) -> None:
        self._directory(self.directory)
        for suffix in ("", "-wal", "-shm", "-journal"):
            self._regular(Path(str(self.path) + suffix), missing=missing or bool(suffix))

    def reject_sidecars(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            if self._regular(Path(str(self.path) + suffix), missing=True) is not None:
                raise BlackboardError("SQLite destination has sidecars; recover or move them before publishing a database.")

    def _connect(self, *, create: bool = False, readonly: bool = False) -> sqlite3.Connection:
        self._safe_database(missing=create)
        before = self._regular(self.path, missing=create)
        connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        after = self._regular(self.path)
        if before and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            connection.close()
            raise BlackboardError("Blackboard database identity changed while opening.")
        self._safe_database()
        return connection

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, Any]:
        try:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as error:
            raise BlackboardError(f"Invalid blackboard database: {error}") from error
        if version != SCHEMA_VERSION:
            raise BlackboardError(f"Unsupported blackboard schema {version}; client supports {SCHEMA_VERSION}.")
        if metadata.get("uuid") != self.project_uuid or metadata.get("generation") != self.generation:
            raise BlackboardError("Blackboard UUID/generation does not match this project session.")
        return metadata

    @staticmethod
    def ensure_schema(connection: sqlite3.Connection) -> None:
        # Called only by explicit initialization, never by readers or ordinary writers.
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,actor TEXT NOT NULL,time TEXT NOT NULL,
                before_json TEXT,after_json TEXT NOT NULL);
            CREATE TABLE entries(id TEXT PRIMARY KEY,author TEXT NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,recipient TEXT NOT NULL,kind TEXT NOT NULL,
                parent_id TEXT REFERENCES entries(id),task_id TEXT,body TEXT NOT NULL,version INTEGER NOT NULL);
            CREATE TABLE overview(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,version INTEGER NOT NULL);
            -- Indexes are optional accelerators: schema 2 boards created without them stay valid.
            CREATE INDEX entry_parent ON entries(parent_id);
            CREATE INDEX event_entity ON events(entity_id);
            PRAGMA user_version=2;
        """)

        from .tasks import create_schema
        create_schema(connection)

    def append_event(self, connection: sqlite3.Connection, kind: str, entity_id: str,
                     before: Any, after: Any, actor: str) -> int:
        cursor = connection.execute("INSERT INTO events(kind,entity_id,actor,time,before_json,after_json) VALUES(?,?,?,?,?,?)",
                                    (kind, entity_id, actor, self._timestamp(), None if before is None else encoded(before), encoded(after)))
        return int(cursor.lastrowid)

    def _empty_initialization(self, connection: sqlite3.Connection) -> bool:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"metadata", "events", "entries", "overview", "sqlite_sequence", "tasks", "task_dependencies"}
        return connection.execute("PRAGMA user_version").fetchone()[0] in (0, SCHEMA_VERSION) and tables <= expected and all(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                                         for table in tables)

    def validate_initialization(self) -> None:
        self._directory(self.directory)
        if self._regular(self.directory / "blackboard-lock.md", missing=True) is None:
            if self._regular(self.path, missing=True) is not None:
                raise BlackboardError("Existing SQLite database has no initialization journal.")
            return
        with self._locked(read_only=True) as handle:
            marker = self._verified(handle, ("initializing", "active", "finalized"))
            if self._regular(self.path, missing=True) is None:
                if marker["status"] != "initializing":
                    raise BlackboardError("Active SQLite blackboard is missing its database.")
                return
            with contextlib.closing(self._connect(readonly=True)) as connection:
                if marker["status"] == "initializing" and self._empty_initialization(connection):
                    return
                self._metadata(connection)

    def initialize(self, *, overview: str | None = None) -> dict[str, Any]:
        with self._locked(create=True) as handle:
            if self._regular(self.path, missing=True) is None:
                self.reject_sidecars()
            marker = self._marker(handle)
            if marker and (marker["project"], marker["generation"]) != (self.project, self.generation):
                raise BlackboardError("Another blackboard generation owns this initialization journal.")
            if self._regular(self.path, missing=True) is not None:
                if marker is None:
                    raise BlackboardError("Existing SQLite database has no initialization journal.")
                with contextlib.closing(self._connect()) as connection:
                    empty = marker["status"] == "initializing" and self._empty_initialization(connection)
                    if not empty:
                        self._metadata(connection)
                if not empty:
                    if marker["status"] == "initializing":
                        self._record(handle, {"project": self.project, "generation": self.generation, "status": "active", "backend": "sqlite"})
                    return self.read_unlocked()
                for suffix in ("", "-wal", "-shm", "-journal"):
                    path = Path(str(self.path) + suffix)
                    if self._regular(path, missing=True) is not None:
                        path.unlink()
            if marker and marker["status"] != "initializing":
                raise BlackboardError("Initialized blackboard is missing its database; restore it before retrying.")
            descriptor, temporary = tempfile.mkstemp(prefix=".blackboard-init-", suffix=".tmp", dir=self.directory)
            os.close(descriptor)
            staged = SQLiteBlackboard(self.directory, self.project, self.generation, self.project_uuid)
            staged._database_name = Path(temporary).name
            self._record(handle, {"project": self.project, "generation": self.generation, "status": "initializing", "initialization_file": staged._database_name})
            try:
                with contextlib.closing(staged._connect(create=True)) as connection:
                    self.ensure_schema(connection)
                    connection.executemany("INSERT INTO metadata VALUES(?,?)", [("uuid", self.project_uuid), ("generation", self.generation),
                        ("project", self.project), ("status", "active")])
                    body = overview if overview is not None else initial_overview(self.project)
                    connection.execute("INSERT INTO overview VALUES(1,?,1)", (body,))
                    self.append_event(connection, "overview", "overview", None, {"body": body, "version": 1}, "framework")
                    connection.commit()
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                os.replace(staged.path, self.path)
                self._sync_directory()
            finally:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    path = Path(str(staged.path) + suffix)
                    if self._regular(path, missing=True) is not None:
                        path.unlink()
            self._record(handle, {"project": self.project, "generation": self.generation, "status": "active", "backend": "sqlite"})
            with contextlib.closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL").fetchone()
            return self.read_unlocked()

    @contextlib.contextmanager
    def transaction(self, agent: str, guard: Callable[[], Any] | None = None) -> Iterator[sqlite3.Connection]:
        self._single_line("agent", agent)
        with self._locked() as handle, (guard() if guard else contextlib.nullcontext()):
            self._verified(handle, ("active",))
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    metadata = self._metadata(connection)
                    if metadata["status"] != "active":
                        raise BlackboardError("Blackboard updates are frozen.")
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def schema_version(self) -> int:
        with self._locked(read_only=True), contextlib.closing(self._connect(readonly=True)) as connection:
            self._metadata(connection)
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def new_entry(self, agent: str, text: str, to: str, kind: str, parent_id: str | None, task_id: str | None) -> dict[str, Any]:
        self._single_line("recipient", to)
        self._single_line("kind", kind)
        if not text.strip():
            raise BlackboardError("Blackboard message must not be empty.")
        return {"id": uuid4().hex, "author": agent, "created_at": self._timestamp(), "updated_at": self._timestamp(),
                "recipient": to, "kind": kind, "parent_id": parent_id, "task_id": task_id, "body": text, "version": 1}

    def insert_entry(self, connection: sqlite3.Connection, entry: dict[str, Any]) -> int:
        if entry["task_id"] is not None:
            if not connection.execute("SELECT 1 FROM tasks WHERE id=?", (entry["task_id"],)).fetchone():
                raise BlackboardError("Task does not exist in this board.")
        if entry["parent_id"] is not None and not connection.execute("SELECT 1 FROM entries WHERE id=?", (entry["parent_id"],)).fetchone():
            raise BlackboardError("Reply parent does not exist in this board.")
        connection.execute("INSERT INTO entries VALUES(:id,:author,:created_at,:updated_at,:recipient,:kind,:parent_id,:task_id,:body,:version)", entry)
        return self.append_event(connection, "entry", entry["id"], None, entry, entry["author"])

    def post(self, agent: str, text: str, to: str = "all", *, guard: Any = None,
             kind: str = "note", parent_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        if kind == SETTLEMENT:
            raise BlackboardError("The settlement kind is reserved for baton task settle.")
        entry = self.new_entry(agent, text, to, kind, parent_id, task_id)
        with self.transaction(agent, guard) as connection:
            seq = self.insert_entry(connection, entry)
        return {"id": entry["id"], "version": 1, "sequence": seq, "generation": self.generation}

    def update(self, agent: str, identifier: str, expected_version: int, text: str, *, kind: str | None = None, guard: Any = None) -> dict[str, Any]:
        if not text.strip():
            raise BlackboardError("Blackboard message must not be empty.")
        if kind is not None:
            self._single_line("kind", kind)
        with self.transaction(agent, guard) as connection:
            row = connection.execute("SELECT * FROM entries WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise BlackboardError("Blackboard entry does not exist.")
            before = dict(row)
            if before["version"] != expected_version:
                raise BlackboardError("Blackboard entry version is stale.")
            after = {**before, "body": text, "kind": kind or before["kind"], "version": expected_version + 1, "updated_at": self._timestamp()}
            if parse_settlement(before) is not None or parse_settlement(after) is not None:
                raise BlackboardError("Entries cannot be changed to or from a task settlement.")
            connection.execute("UPDATE entries SET body=:body,kind=:kind,version=:version,updated_at=:updated_at WHERE id=:id", after)
            seq = self.append_event(connection, "entry", identifier, before, after, agent)
        return {"id": identifier, "version": after["version"], "sequence": seq}

    def edit(self, agent: str, expected_revision: str | None, text: str, *, guard: Any = None) -> dict[str, Any]:
        if not text.strip():
            raise BlackboardError("Blackboard overview must not be empty.")
        with self.transaction(agent, guard) as connection:
            before = dict(connection.execute("SELECT body,version FROM overview WHERE id=1").fetchone())
            if expected_revision != revision_of(encoded(before).encode()):
                raise BlackboardError("Blackboard overview revision is stale; reread it with read --tail 0 --json and reconcile.")
            after = {"body": text, "version": before["version"] + 1}
            connection.execute("UPDATE overview SET body=?,version=? WHERE id=1", (text, after["version"]))
            seq = self.append_event(connection, "overview", "overview", before, after, agent)
        return {"revision": revision_of(encoded(after).encode()), "version": after["version"], "sequence": seq}

    def snapshot_data(self, connection: sqlite3.Connection) -> dict[str, Any]:
        metadata = self._metadata(connection)
        data = {"metadata": metadata, "overview": dict(connection.execute("SELECT body,version FROM overview WHERE id=1").fetchone()),
                "entries": [dict(row) for row in connection.execute("SELECT * FROM entries ORDER BY rowid")],
                "events": [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY seq")]}
        from .tasks import task_data
        data.update(task_data(connection))
        return data

    def read_unlocked(self, *, to: str | None = None, tail: int | None = None, since: str | None = None, limit: int | None = None) -> dict[str, Any]:
        if to is not None:
            self._single_line("recipient", to)
        if tail is not None and (type(tail) is not int or tail < 0):
            raise BlackboardError("Blackboard tail must be nonnegative.")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise BlackboardError("Blackboard limit must be positive.")
        if tail is not None and (since is not None or limit is not None):
            raise BlackboardError("--tail cannot be combined with --since or --limit.")
        with contextlib.closing(self._connect(readonly=True)) as connection:
            connection.execute("BEGIN")
            data = self.snapshot_data(connection)
        events = data["events"]
        last = events[-1]["seq"] if events else 0
        start = 0
        if since is not None:
            cursor = self.decode_cursor(since)
            if cursor["seq"] > last or cursor["to"] not in (None, to):
                raise BlackboardError(CURSOR_ERROR)
            start = cursor["seq"]
        def matches(event: dict[str, Any]) -> bool:
            if to is None:
                return True
            if event["kind"] != "entry":
                return False
            return json.loads(event["after_json"]).get("recipient") in (to, "all")
        selected = []
        boundary = last
        for event in events:
            if event["seq"] <= start or not matches(event):
                continue
            if limit is not None and len(selected) == limit:
                boundary = event["seq"] - 1
                break
            selected.append(event)
        entries = [entry for entry in data["entries"] if to is None or entry["recipient"] in (to, "all")]
        filtered = to is not None or tail is not None or since is not None or limit is not None
        if tail is not None:
            entries = entries[-tail:] if tail else []
            ids = {entry["id"] for entry in entries}
            selected = [event for event in selected if event["kind"] == "entry" and event["entity_id"] in ids]
        elif since is not None or limit is not None:
            ids = {event["entity_id"] for event in selected if event["kind"] == "entry"}
            entries = [entry for entry in entries if entry["id"] in ids]
        text = (data["overview"]["body"].rstrip() + "\n\n" if not filtered else "") + "\n\n".join(self.entry_markdown(entry) for entry in entries)
        token = self.encode_cursor(boundary, to)
        return {"json_version": 1, "project": self.project, "backend": "sqlite", "generation": self.generation, "uuid": self.project_uuid,
                "status": data["metadata"]["status"], "path": str(self.path), "directory": str(self.directory),
                "revision": revision_of(encoded(data["overview"]).encode()), "overview": data["overview"],
                "entries": entries, "events": selected, "sequence": last, "cursor": token, "text": text}

    def read(self, **kwargs: Any) -> dict[str, Any]:
        with self._locked(read_only=True) as handle:
            self._verified(handle, ("active", "finalized"))
            return self.read_unlocked(**kwargs)

    def encode_cursor(self, seq: int, to: str | None) -> str:
        return base64.urlsafe_b64encode(encoded({"v": 1, "uuid": self.project_uuid, "generation": self.generation, "to": to, "seq": seq}).encode()).decode()

    def decode_cursor(self, token: str) -> dict[str, Any]:
        try:
            cursor = json.loads(base64.urlsafe_b64decode(token))
            if not isinstance(cursor, dict) or set(cursor) != {"v", "uuid", "generation", "to", "seq"}:
                raise ValueError()
            if (type(cursor["v"]) is not int or cursor["v"] != 1 or cursor["uuid"] != self.project_uuid
                    or cursor["generation"] != self.generation or type(cursor["seq"]) is not int or cursor["seq"] < 0):
                raise ValueError()
            return cursor
        except (ValueError, KeyError, TypeError) as error:
            raise BlackboardError(CURSOR_ERROR) from error

    @staticmethod
    def _wait_limits(timeout: float, poll_interval: float) -> None:
        if not 0 <= timeout < float("inf") or not 0 < poll_interval < float("inf"):
            raise BlackboardError("Wait timeout must be nonnegative and poll interval positive.")

    def _poll(self, start: int | None, reply_to: str | None, to: str | None,
              kinds: list[str] | None, task_id: str | None) -> tuple[list[dict[str, Any]], int]:
        # Hold the shared lock only for one targeted read; never while sleeping.
        with self._locked(read_only=True) as handle:
            self._verified(handle, ("active",))
            with contextlib.closing(self._connect(readonly=True)) as connection:
                connection.execute("BEGIN")
                if self._metadata(connection)["status"] != "active":
                    raise BlackboardError("Blackboard updates are frozen.")
                head = connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                if reply_to is not None:
                    if not connection.execute("SELECT 1 FROM entries WHERE id=?", (reply_to,)).fetchone():
                        raise BlackboardError("Reply parent does not exist in this board.")
                    condition, parameter = "en.parent_id=?", reply_to
                else:
                    assert start is not None
                    if start > head:
                        raise BlackboardError(CURSOR_ERROR)
                    condition, parameter = "ev.seq>?", start
                entries = []
                # Filters match the immutable creation snapshot, so later updates are never re-reported.
                for row in connection.execute(f"""SELECT ev.seq AS created_seq,ev.after_json AS created,en.* FROM events ev
                        JOIN entries en ON en.id=ev.entity_id WHERE ev.kind='entry' AND ev.before_json IS NULL AND {condition}
                        ORDER BY ev.seq""", (parameter,)):
                    entry = dict(row)
                    sequence, created = entry.pop("created_seq"), json.loads(entry.pop("created"))
                    if ((to is None or created["recipient"] in (to, "all")) and (not kinds or created["kind"] in kinds)
                            and (task_id is None or created["task_id"] == task_id)):
                        entries.append({**entry, "sequence": sequence})
                return entries, head

    def wait(self, *, since: str | None = None, reply_to: str | None = None, to: str | None = None,
             kinds: list[str] | None = None, task_id: str | None = None,
             timeout: float = 90, poll_interval: float = 1) -> dict[str, Any]:
        if (since is None) == (reply_to is None):
            raise BlackboardError("Wait requires exactly one of --since CURSOR or --reply-to ID.")
        if reply_to is not None and (to is not None or kinds or task_id is not None):
            raise BlackboardError("--reply-to cannot be combined with --to, --kind or --task.")
        for label, value in (("recipient", to), *(("kind", kind) for kind in kinds or [])):
            if value is not None:
                self._single_line(label, value)
        self._wait_limits(timeout, poll_interval)
        start = None if since is None else self.decode_cursor(since)["seq"]
        deadline = time.monotonic() + timeout
        while True:
            entries, head = self._poll(start, reply_to, to, kinds, task_id)
            remaining = deadline - time.monotonic()
            if entries or remaining <= 0:
                break
            start = None if start is None else head
            time.sleep(min(poll_interval, remaining))
        return {"json_version": 1, "generation": self.generation, "timed_out": not entries, "reply_to": reply_to,
                "sequence": head, "cursor": self.encode_cursor(head, None), "entries": entries,
                "text": "\n\n".join(self.entry_markdown(entry) for entry in entries)}

    def ask(self, agent: str, to: str, text: str, *, task_id: str | None = None, guard: Any = None,
            timeout: float = 90, poll_interval: float = 1,
            posted: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        self._wait_limits(timeout, poll_interval)
        question = self.post(agent, text, to, guard=guard, kind="question", task_id=task_id)
        if posted is not None:
            posted(question)
        return {**self.wait(reply_to=question["id"], timeout=timeout, poll_interval=poll_interval), "question": question}

    def agents(self, quiet_after: float = 900) -> dict[str, Any]:
        if not 0 <= quiet_after < float("inf"):
            raise BlackboardError("--quiet-after must be nonnegative.")
        with self._locked(read_only=True) as handle:
            self._verified(handle, ("active", "finalized"))
            with contextlib.closing(self._connect(readonly=True)) as connection:
                connection.execute("BEGIN")
                self._metadata(connection)
                # Initialization, sealing and renames are recorded as the framework, not an agent.
                writes = connection.execute("""SELECT e.actor,e.seq,e.time,e.kind,e.after_json FROM events e
                    JOIN (SELECT MAX(seq) AS seq FROM events WHERE actor!='framework' GROUP BY actor) l ON e.seq=l.seq ORDER BY e.actor""").fetchall()
                tasks = {row["id"]: dict(row) for row in connection.execute("SELECT id,title,state,assignee,version FROM tasks")}
                settlements = latest_settlements(connection)
                # NOT IN evaluates the unindexed parent_id scan once rather than once per question.
                questions = connection.execute("""SELECT id,author,recipient,created_at,task_id FROM entries WHERE kind='question'
                    AND id NOT IN (SELECT parent_id FROM entries WHERE parent_id IS NOT NULL) ORDER BY rowid""").fetchall()
        now = datetime.now(timezone.utc)
        agents = []
        for actor, seq, written, kind, after in writes:
            idle = (now - datetime.fromisoformat(written)).total_seconds()
            in_progress = [{"id": task["id"], "title": task["title"], "version": task["version"]} for task in tasks.values()
                           if task["state"] == "in_progress" and task["assignee"] == actor]
            settled = [{"task_id": identifier, **settlement} for identifier, settlement in settlements.items()
                       if settlement["agent"] == actor and current_settlement(tasks[identifier], settlements) is settlement]
            waiting = [{"id": row["id"], "to": row["recipient"], "created_at": row["created_at"], "task_id": row["task_id"]}
                       for row in questions if row["author"] == actor]
            # Advisory only: Baton cannot observe processes, so silence never means exited.
            verdict = ("waiting" if waiting else "settled" if settled and not in_progress
                       else "active" if idle < quiet_after else "quiet")
            agents.append({"agent": actor, "verdict": verdict, "idle_seconds": int(idle),
                           "last_write": {"time": written, "sequence": seq, "event": kind,
                                          "kind": json.loads(after)["kind"] if kind == "entry" else None},
                           "in_progress": in_progress, "settlements": settled, "unanswered_questions": waiting})
        return {"json_version": 1, "generation": self.generation, "now": now.isoformat(timespec="seconds"),
                "quiet_after": quiet_after, "agents": agents}

    @staticmethod
    def entry_markdown(entry: dict[str, Any]) -> str:
        return (f"## {entry['created_at']} | {entry['author']} → {entry['recipient']}\n"
                f"ID: {entry['id']} | kind: {entry['kind']} | version: {entry['version']} | parent: {entry['parent_id'] or '-'} | task: {entry['task_id'] or '-'}\n\n{entry_text(entry)}")

    def render_snapshot(self, data: dict[str, Any]) -> tuple[str, str]:
        metadata = data["metadata"]
        heading = f"# Coordination: {metadata['project']}\n\nUUID: {metadata['uuid']} | generation: {metadata['generation']} | state: {metadata['status']}\n\n"
        if data.get("context"):
            heading += render_coordination_context(data["context"])
        overview = heading + data["overview"]["body"] + "\n\n" + "\n\n".join(self.entry_markdown(row) for row in data["entries"]) + "\n"
        history = heading + "# Complete history\n\n"
        for event in data["events"]:
            history += f"## Event {event['seq']}: {event['kind']} / {event['entity_id']}\n\n{event['time']} | {event['actor']}\n\n"
            for label, payload in (("Before", event["before_json"]), ("After", event["after_json"])):
                if payload is not None:
                    value = json.loads(payload)
                    history += f"### {label}\n\n"
                    if isinstance(value, dict):
                        history += (entry_text(value) if event["kind"] == "entry" else value.get("body", value.get("description", ""))) + "\n\n"
                    history += "    " + encoded(value).replace("\n", "\n    ") + "\n\n"
        from .tasks import group_dependencies
        tasks = {task["id"]: task for task in data["tasks"]}
        dependencies = group_dependencies(data["task_dependencies"])
        # Derived only from the snapshot (no clock or live reads) so rendering stays byte-deterministic.
        settlements = settlements_by_task(data["entries"])
        replied = {entry["parent_id"] for entry in data["entries"] if entry["parent_id"] is not None}
        unanswered = [entry for entry in data["entries"] if entry["kind"] == "question" and entry["id"] not in replied]
        overview += "\n# Tasks and unresolved work\n\n"
        for task in data["tasks"]:
            prerequisites = dependencies.get(task["id"], [])
            blockers = [f"{identifier} ({tasks[identifier]['state']})" for identifier in prerequisites if tasks[identifier]["state"] != "done"]
            held = "yes" if is_held(current_settlement(task, settlements)) else "no"
            overview += (f"## {task['title']}\n\nID: {task['id']} | state: {task['state']} | held: {held} | assignee: {task['assignee'] or '-'} | priority: {task['priority']} | version: {task['version']}\n\n"
                         f"Repository: {task['repo'] or '-'} | member: {task['member'] or '-'} | plan: {task['plan'] or '-'}\n\n"
                         f"Prerequisites: {', '.join(prerequisites) or '-'}\n\nBlockers: {', '.join(blockers) or '-'}\n\n{task['description']}\n\n")
        overview += "## Unanswered questions\n\n" + ("".join(
            f"- {entry['id']} | {entry['created_at']} | {entry['author']} → {entry['recipient']} | task: {entry['task_id'] or '-'} | "
            f"{(entry['body'].strip().splitlines() or [''])[0]}\n" for entry in unanswered) or "None.\n")
        return overview, history

    def freeze(self, guard: Any = None) -> None:
        with self._locked() as handle, (guard() if guard else contextlib.nullcontext()):
            self._verified(handle, ("active", "finalized"))
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                metadata = self._metadata(connection)
                if metadata["status"] == "active":
                    connection.execute("UPDATE metadata SET value='sealed' WHERE key='status'")
                    self.append_event(connection, "sealed", "board", {"status": "active"}, {"status": "sealed"}, "framework")
                connection.commit()
            self._record(handle, {"project": self.project, "generation": self.generation, "status": "finalized", "backend": "sqlite", "operation": "cleanup"})

    def export(self, destination: Path, *, replace_sealed: bool = False, context: dict[str, Any] | None = None) -> dict[str, Any]:
        destination = destination.absolute()
        self._directory(destination, create=True)
        destination = destination.resolve()
        if destination == self.directory.resolve():
            raise BlackboardError("Export destination must differ from the live board directory.")
        live = destination / "blackboard.sqlite"
        if live.exists() or live.is_symlink():
            raise BlackboardError("Export destination contains live coordination state.")
        schema_version = self.schema_version()
        output = Blackboard(destination, self.project, self.generation)
        with output._locked(create=True) as handle:
            publication = output._marker(handle)
            if publication is not None and ((publication.get("operation"), publication.get("uuid"), publication["generation"]) != ("sqlite-export", self.project_uuid, self.generation) or publication.get("schema_version") != SCHEMA_VERSION):
                raise BlackboardError("Export destination has another coordination journal.")
            return self._export_locked(destination, replace_sealed=replace_sealed, context=context,
                                       output=output, handle=handle, publication=publication, schema_version=schema_version)

    @staticmethod
    def logical_digest(data: dict[str, Any]) -> str:
        return revision_of(encoded({key: value for key, value in data.items() if key != "context"}).encode())

    def _export_locked(self, destination: Path, *, replace_sealed: bool, context: dict[str, Any] | None,
                       output: Blackboard, handle: Any, publication: dict[str, Any] | None, schema_version: int) -> dict[str, Any]:
        self._directory(destination)
        for suffix in ("-wal", "-shm", "-journal"):
            if self._regular(destination / ("board.sqlite" + suffix), missing=True) is not None:
                raise BlackboardError("Export destination has SQLite sidecars; recover or move the existing database first.")
        marker = {"project": self.project, "generation": self.generation, "uuid": self.project_uuid,
                  "schema_version": schema_version, "operation": "sqlite-export", "status": "initializing"}
        if replace_sealed and publication and publication.get("sealed_context"):
            context = publication.get("context", {})
        target = destination / "board.sqlite"
        manifest_path = destination / "manifest.json"
        if manifest_path.exists():
            previous = json.loads(self._read_export_file(manifest_path))
            if not isinstance(previous, dict):
                raise BlackboardError("Invalid export manifest; expected an object.")
            if previous.get("export_version") != EXPORT_VERSION or previous.get("schema_version") != SCHEMA_VERSION:
                raise BlackboardError("Unsupported coordination export format.")
            if previous.get("uuid") != self.project_uuid or previous.get("generation") != self.generation:
                raise BlackboardError("Export destination belongs to another UUID/generation.")
            if previous.get("status") == "sealed":
                context = previous.get("context", {})
                with self._locked(read_only=True), contextlib.closing(self._connect(readonly=True)) as source:
                    source.execute("BEGIN")
                    data = self.snapshot_data(source)
                if data["metadata"]["status"] != "sealed" or self.logical_digest(data) != previous.get("logical_sha256"):
                    raise BlackboardError("Sealed export is immutable and does not match this snapshot.")
            if previous.get("status") == "sealed" and not replace_sealed:
                output = Blackboard(destination, self.project, self.generation)
                checksums = previous.get("checksums")
                if not isinstance(checksums, dict) or set(checksums) != {"board.sqlite", "overview.md", "history.md"}:
                    raise BlackboardError("Sealed export manifest has invalid checksums.")
                for name, checksum in checksums.items():
                    if revision_of(output._data(name)) != checksum:
                        raise BlackboardError("Sealed export checksum mismatch; restore the immutable evidence.")
                if publication is None or publication["status"] != "active":
                    output._record(handle, {**marker, "status": "active", "sequence": previous["sequence"]})
                return previous
        elif any((destination / name).exists() for name in ("board.sqlite", "overview.md", "history.md")) and not (publication and (publication["status"] == "initializing" or replace_sealed)):
            raise BlackboardError("Export destination contains unrecognized evidence; choose an empty directory.")
        for name in ("board.sqlite", "board.sqlite-wal", "board.sqlite-shm", "board.sqlite-journal", "overview.md", "history.md", "manifest.json"):
            self._regular(destination / name, missing=True)
        context = context or {}
        if context and (context.get("uuid"), context.get("generation")) != (self.project_uuid, self.generation):
            raise BlackboardError("Export context UUID/generation does not match this project.")
        if replace_sealed:
            marker.update(sealed_context=True, context=context)
        output._record(handle, marker)
        temporary = destination / (".snapshot-" + uuid4().hex + ".sqlite")
        try:
            with self._locked(read_only=True) as source_handle:
                self._verified(source_handle, ("active", "finalized"))
                with contextlib.closing(self._connect(readonly=True)) as source, contextlib.closing(sqlite3.connect(temporary)) as copy:
                    source.execute("BEGIN")
                    self._metadata(source)
                    source.backup(copy)
                    copy.execute("PRAGMA journal_mode=DELETE")
            with contextlib.closing(sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True)) as copy:
                copy.row_factory = sqlite3.Row
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BlackboardError("Snapshot integrity check failed.")
                data = self.snapshot_data(copy)
                self.validate_data(copy, data)
                snapshot_schema = copy.execute("PRAGMA user_version").fetchone()[0]
                if snapshot_schema != schema_version:
                    raise BlackboardError("Schema changed during export; retry into a new export destination.")
            data["context"] = context or {}
            overview, history = self.render_snapshot(data)
            os.replace(temporary, target)
            output = Blackboard(destination, self.project, self.generation)
            for name, body in (("overview.md", overview), ("history.md", history)):
                output._replace(name, body.encode())
            manifest = {"export_version": EXPORT_VERSION, "schema_version": schema_version,
                        **data["metadata"], "sequence": data["events"][-1]["seq"] if data["events"] else 0,
                        "logical_sha256": self.logical_digest(data), "context": data["context"],
                        "counts": {"entries": len(data["entries"]), "events": len(data["events"]),
                                   "tasks": len(data["tasks"]), "dependencies": len(data["task_dependencies"]),
                                   "task_states": {state: sum(task["state"] == state for task in data["tasks"]) for state in ("open", "in_progress", "done", "cancelled")}},
                        "checksums": {name: revision_of((destination / name).read_bytes()) for name in ("board.sqlite", "overview.md", "history.md")}}
            output._replace("manifest.json", (encoded(manifest) + "\n").encode())
            for name in ("board.sqlite", "overview.md", "history.md", "manifest.json"):
                descriptor = os.open(destination / name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._sync_directory(destination)
            output._record(handle, {**marker, "status": "active", "sequence": manifest["sequence"]})
            return manifest
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read_export_file(path: Path) -> str:
        Blackboard._regular(path)
        return path.read_text()

    @staticmethod
    def validate_data(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
        from .tasks import validate_tasks
        validate_tasks(connection, data)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BlackboardError("Snapshot contains invalid references.")
        latest = {(event["kind"], event["entity_id"]): json.loads(event["after_json"]) for event in data["events"]}
        if latest.get(("overview", "overview")) != data["overview"]:
            raise BlackboardError("Overview does not match durable audit history.")
        if {entry["id"] for entry in data["entries"]} != {identifier for kind, identifier in latest if kind == "entry"}:
            raise BlackboardError("Current entry IDs do not match durable audit history.")
        for entry in data["entries"]:
            if latest.get(("entry", entry["id"])) != entry:
                raise BlackboardError("Entry does not match durable audit history.")

    def rename(self, new_project: str) -> dict[str, Any]:
        self._single_line("project", new_project)
        with self._locked() as handle:
            marker = self._marker(handle)
            if (marker and marker["generation"] == self.generation and marker["project"] == new_project
                    and marker["status"] == "active"):
                with contextlib.closing(self._connect()) as connection:
                    self._metadata(connection)
                self.project = new_project
                return {**marker, "directory": str(self.directory)}
            marker = self._verified(handle, ("active", "renaming"))
            if marker["status"] == "renaming" and marker.get("new_project") != new_project:
                raise BlackboardError("A different blackboard rename is pending.")
            with contextlib.closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                metadata = self._metadata(connection)
                if metadata["status"] != "active":
                    raise BlackboardError("Sealed board cannot be renamed.")
                self._record(handle, {**marker, "status": "renaming", "new_project": new_project})
                if metadata["project"] != new_project:
                    connection.execute("UPDATE metadata SET value=? WHERE key='project'", (new_project,))
                    self.append_event(connection, "renamed", "board", {"project": metadata["project"]}, {"project": new_project}, "framework")
                connection.commit()
            self.project = new_project
            result = {"project": new_project, "generation": self.generation,
                      "status": "active", "backend": "sqlite"}
            self._record(handle, result)
            return {**result, "directory": str(self.directory)}
