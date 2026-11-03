"""Confirmed, resumable PR closure followed by ordinary project cleanup."""

import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

from blackboard import BlackboardError
from .core import ManagerCore
from .errors import ProjectError
from .gitutil import common_dir, git
from .manifest import entries, project_entries
from .native import github_repository


def open_repository_prs(source: Path, repository: str, branches: set[str], confirmed: set[int]) -> list[dict[str, Any]]:
    """Exhaust GitHub pages and require exact base and head repository identity."""
    name = repository.removeprefix("github.com/")
    result = []
    page = 1
    while True:
        endpoint = f"repos/{name}/pulls?state=open&per_page=100&page={page}"
        try:
            response = subprocess.run(["gh", "api", endpoint, "--hostname", "github.com"], cwd=source, text=True, capture_output=True, timeout=30)
            if response.returncode:
                raise ProjectError(f"GitHub PR discovery failed for {repository}: {response.stderr.strip()}")
            records = json.loads(response.stdout)
            if not isinstance(records, list):
                raise ValueError("expected a pull request list")
            for pr in records:
                if (not isinstance(pr, dict) or type(pr.get("number")) is not int
                        or not isinstance(pr.get("html_url"), str) or pr.get("state") != "open"
                        or not isinstance(pr.get("head"), dict) or not isinstance(pr.get("base"), dict)
                        or not isinstance(pr["head"].get("ref"), str)
                        or not isinstance(pr["head"].get("sha"), str)
                        or "repo" not in pr["head"]
                        or not isinstance(pr["base"].get("repo"), dict)
                        or not isinstance(pr["base"]["repo"].get("full_name"), str)):
                    raise ValueError("malformed pull request identity")
                if pr["base"]["repo"]["full_name"].lower() != name.lower():
                    raise ValueError(f"pull request base repository {pr['base']['repo']['full_name']} differs from requested "
                                     f"{name}; verify origin and the GitHub repository identity before retrying")
                head = pr["head"]["repo"]
                if head is None:
                    # Deleted forks have no head repository; a confirmed project PR cannot disappear this way.
                    if pr["number"] in confirmed:
                        raise ValueError("confirmed pull request head repository is unavailable")
                    continue
                if not isinstance(head, dict) or not isinstance(head.get("full_name"), str):
                    raise ValueError("malformed pull request head repository")
                if pr["number"] in confirmed and (head["full_name"].lower() != name.lower() or pr["head"]["ref"] not in branches):
                    raise ValueError("confirmed pull request identity changed")
                if pr["head"]["ref"] in branches and head["full_name"].lower() == name.lower():
                    result.append({"repository": repository, "number": pr["number"], "url": pr["html_url"], "branch": pr["head"]["ref"], "head_sha": pr["head"]["sha"]})
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise ProjectError(f"Cannot discover all PRs for {repository}: {error}") from error
        if len(records) < 100:
            return result
        page += 1


