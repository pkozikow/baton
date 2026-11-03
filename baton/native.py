"""Native gh stack catalog access and merged-PR proof."""

import contextlib
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterator

from .errors import ProjectError, NativeOperationPending
from .gitutil import git
from .manifest import entries, stack_records, entry_stack_root, stack_scope


def github_repository(source: Path) -> str | None:
    remote = git(source, "config", "--get", "remote.origin.url", check=False)
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    return f"github.com/{match.group(1)}" if match else None


def retirement_pr(source: Path, branch_name: str, sha: str, *, integrated: bool) -> dict[str, Any] | None:
    """Block open PRs and return exact-tip evidence when a branch was abandoned."""
    repository = github_repository(source)
    if repository is None:
        return None
    for state in (("open",) if integrated else ("open", "closed")):
        try:
            response = subprocess.run(
                ["gh", "pr", "list", "--repo", repository, "--state", state, "--head", branch_name,
                 "--limit", "100", "--json", "state,headRefName,headRefOid,baseRefName,mergeCommit"],
                cwd=source, text=True, capture_output=True, timeout=20,
            )
            if response.returncode:
                raise ProjectError(f"Cannot verify pull request state for {branch_name}; retry before retirement.")
            prs = json.loads(response.stdout)
            if not isinstance(prs, list) or any(
                not isinstance(pr, dict) or pr.get("state") not in ("OPEN", "CLOSED", "MERGED")
                or not isinstance(pr.get("headRefName"), str) for pr in prs
            ):
                raise ValueError("invalid pull request response")
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise ProjectError(f"Cannot verify pull request state for {branch_name}; retry before retirement.") from error
        matching = [pr for pr in prs if pr["headRefName"] == branch_name]
        if any(pr["state"] == "OPEN" for pr in matching):
            raise ProjectError(f"Open pull request for {branch_name}; close or merge it before retirement.")
        if state == "closed":
            return next((dict(pr) for pr in matching if pr["state"] == "CLOSED" and pr.get("headRefOid") == sha), None)
    return None


def merged_pr(source: Path, branch_name: str, sha: str, target: str) -> bool:
    if not target.startswith("refs/remotes/origin/"):
        return False
    repository = github_repository(source)
    if repository is None:
        return False
    base = target.removeprefix("refs/remotes/origin/")
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repository,
             "--state", "merged", "--head", branch_name, "--base", base,
             "--limit", "100", "--json", "state,headRefName,headRefOid,baseRefName,mergeCommit"],
            cwd=source, text=True, capture_output=True, timeout=20,
        )
        if result.returncode:
            print("Merged-PR verification failed; cleanup still requires proof of integration.", file=sys.stderr)
            return False
        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            return False
        for pr in prs:
            if not isinstance(pr, dict) or (
                pr.get("state") != "MERGED" or pr.get("headRefName") != branch_name
                or pr.get("headRefOid") != sha or pr.get("baseRefName") != base
            ):
                continue
            merge = pr.get("mergeCommit")
            commit = merge.get("oid") if isinstance(merge, dict) else None
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                continue
            if subprocess.run(
                ["git", "-C", str(source), "merge-base", "--is-ancestor", commit, target],
                capture_output=True,
            ).returncode == 0:
                return True
    except (OSError, subprocess.TimeoutExpired, ValueError):
        print("Merged-PR verification unavailable; cleanup still requires proof of integration.", file=sys.stderr)
    return False


def require_active_top(repo: dict[str, Any], stack: dict[str, Any]) -> None:
    top = stack["branches"][-1]["branch"]
    if any(entry["branch"] == top and entry.get("retirement_state") == "retired"
           for entry in entries(repo, include_retired=True)):
        raise ProjectError(f"Native stack top {top} is retired; it cannot be extended from a surviving earlier member. "
                           "Add a new independent member without --after, then start a new native group if needed.")


