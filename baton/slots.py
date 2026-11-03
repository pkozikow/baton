"""Coordinator slot names and active registry validation."""

import json
from pathlib import Path
from typing import Any

from .errors import ProjectError

SLOT_NAMES = tuple(f"p{number}" for number in range(1, 11))


def read_active_project(path: Path) -> dict[str, Any]:
    if path.stem not in SLOT_NAMES:
        raise ProjectError(f"Unsupported active coordinator slot {path.stem}: {path}; expected p1 through p10.")
    try:
        project = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ProjectError(f"Invalid active coordinator record {path}: {error}") from error
    if not isinstance(project, dict):
        raise ProjectError(f"Active coordinator record {path} must be a JSON object.")
    if project.get("slot") != path.stem:
        raise ProjectError(f"Coordinator slot in {path} does not match its registry filename.")
    return project
