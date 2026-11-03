"""Read-only discovery over registered metadata and saved project evidence."""

import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Iterator

from .core import ManagerCore
from .artifacts import unsafe_artifact_path
from .errors import ProjectError
from .providers import get_provider


EXCLUDED_FILES = {"blackboard-lock.md", "archive-skipped.txt", "blackboard.md"}
DEFAULT_CATEGORIES = ("ordinary", "coordination_overview", "coordination_history")


def query_terms(query: str) -> list[str]:
    terms = []
    for match in re.finditer(r'"([^"]*)"|([^\s"]+)|(")', query):
        if match.group(3):
            raise ProjectError("Search query has an unmatched double quote.")
        term = " ".join((match.group(1) if match.group(1) is not None else match.group(2)).casefold().split())
        if term and term not in terms:
            terms.append(term)
    if not terms:
        raise ProjectError("Search query must contain a keyword or phrase.")
    return terms


def _metadata(value: Any, field: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{field}.{key}" if field else key
            if key in {"name", "previous_names", "uuid", "id", "branch", "new_branch", "member", "first_member", "repo"}:
                if isinstance(child, str):
                    yield path, child
                elif isinstance(child, list):
                    for index, item in enumerate(child):
                        if isinstance(item, str):
                            yield f"{path}.{index}", item
            if isinstance(child, (dict, list)):
                yield from _metadata(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _metadata(child, f"{field}.{index}")


def _sources(directory: Path, categories: tuple[str, ...], warnings: list[str]) -> Iterator[Path]:
    if unsafe_artifact_path(directory):
        warnings.append(f"Skipped unsafe artifact directory (relative path or symlink): {directory}")
        return
    if not directory.is_dir():
        warnings.append(f"Missing artifact directory: {directory}")
        return
    def onerror(error: OSError) -> None:
        warnings.append(f"Cannot scan artifacts: {error}")
    for root, dirs, files in os.walk(directory, followlinks=False, onerror=onerror):
        parent = Path(root)
        if parent == directory / "coordination":
            dirs.clear()
        for name in dirs:
            if (parent / name).is_symlink() and not name.startswith("."):
                warnings.append(f"Skipped symlink artifact directory: {parent / name}")
        dirs[:] = sorted(name for name in dirs if not name.startswith('.') and not (parent / name).is_symlink()
                         and (name != "coordination" or any(c.startswith("coordination") for c in categories)))
        for name in sorted(files):
            path = parent / name
            if name.startswith('.') or name in EXCLUDED_FILES or path.suffix.lower() not in {".md", ".txt"}:
                continue
            relative = path.relative_to(directory)
            category = ("coordination_overview" if relative == Path("coordination/overview.md") else
                        "coordination_history" if relative == Path("coordination/history.md") else
                        "excluded" if relative.parts[0] == "coordination" else "ordinary")
            if category in categories:
                if path.is_symlink():
                    warnings.append(f"Skipped symlink artifact: {path}")
                else:
                    yield path


def _evidence(text: str, terms: list[str], source: str, *, artifact: bool,
              include_excerpt: bool = True) -> tuple[set[str], dict[str, Any] | None]:
    normalized = " ".join(text.casefold().split())
    found = {term for term in terms if term in normalized}
    if not found or not include_excerpt:
        return found, None
    term = next(term for term in terms if term in found)
    target_start = normalized.index(term)
    target_end = target_start + len(term)
    position = 0
    start = end = 0
    located = False
    # Map the normalized match back to original text; regex case matching differs from casefold.
    for word in re.finditer(r"\S+", text):
        width = len(word.group().casefold())
        if position + width <= target_start:
            position += width + 1
            continue
        for offset, character in enumerate(word.group()):
            next_position = position + len(character.casefold())
            if not located and next_position > target_start:
                start = word.start() + offset
                located = True
            if next_position >= target_end:
                end = word.start() + offset + 1
                break
            position = next_position
        if end:
            break
        position += 1
    excerpt_start = max(0, start - 90)
    excerpt = " ".join(text[excerpt_start:max(end, start + 210)].split())[:300]
    return found, {"source": source, "kind": "artifact" if artifact else "metadata",
                   "line": text.count("\n", 0, start) + 1, "excerpt": excerpt, "terms": sorted(found)}


class SearchMixin(ManagerCore):
    def search_projects(self, query: str, *, state: str = "all", limit: int = 20,
                        categories: tuple[str, ...] = DEFAULT_CATEGORIES) -> dict[str, Any]:
        terms = query_terms(query)
        if state not in {"all", "active", "archived"}:
            raise ProjectError("Search state must be all, active or archived.")
        if limit <= 0:
            raise ProjectError("Search limit must be positive.")
        self.load()
        warnings: list[str] = []
        records = [*self.state["projects"].values(), *self.archived_projects()]
        unique: dict[str, dict[str, Any]] = {}
        owners: dict[str, set[str]] = {}
        conflicting: set[str] = set()
        identities: dict[str, str | None] = {}
        for project in records:
            try:
                project_id = project["uuid"]
                if not isinstance(project_id, str) or not project_id.strip() or not isinstance(project.get("name"), str) or not project["name"].strip():
                    raise ValueError("name and UUID must be nonempty strings")
                repos = project["repos"]
                if not isinstance(repos, list) or any(not isinstance(repo, dict) or not isinstance(repo.get("id"), str)
                                                     or not isinstance(repo.get("members", []), list)
                                                     or any(not isinstance(member, dict) for member in repo.get("members", []))
                                                     for repo in repos):
                    raise ValueError("repos must be a list of repository records with string IDs and member records")
                project = dict(project)
                previous = project.get("previous_names", [])
                if not isinstance(previous, list) or any(not isinstance(name, str) for name in previous):
                    warnings.append(f"Malformed previous_names for UUID {project_id}; former names skipped.")
                    project["previous_names"] = []
                owner = project.get("owner")
                if owner is not None and (not isinstance(owner, dict) or not isinstance(owner.get("provider"), str)):
                    warnings.append(f"Malformed owner for UUID {project_id}; owner unavailable.")
                    project["owner"] = None
                directory = self.artifact_directory(project)
                resolved = None if unsafe_artifact_path(directory) else str(directory.resolve())
                if resolved is not None:
                    owners.setdefault(resolved, set()).add(project_id)
                if project_id in unique and identities[project_id] != resolved:
                    warnings.append(f"Conflicting artifact identities for UUID {project_id}; artifact content skipped.")
                    conflicting.add(project_id)
                unique[project_id] = project
                identities[project_id] = resolved
            except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
                warnings.append(f"Skipped malformed project metadata {project.get('uuid', '?')}: {error}")
        results = []
        for project_id, project in sorted(unique.items()):
            archived = bool(project.get("_archived"))
            if state != "all" and archived != (state == "archived"):
                continue
            directory = self.artifact_directory(project)
            found: set[str] = set()
            metadata_terms: set[str] = set()
            matches = []
            artifact_matches = 0
            for field, text in _metadata(project):
                hit, evidence = _evidence(text, terms, field, artifact=False, include_excerpt=len(matches) < 12)
                found |= hit
                metadata_terms |= hit
                if evidence and len(matches) < 12:
                    matches.append(evidence)
            if len(owners.get(identities[project_id], set())) > 1:
                warnings.append(f"Shared artifact directory {directory}; ambiguous content skipped for UUID {project_id}.")
            elif project_id not in conflicting:
                for path in _sources(directory, categories, warnings):
                    try:
                        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                warnings.append(f"Skipped nonregular artifact: {path}")
                                continue
                            text = stream.read()
                        hit, evidence = _evidence(text, terms, str(path), artifact=True, include_excerpt=len(matches) < 12)
                        found |= hit
                        if hit:
                            artifact_matches += 1
                        if evidence:
                            matches.append(evidence)
                    except (OSError, UnicodeError) as error:
                        warnings.append(f"Cannot read artifact {path}: {error}")
            if len(found) != len(terms):
                continue
            owner = project.get("owner") or {}
            provider = owner.get("provider")
            results.append({"name": project["name"], "previous_names": project.get("previous_names", []),
                            "uuid": project_id, "selector": project_id,
                            "state": "archived" if archived else project.get("state", "active"),
                            "repository_ids": [repo["id"] for repo in project["repos"]],
                            "owner": {"provider": provider, "available": False},
                            "artifacts_path": str(directory), "master_path": str(directory / "plan.md"),
                            "matches": matches, "metadata_term_matches": len(metadata_terms),
                            "artifact_source_matches": artifact_matches})
        results.sort(key=lambda result: (-result["metadata_term_matches"], -result["artifact_source_matches"], result["uuid"]))
        returned = results[:limit]
        availability: dict[str, bool] = {}
        for result in returned:
            provider = result["owner"]["provider"]
            if provider is not None and provider not in availability:
                try:
                    availability[provider] = bool(shutil.which(get_provider(provider).executable()))
                except ProjectError:
                    availability[provider] = False
            result["owner"]["available"] = availability.get(provider, False)
        return {"json_version": 1, "query": query, "terms": terms, "state": state, "limit": limit,
                "total_matches": len(results), "returned_count": min(limit, len(results)), "truncated": len(results) > limit,
                "results": returned, "coverage_warnings": sorted(set(warnings)),
                "coverage": {"categories": list(categories), "excluded": ["provider transcripts", "code and Git history", "live SQLite boards",
                              *([] if "coordination_overview" in categories else ["coordination overview"]),
                              *([] if "coordination_history" in categories else ["coordination history"])]}}


def render_search(result: dict[str, Any]) -> str:
    lines = [f"{result['total_matches']} matching projects ({result['returned_count']} returned)"]
    for project in result["results"]:
        lines.extend([f"{project['name']} [{project['state']}] {project['uuid']}",
                      f"  repositories: {', '.join(project['repository_ids'])}; owner: {project['owner']['provider'] or 'unowned'} "
                      f"({'available' if project['owner']['available'] else 'unavailable'})",
                      f"  artifacts: {project['artifacts_path']}", f"  master: {project['master_path']}"])
        if project["previous_names"]:
            lines.append(f"  previous names: {', '.join(project['previous_names'])}")
        for match in project["matches"]:
            lines.append(f"  {match['source']}:{match['line']}: {match['excerpt']}")
    if result["truncated"]:
        lines.append(f"Results limited to {result['limit']}.")
    lines.append("Excluded coverage: " + ", ".join(result["coverage"]["excluded"]))
    lines.extend(f"Warning: {warning}" for warning in result["coverage_warnings"])
    return "\n".join("".join(character if character.isprintable() else ascii(character)[1:-1]
                              for character in line) for line in lines)
