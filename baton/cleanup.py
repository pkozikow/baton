"""Project cleanup."""

import contextlib
import filecmp
import os
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any

from .errors import ProjectError
from .gitutil import git, common_dir, branch
from .manifest import (entries, member_name, stack_records, ensure_stack_records, stack_scope,
                       project_entries, managed_links, stack_worktree, note_directories)
from .native import merged_pr, registered_stacks, integrated_stack, native_lock
from .force_cleanup import ForceCleanupMixin


# Worktree-relative paths the retro reviewed and deliberately did not archive (irrelevant or older conflicting copies).
ARCHIVE_SKIPPED = "archive-skipped.txt"


def unarchived_notes(worktree: Path, archive: Path) -> list[str]:
    """Ignored note files with neither an identical copy under the project's artifacts nor a retro skip record."""
    skip_list = archive / ARCHIVE_SKIPPED
    skipped = set(skip_list.read_text().split("\n")) if skip_list.is_file() else set()
    missing = []
    for name in note_directories():
        directory = worktree / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        # Listing files also covers ignore patterns within each configured directory.
        for relative in sorted(filter(None, git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard",
                                                 "-z", "--", name).split("\0"))):
            path = worktree / relative
            if path.is_symlink() or not path.is_file() or relative in skipped:
                continue
            copy = archive / relative
            if not copy.is_file() or not filecmp.cmp(path, copy, shallow=False):
                missing.append(relative)
    return missing


