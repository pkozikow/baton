"""Project cleanup."""

import contextlib
import filecmp
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import uuid
from typing import Any

from .errors import ProjectError
from .artifacts import EVIDENCE_FILES
from .gitutil import git, common_dir, branch, inside, worktree_on_branch
from .manifest import (entries, member_name, stack_records, ensure_stack_records, stack_scope,
                       project_entries, managed_links, stack_worktree, note_directories, artifacts_repository)
from .native import merged_pr, registered_stacks, integrated_stack, native_lock, github_repository
from .force_cleanup import ForceCleanupMixin


# Worktree-relative paths the retro reviewed and deliberately did not archive (irrelevant or older conflicting copies).
ARCHIVE_SKIPPED = "archive-skipped.txt"


def board_file(relative: PurePosixPath) -> bool:
    """Baton's own ignored board files in a project's artifact directory: the live board, locks and exports."""
    return any(part == "blackboard-lock.md" or part.startswith(("blackboard.sqlite", ".blackboard-", ".snapshot-"))
               for part in relative.parts)


def local_changes(root: Path, *paths: str) -> list[str]:
    """Modified, staged, deleted or untracked non-ignored paths of a checkout, optionally limited to paths."""
    changed = git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--", *paths).split("\0")
    # The index on its own: a staged edit whose file was put back to HEAD shows in neither other listing.
    staged = git(root, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--", *paths).split("\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).split("\0")
    return sorted(set(filter(None, changed + staged + untracked)))


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
        if project.get("artifacts_branch"):
            record = project["artifacts_branch"]
            print(f"  Artifacts branch {record['branch']}: open and merge its pull request, then remove "
                  f"{record['path']}; the project's artifacts move to {record['final_path']}")
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
        if project.get("artifacts_branch"):
            self.publish_artifacts_branch(project)
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
        if project.get("artifacts_branch"):
            self.tidy_artifacts_branch(project)

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
        failures: list[str] = self.inspect_artifacts_branch(project) if project.get("artifacts_branch") else []
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
        if project.get("artifacts_branch"):
            self.relocate_artifacts(project)
        project["state"] = "archived"
        self.archive_project(project)
        print(f"Slot {project['slot']} is ready for another project.")

    def artifacts_branch_paths(self, project: dict[str, Any]) -> tuple[Path, Path, str, Path]:
        """The artifacts worktree, the shared checkout, the branch and the final directory of a project's record."""
        record = project["artifacts_branch"]
        return Path(record["path"]), Path(record["source"]), record["branch"], Path(record["final_path"])

    def sealed_artifacts_tip(self, project: dict[str, Any]) -> str | None:
        """The commit cleanup merges: the recorded coordination archive commit, or the branch tip without a board."""
        worktree, _shared, name, _final = self.artifacts_branch_paths(project)
        archive = project.get("coordination_archive") or {}
        if archive.get("commit"):
            return str(archive["commit"])
        if not project.get("blackboard") or archive.get("uninitialized"):
            return git(worktree, "rev-parse", "--verify", f"refs/heads/{name}", check=False) or None
        return None

    def inspect_artifacts_branch(self, project: dict[str, Any]) -> list[str]:
        """Refuse every predictable blocker of merging the artifacts branch before cleanup changes anything.

        Runs on every invocation, including a resume; returns the warnings that need confirmation.
        """
        worktree, shared, name, final = self.artifacts_branch_paths(project)
        directory = self.artifact_directory(project)
        record = project["artifacts_branch"]
        if common_dir(shared) != Path(record["common_dir"]):
            raise ProjectError(f"Artifacts repository identity changed: {shared}")
        if shared not in final.parents:
            raise ProjectError(f"The recorded final directory {final} is not inside the shared artifacts checkout {shared}.")
        if not worktree_on_branch(worktree, Path(record["common_dir"]), name):
            raise ProjectError(f"The artifacts worktree {worktree} is missing or not on its branch {name}; "
                               "restore it before cleanup.")
        repository = github_repository(shared)
        if repository is None:
            raise ProjectError(f"Cleanup merges artifacts branch {name} through a GitHub pull request, but the origin "
                               f"of {shared} is not a GitHub repository.")
        default = self.artifact_checkout(shared, [])
        upstream = f"refs/remotes/origin/{default}"
        if not self.fetch_artifacts(shared, default):
            raise ProjectError(f"Cannot fetch origin/{default} in {shared}; cleanup checks the merge against it. "
                               "Retry when the remote is reachable.")
        closed = [pr for pr in self.artifacts_prs(worktree, repository, name) if pr["state"] == "CLOSED"]
        if closed:
            raise ProjectError(f"Pull request #{closed[0]['number']} for artifacts branch {name} was closed without "
                               "merging; ask the user whether to reopen it or keep the branch, then rerun cleanup.")
        tip = git(worktree, "rev-parse", f"refs/heads/{name}")
        base = git(worktree, "merge-base", upstream, tip, check=False)
        outside = self.outside_directory(worktree, base, tip, directory) if base else ["(no merge base)"]
        if outside:
            raise ProjectError(f"Artifacts branch {name} changes files outside {directory} "
                               f"({self.summarize(outside)}); cleanup merges only the project's own directory. "
                               "Remove those changes from the branch before cleanup.")
        coordination = directory.relative_to(worktree) / "coordination"
        dirty = [path for path in local_changes(worktree) if not (
            # An interrupted seal leaves these, and preserve_blackboard recovers them on resume.
            project["state"] == "cleaning" and PurePosixPath(path).parent == PurePosixPath(coordination)
            and PurePosixPath(path).name in EVIDENCE_FILES)]
        if dirty:
            raise ProjectError(f"Uncommitted files in the artifacts worktree {worktree} would not be merged: "
                               f"{self.summarize(dirty)}. Commit them with baton artifacts commit, or remove them, "
                               "before cleanup.")
        head = git(shared, "rev-parse", "HEAD")
        if not self.is_ancestor(shared, head, upstream):
            shared_base = git(shared, "merge-base", upstream, head, check=False)
            unpublished = (self.outside_directory(shared, shared_base, head, final.parent)
                           if shared_base else ["(no merge base)"])
            if unpublished:
                raise ProjectError(f"The shared artifacts checkout {shared} has unpublished commits outside "
                                   f"{final.parent} ({self.summarize(unpublished)}); ask the user to publish or "
                                   "reconcile them before cleanup.")
        incoming = set(self.changed_paths(shared, head, upstream))
        collisions = sorted(incoming & set(local_changes(shared)))
        if collisions:
            raise ProjectError(f"Local files in the shared artifacts checkout {shared} collide with changes in "
                               f"origin/{default} ({self.summarize(collisions)}); ask the user to resolve them "
                               "before cleanup.")
        if final.exists() or final.is_symlink():
            relative = final.relative_to(shared).as_posix()
            sealed = self.sealed_artifacts_tip(project) if project["state"] == "cleaning" else None
            published = sealed and self.is_ancestor(shared, sealed, head)
            # A migrated branch can inherit the project directory from main.
            tracked = (base and directory.relative_to(worktree).as_posix() == relative
                       and git(shared, "cat-file", "-t", f"{base}:{relative}", check=False) == "tree"
                       and git(shared, "cat-file", "-t", f"{head}:{relative}", check=False) == "tree")
            if not final.is_dir() or final.resolve() != final or not (published or tracked):
                raise ProjectError(f"{final} already exists in the shared artifacts checkout; verify the "
                                   "destination before cleanup.")
            changed = local_changes(shared, relative)
            if changed:
                raise ProjectError(f"{final} has local changes in the shared artifacts checkout "
                                   f"({self.summarize(changed)}); ask the user to resolve them before cleanup.")
        ignored = filter(None, git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"))
        extra = [path for path in ignored if not (inside(worktree / path, directory)
                                                  and board_file(PurePosixPath((worktree / path).relative_to(directory))))]
        if extra:
            return [f"{len(extra)} ignored files in the artifacts worktree {worktree} are not Baton board files and "
                    "will be deleted with it:\n" + "\n".join(extra[:20]) + ("\n..." if len(extra) > 20 else "")]
        return []

    def publish_artifacts_branch(self, project: dict[str, Any]) -> None:
        """Merge the sealed artifacts branch, verify the merge and bring the shared checkout up to it."""
        worktree, shared, name, final = self.artifacts_branch_paths(project)
        sealed = self.sealed_artifacts_tip(project)
        if sealed is None:
            raise ProjectError(f"Artifacts branch {name} has no sealed commit; rerun cleanup to seal it.")
        default = self.artifact_checkout(shared, [])
        upstream = f"refs/remotes/origin/{default}"
        # The PR helper checks ancestry first, so a resume after the merge neither pushes nor merges again.
        if not self.merge_artifacts_pr(worktree, default, name, sealed, self.artifact_directory(project),
                                       title=f"Archive project {project['name']}",
                                       body=f"Opened by `baton cleanup` for project {project['name']} "
                                            f"({project['uuid']}). It changes only the project's own directory, "
                                            "and Baton merges it itself.",
                                       # Tidy deletes it once the project is archived.
                                       keep_branch=True):
            raise ProjectError(f"Merging artifacts branch {name} failed (see above); every worktree is intact. "
                               "Rerun cleanup once the cause is fixed.")
        tip = git(worktree, "rev-parse", "--verify", f"refs/heads/{name}", check=False)
        if not self.is_ancestor(shared, sealed, upstream) or tip != sealed:
            raise ProjectError(f"Artifacts branch {name} moved after it was sealed at {sealed[:12]} (now "
                               f"{tip[:12] or 'missing'}); every worktree is intact. Restore the sealed commit, or "
                               "ask the user, before rerunning cleanup.")
        # The project's record decides this, so turning the setting off mid-project does not strand its cleanup.
        self.publish_artifacts(shared, default, artifacts_root=final.parent)
        with self.artifacts_lock(shared):
            self.fast_forward_artifacts(shared, default, artifacts_root=final.parent, any_change=True)
            if branch(shared) != default or not self.is_ancestor(shared, sealed, "HEAD"):
                raise ProjectError(f"The shared artifacts checkout {shared} cannot take the merged artifacts branch "
                                   "(see above); every worktree is intact. Ask the user to resolve that, then rerun "
                                   "cleanup.")
            changed = local_changes(shared, str(final.relative_to(shared)))
            if changed:
                raise ProjectError(f"{final} has local changes in the shared artifacts checkout "
                                   f"({self.summarize(changed)}); every worktree is intact. Ask the user to resolve "
                                   "them, then rerun cleanup.")

    def relocate_artifacts(self, project: dict[str, Any]) -> None:
        """Point every artifact path at the merged directory in the shared checkout, for the archiving save."""
        worktree, shared, name, final = self.artifacts_branch_paths(project)
        sealed = self.sealed_artifacts_tip(project)
        tracked = sealed and git(shared, "ls-tree", "--name-only", sealed, "--",
                                 str(self.artifact_directory(project).relative_to(worktree)))
        if tracked and (final.is_symlink() or not final.is_dir()):
            raise ProjectError(f"{final} is missing from the shared artifacts checkout; the project stays in cleanup "
                               "with its artifacts worktree intact. Restore it and rerun cleanup.")
        project["artifacts_path"] = str(final)
        if project.get("blackboard"):
            project["blackboard"]["directory"] = str(final)
        archive = project.get("coordination_archive")
        if archive and archive.get("directory"):
            archive["directory"] = str(final / "coordination")
        if project.get("follow_up_path"):
            project["follow_up_path"] = str(final / "follow-up.md")

    def tidy_artifacts_branch(self, project: dict[str, Any]) -> None:
        """Remove the merged artifacts worktree and branches, best-effort: the project is already archived."""
        worktree, shared, name, _final = self.artifacts_branch_paths(project)
        record = project["artifacts_branch"]
        # Read before the worktree goes: a project without a board has no recorded sealed commit.
        sealed = self.sealed_artifacts_tip(project)
        leftovers = []
        try:
            with self.repo_lock(artifacts_repository(record)):
                if worktree.exists():
                    git(shared, "worktree", "remove", str(worktree))
                tip = git(shared, "rev-parse", "--verify", f"refs/heads/{name}", check=False)
                if tip:
                    if not self.is_ancestor(shared, tip, "HEAD"):
                        raise ProjectError(f"branch {name} has commits that were not merged")
                    git(shared, "update-ref", "-d", f"refs/heads/{name}", tip)
        except ProjectError as error:
            leftovers.append(f"artifacts worktree {worktree} or local branch {name} in {shared}: {error}")
        if sealed and not self.delete_remote_branch(shared, name, sealed):
            leftovers.append(f"remote branch origin/{name}")
        for leftover in leftovers:
            # Not "its content is merged": a worktree Git refuses to remove may hold changes made after sealing.
            print(f"Leftover (the sealed commit is merged; check the reason before removing it by hand): {leftover}",
                  file=sys.stderr)
