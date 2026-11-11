"""Independent members and native stacks: branch init, add, register and stack status."""

import contextlib
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any

from .errors import ProjectError
from .gitutil import git, common_dir, branch, inside
from .manifest import MEMBER_NAME_PATTERN, entries, default_entry, member_name, stack_records, entry_stack_root, ensure_stack_records, stack_scope, stack_worktree, entry_claims, project_claims, claim_conflict, managed_links, link_name, subplan_directory
from .native import require_active_top, require_top_changes, native, native_catalog, native_stack, registered_stacks
from .core import ManagerCore


class MembersMixin(ManagerCore):
    def check_member_destination(self, project: dict[str, Any], repo: dict[str, Any], name: str,
                                 target: str, path: Path, *, allow_existing: bool = False) -> str:
        if any(member_name(entry) == name or entry["branch"] == target or entry["path"] == str(path)
               for entry in entries(repo, include_retired=True)):
            raise ProjectError(f"Member already registered: {name}")
        reserved_links = {link_name(entry) for registered in project["repos"]
                          for entry in entries(registered, include_retired=True)}
        reserved_links.update(registered["id"] for registered in project["repos"])
        if f"{repo['id']}--{name}" in reserved_links:
            raise ProjectError("Member link collides with another repository or member.")
        tip = git(Path(repo["source"]), "rev-parse", "--verify", f"refs/heads/{target}", check=False)
        if not allow_existing and (tip or path.exists() or path.is_symlink()):
            raise ProjectError(f"Member branch or path already exists: {target}; use register for explicit adoption.")
        claims = entry_claims(repo["common_dir"], target, str(path))
        for other in self.state["projects"].values():
            if other["slot"] != project["slot"]:
                for claim in sorted(claims & project_claims(other)):
                    raise claim_conflict(claim, f"project {other['name']} in slot {other['slot']}")
        return tip

    def prepare_first_member(self, project: dict[str, Any], repo: dict[str, Any], member: str,
                            current: dict[str, Any] | None) -> dict[str, Any] | None:
        first = repo.get("first_member")
        if first:
            if first != member:
                raise ProjectError(f"First member is already named {first}; native tracking must not be renamed here.")
            return None
        if current is not None or entry_stack_root(repo, repo) is not None:
            raise ProjectError("Name the first member before native stack registration; existing stacks are not renamed.")
        if repo["branch"] != project["branch"]:
            raise ProjectError("Unexpected root branch before first-member initialization.")
        name, path = self.member_branch_and_path(project, repo["id"], member)
        self.check_member_destination(project, repo, member, name, path)
        if Path(repo["path"]).is_symlink() or not inside(path.resolve(), self.worktrees):
            raise ProjectError("Unsafe first-member worktree path.")
        links = self.slots / project["slot"] / "repos"
        expected = managed_links(project)
        if links.is_symlink() or not links.is_dir() or not set(managed_links(project)) <= {p.name for p in links.iterdir()}:
            raise ProjectError("Unexpected coordinator repos directory or unmanaged entries.")
        for link in links.iterdir():
            if not link.is_symlink() or link.name not in expected or link.readlink() != expected[link.name]:
                raise ProjectError(f"Unexpected managed link: {link}")
        return {"old_path": repo["path"], "new_path": str(path), "old_branch": repo["branch"],
                "new_branch": name, "sha": git(Path(repo["path"]), "rev-parse", "HEAD")}

    def finish_first_member(self, project: dict[str, Any], repo: dict[str, Any],
                           pending: dict[str, Any]) -> None:
        move = pending["first_move"]
        old, new = Path(move["old_path"]), Path(move["new_path"])
        if old.is_symlink() or new.is_symlink() or (old.exists() and new.exists()):
            raise ProjectError("First-member worktree destination changed during initialization.")
        path = old if old.exists() else new
        if not path.exists() or not inside(path.resolve(), self.worktrees):
            raise ProjectError("First-member worktree disappeared or moved outside managed paths.")
        original = branch(path) == move["old_branch"]
        if branch(path) not in (move["old_branch"], move["new_branch"]):
            raise ProjectError("First-member worktree branch changed during initialization.")
        entry = dict(repo, path=str(path), branch=move["old_branch"] if original else move["new_branch"], members=[])
        # A branch rename can precede its worktree move when resuming the journal.
        if common_dir(path) != Path(repo["common_dir"]) or common_dir(Path(repo["source"])) != Path(repo["common_dir"]):
            raise ProjectError("First-member repository identity changed during initialization.")
        if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
            raise ProjectError("First-member path is not an exact worktree root.")
        if git(path, "status", "--porcelain", "--untracked-files=all") or git(path, "rev-parse", "HEAD") != move["sha"]:
            raise ProjectError("First-member worktree has unexpected changes during initialization.")
        current = native_stack(entry, required=False)
        if current is not None:
            raise ProjectError("Native tracking appeared before first-member initialization completed.")
        if original:
            if git(path, "rev-parse", "--verify", f"refs/heads/{move['new_branch']}", check=False):
                raise ProjectError("First-member branch destination appeared during initialization.")
            git(path, "branch", "-m", move["new_branch"])
        elif git(path, "rev-parse", "--verify", f"refs/heads/{move['old_branch']}", check=False):
            raise ProjectError("Original project branch was recreated during initialization.")
        if old.exists():
            os.chdir(self.slots / project["slot"])
            git(Path(repo["source"]), "worktree", "move", str(old), str(new))
        link = self.slots / project["slot"] / "repos" / repo["id"]
        if not link.is_symlink() and not (not link.exists() and pending.get("link_updating")):
            raise ProjectError(f"Unexpected root link during initialization: {link}")
        if link.is_symlink() and link.readlink() not in (old, new):
            raise ProjectError(f"Root link target changed during initialization: {link}")
        pending["link_updating"] = True
        self.save()
        previous_links = {repo["id"]: link.readlink()} if link.is_symlink() else {}
        repo.update(branch=move["new_branch"], path=str(new), first_member=pending["name"])
        self.save()
        self.reconcile_coordinator(project, previous_links)
        print(f"Moved {old} ({move['old_branch']}) -> {new} ({move['new_branch']}). "
              "Shells and agents still in the old path must cd to the new one.")
        stale, renamed = (subplan_directory(self.artifact_directory(project), repo["id"], b, project["branch_prefix"])
                          for b in (move["old_branch"], move["new_branch"]))
        if stale.is_dir() and not renamed.exists():
            root = self.artifacts_git(stale)
            print(f"baton: warning: subplan directory {stale} still uses the old branch name; rename it with:\n"
                  f"  git -C {shlex.quote(str(root))} mv {shlex.quote(str(stale.relative_to(root)))} "
                  f"{shlex.quote(str(renamed.relative_to(root)))} && baton artifacts commit --selector "
                  f"{shlex.quote(self.selector_for(project))} -m {shlex.quote('Rename subplan for ' + move['new_branch'])} "
                  f"{shlex.quote(str(stale))} {shlex.quote(str(renamed))}\n"
                  "  (use plain mv if it is untracked) and update references to the old path.", file=sys.stderr)

    def member_for(self, repo: dict[str, Any], name: str | None) -> dict[str, Any]:
        if name is None:
            entry = default_entry(repo)
            if entry is None:
                raise ProjectError(f"Repository has no active registered members: {repo['id']}")
            return entry
        matches = [e for e in entries(repo) if name in (member_name(e), e["branch"])]
        if len(matches) != 1:
            hint = ""
            if not matches and member_name(repo) is None:
                hint = (f"; the first {repo['id']} checkout has no member name yet, so name it with "
                        f"baton branch init {repo['id']} <name> before referring to it")
            raise ProjectError(f"Member is not registered or is ambiguous: {name}{hint}")
        return matches[0]

    def selected_stack(self, repo: dict[str, Any], name: str | None) -> dict[str, Any]:
        records = stack_records(repo)
        if name is not None:
            entry = self.member_for(repo, name)
            root = entry_stack_root(repo, entry)
            if root is None:
                raise ProjectError(f"Member {name} is independent; use branch add --after to start a native stack.")
            return next(record for record in records if record["root"] == root)
        cwd = Path.cwd().resolve()
        current = [e for e in entries(repo) if inside(cwd, Path(e["path"]))]
        if current:
            root = entry_stack_root(repo, current[0])
            if root is not None:
                return next(record for record in records if record["root"] == root)
            raise ProjectError("Current checkout is independent; select a stack with --member MEMBER.")
        if len(records) != 1:
            raise ProjectError("Select a native stack with --member MEMBER; no single stack can be inferred.")
        return records[0]

    def stack(self, action: str, repo_id: str, selector: str | None,
              member: str | None = None) -> None:
        project = self.project(selector)
        matches = [r for r in project["repos"] if r["id"] == repo_id]
        if not matches:
            raise ProjectError(f"Repository is not part of project: {repo_id}")
        repo = matches[0]
        record = self.selected_stack(repo, member)
        registered_stacks(repo)
        scope = stack_scope(repo, record)
        print(self.gh_stack(repo, stack_worktree(scope), "view", "--json"))

    def manage_member(self, action: str, repo_id: str, name: str | None, selector: str | None,
                      after: str | None = None) -> None:
        project = self.project(selector)
        matches = [r for r in project["repos"] if r["id"] == repo_id]
        if not matches:
            raise ProjectError(f"Repository is not part of project: {repo_id}")
        repo = matches[0]
        if not name or not re.fullmatch(MEMBER_NAME_PATTERN, name):
            raise ProjectError("Use a simple member slug containing letters/digits/dashes/underscores.")
        if after is not None:
            version = re.search(r"(\d+)\.(\d+)\.(\d+)", native(Path(repo["source"]), "--version"))
            if not version or tuple(map(int, version.groups())) < (0, 2, 0):
                raise ProjectError("Stack worktrees require gh stack v0.2.0 or newer.")
        signature = {"action": action, "repo": repo_id, "name": name, "after": after}
        pending = project.get("pending_member")
        if pending:
            if any(pending.get(key) != value for key, value in signature.items()):
                raise ProjectError("Repeat the outstanding branch command to finish it first.")
        else:
            if project["state"] != "active":
                raise ProjectError("Finish the outstanding project operation first.")
            self.check_coordinator(project)
            # A running native rebase checks out other branches, so report it before the worktree checks.
            with self.native_lock(repo, create=True):
                native_catalog(repo)
            for entry in entries(repo):
                # Independent adds leave existing checkouts and their history untouched.
                self.validate_clean_repo(entry, allow_dirty=action == "add" and after is None)
            ensure_stack_records(repo)
            pending = dict(signature)
            if action == "init":
                anchor = repo
                scope = dict(anchor, members=[])
                with self.native_lock(repo, create=True):
                    current = native_stack(scope, required=False)
                    registered_stacks(repo)
                    move = self.prepare_first_member(project, repo, name, current)
                    if move:
                        self.reserve(entry_claims(repo["common_dir"], move["new_branch"], move["new_path"]))
                        pending["first_move"] = move
                pending["anchor"] = pending.get("first_move", {}).get("new_branch", anchor["branch"])
                pending["root"] = None
            else:
                assert name is not None
                target, path = self.member_branch_and_path(project, repo_id, name)
                tip = self.check_member_destination(project, repo, name, target, path,
                                                    allow_existing=action == "register")
                self.reserve(entry_claims(repo["common_dir"], target, str(path)))
                parent = self.member_for(repo, after) if after else None
                root = entry_stack_root(repo, parent) if parent is not None else None
                if action == "register" and root is None:
                    containing = [s for s in native_catalog(repo) if any(b["branch"] == target for b in s["branches"])]
                    records = [r for r in stack_records(repo) if any(
                        b["branch"] == r["root"] for s in containing for b in s["branches"])]
                    if records:
                        if len(records) != 1:
                            raise ProjectError("Registered branch belongs to ambiguous native stacks.")
                        root = records[0]["root"]
                    elif containing:
                        raise ProjectError("Initialize this native stack explicitly before registering its members.")
                groups = registered_stacks(repo, relaxed_root=root if action == "register" else None)
                if parent is not None and action == "add":
                    if root is None:
                        root = parent["branch"]
                        scope = dict(parent, members=[])
                        current = {"trunk": {"branch": parent["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")},
                                   "branches": [{"branch": root}]}
                    else:
                        record, scope, current = next(item for item in groups if item[0]["root"] == root)
                        assert current is not None
                    require_active_top(scope, current)
                    if current["branches"][-1]["branch"] != parent["branch"]:
                        raise ProjectError("--after must name the native stack top; use native restructuring for other positions.")
                    require_top_changes(scope, current)
                    tip = git(Path(parent["path"]), "rev-parse", "HEAD")
                    base = parent["base_ref"]
                    if not any(r["root"] == root for r in repo["stacks"]):
                        parent["stack_root"] = root
                        repo["stacks"].append({"root": root})
                else:
                    with self.repo_lock(repo) if action == "add" else contextlib.nullcontext():
                        base = self.base(Path(repo["source"]), refresh=action == "add")
                    if action == "add":
                        tip = git(Path(repo["source"]), "rev-parse", base)
                    if action == "register" and (not tip or path.is_symlink()):
                        raise ProjectError(f"Missing member branch or unsafe path: {target}")
                    if root is not None:
                        current = next(s for r, _, s in groups if r["root"] == root)
                        if current is None or target not in {b["branch"] for b in current["branches"]}:
                            raise ProjectError("Register only an existing member of the selected native stack.")
                entry = {k: repo[k] for k in ("id", "source", "common_dir")}
                entry.update(branch_prefix=repo["branch_prefix"], branch=target, path=str(path), base_ref=base, base_sha=tip,
                             stack_root=root, step="pending", member=name)
                if action == "register":
                    validation = dict(repo, members=[*repo.get("members", []), entry])
                    with self.native_lock(repo, create=True):
                        registered_stacks(validation, relaxed_root=root)
                    for record in git(Path(repo["source"]), "worktree", "list", "--porcelain").split("\n\n"):
                        if f"branch refs/heads/{target}" in record.splitlines() and f"worktree {path}" not in record.splitlines():
                            raise ProjectError("Branch is checked out outside its exact managed path.")
                    if path.exists():
                        self.validate_clean_repo(entry)
                pending.update(entry=entry, root=root,
                               owner_path=parent["path"] if parent else None,
                               owner_branch=parent["branch"] if parent else None)
            project.update(state="branching", pending_member=pending)
            self.save()
        self.finish_member(project, repo, pending)

    def finish_member(self, project: dict[str, Any], repo: dict[str, Any], pending: dict[str, Any]) -> None:
        self.check_coordinator(project, allow_dirty=bool(project.get("pending_manifest")))
        with self.native_lock(repo, create=True):
            native_catalog(repo)
        if pending["action"] == "init":
            if pending.get("first_move"):
                with self.native_lock(repo, create=True):
                    self.finish_first_member(project, repo, pending)
            if pending["name"] and pending["anchor"] == repo["branch"]:
                repo["first_member"] = pending["name"]
        else:
            entry = pending["entry"]
            root = pending["root"]
            if root is not None and pending["action"] == "add":
                record = next(r for r in stack_records(repo) if r["root"] == root)
                scope = stack_scope(repo, record)
                current = native_stack(scope, required=False)
                if current is None:
                    base = scope["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
                    self.gh_stack(repo, stack_worktree(scope), "init", "--base", base, scope["branch"])
                    current = native_stack(scope)
                assert current is not None
                owner_path = Path(pending["owner_path"])
                owner = self.member_for(repo, pending["owner_branch"])
                with self.native_lock(repo, create=True):
                    if branch(owner_path) == entry["branch"]:
                        if common_dir(owner_path) != Path(repo["common_dir"]) or git(owner_path, "status", "--porcelain", "--untracked-files=all") or git(owner_path, "rev-parse", "HEAD") != entry["base_sha"]:
                            raise ProjectError("Interrupted native add checkout changed; preserve changes before resuming.")
                        git(owner_path, "switch", pending["owner_branch"])
                if entry["branch"] not in {b["branch"] for b in current["branches"]}:
                    self.validate_clean_repo(owner)
                    if git(owner_path, "rev-parse", "HEAD") != entry["base_sha"]:
                        raise ProjectError("Initiating branch changed during creation.")
                    existing = git(Path(repo["source"]), "rev-parse", "--verify", f"refs/heads/{entry['branch']}", check=False)
                    if existing and existing != entry["base_sha"]:
                        raise ProjectError("Interrupted member branch has unexpected commits.")
                    self.gh_stack(repo, owner_path, "add", entry["branch"])
                    current = native_stack(scope)
                    assert current is not None
                    if entry["branch"] not in {b["branch"] for b in current["branches"]}:
                        if all((b.get("pullRequest") or {}).get("merged") for b in current["branches"]) and not existing and not Path(entry["path"]).exists():
                            self.validate_clean_repo(owner)
                            if git(owner_path, "rev-parse", "HEAD") == entry["base_sha"]:
                                self.materialize(project)
                                project.pop("pending_member")
                                project["state"] = "active"
                                self.save()
                                require_top_changes(scope, current)
                        raise ProjectError("Native add did not register the requested member.")
                with self.native_lock(repo, create=True):
                    if branch(owner_path) == entry["branch"]:
                        if git(owner_path, "status", "--porcelain", "--untracked-files=all") or git(owner_path, "rev-parse", "HEAD") != entry["base_sha"]:
                            raise ProjectError("Interrupted native checkout has unexpected changes.")
                        git(owner_path, "switch", pending["owner_branch"])
            if not any(e["branch"] == entry["branch"] for e in entries(repo)):
                repo.setdefault("members", []).append(entry)
                self.save()
        with self.native_lock(repo, create=True):
            registered_stacks(repo, relaxed_root=pending["root"] if pending["action"] == "register" else None,
                              resume=self.resume_command(project))
            self.materialize(project)
            project.pop("pending_member")
            project["state"] = "active"
            self.save()
        print(f"Member operation complete: {repo['id']} {pending['name'] or 'root'}")
