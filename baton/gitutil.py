"""Git and file-lock primitives."""

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Iterator

from .errors import ProjectError


def git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True
    )
    if check and result.returncode:
        raise ProjectError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip() if result.returncode == 0 else ""


def common_dir(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def branch(path: Path) -> str:
    return git(path, "branch", "--show-current")


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)



def lock_holder(fd: int) -> str:
    try:
        data = json.loads(os.pread(fd, 4096, 0) or b"null")
        pid, command = data["pid"], data["command"]
        if type(pid) is not int or not 0 < pid < 2**31 or not isinstance(command, str):
            raise TypeError
    except (ValueError, TypeError, KeyError):
        return "an unknown holder"
    try:
        os.kill(pid, 0)
        alive = ""
    except ProcessLookupError:
        alive = "; that process is gone, so the lock may be stale"
    except PermissionError:
        alive = ""
    return f"pid {pid} ({command}, slot {data.get('slot') or 'none'}, since {data.get('started', 'unknown')}){alive}"


@contextlib.contextmanager
def file_lock(path: Path, label: str, slot: str | None = None, blocking: bool = True) -> Iterator[bool]:
    """Exclusive flock that records its holder in place; the file is never truncated on open or unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not blocking:
                yield False
                return
            print(f"baton: waiting for the {label} lock held by {lock_holder(fd)}", file=sys.stderr, flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        holder = {"pid": os.getpid(), "command": shlex.join(["baton", *sys.argv[1:]]), "slot": slot,
                  "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(holder).encode() + b"\n", 0)
        try:
            yield True
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def require_agent_write_path(path: Path) -> None:
    resolved = path.resolve()
    if any(part.casefold() == ".git" for candidate in (path, resolved) for part in candidate.parts):
        raise ProjectError(f"Agent write paths must be outside Git metadata: {path}")
    current = resolved
    while not current.exists():
        current = current.parent
    if current.is_file():
        current = current.parent
    if git(current, "rev-parse", "--is-inside-git-dir", check=False) == "true":
        raise ProjectError(f"Agent write paths must be outside Git metadata: {path}")
