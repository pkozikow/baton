"""Project creation, repository addition and coordinator materialization."""

import json
import os
from pathlib import Path
import re
import sys
import uuid
from typing import Any

from .errors import ProjectError
from .gitutil import git, common_dir, branch, write_json
from .manifest import project_entries, entry_claims, managed_links
from .core import ManagerCore
from .slots import SLOT_NAMES


def manifest_message(name: str, previous: str, manifest: dict[str, Any]) -> str:
    """Name the branches a manifest commit adds or removes, so the coordinator history says what changed."""
    try:
        before = {entry["branch"] for entry in project_entries(json.loads(previous))} if previous else None
    except (ValueError, KeyError, TypeError):
        before = None
    if before is None:
        return f"Record project {name}"
    after = {entry["branch"] for entry in project_entries(manifest)}
    changes = [f"add {b}" for b in sorted(after - before)] + [f"remove {b}" for b in sorted(before - after)]
    return f"Record project {name}: {', '.join(changes) or 'update metadata'}"


class LifecycleMixin(ManagerCore):
    def create(self, name: str, repos: list[str] | None, *, from_uuid: str | None = None) -> None:
        follow_up = None
        if from_uuid is not None:
            if name != "follow-up" or repos is not None:
                raise ProjectError("Use baton create follow-up --from FULL_UUID, or baton create NAME --repos REPOS.")
            # Matching interrupted creation must precede fresh artifact/coordinator checks.
            existing = self.state["projects"].get(self.current_slot())
            if existing and existing["state"] == "creating" and existing.get("follow_up_from") == from_uuid:
                self.finish_create(existing)
                return
            follow_up = self.follow_up_source(from_uuid)
            name, repos = follow_up["name"], follow_up["sources"]
            print(f"Follow-up project: {name}")
        assert repos is not None
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
            raise ProjectError("Project name must be a simple letters/digits/dashes/underscores slug.")
        slot = self.current_slot()
        existing = self.state["projects"].get(slot)
        if existing:
            if existing["state"] == "creating" and existing["name"] == name and existing["requested"] == repos and existing.get("follow_up_from") == from_uuid:
                self.finish_create(existing)
                return
            if existing["state"] != "active":
                raise ProjectError(f"Slot {slot} has an unfinished {existing['state']} operation; finish it before replacing the project.")
        if name in SLOT_NAMES:
            raise ProjectError("Project names p1 through p10 are reserved for coordinator slots.")
        if any(name in (p["name"], p.get("pending_rename", {}).get("name"), *p.get("previous_names", []))
               and p["slot"] != slot for p in self.state["projects"].values()):
            raise ProjectError(f"Project already active or a former name of one: {name}")
        project = {"name": name, "slot": slot, "branch": f"{self.branch_prefix}/{name}", "branch_prefix": self.branch_prefix, "state": "creating", "repos": [],
                   "requested": repos, "uuid": uuid.uuid4().hex, "owner": None}
        project["artifacts_path"] = str(self.artifacts / f"{name}-{project['uuid'][:8]}")
        if follow_up is not None:
            project.update(follow_up_from=from_uuid, follow_up_root_name=follow_up["root"],
                           follow_up_number=follow_up["number"],
                           follow_up_path=str(self.artifact_directory(project) / "follow-up.md"),
                           pending_follow_up_context=follow_up["context"])
            self.preflight_follow_up(project)
        self.check_fresh_artifacts(project)
        self.reserve({("name", name), ("artifacts", project["artifacts_path"])} | set().union(*(entry_claims(r["common_dir"], r["branch"], r["path"])
                                                      for r in self.resolve_repos(repos, project, existing))))
        if existing:
            if not sys.stdin.isatty():
                raise ProjectError(f"Slot {slot} is occupied by {existing['name']}; interactive confirmation is required, or run cleanup explicitly.")
            print(f"Slot {slot} contains project {existing['name']}. Cleanup will remove its registered worktrees and local branches:", file=sys.stderr)
            for repo in project_entries(existing):
                print(f"  {repo['path']} ({repo['branch']})", file=sys.stderr)
            print(f"Clean up {existing['name']} and create {name} instead? [y/N] ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                raise ProjectError("Replacement cancelled; existing project unchanged.")
        self.check_coordinator(existing or project, idle=existing is None, allow_dirty=existing is not None)
        if (not existing or existing["branch"] != project["branch"]) and git(self.repo, "rev-parse", "--verify", f"refs/heads/{project['branch']}", check=False):
            raise ProjectError(f"Coordinator branch already exists: {project['branch']}")
        project["repos"] = self.prepare(repos, project, replacing=existing)
        self.check_fresh_artifacts(project)
        self.preflight_blackboard(project)
        if follow_up is not None:
            self.preflight_follow_up(project)
        if existing:
            os.chdir(self.slots / slot)
            self.cleanup(slot, dry_run=False, allow_unmerged=[])
            self.check_coordinator(project, idle=True)
        self.state["projects"][slot] = project
        self.save()
        self.finish_create(project)

    def check_fresh_artifacts(self, project: dict[str, Any]) -> None:
        """A new UUID must never adopt another project's directory."""
        directory = self.artifact_directory(project)
        if directory.exists() or directory.is_symlink():
            raise ProjectError(f"Artifact destination already exists: {directory}; retry creation for a new UUID.")
        root = self.artifacts_git(directory)
        tracked = git(root, "--literal-pathspecs", "ls-files", "-z", "--", str(directory.relative_to(root)))
        if tracked:
            raise ProjectError(f"Artifact destination still has tracked files: {directory}; retry creation for a new UUID.")

    def finish_create(self, project: dict[str, Any]) -> None:
        coordinator = self.slots / project["slot"]
        if branch(coordinator) == project["slot"]:
            self.check_coordinator(project, idle=True)
            git(coordinator, "switch", "-c", project["branch"])
        elif branch(coordinator) != project["branch"]:
            raise ProjectError("Coordinator branch changed during creation.")
        self.initialize_blackboard(project)
        if project.get("follow_up_from"):
            self.finish_follow_up_context(project)
        self.materialize(project)

    def reconcile_coordinator(self, project: dict[str, Any], previous_links: dict[str, Path] | None = None) -> None:
        self.materialize(project, create_worktrees=False, previous_links=previous_links)

    def materialize(self, project: dict[str, Any], *, create_worktrees: bool = True,
                    previous_links: dict[str, Path] | None = None) -> None:
        coordinator = self.slots / project["slot"]
        if common_dir(coordinator) != common_dir(self.repo) or branch(coordinator) != project["branch"]:
            raise ProjectError("Coordinator identity or branch changed during project operation.")
        links = coordinator / "repos"
        if links.is_symlink() or (links.exists() and not links.is_dir()):
            raise ProjectError("Unexpected coordinator repos directory.")
        manifest_path = coordinator / "project.json"
        if manifest_path.is_symlink():
            raise ProjectError("Unexpected symlink at project manifest.")
        if manifest_path.exists() and not project.get("pending_manifest"):
            if project["state"] == "creating" or not git(coordinator, "ls-files", "--", "project.json"):
                raise ProjectError("Refusing to overwrite an unrelated project manifest.")
        changed = set()
        for args in (("diff", "--name-only", "-z"), ("diff", "--cached", "--name-only", "-z"),
                     ("ls-files", "--others", "--exclude-standard", "-z")):
            changed.update(name for name in git(coordinator, *args).split("\0") if name)
        if changed:
            manifest_path = coordinator / "project.json"
            if changed != {"project.json"} or not project.get("pending_manifest") or not manifest_path.is_file() or manifest_path.read_text() != project["pending_manifest"]:
                raise ProjectError("Coordinator has unrelated or modified files; preserve them before resuming.")
            staged = git(coordinator, "show", ":project.json", check=False)
            staged_names = git(coordinator, "diff", "--cached", "--name-only", "-z").split("\0")
            if "project.json" in staged_names and staged != project["pending_manifest"].strip():
                raise ProjectError("Coordinator has unexpected staged manifest changes.")
        for repo in project_entries(project) if create_worktrees else []:
            if repo.get("retirement_state") == "retiring":
                continue
            path, source = Path(repo["path"]), Path(repo["source"])
            if common_dir(source) != Path(repo["common_dir"]):
                raise ProjectError(f"Source repository identity changed: {source}")
            if repo["step"] == "ready":
                self.validate_repo(repo)
                continue
            if repo["step"] == "pending":
                repo["step"] = "creating"
                self.save()
            if path.exists():
                self.validate_clean_repo(repo)
                if git(path, "rev-parse", "HEAD") != repo["base_sha"]:
                    raise ProjectError("Interrupted creation worktree has unexpected commits.")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with self.repo_lock(repo):
                    existing = git(source, "rev-parse", "--verify", f"refs/heads/{repo['branch']}", check=False)
                    if existing:
                        if existing != repo["base_sha"]:
                            raise ProjectError("Interrupted creation branch has unexpected commits.")
                        git(source, "worktree", "add", str(path), repo["branch"])
                    else:
                        git(source, "worktree", "add", "--no-track", "-b", repo["branch"], str(path), repo["base_sha"])
            repo["step"] = "ready"
            self.save()
        links.mkdir(exist_ok=True)
        expected_links = managed_links(project)
        previous_mappings = (previous_links or {},)
        for link in links.iterdir():
            targets = [mapping[link.name] for mapping in (expected_links, *previous_mappings) if link.name in mapping]
            if not link.is_symlink() or link.readlink() not in targets:
                raise ProjectError(f"Unexpected file at managed link: {link}")
        for mapping in previous_mappings:
            for name, target in mapping.items():
                link = links / name
                if link.is_symlink() and link.readlink() == target and expected_links.get(name) != target:
                    link.unlink()
        for name, target in expected_links.items():
            link = links / name
            if link.is_symlink() and link.readlink() == target:
                continue
            if link.exists() or link.is_symlink():
                raise ProjectError(f"Unexpected file at managed link: {link}")
            link.symlink_to(target, target_is_directory=True)
        manifest = {k: v for k, v in project.items() if k not in ("state", "requested", "pending_add", "pending_manifest", "pending_rename", "pending_member", "pending_retirement", "pending_abandon", "pending_follow_up_context")}
        project["pending_manifest"] = json.dumps(manifest, indent=2) + "\n"
        self.save()
        message = manifest_message(project["name"], git(coordinator, "show", "HEAD:project.json", check=False), manifest)
        write_json(coordinator / "project.json", manifest)
        git(coordinator, "add", "project.json")
        if git(coordinator, "diff", "--cached", "--name-only"):
            git(coordinator, "commit", "--only", "project.json", "-m", message)
        project["state"] = ("renaming" if project.get("pending_rename") else "branching" if project.get("pending_member")
                            else "retiring" if project.get("pending_retirement") else "active")
        project.pop("pending_add", None)
        project.pop("pending_manifest", None)
        project.pop("pending_follow_up_context", None)
        self.save()
        print(f"Project {project['name']} ready in {coordinator}")

    def add(self, names: list[str]) -> None:
        project = self.project()
        if project["state"] == "adding" and project.get("pending_add") == names:
            self.materialize(project)
            return
        if project["state"] != "active":
            raise ProjectError("Finish the outstanding project operation first.")
        self.check_coordinator(project)
        prepared = self.prepare(names, project)
        project["repos"].extend(prepared)
        project.update(state="adding", pending_add=names)
        self.save()
        self.materialize(project)