class CleanupMixin(ForceCleanupMixin):
    def cleanup(self, selector: str | None, dry_run: bool, allow_unmerged: list[str], *, abandonment_verified: bool = False,
                force: bool = False) -> None:
        if force:
            self.force_cleanup(selector, dry_run)
            return
        project = self.project(selector)
        if project.get("pending_abandon") and not abandonment_verified:
            if dry_run:
                raise ProjectError(f"Abandonment is pending; resume with: baton abandon {project['uuid']}")
            self.abandon(project["uuid"])
            return
        allowed_repos, allowed_members = self.allowed_unmerged(project, allow_unmerged)

        if project["state"] not in (("active", "cleaning", "abandoning") if abandonment_verified else ("active", "cleaning")):
            raise ProjectError("Finish project creation/addition before cleanup.")
        coordinator, parked, links, failures, dirty_repos, coordinator_status = self.inspect_cleanup(project, dry_run, allowed_repos, allowed_members)
        print(f"{'Would clean' if dry_run else 'Cleaning'} {project['name']} ({project['slot']}):")
        for repo in project_entries(project):
            print(f"  {repo['path']} and local branch {repo['branch']}")
            if repo.get("backup_ref"):
                print(f"    Preserved tip: {repo['backup_ref']}")
        if dry_run:
            if failures:
                print("Cleanup checks failed:\n" + "\n\n".join(failures), file=sys.stderr)
                raise ProjectError("Dry run found checks requiring interactive confirmation during cleanup.")
            return
        if failures:
            print("Cleanup checks failed:\n" + "\n\n".join(failures), file=sys.stderr)
            print("Continuing removes the listed code worktrees and branches. Unmerged commits will be preserved in local archive refs; dirty and untracked files in code worktrees will be permanently discarded. Coordinator changes will be saved in a local Git stash.", file=sys.stderr)
            if not sys.stdin.isatty():
                raise ProjectError("Interactive confirmation is required to override failed cleanup checks.")
            print("Continue cleanup anyway? [y/N] ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                raise ProjectError("Cleanup cancelled; project unchanged.")
        if project.get("pending_abandon"):
            self.abandonment_repositories(project)
        self.check_coordinator(project, idle=parked, allow_dirty=bool(coordinator_status))
        if coordinator_status:
            if git(coordinator, "status", "--porcelain", "--untracked-files=all") != coordinator_status:
                raise ProjectError("Coordinator files changed during cleanup checks; retry to confirm the current changes.")
            # Slots share refs/stash, so find this cleanup's entry by its unique message.
            message = f"baton cleanup {project['name']} {project['uuid']}"
            git(coordinator, "stash", "push", "--include-untracked", "-m", message)
            stash = next((line.split(" ", 1)[0] for line in git(coordinator, "stash", "list", "--format=%H %gs").splitlines()
                          if line.endswith(": " + message)), None)
            if stash is None:
                raise ProjectError(f"Cannot find coordinator stash {message!r}; inspect git stash list in {self.repo}.")
            print(f"Preserved coordinator changes in {self.repo}: stash {stash}")
            self.check_coordinator(project, idle=parked)
        project["state"] = "cleaning"
        self.save()
        self.preserve_blackboard(project)
        for repo in project["repos"]:
            for record in stack_records(repo):
                group = next(group for group in registered_stacks(repo, allow_missing=True)
                             if group[0] is record)
                _, scope, current = group
                if record.get("untracked"):
                    continue
                if current is not None:
                    self.gh_stack(repo, stack_worktree(scope), "unstack", "--local")
                record["untracked"] = True
                self.save()
        with contextlib.ExitStack() as locks:
            for repo in sorted(project["repos"], key=lambda r: r["common_dir"]):
                locks.enter_context(self.repo_lock(repo, ordered=True))
                if stack_records(repo) or (Path(repo["common_dir"]) / "gh-stack").exists():
                    locks.enter_context(self.native_lock(repo, create=True))
                    if any(current is not None for _, _, current in registered_stacks(repo, allow_missing=True)):
                        raise ProjectError("Native stack was recreated during cleanup; preserve it before retrying.")
            self.finish_cleanup(project, coordinator, parked, dirty_repos, links)

    def inspect_cleanup(self, project: dict[str, Any], dry_run: bool,
                        allowed_repos: set[str], allowed_members: set[tuple[str, str]]) -> tuple[Path, bool, Path, list[str], set[str], str]:
        """Inspect a caller-owned snapshot; assignments become durable only when cleanup starts."""
        def allowed(entry: dict[str, Any]) -> bool:
            return entry["id"] in allowed_repos or (entry["id"], member_name(entry) or "root") in allowed_members
        self.preflight_coordination_export(project)
        coordinator = self.slots / project["slot"]
        parked = project["state"] == "cleaning" and branch(coordinator) == project["slot"]
        self.check_coordinator(project, idle=parked, allow_dirty=True)
        links = coordinator / "repos"
        failures: list[str] = []
        coordinator_status = git(coordinator, "status", "--porcelain", "--untracked-files=all")
        if coordinator_status:
            failures.append(f"Coordinator has uncommitted files: {coordinator}\n{coordinator_status}\nThese changes will be saved in a local Git stash before cleanup.")
        dirty_repos: set[str] = set()
        targets: dict[str, str] = {}
        worktree_records: dict[str, list[str]] = {}
        proofs: dict[str, set[str]] = {}
        predecessors: dict[str, str] = {}
        for repo in project["repos"]:
            records = ensure_stack_records(repo)
            context = contextlib.nullcontext()
            if records:
                # Registry locks write holder metadata, so dry runs take only the native lock.
                context = native_lock(repo) if dry_run else self.native_lock(repo)
            with context:
                for record, _scope, current in registered_stacks(repo, allow_missing=project["state"] == "cleaning"):
                    if current is not None:
                        record["cleanup_order"] = [b["branch"] for b in current["branches"]]
                        predecessors.update(zip(record["cleanup_order"][1:], record["cleanup_order"]))
            source = Path(repo["source"])
            with self.repo_lock(repo):
                targets[repo["id"]] = self.base(source, refresh=True, use_cached_default=True)
            worktree_records[repo["id"]] = git(source, "worktree", "list", "--porcelain").split("\n\n")
            if (records or len(entries(repo)) > 1) and repo["id"] not in allowed_repos:
                proofs[repo["id"]] = integrated_stack(repo, targets[repo["id"]])
        if links.exists():
            known = managed_links(project)
            if not links.is_dir() or links.is_symlink():
                raise ProjectError("Unexpected coordinator repos directory.")
            for link in links.iterdir():
                if link.name not in known or not link.is_symlink() or link.readlink() != known[link.name]:
                    raise ProjectError(f"Unmanaged entry in coordinator: {link}")
        for repo in project_entries(project):
            path, source = Path(repo["path"]), Path(repo["source"])
            if repo["step"] == "removed":
                continue
            if common_dir(source) != Path(repo["common_dir"]):
                raise ProjectError(f"Source repository identity changed: {source}")
            for entry in worktree_records[repo["id"]]:
                lines = entry.splitlines()
                if f"branch refs/heads/{repo['branch']}" in lines and f"worktree {path}" not in lines:
                    raise ProjectError(f"Project branch is checked out in an unregistered worktree: {repo['branch']}")
            if path.exists():
                self.validate_clean_repo(repo, allow_dirty=True)
                status = git(path, "status", "--porcelain", "--untracked-files=all")
                if status:
                    dirty_repos.add(repo["path"])
                    failures.append(f"Dirty or untracked files in {path}:\n{status}")
                notes = unarchived_notes(path, self.artifact_directory(project))
                if notes:
                    failures.append(f"{len(notes)} unarchived ignored note files in {path} will be deleted; read "
                                    "the installed baton-retro skill and run the retro first (it archives them "
                                    f"or lists deliberately skipped ones in {ARCHIVE_SKIPPED}):\n"
                                    + "\n".join(notes[:20]) + ("\n..." if len(notes) > 20 else ""))
            elif project["state"] != "cleaning" or repo["step"] != "removing":
                raise ProjectError(f"Registered worktree is missing: {path}")
            ref = f"refs/heads/{repo['branch']}"
            sha = git(source, "rev-parse", "--verify", ref, check=False)
            if not sha and project["state"] == "cleaning" and repo["step"] == "removing":
                continue
            target = targets[repo["id"]]
            result = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", sha, target], capture_output=True)
            needs_proof = result.returncode and not allowed(repo) and (
                not repo.get("backup_ref") or bool(repo.get("merged_pr_sha"))
            )
            verified = bool(sha) and needs_proof and (
                repo["branch"] in proofs[repo["id"]] if repo["id"] in proofs
                else merged_pr(source, repo["branch"], sha, target)
            )
            if not sha:
                raise ProjectError(f"Missing project branch: {repo['branch']} in {source}")
            if needs_proof and not verified:
                # A stack child also lacks its predecessor's commits; list only its own, which its predecessor reports.
                predecessor = predecessors.get(repo["branch"])
                since = target
                if predecessor and git(source, "rev-parse", "--verify", f"refs/heads/{predecessor}", check=False):
                    since = f"refs/heads/{predecessor}"
                commits = git(source, "log", "--oneline", "--max-count=20", f"{since}..{sha}")
                if since != target:
                    commits = f"Own commits on top of {predecessor}:\n{commits or '(none)'}"
                failures.append(f"Unmerged commits in {repo['id']} ({repo['branch']} versus {target}); no matching integrated PR verified.\n{commits}")
            if verified:
                repo["merged_pr_sha"] = sha
            backup_tip = git(source, "rev-parse", "--verify", repo["backup_ref"], check=False) if repo.get("backup_ref") else ""
            if (result.returncode and not repo.get("backup_ref")) or (backup_tip and backup_tip != sha):
                repo["backup_ref"] = f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}"
            repo["cleanup_sha"] = sha
        return coordinator, parked, links, failures, dirty_repos, coordinator_status

    def allowed_unmerged(self, project: dict[str, Any], names: list[str]) -> tuple[set[str], set[tuple[str, str]]]:
        """Split --allow-unmerged values into whole repositories and REPO:MEMBER entries ("root" names an unnamed first checkout)."""
        repos: set[str] = set()
        members: set[tuple[str, str]] = set()
        known = {r["id"]: {member_name(entry) or "root" for entry in entries(r)} for r in project["repos"]}
        for name in names:
            repo_id, separator, member = name.partition(":")
            repo_id = self.resolve_repo(repo_id)
            if repo_id not in known:
                raise ProjectError(f"Unknown repository in --allow-unmerged: {repo_id}")
            if separator and not member:
                # An empty $MEMBER must not silently widen the override to the whole repository.
                raise ProjectError(f"Empty member in --allow-unmerged: {name}")
            if not separator:
                repos.add(repo_id)
            elif member in known[repo_id]:
                members.add((repo_id, member))
            else:
                raise ProjectError(f"Unknown member in --allow-unmerged: {name} (members: {', '.join(sorted(known[repo_id]))})")
        return repos, members

    def finish_cleanup(self, project: dict[str, Any], coordinator: Path, parked: bool,
                       dirty_repos: set[str], links: Path) -> None:
        os.chdir(coordinator)
        ordered = []
        for repo in project["repos"]:
            by_branch = {entry["branch"]: entry for entry in entries(repo)}
            group_order = [name for record in stack_records(repo)
                           for name in record.get("cleanup_order", [entry["branch"] for entry in entries(stack_scope(repo, record))])
                           if name in by_branch]
            ordered.extend(entry for entry in entries(repo) if entry["branch"] not in group_order)
            ordered.extend(by_branch[name] for name in group_order)
        for repo in reversed(ordered):
            if repo["step"] == "removed":
                continue
            repo["step"] = "removing"
            self.save()
            source, path = Path(repo["source"]), Path(repo["path"])
            if repo.get("backup_ref") and repo.get("cleanup_sha"):
                existing_tip = git(source, "rev-parse", "--verify", repo["backup_ref"], check=False)
                if existing_tip and existing_tip != repo["cleanup_sha"]:
                    raise ProjectError("Archive reference changed during cleanup; retry to preserve both tips.")
                git(source, "update-ref", repo["backup_ref"], repo["cleanup_sha"], existing_tip)
            if path.exists():
                self.validate_clean_repo(repo, allow_dirty=repo["path"] in dirty_repos)
                if git(path, "rev-parse", "HEAD") != repo["cleanup_sha"]:
                    raise ProjectError("Branch changed during cleanup; retry checks before removing its worktree.")
                git(source, "worktree", "remove", *(["--force"] if repo["path"] in dirty_repos else []), str(path))
            ref = f"refs/heads/{repo['branch']}"
            if git(source, "rev-parse", "--verify", ref, check=False):
                git(source, "update-ref", "-d", ref, repo["cleanup_sha"])
            repo["step"] = "removed"
            self.save()
        if links.exists():
            for link in links.iterdir():
                link.unlink()
            links.rmdir()
        project["coordinator_sha"] = git(coordinator, "rev-parse", f"refs/heads/{project['branch']}", check=False) or project.get("coordinator_sha")
        project.setdefault("archive_ref", f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}")
        archived_tip = git(self.repo, "rev-parse", "--verify", project["archive_ref"], check=False)
        if archived_tip and archived_tip != project["coordinator_sha"]:
            project["archive_ref"] = f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}"
            archived_tip = ""
        git(self.repo, "update-ref", project["archive_ref"], project["coordinator_sha"], archived_tip)
        self.save()
        if not parked:
            git(coordinator, "switch", project["slot"])
        if git(self.repo, "rev-parse", "--verify", f"refs/heads/{project['branch']}", check=False):
            git(self.repo, "update-ref", "-d", f"refs/heads/{project['branch']}", project["coordinator_sha"])
        project["state"] = "archived"
        self.archive_project(project)
        print(f"Slot {project['slot']} is ready for another project.")