def require_top_changes(repo: dict[str, Any], stack: dict[str, Any]) -> None:
    require_active_top(repo, stack)
    default = repo["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
    if stack["trunk"]["branch"] != default:
        raise ProjectError(f"Native stack trunk must be {default}, not {stack['trunk']['branch']}.")
    if all((b.get("pullRequest") or {}).get("merged") for b in stack["branches"]):
        raise ProjectError("This native stack is fully merged; start another stack from a new independent member, "
                           "or clean up the project when all its work is finished.")
    top = stack["branches"][-1]
    if (top.get("pullRequest") or {}).get("merged"):
        return
    parent = next((b["branch"] for b in reversed(stack["branches"][:-1])
                   if not (b.get("pullRequest") or {}).get("merged")), repo["base_ref"])
    result = subprocess.run(["git", "-C", repo["source"], "diff", "--quiet",
                             f"{parent}...{top['branch']}", "--"], text=True, capture_output=True)
    if result.returncode == 0:
        raise ProjectError(f"Top layer {top['branch']} has no changes against {parent}. "
                           "Implement and commit this layer before adding another; do not create an empty project base.")
    if result.returncode != 1:
        raise ProjectError(result.stderr.strip() or "Cannot verify top-layer changes.")


def native(path: Path, *args: str) -> str:
    result = subprocess.run(["gh", "stack", *args], cwd=path, text=True,
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ProjectError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


@contextlib.contextmanager
def native_lock(repo: dict[str, Any], *, create: bool = False) -> Iterator[None]:
    lock = Path(repo["common_dir"]) / "gh-stack-operation.lock"
    with lock.open("a" if create else "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProjectError(f"Native gh stack operation is running in {repo['id']}; retry when idle.") from error
        yield


def native_catalog(repo: dict[str, Any]) -> list[dict[str, Any]]:
    # Reading the shared catalog avoids view's network refresh and cache writes in dry runs.
    directory = Path(repo["common_dir"])
    for kind in ("rebase", "modify", "migration"):
        journal = directory / ("gh-stack-migration" if kind == "migration" else f"gh-stack-{kind}-state")
        if journal.exists():
            raise NativeOperationPending(f"Finish or abort the native gh stack {kind} operation in {repo['id']} first.")
    catalog = directory / "gh-stack"
    data = json.loads(catalog.read_text()) if catalog.exists() else {"stacks": []}
    if not isinstance(data, dict) or not isinstance(data.get("stacks"), list):
        raise ProjectError("Invalid native stack catalog; inspect gh stack view before changing the project.")
    version = data.get("schemaVersion", 1)
    if type(version) is not int or version < 1:
        raise ProjectError("Invalid native stack catalog schema; inspect gh stack view before changing the project.")
    if version > 1:
        raise ProjectError("Unsupported native stack catalog schema; update project before cleanup.")
    stacks = data.get("stacks", [])
    if any(not isinstance(s, dict) or not isinstance(s.get("trunk"), dict)
           or not isinstance(s["trunk"].get("branch"), str) or not s["trunk"]["branch"]
           or not isinstance(s.get("branches"), list) or not s["branches"]
           or any(not isinstance(b, dict) or not isinstance(b.get("branch"), str) for b in s["branches"])
           for s in stacks):
        raise ProjectError("Invalid native stack branches; inspect gh stack view before changing the project.")
    return stacks


def native_stack(repo: dict[str, Any], required: bool = True) -> dict[str, Any] | None:
    stacks = native_catalog(repo)
    matches = [s for s in stacks if any(b["branch"] == repo["branch"] for b in s["branches"])]
    if len(matches) > 1 or (required and not matches):
        raise ProjectError(f"Expected one native stack for {repo['branch']}; inspect gh stack view.")
    owned = {entry["branch"] for entry in entries(repo, include_retired=True)}
    for stack in stacks:
        if matches and stack is matches[0]:
            continue
        referenced = {stack["trunk"]["branch"], *(b["branch"] for b in stack["branches"])}
        if owned & referenced:
            raise ProjectError(f"Project branches are referenced by another native stack in {repo['id']}: "
                               f"{', '.join(sorted(owned & referenced))}; preserve that stack before changing the project.")
    return matches[0] if matches else None


def registered_stacks(repo: dict[str, Any], allow_missing: bool = False, relaxed_root: str | None = None,
                      resume: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    catalog = native_catalog(repo)
    owned = {entry["branch"] for entry in entries(repo, include_retired=True)}
    used: set[int] = set()
    result = []
    for record in stack_records(repo):
        scope = stack_scope(repo, record)
        matches = [(index, stack) for index, stack in enumerate(catalog)
                   if any(b["branch"] == record["root"] for b in stack["branches"])]
        if len(matches) > 1 or (not matches and not allow_missing):
            raise ProjectError(f"Expected one native stack for {record['root']}; inspect gh stack view.")
        current = matches[0][1] if matches else None
        if current is not None:
            index = matches[0][0]
            if record.get("untracked") or index in used:
                raise ProjectError("Native stacks were recreated or combined outside project registration.")
            used.add(index)
            expected = {entry["branch"] for entry in entries(scope, include_retired=True)}
            actual = {b["branch"] for b in current["branches"]}
            if expected - actual or (actual != expected and record["root"] != relaxed_root):
                raise ProjectError(f"Native stack membership differs from project registration in {repo['id']}. "
                                   f"Unregistered: {', '.join(sorted(actual - expected)) or 'none'}; "
                                   f"missing: {', '.join(sorted(expected - actual)) or 'none'}. "
                                   + (f"Repeat the outstanding command to finish it: {resume}" if resume else
                                      "Use baton branch register REPO MEMBER for new native members."))
            default = scope["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
            if current["trunk"]["branch"] != default or current["trunk"]["branch"] in owned:
                raise ProjectError(f"Native stack trunk must be repository default {default}.")
        result.append((record, scope, current))
    for index, stack in enumerate(catalog):
        referenced = {stack["trunk"]["branch"], *(b["branch"] for b in stack["branches"])}
        if index not in used and owned & referenced:
            raise ProjectError(f"Project branches are referenced by another native stack or an unregistered stack in {repo['id']}: "
                               f"{', '.join(sorted(owned & referenced))}; preserve or repair that external stack before retrying. Create managed stacks with baton branch add REPO MEMBER --after PARENT.")
    return result


def integrated_stack(repo: dict[str, Any], target: str,
                     evidence: dict[str, dict[str, Any]] | None = None) -> set[str]:
    source = Path(repo["source"])
    tips = {}
    for entry in entries(repo, include_retired=True):
        if entry.get("retirement_state") == "retired":
            archived = git(source, "rev-parse", "--verify", entry["backup_ref"], check=False) if entry.get("backup_ref") else ""
            tips[entry["branch"]] = archived if archived == entry.get("cleanup_sha") else ""
        else:
            tips[entry["branch"]] = git(source, "rev-parse", "--verify", f"refs/heads/{entry['branch']}", check=False)
    groups = {entry["branch"]: entry_stack_root(repo, entry) for entry in entries(repo, include_retired=True)}
    integrated = {name for name, sha in tips.items() if sha and subprocess.run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", sha, target], capture_output=True
    ).returncode == 0}
    if len(integrated) == len(tips) or not target.startswith("refs/remotes/origin/"):
        return integrated
    repository = github_repository(source)
    if repository is None:
        return integrated
    try:
        prs: list[Any] = []
        # Per-head queries stay exact however many PRs the repository has merged.
        for name in sorted(name for name, sha in tips.items() if sha and name not in integrated):
            result = subprocess.run(["gh", "pr", "list", "--repo", repository,
                                     "--state", "merged", "--head", name, "--limit", "100", "--json",
                                     "state,headRefName,headRefOid,baseRefName,mergeCommit"],
                                    cwd=source, text=True, capture_output=True, timeout=20)
            if result.returncode:
                print("Stack PR verification failed; cleanup still requires proof of integration.", file=sys.stderr)
                return integrated
            found = json.loads(result.stdout)
            if not isinstance(found, list):
                return integrated
            prs.extend(found)
        default = target.removeprefix("refs/remotes/origin/")
        changed = True
        while changed:
            changed = False
            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                name, base = pr.get("headRefName"), pr.get("baseRefName")
                merge = pr.get("mergeCommit")
                commit = merge.get("oid") if isinstance(merge, dict) else None
                if (not isinstance(name, str) or not isinstance(base, str)
                        or name not in tips or name in integrated or pr.get("state") != "MERGED"
                        or pr.get("headRefOid") != tips[name] or not isinstance(commit, str)
                        or not re.fullmatch(r"[0-9a-f]{40,64}", commit)):
                    continue
                same_stack = groups.get(name) is not None and groups.get(name) == groups.get(base)
                # A merged stack PR can retain its old parent base after being squashed onto default.
                destinations = [target] if base == default or same_stack else []
                if same_stack and base in integrated and tips.get(base):
                    destinations.append(tips[base])
                if any(subprocess.run(
                    ["git", "-C", str(source), "merge-base", "--is-ancestor", commit, destination],
                    capture_output=True,
                ).returncode == 0 for destination in destinations):
                    integrated.add(name)
                    if evidence is not None:
                        evidence[name] = dict(pr)
                    changed = True
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        print("Stack PR verification unavailable; cleanup still requires proof of integration.", file=sys.stderr)
    return integrated
