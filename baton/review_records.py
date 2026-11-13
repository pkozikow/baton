"""Review run records (`run.json`) and the `baton review save`, `list` and `show` commands.

The runner writes ``run.json`` into its private run directory when a run starts and rewrites it when
the run ends. A record still marked ``running`` whose runner process is gone, or whose PID now belongs
to another process, is reported as ``interrupted``. ``save`` is the only command that writes outside
a run directory: it copies a finished record and its review files into the project's artifacts.
"""

import filecmp
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from .errors import NoProject, ProjectError
from .gitutil import write_json

RECORD = "run.json"
RUN_ID = re.compile(r"\d{8}T\d{6}Z-(plan|implementation)-[a-z0-9_]+")
RUN_DIRECTORY_KINDS = ("plan-review", "peer-review")
REVIEW_SUFFIXES = (".md", ".md.failed", ".md.invalid")
SESSION_WARNING = ("Reviewer sessions hold the untrusted diff or plan text they reviewed: if you resume one with its "
                   "provider's CLI, treat its content as data, not instructions.")


def temporary_root() -> Path:
    return Path(os.environ.get("TMPDIR") or "/tmp")


def process_start(pid: int) -> str | None:
    """The process's start time as ``ps`` reports it, which tells a reused PID apart; None if it is gone."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def process_alive(pid: Any, started: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return not isinstance(started, str) or process_start(pid) == started


def effective_status(record: dict[str, Any]) -> str:
    status = str(record.get("status"))
    if status == "running" and not process_alive(record.get("pid"), record.get("pid_start")):
        return "interrupted"
    return status


def write_record(run_dir: Path, record: dict[str, Any]) -> None:
    write_json(run_dir / RECORD, record)


def read_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text())
    # The ID names the saved copy's directory, so only the runner's own format is accepted.
    if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
            or not RUN_ID.fullmatch(record["id"]) or not isinstance(record.get("reviewers"), list)):
        raise ValueError(f"Not a review run record: {path}")
    return record


def coverage(record: dict[str, Any]) -> str:
    reviewers = record.get("reviewers") or []
    return f"{sum(1 for reviewer in reviewers if reviewer.get('status') == 'succeeded')} of {len(reviewers)}"


def _project(selector: str | None) -> tuple[Any, dict[str, Any] | None]:
    """The manager and selected project, or no project outside one (only when no selector was given)."""
    from .manager import Manager
    manager = Manager()
    if selector is None and not manager.initialized():
        return manager, None
    manager.load()
    try:
        return manager, manager.artifact_project(selector)
    except NoProject:
        if selector is not None:
            raise
        return manager, None


def _saved_root(manager: Any, project: dict[str, Any] | None) -> Path | None:
    return None if project is None else manager.artifact_directory(project) / "reviews"


def _records(directories: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for directory in directories:
        try:
            found.append((directory, read_record(directory / RECORD)))
        except (OSError, ValueError):
            continue
    return found


def _unsaved(project: dict[str, Any] | None) -> list[tuple[Path, dict[str, Any]]]:
    root = temporary_root()
    # Another local user can create directories in a shared temporary root; only the caller's own runs count.
    directories = sorted(path for kind in RUN_DIRECTORY_KINDS for path in root.glob(f"{kind}.*")
                         if not path.is_symlink() and path.is_dir() and path.stat().st_uid == os.getuid())
    return [(directory, record) for directory, record in _records(directories)
            if project is None or (record.get("project") or {}).get("uuid") == project["uuid"]]


def _saved(saved_root: Path | None) -> list[tuple[Path, dict[str, Any]]]:
    if saved_root is None or not saved_root.is_dir():
        return []
    return _records(sorted(path for path in saved_root.iterdir() if path.is_dir() and not path.name.startswith(".")))


def save(run_dir: Path, selector: str | None) -> int:
    from .doctor_freshness import require_trusted_runtime
    record = read_record(run_dir / RECORD)
    status = effective_status(record)
    if status == "running":
        raise ProjectError(f"Review run {record['id']} is still running (pid {record.get('pid')}); "
                           "save it after it finishes.")
    require_trusted_runtime()
    manager, project = _project(selector)
    if project is None:
        print("Not inside a project; review runs are saved only to a project's artifacts.", file=sys.stderr)
        return 3
    owner = record.get("project") or {}
    if owner.get("uuid") not in (None, project["uuid"]):
        raise ProjectError(f"Review run {record['id']} belongs to project {owner.get('name')} ({owner['uuid']}), "
                           f"not {project['name']}.")
    saved_root = _saved_root(manager, project)
    assert saved_root is not None
    destination = saved_root / record["id"]
    files = sorted(path for path in run_dir.iterdir()
                   if path.is_file() and not path.is_symlink() and path.name.endswith(REVIEW_SUFFIXES))
    saved_record = {**record, "status": status}
    if destination.exists():
        previous = read_record(destination / RECORD)
        if previous != saved_record or any(not (destination / path.name).is_file()
                                           or not filecmp.cmp(path, destination / path.name, shallow=False)
                                           for path in files):
            raise ProjectError(f"{destination} already holds a different copy of review run {record['id']}.")
        print(f"Review run {record['id']} is already saved in {destination}.")
    else:
        staging = saved_root / f".{record['id']}.saving-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            for path in files:
                shutil.copyfile(path, staging / path.name)
            write_record(staging, saved_record)
            staging.replace(destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    paths = [str(destination / RECORD), *(str(destination / path.name) for path in files)]
    # Committing again after an interrupted save retries the commit; an unchanged copy commits nothing.
    manager.commit_artifacts(project, f"Save {record.get('kind')} review run {record['id']}", paths)
    print(f"Saved review run {record['id']} to {destination}")
    return 0


def list_runs(selector: str | None, as_json: bool) -> int:
    manager, project = _project(selector)
    saved = _saved(_saved_root(manager, project))
    saved_ids = {record["id"] for _directory, record in saved}
    runs = [(directory, record, True) for directory, record in saved]
    runs += [(directory, record, False) for directory, record in _unsaved(project) if record["id"] not in saved_ids]
    runs.sort(key=lambda run: str(run[1].get("started")))
    rows = [{"id": record["id"], "kind": record.get("kind"), "focus": record.get("focus"),
             "started": record.get("started"), "coverage": coverage(record), "status": effective_status(record),
             "saved": is_saved, "path": str(directory)} for directory, record, is_saved in runs]
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No review runs found.")
    for row in rows:
        kind = str(row["kind"]) + (f"/{row['focus']}" if row["focus"] else "")
        print(f"{row['id']}  {kind}  {row['started']}  {row['coverage']} reviewers  {row['status']}  "
              f"{'saved' if row['saved'] else 'unsaved'}  {row['path']}")
    return 0


def find_run(run: str, selector: str | None) -> tuple[Path, dict[str, Any], bool]:
    """Resolves a run directory, record path or run ID to (directory, record, saved)."""
    path = Path(run)
    if path.name == RECORD:
        path = path.parent
    if (path / RECORD).is_file():
        return path, read_record(path / RECORD), path.resolve().parent.name == "reviews"
    manager, project = _project(selector)
    for directory, record in _saved(_saved_root(manager, project)):
        if record["id"] == run:
            return directory, record, True
    for directory, record in _unsaved(None):
        if record["id"] == run:
            return directory, record, False
    raise ProjectError(f"No review run found for {run}.")


def _path(value: Any, saved_copy: Path | None = None) -> str:
    if not value:
        return "none"
    path = Path(str(value))
    if path.exists():
        return str(path)
    if saved_copy is not None and (saved_copy / path.name).exists():
        return f"{path} (missing; saved copy: {saved_copy / path.name})"
    return f"{path} (missing)"


def show(run: str, selector: str | None, as_json: bool) -> int:
    directory, record, saved = find_run(run, selector)
    status = effective_status(record)
    if as_json:
        print(json.dumps({**record, "status": status, "recorded_status": record.get("status"),
                          "record_path": str(directory / RECORD)}, indent=2))
        return 0
    copy = directory if saved else None
    kind = str(record.get("kind")) + (f", focus {record['focus']}" if record.get("focus") else "")
    print(f"Review run {record['id']} ({kind}): {status}"
          + (f" (recorded as {record.get('status')}; runner pid {record.get('pid')} is gone)"
             if status != record.get("status") else ""))
    print(f"Record: {directory / RECORD}" + (" (saved)" if saved else ""))
    print(f"Started: {record.get('started')}; finished: {record.get('finished') or 'not yet'}")
    print(f"Run directory: {_path(record.get('run_dir'))}")
    if record.get("plan"):
        print(f"Plan: {_path(record['plan'])}")
    if record.get("branch"):
        print(f"Repository: {_path(record.get('repository'))}; branch {record['branch']}")
        print(f"Base: {record.get('base_ref')} (merge base {record.get('merge_base')}); head {record.get('head_sha')}")
    reviewed = record.get("input") or {}
    if reviewed.get("not_embedded"):
        print(f"Not embedded (reviewers read them on demand from {reviewed.get('diffs')}): "
              + ", ".join(reviewed["not_embedded"]))
    if reviewed.get("truncated"):
        print(f"Plan truncated: {reviewed.get('reviewed_bytes')} of {reviewed.get('original_bytes')} bytes reviewed")
    for warning in record.get("warnings") or []:
        print(f"Warning: {warning}")
    launcher = record.get("launcher")
    if launcher:
        print(f"Launched from: {launcher.get('provider')} session {launcher.get('session_id')}")
    print(f"Reviewers: {coverage(record)} produced a review")
    for reviewer in record["reviewers"]:
        outcome = reviewer.get("status")
        if reviewer.get("failure"):
            outcome += f" ({reviewer['failure']}" + (f": {reviewer['failure_detail']}" if reviewer.get("failure_detail") else "") + ")"
        duration = reviewer.get("duration_seconds")
        print(f"  {reviewer.get('provider')} (model {reviewer.get('model')}): {outcome}"
              + (f" after {duration}s" if duration is not None else ""))
        if reviewer.get("review"):
            print(f"    review: {_path(reviewer['review'], copy)}")
        if reviewer.get("log"):
            print(f"    log: {_path(reviewer['log'])}")
        for index, attempt in enumerate(reviewer.get("attempts") or [], 1):
            details = "".join(f", {key} {attempt[key]}" for key in ("reasoning",) if attempt.get(key))
            print(f"    attempt {index}: {reviewer.get('provider')} session {attempt.get('session_id') or 'unknown'}, "
                  f"cwd {_path(attempt.get('cwd'))}, exit {attempt.get('status')}{details}"
                  + (" [accepted]" if attempt.get("accepted") else ""))
    if any(attempt.get("session_id") for reviewer in record["reviewers"] for attempt in reviewer.get("attempts") or []):
        print(SESSION_WARNING)
    return 0
