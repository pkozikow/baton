"""Resolve canonical registry storage without changing it."""

from pathlib import Path
import stat

from .errors import ProjectError


def registry_directory(common: Path) -> Path:
    directory = common / "baton"
    try:
        entry = directory.lstat()
    except FileNotFoundError:
        return directory
    if not stat.S_ISDIR(entry.st_mode):
        raise ProjectError(f"Refusing unexpected registry entry: {directory}")
    return directory
