"""Helpers over project manifests: entries, native stack records, ownership claims and links."""

import os
from pathlib import Path
from typing import Any

from .errors import ProjectError


MEMBER_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]*"


def note_directories() -> list[str]:
    directories = []
    for name in os.environ.get("BATON_NOTE_DIRS", os.pathsep.join(("notes", "thoughts"))).split(os.pathsep):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ProjectError("BATON_NOTE_DIRS must contain worktree-relative note directories.")
        if any(part.casefold() == ".git" for part in relative.parts):
            raise ProjectError("BATON_NOTE_DIRS must be outside Git metadata.")
        directories.append(relative.as_posix())
    return directories


def entries(repo: dict[str, Any], include_retired: bool = False) -> list[dict[str, Any]]:
    members = [repo, *repo.get("members", [])]
    return members if include_retired else [entry for entry in members if entry.get("retirement_state") != "retired"]


def default_entry(repo: dict[str, Any]) -> dict[str, Any] | None:
    return next(iter(entries(repo)), None)


def stack_worktree(scope: dict[str, Any]) -> Path:
    entry = default_entry(scope)
    if entry is None:
        raise ProjectError(f"Native stack has no active registered worktree: {scope['branch']}")
    return Path(entry["path"])


def member_name(entry: dict[str, Any]) -> str | None:
    return entry.get("member") or entry.get("first_member")


def branch_suffix(branch: str, prefix: str | None = None) -> str:
    prefix = "baton" if prefix is None else prefix
    return branch.removeprefix(prefix + "/")



def subplan_directory(project_directory: Path, repo: str, branch: str, prefix: str | None = None) -> Path:
    return project_directory / "branches" / repo / branch_suffix(branch, prefix)


def stack_records(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return [record for record in repo.get("stacks", []) if not record.get("closed")]


def entry_stack_root(repo: dict[str, Any], entry: dict[str, Any]) -> str | None:
    return entry.get("stack_root")


def ensure_stack_records(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return repo.setdefault("stacks", [])



def stack_scope(repo: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    members = [entry for entry in entries(repo, include_retired=True) if entry_stack_root(repo, entry) == record["root"]]
    anchors = [entry for entry in members if entry["branch"] == record["root"]]
    if len(anchors) != 1:
        raise ProjectError(f"Missing or ambiguous registered native stack anchor: {record['root']}")
    return dict(anchors[0], members=[entry for entry in members if entry is not anchors[0]])


def project_entries(project: dict[str, Any], include_retired: bool = False) -> list[dict[str, Any]]:
    return [entry for repo in project["repos"] for entry in entries(repo, include_retired=include_retired)]


def entry_claims(common: str, branch_name: str, path: str) -> set[tuple[str, ...]]:
    return {("branch", common, branch_name), ("path", path)}


def project_claims(project: dict[str, Any]) -> set[tuple[str, ...]]:
    """Names, branches and paths a manifest owns, including those of unfinished operations."""
    rename = project.get("pending_rename") or {}
    claims: set[tuple[str, ...]] = {("name", name) for name in (project["name"], rename.get("name"),
                                                                *project.get("previous_names", [])) if name}
    artifact_path = project.get("artifacts_path")
    if artifact_path:
        claims.add(("artifacts", str(artifact_path)))
    for entry in project_entries(project, include_retired=True):
        claims |= entry_claims(entry["common_dir"], entry["branch"], entry["path"])
    for move in rename.get("repos", []):
        claims |= entry_claims(move["common_dir"], rename["branch"], move["new"])
    for pending in (project.get("pending_member"),):
        if not pending:
            continue
        if pending.get("entry"):
            entry = pending["entry"]
            claims |= entry_claims(entry["common_dir"], entry["branch"], entry["path"])
        common = next((r["common_dir"] for r in project["repos"] if r["id"] == pending.get("repo")), None)
        if pending.get("first_move") and common:
            claims |= entry_claims(common, pending["first_move"]["new_branch"], pending["first_move"]["new_path"])
    return claims


def claim_conflict(claim: tuple[str, ...], owner: str) -> ProjectError:
    if claim[0] == "name":
        return ProjectError(f"Project already active or a former name of one: {claim[1]} (claimed by {owner})")
    if claim[0] == "artifacts":
        return ProjectError(f"Artifact destination belongs to another project: {claim[1]} (claimed by {owner})")
    if claim[0] == "branch":
        return ProjectError(f"Branch belongs to another project: {claim[2]} (claimed by {owner})")
    return ProjectError(f"Worktree path belongs to another project: {claim[1]} (claimed by {owner})")


def link_name(entry: dict[str, Any]) -> str:
    label = member_name(entry)
    return entry["id"] + ("--" + label if label else "")


def managed_links(project: dict[str, Any]) -> dict[str, Path]:
    links: dict[str, Path] = {}
    for entry in project_entries(project):
        name = link_name(entry)
        if name in links:
            raise ProjectError(f"Managed link collides with another repository or member: {name}")
        links[name] = Path(entry["path"])
    return links


def coordination_context(project: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Describe only recorded metadata; archived paths are never inspected."""
    return {"uuid": project["uuid"], "generation": (project.get("blackboard") or {}).get("generation"),
            "owner": project.get("owner"), "master_path": str(directory / "plan.md"),
            "entries": [{"repo": repo["id"], "member": member_name(entry), "branch": entry.get("branch"),
                         "base_ref": entry.get("base_ref"), "cleanup_sha": entry.get("cleanup_sha"),
                         "retirement_state": entry.get("retirement_state"),
                         "subplan_path": str(subplan_directory(directory, repo["id"], entry["branch"], project.get("branch_prefix")) / "plan.md") if entry.get("branch") else None}
                        for repo in project.get("repos", []) for entry in entries(repo, include_retired=True)]}


def render_coordination_context(context: dict[str, Any]) -> str:
    def text(value: Any) -> str:
        return str(value).replace("\n", " ").replace("\r", " ") if value is not None else "not recorded"
    owner = context.get("owner") or {}
    result = ("## Project context\n\n"
              f"Project UUID: {text(context.get('uuid'))}\n\n"
              f"Generation: {text(context.get('generation'))}\n\n"
              f"Owner provider: {text(owner.get('provider'))} | session: {text(owner.get('session_id'))}\n\n"
              f"Master plan: {text(context.get('master_path'))}\n\n")
    for entry in context.get("entries", []):
        result += (f"Repository: {text(entry.get('repo'))} | member: {text(entry.get('member'))}\n\n"
                   f"Branch: {text(entry.get('branch'))} | recorded base ref: {text(entry.get('base_ref'))}\n\n"
                   f"Retirement: {text(entry.get('retirement_state'))} | cleanup SHA: {text(entry.get('cleanup_sha'))}\n\n"
                   f"Branch plan: {text(entry.get('subplan_path'))}\n\n")
    return result
