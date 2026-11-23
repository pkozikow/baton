"""Pull request state for the web UI, looked up with `gh` under per-call and total time budgets."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any

from ..manifest import entries
from ..native import github_repository_for_remote

CALL_TIMEOUT = 5.0
TOTAL_BUDGET = 15.0
STATES = ("OPEN", "CLOSED", "MERGED")

_lock = threading.Lock()
_children: set[subprocess.Popen[str]] = set()
_closing = False


def _kill(process: subprocess.Popen[str]) -> None:
    # Each lookup leads its own process group, so a wrapper's children go too.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_all() -> None:
    """Stop every in-flight lookup and refuse new ones; called once when the server stops."""
    global _closing
    with _lock:
        _closing = True
        children = list(_children)
    for process in children:
        _kill(process)


def _run(args: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    with _lock:
        if _closing:
            raise OSError("The web server is stopping.")
        process = subprocess.Popen(args, cwd=cwd, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        _children.add(process)
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            process.communicate()
            raise
        return process.returncode, output
    finally:
        with _lock:
            _children.discard(process)


@dataclass
class RepositoryPulls:
    repo: str
    unavailable: str | None = None
    branches: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _pull_requests(output: str) -> list[dict[str, Any]]:
    data = json.loads(output)
    if not isinstance(data, list) or any(
            not isinstance(pr, dict) or type(pr.get("number")) is not int or not isinstance(pr.get("title"), str)
            or pr.get("state") not in STATES or not isinstance(pr.get("url"), str) for pr in data):
        raise ValueError("invalid pull request response")
    return data


def lookup(project: dict[str, Any], *, call_timeout: float = CALL_TIMEOUT,
           total_budget: float = TOTAL_BUDGET) -> list[RepositoryPulls]:
    """Each recorded branch's pull requests; a repository stops at its first failure or when the budget runs out."""
    deadline = time.monotonic() + total_budget
    result = []
    for repo in project.get("repos", []):
        pulls = RepositoryPulls(str(repo.get("id")))
        result.append(pulls)
        branches = [entry["branch"] for entry in entries(repo, include_retired=True) if isinstance(entry.get("branch"), str)]
        source = Path(str(repo.get("source", "")))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pulls.unavailable = "lookup time budget exhausted"
            continue
        if not source.is_absolute() or not source.is_dir():
            pulls.unavailable = "source checkout is missing"
            continue
        try:
            code, remote = _run(["git", "-C", str(source), "config", "--get", "remote.origin.url"], source,
                                min(call_timeout, remaining))
        except (OSError, subprocess.TimeoutExpired):
            pulls.unavailable = "lookup failed"
            continue
        repository = github_repository_for_remote(remote.strip()) if code == 0 else None
        if repository is None:
            pulls.unavailable = "not a GitHub repository"
            continue
        for branch in branches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pulls.unavailable = "lookup time budget exhausted"
                break
            try:
                code, output = _run(["gh", "pr", "list", "--repo", repository, "--head", branch, "--state", "all",
                                     "--limit", "20", "--json", "number,title,state,url"],
                                    source, min(call_timeout, remaining))
                if code:
                    raise ValueError(f"gh exited with status {code}")
                pulls.branches[branch] = _pull_requests(output)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                pulls.unavailable = "lookup failed" if not isinstance(error, FileNotFoundError) else "gh is not installed"
                break
    return result
