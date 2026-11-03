"""Filesystem safety and permanent journal locks for SQLite coordination state."""

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, BinaryIO, Iterator


class BlackboardError(Exception):
    pass


def revision_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Blackboard:
    def __init__(self, directory: Path, project: str, generation: str) -> None:
        self.directory = directory.absolute()
        self.project = project
        self.generation = generation

    @staticmethod
    def _regular(path: Path, missing: bool = False) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise BlackboardError(f"Blackboard file is missing: {path}") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BlackboardError(f"Blackboard path must be a regular, unlinked file: {path}")
        return info

    @staticmethod
    def _directory(path: Path, create: bool = False) -> None:
        for ancestor in [*reversed(path.parents), path]:
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                if create:
                    ancestor.mkdir(exist_ok=True)
                    info = ancestor.lstat()
                else:
                    raise BlackboardError(f"Blackboard directory is missing: {path}") from None
            if not stat.S_ISDIR(info.st_mode):
                raise BlackboardError(f"Blackboard directory must not contain symlinks: {ancestor}")

    def _data(self, name: str, missing: bool = False) -> bytes | None:
        path = self.directory / name
        before = self._regular(path, missing)
        if before is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise BlackboardError(f"Cannot read blackboard file {path}: {exc}") from exc
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            after = self._regular(path)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
                opened.st_dev, opened.st_ino
            ) != (after.st_dev, after.st_ino):
                raise BlackboardError(f"Blackboard file changed outside the helper: {path}")
            return handle.read()

    def _check_lock(self, handle: BinaryIO, path: Path) -> None:
        self._directory(self.directory)
        actual = self._regular(path)
        opened = os.fstat(handle.fileno())
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise BlackboardError("The permanent blackboard lock was replaced.")

    @contextlib.contextmanager
    def _locked(self, create: bool = False, read_only: bool = False) -> Iterator[BinaryIO]:
        self._directory(self.directory, create)
        path = self.directory / "blackboard-lock.md"
        self._regular(path, missing=create)
        flags = (os.O_RDONLY if read_only else os.O_RDWR) | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise BlackboardError(f"Cannot open permanent blackboard lock {path}: {exc}") from exc
        with os.fdopen(fd, "rb" if read_only else "r+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if read_only else fcntl.LOCK_EX)
            try:
                self._check_lock(handle, path)
                yield handle
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _marker(handle: BinaryIO) -> dict[str, Any] | None:
        handle.seek(0)
        data = handle.read()
        marker = None
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                candidate = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise BlackboardError("Permanent blackboard lock has a corrupt journal record.") from None
            if isinstance(candidate, dict) and all(
                isinstance(candidate.get(key), str) and candidate[key]
                for key in ("project", "generation", "status")
            ) and candidate["status"] in ("initializing", "active", "finalized", "renaming"):
                marker = candidate
            else:
                raise BlackboardError("Permanent blackboard lock has an invalid journal record.")
        if marker is None and data and b"\n" in data:
            raise BlackboardError("Permanent blackboard lock has no valid journal record.")
        return marker

    @staticmethod
    def _record(handle: BinaryIO, marker: dict[str, Any]) -> None:
        handle.seek(0)
        data = handle.read()
        boundary = data.rfind(b"\n") + 1
        # Discard only an interrupted trailing record, never replace the lock inode.
        handle.seek(boundary)
        handle.truncate()
        handle.write((json.dumps(marker, sort_keys=True) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())

    def _verified(self, handle: BinaryIO, statuses: tuple[str, ...]) -> dict[str, Any]:
        marker = self._marker(handle)
        if marker is None or marker["generation"] != self.generation or marker["project"] != self.project:
            raise BlackboardError("Blackboard project/generation does not match this project session.")
        if marker["status"] == "renaming" and "renaming" not in statuses:
            raise BlackboardError("Blackboard is being renamed by baton rename; retry after the rename finishes.")
        if marker["status"] not in statuses:
            raise BlackboardError(f"Blackboard is {marker['status']}; updates are frozen.")
        return marker

    def _sync_directory(self, directory: Path | None = None) -> None:
        fd = os.open(directory or self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _replace(self, name: str, data: bytes) -> None:
        self._regular(self.directory / name, missing=True)
        fd, temporary = tempfile.mkstemp(prefix=".blackboard-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._regular(self.directory / name, missing=True)
            os.replace(temporary, self.directory / name)
            self._sync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _single_line(label: str, value: str) -> None:
        if not value.strip() or "\n" in value or "\r" in value:
            raise BlackboardError(f"Blackboard {label} must be a nonempty single line.")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
