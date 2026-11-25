"""A fake gh for pull requests in local bare repositories, installed by ProjectManagerTest.fake_pull_requests.

Usage: fake_gh.py ROOT ARGS...; pull requests live in ROOT/gh-prs.json and github.com/OWNER/NAME maps to
ROOT/NAME.git. Files under ROOT change its behavior: gh-offline fails every call, gh-fail-merge fails a merge without
merging, gh-fail-after-merge merges and then fails, and gh-after-merge.sh runs once after a merge with BATON_PID set
to the calling process. gh-merge-methods.json restricts merge methods. Every call is appended to ROOT/gh.log.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

root = Path(sys.argv[1])
args = sys.argv[2:]
state = root / "gh-prs.json"
with (root / "gh.log").open("a") as log:
    log.write(json.dumps(args) + "\n")
if (root / "gh-offline").exists():
    sys.exit("gh: authentication required")


def option(name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


repository = option("--repo") or ""
bare = root / (repository.rsplit("/", 1)[-1] + ".git")


def run(*command: str, check: bool = True) -> str:
    result = subprocess.run(["git", "--git-dir", str(bare), *command], text=True, capture_output=True)
    if check and result.returncode:
        sys.exit(result.stderr)
    return result.stdout.strip() if result.returncode == 0 else ""


prs: list[dict[str, Any]] = json.loads(state.read_text()) if state.exists() else []


def save() -> None:
    state.write_text(json.dumps(prs, indent=1))


def current(pr: dict[str, Any]) -> dict[str, Any]:
    # GitHub moves an open pull request's head with its branch.
    oid = pr["state"] == "OPEN" and run("rev-parse", "--verify", "-q", "refs/heads/" + pr["headRefName"], check=False)
    return dict(pr, headRefOid=oid or pr["headRefOid"])


policy = root / "gh-merge-methods.json"
methods = json.loads(policy.read_text()) if policy.exists() else ["merge", "squash"]
merge_flags = {"--merge": "merge", "--squash": "squash"}

if args[:1] == ["api"] and args[1].startswith("repos/"):
    setting = {".allow_merge_commit": "merge", ".allow_squash_merge": "squash"}.get(option("--jq"))
    if setting is None:
        sys.exit("unsupported fake gh repository setting")
    print(json.dumps(setting in methods))
elif args[:2] == ["pr", "list"]:
    states = {"open": ["OPEN"], "closed": ["CLOSED", "MERGED"], "merged": ["MERGED"],
              "all": ["OPEN", "CLOSED", "MERGED"]}[option("--state") or "open"]
    print(json.dumps([current(pr) for pr in prs if pr["repository"] == repository
                      and pr["headRefName"] == option("--head") and pr["state"] in states]))
elif args[:2] == ["pr", "create"]:
    head, base = option("--head") or "", option("--base") or ""
    oid = run("rev-parse", "--verify", "refs/heads/" + head)
    if any(pr["repository"] == repository and pr["headRefName"] == head and pr["baseRefName"] == base
           and pr["state"] == "OPEN" for pr in prs):
        sys.exit("a pull request for this head and base already exists")
    url = f"https://github.com/{repository.removeprefix('github.com/')}/pull/{len(prs) + 1}"
    prs.append({"repository": repository, "number": len(prs) + 1, "url": url, "state": "OPEN", "headRefName": head,
                "headRefOid": oid, "baseRefName": base, "isCrossRepository": False, "title": option("--title"),
                "body": option("--body")})
    save()
    print(url)
elif args[:2] == ["pr", "merge"] and set(merge_flags) & set(args):
    method = next(value for flag, value in merge_flags.items() if flag in args)
    if method not in methods:
        sys.exit(f"{method} merges are not allowed")
    pr = next(pr for pr in prs if pr["repository"] == repository and pr["number"] == int(args[2]))
    if (root / "gh-fail-merge").exists():
        sys.exit("merge failed")
    if pr["state"] != "OPEN":
        sys.exit(f"pull request #{pr['number']} is already {pr['state'].lower()}")
    head = run("rev-parse", "refs/heads/" + pr["headRefName"])
    if head != option("--match-head-commit"):
        sys.exit("head branch was modified; review and try the merge again")
    base = run("rev-parse", "refs/heads/" + pr["baseRefName"])
    tree = run("merge-tree", "--write-tree", base, head)
    parents = ["-p", base] if method == "squash" else ["-p", base, "-p", head]
    commit = run("commit-tree", tree, *parents, "-m", f"Merge pull request #{pr['number']}")
    run("update-ref", "refs/heads/" + pr["baseRefName"], commit, base)
    pr.update(state="MERGED", headRefOid=head, mergeCommit={"oid": commit})
    save()
    hook = root / "gh-after-merge.sh"
    if hook.exists():
        script = hook.read_text()
        hook.unlink()
        subprocess.run(["bash", "-c", script], env=dict(os.environ, BATON_PID=str(os.getppid())), check=True)
    if (root / "gh-fail-after-merge").exists():
        sys.exit("connection reset")
else:
    sys.exit("unsupported fake gh call: " + " ".join(args))