class AbandonMixin(ManagerCore):
    def abandonment_repositories(self, project: dict[str, Any]) -> dict[str, dict[str, str | None]]:
        identities = {}
        for repo in project["repos"]:
            source = Path(repo["source"])
            if common_dir(source) != Path(repo["common_dir"]):
                raise ProjectError(f"Source repository identity changed: {source}")
            identities[repo["id"]] = {"origin": git(source, "config", "--get", "remote.origin.url", check=False),
                                      "repository": github_repository(source)}
        pending = project.get("pending_abandon")
        if pending is not None and pending.get("repositories") != identities:
            expected = pending.get("repositories")
            raise ProjectError(f"Origin repository identities changed during pending abandonment. Restore the recorded "
                               f"origin URLs {expected!r} before resuming: baton abandon {project['uuid']}")
        return identities

    def abandonment_branches(self, project: dict[str, Any]) -> dict[str, dict[str, Any]]:
        inventory = {repo["id"]: {"common_dir": repo["common_dir"],
                                   "branches": sorted({entry["branch"] for entry in entries(repo, include_retired=True)})}
                     for repo in project["repos"]}
        saved = project.get("pending_abandon", {}).get("branches")
        if saved is not None:
            if (not isinstance(saved, dict) or saved.keys() != inventory.keys() or any(
                    not isinstance(saved[key], dict) or saved[key].get("common_dir") != value["common_dir"]
                    or not isinstance(saved[key].get("branches"), list)
                    or not all(isinstance(name, str) and name for name in saved[key]["branches"])
                    or not set(value["branches"]).issubset(saved[key]["branches"])
                    for key, value in inventory.items())):
                raise ProjectError(f"Saved abandonment branch inventory is inconsistent; restore its recorded scope before "
                                   f"resuming: baton abandon {project['uuid']}")
            return saved
        named_first = {repo["id"] for repo in project["repos"] if repo.get("first_member")}
        if not project.get("previous_names") and not named_first:
            return inventory
        try:
            candidates = [f"refs/heads/{project['branch']}", project.get("coordinator_sha"), project.get("archive_ref")]
            anchor = next((sha for ref in candidates if ref
                           and (sha := git(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False))), None)
            if anchor is None:
                raise ProjectError("The recorded coordinator project ref/commit is unavailable.")
            tip = json.loads(git(self.repo, "show", f"{anchor}:project.json"))
            if not isinstance(tip, dict) or tip.get("uuid") != project["uuid"]:
                raise ProjectError("The coordinator project ref does not preserve this UUID's manifest.")
            seen_names = set()
            seen_repos = set()
            unnamed_roots = set()
            # Deletion records have no manifest blob; other history read failures must remain fatal.
            for commit in git(self.repo, "log", "--full-history", "--diff-filter=ACMRTUXB", "--format=%H", anchor, "--", "project.json").splitlines():
                manifest = json.loads(git(self.repo, "show", f"{commit}:project.json"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("uuid"), str):
                    raise ProjectError("A coordinator history manifest has no valid project identity.")
                if manifest["uuid"] != project["uuid"]:
                    continue
                if not isinstance(manifest.get("name"), str) or not isinstance(manifest.get("repos"), list):
                    raise ProjectError("A project history manifest has invalid name/repository data.")
                seen_names.add(manifest["name"])
                for repo in manifest["repos"]:
                    if not isinstance(repo, dict):
                        raise ProjectError("A project history manifest has an invalid repository record.")
                    current = inventory.get(repo.get("id"))
                    if current is None or repo.get("common_dir") != current["common_dir"]:
                        continue
                    if not isinstance(repo.get("members", []), list):
                        raise ProjectError("A project history manifest has invalid member records.")
                    recorded = entries(repo, include_retired=True)
                    if any(not isinstance(entry, dict) or not isinstance(entry.get("branch"), str) or not entry["branch"]
                           or entry.get("common_dir") != current["common_dir"] for entry in recorded):
                        raise ProjectError("A project history manifest has invalid branch identities.")
                    current["branches"] = sorted(set(current["branches"]) | {entry["branch"] for entry in recorded})
                    seen_repos.add(repo["id"])
                    if not repo.get("first_member"):
                        unnamed_roots.add(repo["id"])
            if not named_first.issubset(unnamed_roots):
                raise ProjectError("Required original first-member manifests are missing from coordinator history.")
            if not {project["name"], *project.get("previous_names", [])}.issubset(seen_names) or seen_repos != inventory.keys():
                raise ProjectError("Required former-name/repository manifests are missing from coordinator history.")
        except (ProjectError, ValueError, TypeError, KeyError, OSError) as error:
            raise ProjectError(f"Cannot establish exact project branch history: {error} Restore the recorded "
                               f"coordinator project.json history, then retry: baton abandon {project['uuid']}") from error
        return inventory

    def abandonment_prs(self, project: dict[str, Any], branches: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        identities = self.abandonment_repositories(project)
        branches = branches if branches is not None else self.abandonment_branches(project)
        groups: dict[str, dict[str, Any]] = {}
        for repo in project["repos"]:
            repository = identities[repo["id"]]["repository"]
            if repository is None:
                print(f"  {repo['id']}: no supported GitHub PR integration; local cleanup only.")
                continue
            group = groups.setdefault(repository.lower(), {"repository": repository, "source": Path(repo["source"]), "branches": set()})
            group["branches"].update(branches[repo["id"]]["branches"])
        selected = {}
        pending = project.get("pending_abandon", {})
        known = [*pending.get("closed", []), *pending.get("attempted", []), *pending.get("approved", [])]
        for identity, group in groups.items():
            confirmed = {pr["number"] for pr in known
                         if pr["repository"].lower() == identity}
            for pr in open_repository_prs(group["source"], group["repository"], group["branches"], confirmed):
                selected[(identity, pr["number"])] = pr
        return sorted(selected.values(), key=lambda pr: (pr["repository"], pr["number"]))

    def abandonment_snapshot(self, project: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        repositories = self.abandonment_repositories(project)
        branches = self.abandonment_branches(project)
        snapshot = copy.deepcopy(project)
        if snapshot["state"] == "abandoning":
            snapshot["state"] = "active"
        coordinator, _parked, _links, failures, _dirty, status = self.inspect_cleanup(snapshot, True, set(), set())
        fingerprint = {"coordinator": [git(coordinator, "symbolic-ref", "--short", "HEAD"), git(coordinator, "rev-parse", "HEAD"), status], "entries": []}
        for repo in project_entries(snapshot):
            path = Path(repo["path"])
            fingerprint["entries"].append([repo["branch"], repo.get("cleanup_sha"),
                                            git(path, "status", "--porcelain", "--untracked-files=all") if path.exists() else None])
        fingerprint["branches"] = branches
        fingerprint["repositories"] = repositories
        fingerprint["warnings"] = failures
        return fingerprint, failures

    def abandon(self, selector: str | None) -> None:
        project = self.project(selector)
        pending = project.get("pending_abandon")
        if project["state"] not in (("active",) if pending is None else ("active", "abandoning", "cleaning")):
            raise ProjectError("Finish the outstanding project operation before abandonment.")
        resume = f"baton abandon {project['uuid']}"
        if not sys.stdin.isatty():
            raise ProjectError(f"Interactive confirmation is required for abandonment; run: {resume}")
        # Inspection uses a copy: dry-run archive refs and proofs must not change cancellation state.
        fingerprint, failures = self.abandonment_snapshot(project)
        prs = self.abandonment_prs(project, fingerprint["branches"])
        approved = pending.get("approved", []) if pending else []
        attempted = {(pr["repository"].lower(), pr["number"])
                     for pr in [*pending.get("attempted", []), *pending.get("closed", [])]} if pending else set()
        if (pending is None or pending.get("fingerprint") != fingerprint
                or any(pr not in approved or (pr["repository"].lower(), pr["number"]) in attempted for pr in prs)):
            print(f"Abandon {project['name']} ({project['uuid']}) [{project['state']}]:")
            for repo in project_entries(project, include_retired=True):
                print(f"  {repo['id']}: {repo['branch']} ({repo['path']})")
            for pr in prs:
                print(f"  Close {pr['repository']} #{pr['number']}: {pr['url']}")
            print("Local worktrees and branches will be removed. Unmerged tips are preserved in local archive refs.")
            if failures:
                print("Cleanup warnings/blockers:\n" + "\n\n".join(failures))
            print("PR closures may succeed even if later cleanup fails. Run the retrospective before cleanup to preserve notes.")
            print("Abandon this project? [y/N] ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                raise ProjectError("Abandonment cancelled; project unchanged.")
            current, _ = self.abandonment_snapshot(project)
            if current != fingerprint or self.abandonment_prs(project, current["branches"]) != prs:
                raise ProjectError(f"Project or PR scope changed during confirmation; rerun: {resume}")
            # Keep identities after closure; only the latest explicit consent can approve a head version.
            approvals = {(pr["repository"].lower(), pr["number"]): pr
                         for pr in [*(pending.get("closed", []) if pending else []),
                                    *(pending.get("attempted", []) if pending else []), *approved, *prs]}
            project["pending_abandon"] = {"approved": list(approvals.values()), "branches": fingerprint["branches"], "repositories": fingerprint["repositories"], "fingerprint": fingerprint, "attempted": pending.get("attempted", []) if pending else [], "closed": pending.get("closed", []) if pending else []}
            if project["state"] == "active":
                project["state"] = "abandoning"
            self.save()
            pending = project["pending_abandon"]
        try:
            for pr in prs:
                # A failed/terminated close can already have reached GitHub. An open PR on retry
                # needs renewed approval rather than silently closing a possible reopening.
                if pr not in pending.setdefault("attempted", []):
                    pending["attempted"].append(pr)
                self.save()
                response = subprocess.run(["gh", "pr", "close", str(pr["number"]), "--repo", pr["repository"]],
                                          text=True, capture_output=True, timeout=30)
                if response.returncode:
                    raise ProjectError(response.stderr.strip() or "PR closure failed")
                if pr not in pending["closed"]:
                    pending["closed"].append(pr)
                    self.save()
            remaining = self.abandonment_prs(project)
            if remaining:
                raise ProjectError("Open/reopened PRs remain; renewed confirmation is required")
        except (ProjectError, OSError, subprocess.TimeoutExpired) as error:
            raise ProjectError(f"Abandonment paused: {error}. Recorded {len(pending['closed'])} successful PR closure(s); a failed call may already have reached GitHub. Resume: {resume}") from error
        try:
            self.cleanup(project["uuid"], False, [], abandonment_verified=True)
        except (ProjectError, BlackboardError, sqlite3.Error, OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise ProjectError(f"Abandonment cleanup paused: {error}; resume: {resume}") from error
