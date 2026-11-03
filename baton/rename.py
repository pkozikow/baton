"""Resumable project rename."""

import os
from pathlib import Path
import re

from .errors import ProjectError
from .gitutil import git, common_dir, branch, inside
from .manifest import entries, member_name, stack_records, entry_claims
from .native import registered_stacks
from .core import ManagerCore
from .slots import SLOT_NAMES


class RenameMixin(ManagerCore):
    def rename(self, name: str, selector: str | None) -> None:
        project = self.project(selector)
        if any(entry.get("retirement_state") for repo in project["repos"] for entry in entries(repo, include_retired=True)):
            raise ProjectError("Project rename with retired members is not supported; finish this project before renaming.")
        if any(len(entries(repo)) > 1 or member_name(repo) for repo in project["repos"]):
            raise ProjectError("Multi-branch or named-member project rename is not supported; finish this project before renaming.")
        if any(stack_records(repo) or registered_stacks(repo)
               for repo in project["repos"]):
            raise ProjectError("Stacked project rename requires native gh stack modify (currently TUI-only); "
                               "project will not rename branches behind native tracking. Finish this project before renaming.")
        pending = project.get("pending_rename")
        if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name)
                or (name in SLOT_NAMES and not (pending and pending["name"] == name))):
            raise ProjectError("Use a simple project slug; slot names p1 through p10 are reserved.")
        coordinator = self.slots / project["slot"]
        if pending:
            if pending["name"] != name:
                raise ProjectError(f"Finish rename to {pending['name']} first from slot {project['slot']}.")
        else:
            if project["state"] != "active":
                raise ProjectError("Finish the outstanding project operation first.")
            if name == project["name"]:
                raise ProjectError(f"Project is already named {name}.")
            if any(name in (p["name"], p.get("pending_rename", {}).get("name"),
                            *(p.get("previous_names", []) if p is not project else []))
                   for p in self.state["projects"].values()):
                raise ProjectError(f"Project already active or a former name of one: {name}")
            self.reserve({("name", name)} | set().union(*(
                entry_claims(r["common_dir"], f"{project['branch'].rsplit('/', 1)[0]}/{name}", str(self.worktrees / r["id"] / name)) for r in project["repos"])))
            self.check_coordinator(project)
            for repo in project["repos"]:
                self.validate_clean_repo(repo, allow_dirty=True)
            artifact_path = self.artifact_directory(project)
            artifact_stat = artifact_path.stat() if artifact_path.exists() else None
            pending = {"name": name, "old_name": project["name"], "branch": f"{project['branch'].rsplit('/', 1)[0]}/{name}",
                       "old_branch": project["branch"], "artifacts": str(artifact_path),
                       "had_artifacts": artifact_path.exists(),
                       "artifact_identity": [artifact_stat.st_dev, artifact_stat.st_ino] if artifact_stat else None,
                       "repos": [{"id": r["id"], "source": r["source"], "common_dir": r["common_dir"],
                                  "old": r["path"], "new": str(self.worktrees / r["id"] / name)}
                                 for r in project["repos"]]}

        old_branch, new_branch = pending["old_branch"], pending["branch"]
        # Validate every destination before starting, and revalidate journaled paths on resume.
        if common_dir(coordinator) != common_dir(self.repo) or branch(coordinator) not in (old_branch, new_branch):
            raise ProjectError("Coordinator identity or branch changed during rename.")
        if branch(coordinator) == old_branch and git(coordinator, "rev-parse", "--verify", f"refs/heads/{new_branch}", check=False):
            raise ProjectError(f"Branch already exists: {new_branch}")
        links = coordinator / "repos"
        if not links.is_dir() or links.is_symlink():
            raise ProjectError(f"Unexpected coordinator repos directory: {links}")
        actual_links = {link.name for link in links.iterdir()}
        expected_links = {r["id"] for r in pending["repos"]}
        if actual_links - expected_links or (not project.get("pending_rename") and actual_links != expected_links):
            raise ProjectError("Unmanaged entries in coordinator repos directory.")
        for repo in pending["repos"]:
            old, new, source = Path(repo["old"]), Path(repo["new"]), Path(repo["source"])
            if new.is_symlink() or (old.exists() and new.exists()) or (not project.get("pending_rename") and new.exists()):
                raise ProjectError(f"Worktree destination already exists: {new}")
            current = old if old.exists() else new
            if current.is_symlink() or not inside(current.resolve(), self.worktrees):
                raise ProjectError(f"Unsafe worktree path: {current}")
            if common_dir(source) != Path(repo["common_dir"]) or common_dir(current) != Path(repo["common_dir"]):
                raise ProjectError(f"Repository identity changed: {current}")
            if Path(git(current, "rev-parse", "--show-toplevel")).resolve() != current.resolve():
                raise ProjectError(f"Not an exact worktree root: {current}")
            current_branch = branch(current)
            if current_branch not in (old_branch, new_branch):
                raise ProjectError(f"Unexpected branch at {current}")
            if current_branch == old_branch and git(source, "rev-parse", "--verify", f"refs/heads/{new_branch}", check=False):
                raise ProjectError(f"Branch already exists in {repo['id']}: {new_branch}")
            link = links / repo["id"]
            if not link.is_symlink() and not (project.get("pending_rename") and not link.exists()):
                raise ProjectError(f"Unexpected managed link: {link}")
            if link.is_symlink() and link.readlink() not in (old, new):
                raise ProjectError(f"Unexpected managed link: {link}")
        current_artifacts = Path(pending["artifacts"])
        if current_artifacts.is_symlink():
            raise ProjectError("Artifact directory must not be a symlink.")
        if current_artifacts.exists() and not current_artifacts.is_dir():
            raise ProjectError(f"Artifact path is not a directory: {current_artifacts}")
        if current_artifacts.exists():
            info = current_artifacts.stat()
            if [info.st_dev, info.st_ino] != pending["artifact_identity"]:
                raise ProjectError(f"Artifact directory identity changed: {current_artifacts}")
        elif pending["had_artifacts"]:
            raise ProjectError("Project artifact directory disappeared during rename.")
        project.update(state="renaming", pending_rename=pending)
        self.save()
        os.chdir(coordinator)
        for repo in pending["repos"]:
            old, new = Path(repo["old"]), Path(repo["new"])
            current = old if old.exists() else new
            with self.repo_lock(repo):
                if branch(current) == old_branch:
                    git(current, "branch", "-m", new_branch)
                if old.exists():
                    git(Path(repo["source"]), "worktree", "move", str(old), str(new))
            link = links / repo["id"]
            if not link.is_symlink() or link.readlink() != new:
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(new, target_is_directory=True)
        if project.get("blackboard", {}).get("initialized"):
            board = self.board_for(project)
            board.directory = current_artifacts
            board.project = pending["old_name"]
            board.rename(name)
            project["blackboard"]["directory"] = str(current_artifacts)
            project["blackboard"]["project"] = name
            self.save()
        if branch(coordinator) == old_branch:
            git(coordinator, "branch", "-m", new_branch)
        aliases = [*project.get("previous_names", []), pending["old_name"]]
        project.update(name=name, branch=new_branch,
                       previous_names=[alias for i, alias in enumerate(aliases) if alias != name and alias not in aliases[:i]])
        for repo, move in zip(project["repos"], pending["repos"]):
            repo.update(branch=new_branch, path=move["new"])
        self.save()
        self.materialize(project)
        project.pop("pending_rename")
        project["state"] = "active"
        self.save()
        print(f"Renamed {pending['old_name']} to {name}. Remote branches and PRs are unchanged.")
