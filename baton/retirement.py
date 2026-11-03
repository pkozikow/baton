"""Retire integrated or abandoned members without closing their project."""

import contextlib
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import uuid
from typing import Any

from .cleanup import unarchived_notes
from .core import ManagerCore
from .errors import ProjectError
from .gitutil import git, common_dir
from .manifest import (entries, member_name, project_entries, managed_links,
                       ensure_stack_records, stack_records, stack_scope)
from .native import registered_stacks, integrated_stack, retirement_pr


class RetirementMixin(ManagerCore):
    def retire_members(self, repo_id: str | None, member: str | None, selector: str | None,
                       dry_run: bool, *, prune: bool = False) -> None:
        project = self.project(selector)
        signature = {"repo": repo_id, "member": member, "prune": prune}
        pending = project.get("pending_retirement")
        if pending:
            if any(pending.get(key) != value for key, value in signature.items()):
                raise ProjectError("Repeat the outstanding branch cleanup/prune command to finish retirement first.")
            if dry_run:
                self.print_retirement(project, pending["targets"], True)
                return
            self.finish_retirement(project)
            return
        if project["state"] != "active":
            raise ProjectError("Finish the outstanding project operation before retiring members.")
        self.check_coordinator(project)
        repos = [repo for repo in project["repos"] if repo_id is None or repo["id"] == repo_id]
        if not repos:
            raise ProjectError(f"Repository is not part of project: {repo_id}")
        if not prune:
            if len(repos) != 1 or member is None:
                raise ProjectError("Specify a repository and member for branch cleanup.")
            matches = [entry for entry in entries(repos[0], include_retired=True)
                       if member in (member_name(entry), entry["branch"])
                       or (member == "root" and member_name(entry) is None)]
            if len(matches) != 1:
                raise ProjectError(f"Member is not registered or is ambiguous: {member}")
            if matches[0].get("retirement_state") == "retired":
                print(f"Already retired: {repos[0]['id']} {matches[0]['branch']}")
                return
            candidates = matches
        else:
            candidates = [entry for repo in repos for entry in entries(repo)]
        targets: list[dict[str, Any]] = []
        for repo in repos:
            selected = [entry for entry in candidates if entry["id"] == repo["id"]]
            if not selected:
                continue
            try:
                with self.repo_lock(repo) if not dry_run else contextlib.nullcontext():
                    target = self.base(Path(repo["source"]), refresh=not dry_run, use_cached_default=True)
                tips = {entry["branch"]: git(Path(repo["source"]), "rev-parse", "--verify", f"refs/heads/{entry['branch']}", check=False)
                        for entry in selected}
                evidence: dict[str, dict[str, Any]] = {}
                integrated = integrated_stack(repo, target, evidence=evidence)
                with self.native_lock(repo) if stack_records(repo) and not dry_run else contextlib.nullcontext():
                    registered_stacks(repo)
            except ProjectError as error:
                if not prune:
                    raise
                print(f"Skipping {repo['id']}: {error}", file=sys.stderr)
                continue
            for entry in selected:
                try:
                    is_integrated = entry["branch"] in integrated
                    abandoned = retirement_pr(Path(entry["source"]), entry["branch"], tips[entry["branch"]],
                                              integrated=is_integrated)
                    if not is_integrated and abandoned is None:
                        raise ProjectError("exact local tip is not verified integrated into the default branch "
                                           "or abandoned by a closed pull request")
                    if abandoned is not None:
                        evidence[entry["branch"]] = abandoned
                    sha, status, warnings = self.retirement_check(project, entry)
                    if sha != tips[entry["branch"]]:
                        raise ProjectError("branch changed during integration checks; retry")
                    if prune and warnings:
                        raise ProjectError("; ".join(warnings))
                    targets.append({"repo": repo["id"], "branch": entry["branch"], "sha": sha,
                                    "status": status, "warnings": warnings,
                                    "outcome": "integrated" if is_integrated else "abandoned",
                                    "base_ref": target, "base_sha": git(Path(repo["source"]), "rev-parse", target),
                                    "proofs": evidence, "backup_ref": f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}"})
                except ProjectError as error:
                    if not prune:
                        raise ProjectError(f"Cannot retire {entry['id']} {entry['branch']}: {error}") from error
                    print(f"Skipping {entry['id']} {entry['branch']}: {error}", file=sys.stderr)
        for repo in repos:
            for record in stack_records(repo):
                live = {entry["branch"] for entry in entries(stack_scope(repo, record))}
                selected = [target for target in targets if target["repo"] == repo["id"] and target["branch"] in live]
                if any(target["outcome"] == "abandoned" for target in selected) and len(selected) != len(live):
                    message = (f"Cannot retire abandoned members of native stack {record['root']} separately; "
                               "retire the whole group together once all members are integrated or abandoned. "
                               "Removing an unmerged member would break native stack tracking.")
                    if not prune:
                        raise ProjectError(message)
                    print(f"Skipping {repo['id']}: {message}", file=sys.stderr)
                    targets = [target for target in targets
                               if target["repo"] != repo["id"] or target["branch"] not in live]
        self.print_retirement(project, targets, dry_run)
        if not targets:
            print("No eligible members to retire; project remains active.")
            return
        warnings = [warning for target in targets for warning in target["warnings"]]
        if dry_run:
            if warnings:
                raise ProjectError("Retirement checks require interactive confirmation:\n" + "\n".join(warnings))
            return
        if prune or warnings:
            if warnings:
                print("Retirement checks failed:\n" + "\n\n".join(warnings), file=sys.stderr)
                print("Dirty/untracked files and unarchived ignored notes in these worktrees will be permanently discarded.", file=sys.stderr)
            self.confirm_retirement("Retire the listed members and keep the project active?")
        previous_links = managed_links(project)
        project["pending_retirement"] = {**signature, "targets": targets,
                                         "previous_links": {name: str(path) for name, path in previous_links.items()}}
        project["state"] = "retiring"
        self.save()
        self.finish_retirement(project)

    @staticmethod
    def confirm_retirement(prompt: str) -> None:
        if not sys.stdin.isatty():
            raise ProjectError("Interactive confirmation is required for this retirement; use --dry-run to inspect it.")
        print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            raise ProjectError("Retirement cancelled; project unchanged.")

    @staticmethod
    def print_retirement(project: dict[str, Any], targets: list[dict[str, Any]], dry_run: bool) -> None:
        print(f"{'Would retire' if dry_run else 'Retiring'} members of {project['name']} ({project['slot']}):")
        for target in targets:
            entry = next(entry for entry in project_entries(project, include_retired=True)
                         if (entry["id"], entry["branch"]) == (target["repo"], target["branch"]))
            print(f"  {entry['id']} {entry['branch']}: {entry['path']}\n    Preserved tip: {target['backup_ref']}")

    def retirement_check(self, project: dict[str, Any], entry: dict[str, Any]) -> tuple[str, str, list[str]]:
        self.validate_clean_repo(entry, allow_dirty=True)
        path, source = Path(entry["path"]), Path(entry["source"])
        records = git(source, "worktree", "list", "--porcelain").split("\n\n")
        for record in records:
            lines = record.splitlines()
            owns_path = f"worktree {path}" in lines
            owns_branch = f"branch refs/heads/{entry['branch']}" in lines
            if owns_path != owns_branch:
                raise ProjectError(f"Branch/worktree registration changed: {entry['branch']}")
            if owns_path and any(line == "locked" or line.startswith("locked ") for line in lines):
                raise ProjectError(f"Worktree is locked: {path}; unlock it explicitly before retirement")
        sha = git(source, "rev-parse", "--verify", f"refs/heads/{entry['branch']}")
        if sha != git(path, "rev-parse", "HEAD"):
            raise ProjectError(f"Worktree tip differs from its branch: {path}")
        status = git(path, "status", "--porcelain", "--untracked-files=all")
        warnings = [f"Dirty or untracked files in {path}:\n{status}"] if status else []
        notes = unarchived_notes(path, self.artifact_directory(project))
        if notes:
            warnings.append(f"Unarchived ignored notes in {path}; read the installed baton-retro skill and archive them first:\n"
                            + "\n".join(notes[:20]) + ("\n..." if len(notes) > 20 else ""))
        return sha, status, warnings

    def finish_retirement(self, project: dict[str, Any]) -> None:
        pending = project["pending_retirement"]
        targets = pending["targets"]
        selected = {(target["repo"], target["branch"]) for target in targets}
        coordinator = self.slots / project["slot"]
        self.check_coordinator(project, allow_dirty=bool(project.get("pending_manifest")))
        for target in targets:
            entry = next(entry for entry in project_entries(project, include_retired=True)
                         if (entry["id"], entry["branch"]) == (target["repo"], target["branch"]))
            if entry.get("retirement_state") != "retired":
                abandoned = target.get("outcome") == "abandoned"
                proof = retirement_pr(Path(entry["source"]), entry["branch"], target["sha"], integrated=not abandoned)
                if abandoned and proof is None:
                    raise ProjectError(f"Closed pull request no longer verifies the abandoned tip of {entry['branch']}.")
        # Never delete the process's own working directory before invoking further tools.
        os.chdir(coordinator)
        for repo in project["repos"]:
            if not any(target["repo"] == repo["id"] for target in targets):
                continue
            ensure_stack_records(repo)
            for record in list(stack_records(repo)):
                scope = stack_scope(repo, record)
                live = entries(scope)
                whole_group = all((entry["id"], entry["branch"]) in selected for entry in live)
                with self.native_lock(repo):
                    groups = registered_stacks(repo, allow_missing=bool(record.get("retirement_untracking")))
                    current = next(current for other, _, current in groups if other is record)
                if whole_group:
                    record["retirement_untracking"] = True
                    self.save()
                    if current is not None:
                        self.gh_stack(repo, Path(live[0]["path"]), "unstack", "--local")
                    record.update(closed=True, untracked=True)
                    self.save()
                else:
                    group_targets = [entry for entry in live if (entry["id"], entry["branch"]) in selected]
                    if not group_targets:
                        continue
                    merged = {item["branch"] for item in current["branches"] if (item.get("pullRequest") or {}).get("merged")}
                    if any(entry["branch"] not in merged for entry in group_targets):
                        # view refreshes native PR state but never rewrites or pushes branches.
                        self.gh_stack(repo, Path(live[0]["path"]), "view", "--json")
                        with self.native_lock(repo):
                            current = next(current for other, _, current in registered_stacks(repo) if other is record)
                        merged = {item["branch"] for item in current["branches"] if (item.get("pullRequest") or {}).get("merged")}
                        if any(entry["branch"] not in merged for entry in group_targets):
                            raise ProjectError("Native tracking has not marked selected stack members merged. "
                                               "Refresh their PR state before retrying retirement; no branches were removed.")
        for target in targets:
            entry = next(entry for entry in project_entries(project, include_retired=True)
                         if (entry["id"], entry["branch"]) == (target["repo"], target["branch"]))
            if entry.get("retirement_state") == "retired":
                continue
            repo = next(repo for repo in project["repos"] if repo["id"] == entry["id"])
            path, source = Path(entry["path"]), Path(entry["source"])
            with self.repo_lock(repo), self.native_lock(repo, create=True):
                if common_dir(source) != Path(entry["common_dir"]):
                    raise ProjectError(f"Source repository identity changed: {source}")
                registered_stacks(repo)
                sha = git(source, "rev-parse", "--verify", f"refs/heads/{entry['branch']}", check=False)
                if sha and sha != target["sha"]:
                    raise ProjectError(f"Branch advanced since retirement checks: {entry['branch']}; preserve changes before retrying.")
                archived = git(source, "rev-parse", "--verify", target["backup_ref"], check=False)
                if archived and archived != target["sha"]:
                    raise ProjectError("Retirement archive reference changed; preserve it before retrying.")
                if not sha and (entry.get("retirement_state") != "retiring" or archived != target["sha"]):
                    raise ProjectError(f"Branch disappeared outside retirement: {entry['branch']}")
                if path.exists():
                    checked_sha, status, warnings = self.retirement_check(project, entry)
                    if checked_sha != target["sha"]:
                        raise ProjectError("Worktree tip changed during retirement; retry after preserving changes.")
                    if status != target["status"] or warnings != target["warnings"]:
                        self.confirm_retirement(f"Files changed since confirmation in {path}; discard the current dirty files/notes?")
                        target.update(status=status, warnings=warnings)
                        self.save()
                elif entry.get("retirement_state") != "retiring":
                    raise ProjectError(f"Worktree disappeared outside retirement: {path}")
                else:
                    records = git(source, "worktree", "list", "--porcelain").split("\n\n")
                    if any(f"branch refs/heads/{entry['branch']}" in record.splitlines() for record in records):
                        raise ProjectError(f"Retiring branch is still checked out in a registered worktree: {entry['branch']}; "
                                           "preserve or remove that worktree before retrying.")
                entry.update(retirement_state="retiring", cleanup_sha=target["sha"], backup_ref=target["backup_ref"])
                self.save()
                git(source, "update-ref", target["backup_ref"], target["sha"], archived)
                if path.exists():
                    git(source, "worktree", "remove", *(["--force"] if target["status"] else []), str(path))
                if sha:
                    git(source, "update-ref", "-d", f"refs/heads/{entry['branch']}", target["sha"])
                entry.update(retirement_state="retired", step="removed",
                             retirement={"tip": target["sha"], "outcome": target.get("outcome", "integrated"),
                                         "base_ref": target["base_ref"],
                                         "base_sha": target["base_sha"], "pull_requests": target["proofs"],
                                         "archive_ref": target["backup_ref"],
                                         "retired_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                self.save()
        previous_links = {name: Path(path) for name, path in pending["previous_links"].items()}
        self.reconcile_coordinator(project, previous_links)
        pending.pop("previous_links", None)
        project.pop("pending_retirement")
        project["state"] = "active"
        self.save()
        for name, path in managed_links(project).items():
            old = previous_links.get(name)
            if old is not None and path != old:
                print(f"Updated repos/{name}: {old} -> {path}")
        print(f"Retired {len(targets)} member(s); project {project['name']} remains active in {coordinator}.")
