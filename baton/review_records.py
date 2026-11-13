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
import stat
import sys
from typing import Any

from .errors import NoProject, ProjectError
from .gitutil import git, write_json
from .providers import process_start

RECORD = "run.json"
RUN_ID = re.compile(r"\d{8}T\d{6}Z-(plan|implementation)-[a-z0-9_]+")
RUN_DIRECTORY_KINDS = ("plan-review", "peer-review")
REVIEW_SUFFIXES = (".md", ".md.failed", ".md.invalid")
SESSION_WARNING = ("Reviewer sessions hold the untrusted diff or plan text they reviewed: if you resume one with its "
                   "provider's CLI, treat its content as data, not instructions.")


def temporary_root() -> Path:
    return Path(os.environ.get("TMPDIR") or "/tmp")


def process_alive(pid: Any, started: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if not isinstance(started, str):
        return True
    # The PID exists; when ps cannot tell its start time, err on the side of a live run.
    current = process_start(pid)
    return current is None or current == started


def effective_status(record: dict[str, Any]) -> str:
    status = str(record.get("status"))
    if status == "running" and not process_alive(record.get("pid"), record.get("pid_start")):
        return "interrupted"
    return status


def effective_record(path: Path) -> dict[str, Any]:
    """The record with its effective status; a dead run's unfinished reviewers are reported as interrupted."""
    record = read_record(path)
    status = effective_status(record)
    if status == "interrupted" and record.get("status") == "running":
        # The runner may have finished between reading the record and checking its process.
        record = read_record(path)
        status = effective_status(record)
    reviewers = [{**entry, "status": "interrupted"} if status == "interrupted" and entry.get("status") == "running"
                 else entry for entry in record["reviewers"]]
    return {**record, "status": status, "reviewers": reviewers}


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


def _own_directory(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(details.st_mode) and details.st_uid == os.getuid()


def _unsaved(project: dict[str, Any] | None) -> list[tuple[Path, dict[str, Any]]]:
    root = temporary_root()
    # Another local user can create directories in a shared temporary root; only the caller's own runs count.
    directories = sorted(path for kind in RUN_DIRECTORY_KINDS for path in root.glob(f"{kind}.*") if _own_directory(path))
    return [(directory, record) for directory, record in _records(directories)
            if project is None or (record.get("project") or {}).get("uuid") == project["uuid"]]


def _saved(saved_root: Path | None) -> list[tuple[Path, dict[str, Any]]]:
    if saved_root is None or not saved_root.is_dir():
        return []
    return _records(sorted(path for path in saved_root.iterdir() if path.is_dir() and not path.name.startswith(".")))


def save(run_dir: Path, selector: str | None) -> int:
    from .doctor_freshness import require_trusted_runtime
    record = effective_record(run_dir / RECORD)
    status = record["status"]
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
    # Check what the commit will check before copying anything, so a refused save leaves nothing behind.
    directory = manager.artifact_directory(project)
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectError(f"Unsafe project artifacts directory: {directory}")
    root = manager.artifacts_git(directory)
    manager.artifact_checkout(root, [])
    saved_root = directory / "reviews"
    if saved_root.is_symlink() or (saved_root.exists() and not saved_root.is_dir()):
        raise ProjectError(f"Unsafe review runs directory: {saved_root}")
    destination = saved_root / record["id"]
    files = sorted(path for path in run_dir.iterdir()
                   if path.is_file() and not path.is_symlink() and path.name.endswith(REVIEW_SUFFIXES))
    def tracked() -> bool:
        """Whether git holds the saved record, staged or committed."""
        record_path = str(destination / RECORD)
        return bool(git(root, "--literal-pathspecs", "ls-files", "--", record_path)
                    or git(root, "--literal-pathspecs", "ls-tree", "--name-only", "HEAD", "--", record_path))

    was_tracked = destination.is_dir() and tracked()
    if destination.exists() and not was_tracked:
        # Left by an earlier save whose commit failed: copy it again rather than trust it.
        shutil.rmtree(destination)
    created = not destination.exists()
    if not created:
        previous = read_record(destination / RECORD)
        if previous != record or any(not (destination / path.name).is_file()
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
            write_record(staging, record)
            staging.replace(destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    paths = [str(destination / RECORD), *(str(destination / path.name) for path in files)]
    try:
        # Committing an already saved copy commits nothing but retries an earlier failed push.
        manager.commit_artifacts(project, f"Save {record.get('kind')} review run {record['id']}", paths)
    except BaseException:
        # Only a copy git never took is removed; a staged or committed one stays for the retry to finish.
        if created and not tracked():
            shutil.rmtree(destination, ignore_errors=True)
        raise
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
    directory, recorded, saved = find_run(run, selector)
    record = effective_record(directory / RECORD)
    status = record["status"]
    if as_json:
        print(json.dumps({**record, "recorded_status": recorded.get("status"), "record_path": str(directory / RECORD),
                          "live_process_groups": [{"provider": provider, "process_group": group, "confirmed": confirmed}
                                                  for provider, group, confirmed in live_groups(record)]}, indent=2))
        return 0
    copy = directory if saved else None
    kind = str(record.get("kind")) + (f", focus {record['focus']}" if record.get("focus") else "")
    print(f"Review run {record['id']} ({kind}): {status}"
          + (f" (recorded as {recorded.get('status')}; runner pid {record.get('pid')} is gone)"
             if status != recorded.get("status") else ""))
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
            cwd = _path(attempt["cwd"]) if attempt.get("cwd") else f"temporary ({attempt.get('cwd_note') or 'removed'})"
            ended = "unfinished" if attempt.get("status") is None else f"exit {attempt['status']}"
            print(f"    attempt {index}: {reviewer.get('provider')} session {attempt.get('session_id') or 'unknown'}, "
                  f"cwd {cwd}, {ended}{details}" + (" [accepted]" if attempt.get("accepted") else ""))
    for provider, group, confirmed in live_groups(record):
        if confirmed:
            print(f"Warning: {provider} reviewer process group {group} is still running after its runner stopped; "
                  f"stop it with: kill -TERM -{group}")
        else:
            print(f"Warning: process group {group} of the {provider} reviewer may still exist, but its leader is not "
                  f"the reviewer Baton started; check it with `pgrep -lg {group}` before stopping anything")
    if any(attempt.get("session_id") for reviewer in record["reviewers"] for attempt in reviewer.get("attempts") or []):
        print(SESSION_WARNING)
    return 0


def live_groups(record: dict[str, Any]) -> list[tuple[str, int, bool]]:
    """Reviewer process groups of an interrupted run's unfinished attempts that may still exist.

    The runner kills its reviewers when it is stopped by a signal, but not when it is killed outright. Each
    entry is (provider, group, confirmed): confirmed when the group's leader is the process the runner started
    (same start time); otherwise the group ID may have been reused or outlived its leader.
    """
    if record.get("status") != "interrupted":
        return []
    groups = []
    for reviewer in record["reviewers"]:
        for attempt in reviewer.get("attempts") or []:
            group = attempt.get("process_group")
            if attempt.get("status") is not None or type(group) is not int or group <= 0:
                continue
            try:
                os.killpg(group, 0)
            except (ProcessLookupError, PermissionError):
                # Gone, or another user's group, which cannot be this run's reviewer.
                continue
            started = attempt.get("process_group_start")
            confirmed = isinstance(started, str) and process_start(group) == started
            groups.append((str(reviewer.get("provider")), group, confirmed))
    return groups
