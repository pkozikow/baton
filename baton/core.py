"""Manager state, locks, reservations and project resolution."""

import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any, Iterator, Callable

from .doctor_freshness import require_trusted_runtime
from .configuration import REPOSITORY_NAME_PATTERN, effective_paths, read_config, configured_branch_prefix
from .errors import ProjectError, NoProject
from .gitutil import git, common_dir, branch, write_json, file_lock, inside
from .manifest import MEMBER_NAME_PATTERN, project_entries, entry_claims, project_claims, claim_conflict, managed_links, link_name, member_name, branch_suffix
from .native import native, native_lock
from .slots import SLOT_NAMES, read_active_project
from .storage_names import registry_directory


class ManagerCore:
    def __init__(self) -> None:
        config = read_config()
        paths = config.get("paths", {})
        roots = effective_paths(config)
        self.branch_prefix = configured_branch_prefix(config)
        self.repo_aliases: dict[str, str] = dict(config.get("repo_aliases", {}))
        self.repo = roots["projects_repo"]
        self.slots = roots["slots_root"]
        self.worktrees = roots["worktrees_root"]
        self.sources = roots["repos_root"]
        self.artifacts = roots["artifacts_root"]
        self.artifacts_configured = "BATON_ARTIFACTS_ROOT" in os.environ or "artifacts_root" in paths
        self.state: dict[str, Any] = {"projects": {}}
        self.held_slot: str | None = None
        self.global_held = False
        self.repo_locks: dict[str, int] = {}
        self.reserved: set[tuple[str, ...]] = set()

    def resolve_repo(self, name: str) -> str:
        """Resolve once: configured targets are canonical repository names."""
        return self.repo_aliases.get(name, name)

    @property
    def state_dir(self) -> Path:
        return registry_directory(common_dir(self.repo))

    @property
    def layout(self) -> Path:
        return self.state_dir / "v2"

    def initialized(self) -> bool:
        try:
            directory = self.state_dir
        except ProjectError:
            return False
        return any((directory / name).is_dir() for name in ("v2", "v2.tmp"))

    def build_layout(self, staging: Path, state: dict[str, Any]) -> None:
        if staging.exists():
            shutil.rmtree(staging)
        for name in ("projects", "archive", "reservations"):
            (staging / name).mkdir(parents=True)
        for slot, project in state["projects"].items():
            write_json(staging / "projects" / f"{slot}.json", project)
        for index, project in enumerate(state.get("archive", []), 1):
            write_json(staging / "archive" / f"{index:06d}-{project['uuid']}.json", project)

    def ensure_layout(self) -> None:
        if self.layout.is_dir():
            return
        if not self.initialized():
            raise ProjectError("Baton is not initialized; run baton init.")
        require_trusted_runtime()
        with self.global_lock() as directory:
            layout, staging = directory / "v2", directory / "v2.tmp"
            if layout.is_dir():
                return
            if not staging.is_dir() or any(staging.glob("*/*.json")):
                raise ProjectError("Baton is not initialized; run baton init.")
            self.build_layout(staging, {"projects": {}})
            staging.rename(layout)

    def load(self) -> None:
        self.ensure_layout()
        projects = {}
        for path in sorted((self.layout / "projects").glob("*.json")):
            try:
                projects[path.stem] = read_active_project(path)
            except FileNotFoundError:
                continue  # Another slot's cleanup archived it after the glob.
        self.state["projects"] = projects

    def save(self) -> None:
        slot = self.held_slot
        assert slot is not None and slot in self.state["projects"], "save() writes only the locked slot's project"
        with self.global_lock() as directory:
            write_json(directory / "v2" / "projects" / f"{slot}.json", self.state["projects"][slot])

    def artifact_directory(self, project: dict[str, Any]) -> Path:
        return Path(project["artifacts_path"])

    def archive_project(self, project: dict[str, Any]) -> None:
        slot = self.held_slot
        assert slot == project["slot"], "archive_project() requires the project's slot lock"
        with self.global_lock() as directory:
            layout = directory / "v2"
            archive = layout / "archive"
            existing = sorted(archive.glob("*.json"))
            same = [path for path in existing if path.stem.endswith("-" + project["uuid"])]
            index = max((int(path.stem.split("-", 1)[0]) for path in existing), default=0) + 1
            previous = json.loads(same[0].read_text()) if same else {}
            project.setdefault("completed_at", previous.get("completed_at") or datetime.now(timezone.utc).isoformat())
            write_json(same[0] if same else archive / f"{index:06d}-{project['uuid']}.json",
                       {k: v for k, v in project.items() if k != "pending_abandon"})
            (layout / "projects" / f"{slot}.json").unlink(missing_ok=True)
            del self.state["projects"][slot]

    def lock_path(self, name: str) -> Path:
        return self.state_dir / "locks" / f"{name}.lock"

    @contextlib.contextmanager
    def global_lock(self) -> Iterator[Path]:
        """Brief cross-project registry lock; never hold it across prompts, fetches, gh or worktree operations."""
        assert not self.global_held, "the global lock is never nested"
        directory = self.state_dir
        with file_lock(directory / "lock", "global project registry", self.held_slot):
            if self.state_dir != directory:
                raise ProjectError("Project registry changed while waiting for its lock; rerun the command.")
            self.global_held = True
            try:
                yield directory
            finally:
                self.global_held = False

    @contextlib.contextmanager
    def slot_lock(self, slot: str) -> Iterator[None]:
        assert self.held_slot is None and not self.global_held and not self.repo_locks, "slot lock comes first"
        with file_lock(self.lock_path(slot), f"slot {slot}", slot):
            self.held_slot = slot
            # This slot's reservation from an interrupted command is stale once its lock is ours.
            stale = self.layout / "reservations" / f"{slot}.json"
            try:
                if stale.exists():
                    with self.global_lock():
                        stale.unlink(missing_ok=True)
                yield
            finally:
                self.reserved = set()
                if stale.exists():
                    with self.global_lock():
                        stale.unlink(missing_ok=True)
                self.held_slot = None

    @contextlib.contextmanager
    def repo_lock(self, repo: dict[str, Any], ordered: bool = False, *, blocking: bool | None = None) -> Iterator[None]:
        """Short per-source-repository lock around fetches, worktree, branch, ref and gh stack changes."""
        key = repo["common_dir"]
        assert not self.global_held, "per-repo locks are never taken under the global lock"
        if key in self.repo_locks:
            self.repo_locks[key] += 1
            try:
                yield
            finally:
                self.repo_locks[key] -= 1
            return
        name = "repo-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        # An unordered second repository fails fast instead of risking a lock-order deadlock.
        with file_lock(self.lock_path(name), f"repository {repo['id']}", self.held_slot,
                       blocking=(ordered or not self.repo_locks) if blocking is None else blocking) as acquired:
            if not acquired:
                raise ProjectError(f"Repository {repo['id']} is busy with another project command; retry.")
            self.repo_locks[key] = 1
            try:
                yield
            finally:
                del self.repo_locks[key]

    @contextlib.contextmanager
    def native_lock(self, repo: dict[str, Any], *, create: bool = False) -> Iterator[None]:
        with self.repo_lock(repo), native_lock(repo, create=create):
            yield

    def gh_stack(self, repo: dict[str, Any], path: Path, *args: str) -> str:
        with self.repo_lock(repo):
            return native(path, *args)

    @contextlib.contextmanager
    def command_lock(self, selector: str | None, create: bool = False) -> Iterator[None]:
        """Resolve the target slot without a lock, lock it, reload, and fence against replacement while waiting."""
        if create:
            slot, identity = self.current_slot(), None
        else:
            project = self.project(selector)
            slot, identity = project["slot"], (project.get("uuid"), project["name"])
        with self.slot_lock(slot):
            self.load()
            current = self.state["projects"].get(slot)
            if identity is not None and (current is None or (current.get("uuid"), current["name"]) != identity):
                raise ProjectError(f"Project {identity[1]} in slot {slot} changed while waiting for its lock; rerun the command.")
            yield

    def reserve(self, claims: set[tuple[str, ...]]) -> None:
        """Check claims against every other slot and live reservation, then record them for this command."""
        slot = self.held_slot
        assert slot is not None, "reservations require the slot lock"
        with self.global_lock() as directory:
            layout = directory / "v2"
            for path in sorted((layout / "projects").glob("*.json")):
                if path.stem != slot:
                    other = json.loads(path.read_text())
                    for claim in sorted(claims & project_claims(other)):
                        raise claim_conflict(claim, f"project {other['name']} in slot {path.stem}")
            if any(claim[0] == "artifacts" for claim in claims):
                for archived in self.archived_projects():
                    artifact_claim = ("artifacts", str(self.artifact_directory(archived)))
                    if artifact_claim in claims:
                        raise claim_conflict(artifact_claim, f"archived project {archived['name']}")
            for path in sorted((layout / "reservations").glob("*.json")):
                if path.stem == slot:
                    continue
                # A non-blocking probe cannot deadlock; a free slot lock means the reserving command ended.
                with file_lock(self.lock_path(path.stem), f"slot {path.stem}", blocking=False) as free:
                    if free:
                        path.unlink(missing_ok=True)
                        continue
                for claim in sorted(claims & {tuple(c) for c in json.loads(path.read_text())["claims"]}):
                    raise claim_conflict(claim, f"a running command in slot {path.stem}")
            self.reserved |= claims
            write_json(layout / "reservations" / f"{slot}.json",
                       {"slot": slot, "pid": os.getpid(), "claims": sorted(map(list, self.reserved))})

    def initialize_artifacts(self) -> None:
        if self.artifacts_configured:
            return
        root = self.artifacts.parent
        marker = root / ".baton-artifacts"
        if root.exists() and any(root.iterdir()):
            if not marker.is_file() or git(root, "rev-parse", "--show-toplevel", check=False) != str(root):
                raise ProjectError(f"Refusing to adopt artifact directory: {root}; configure BATON_ARTIFACTS_ROOT "
                                   "to use an existing artifacts checkout.")
            self.artifacts.mkdir(exist_ok=True)
            return
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "-b", "main")
        marker.write_text("Baton artifact repository.\n")
        (root / ".gitignore").write_text(
            "/projects/*/blackboard-lock.md\n/projects/*/.blackboard-*.tmp\n/projects/*/blackboard.sqlite*\n/projects/*/.blackboard-*/\n/projects/*/coordination/blackboard-lock.md\n/projects/*/coordination/.blackboard-*.tmp\n/projects/*/coordination/.snapshot-*.sqlite*\n"
        )
        self.artifacts.mkdir(exist_ok=True)
        git(root, "add", ".baton-artifacts", ".gitignore")
        git(root, "commit", "-m", "Initialize project artifacts")

    def initialize(self) -> None:
        if not self.repo.exists():
            self.repo.mkdir(parents=True)
        if not (self.repo / ".git").exists():
            if any(self.repo.iterdir()):
                raise ProjectError(f"Refusing nonempty directory: {self.repo}")
            git(self.repo, "init", "-b", "main")
        elif not self.initialized():
            if (git(self.repo, "rev-parse", "--verify", "HEAD", check=False)
                    or any(path.name != ".git" for path in self.repo.iterdir())
                    or git(self.repo, "ls-files")):
                raise ProjectError("Refusing to adopt an existing repository.")
        self.state_dir.mkdir(exist_ok=True)
        with self.global_lock():
            if not self.initialized():
                self.build_layout(self.state_dir / "v2.tmp", {"projects": {}})
                (self.state_dir / "v2.tmp").rename(self.layout)
        self.load()
        with self.global_lock():
            if git(self.repo, "remote"):
                raise ProjectError("The projects repository must be local-only (no remotes).")
            readme = self.repo / "README.md"
            if not git(self.repo, "rev-parse", "--verify", "HEAD", check=False):
                readme.write_text(
                    "# Local projects\n\n"
                    "This local-only repository coordinates multi-repository projects. "
                    "Manage it with the globally installed baton command.\n\n"
                    f"- Coordinator slots: `{self.slots}/p1` through `p10`.\n"
                    f"- Code worktrees: `{self.worktrees}/<repo>/<project>`.\n"
                    "- Idle slots use branches p1 through p10. Active slots use "
                    f"{self.branch_prefix}/<project>, containing a project.json manifest and ignored "
                    "repos/ symlinks to participating checkouts.\n"
                    "- Durable operation state and archived manifests live in "
                    "the common Git directory under baton/.\n\n"
                    "Enter a slot and run `baton create <name> --repos <repos...>`. "
                    "Use `baton status` (`--json` for scripts), `baton add <repos...>`, and "
                    "`baton cleanup --dry-run` or `baton cleanup` there "
                    "or from a participating code worktree. `baton list` works anywhere.\n\n"
                    "Further commands: `branch init|add|register` and `stack` for more branches per repository, "
                    "`blackboard read|post|edit` for agent coordination, and `artifacts commit` for plans in "
                    "the configured artifacts directory (<project>-<8-UUID-chars>/ for new projects). See the Baton README.\n"
                )
                git(self.repo, "add", "README.md")
                git(self.repo, "commit", "-m", "Initialize local project coordinator")
            exclude = common_dir(self.repo) / "info" / "exclude"
            exclude.parent.mkdir(exist_ok=True)
            content = exclude.read_text() if exclude.exists() else ""
            if "/repos/" not in content.splitlines():
                with exclude.open("a") as handle:
                    handle.write("\n/repos/\n")
            self.initialize_artifacts()
            self.slots.mkdir(parents=True, exist_ok=True)
            self.worktrees.mkdir(parents=True, exist_ok=True)
            for slot in SLOT_NAMES:
                path = self.slots / slot
                if path.exists():
                    if common_dir(path) != common_dir(self.repo):
                        raise ProjectError(f"Slot path belongs to another repository: {path}")
                    continue
                if git(self.repo, "rev-parse", "--verify", f"refs/heads/{slot}", check=False):
                    git(self.repo, "worktree", "add", str(path), slot)
                else:
                    git(self.repo, "worktree", "add", "-b", slot, str(path), "main")
            print(f"Initialized {self.repo} with slots {SLOT_NAMES[0]} through {SLOT_NAMES[-1]} at {self.slots}")

    def current_slot(self) -> str:
        cwd = Path.cwd().resolve()
        for slot in SLOT_NAMES:
            if inside(cwd, self.slots / slot):
                return slot
        raise ProjectError(f"Enter a coordinator slot under {self.slots} to create a project.")

    @staticmethod
    def selector_for(project: dict[str, Any]) -> str:
        return project["name"]

    def project(self, selector: str | None = None) -> dict[str, Any]:
        cwd = Path.cwd().resolve()
        matches = []
        for slot, project in self.state["projects"].items():
            if selector:
                if selector in (slot, project["name"], project.get("uuid")):
                    matches.append(project)
            elif inside(cwd, self.slots / slot) or any(
                inside(cwd, Path(repo["path"])) for repo in project_entries(project)
            ):
                return project
        # Former names keep resolving after rename so existing agent sessions can still find the project.
        aliases = [p for p in self.state["projects"].values() if selector in p.get("previous_names", [])]
        if selector in SLOT_NAMES:
            matches.extend(project for project in aliases if project not in matches)
        if len(matches) > 1:
            raise ProjectError(f"Selector {selector} is ambiguous between project names and coordinator slots: "
                               + ", ".join(f"{p['name']} ({p['slot']})" for p in matches)
                               + "; use an unambiguous current project name or slot.")
        if matches:
            return matches[0]
        if len(aliases) > 1:
            raise ProjectError(f"Former project name {selector} matches several active projects: "
                               + ", ".join(f"{p['name']} ({p['slot']})" for p in aliases) + "; use a current name or slot.")
        if selector and aliases:
            return aliases[0]
        raise NoProject(f"No active project found for {selector or cwd}.")

    def archived_projects(self) -> Iterator[dict[str, Any]]:
        from .retro_automation import identity, read_record
        for path in sorted((self.layout / "archive").glob("*.json")):
            try:
                project = read_record(path)
                identity(project)
                if not isinstance(project.get("name"), str) or not project["name"]:
                    raise ProjectError("Archived project has no name.")
                self.artifact_directory(project)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ProjectError) as error:
                print(f"baton: warning: ignoring invalid archive {path}: {error}", file=sys.stderr)
                continue
            yield {**project, "_archived": True}

    def artifact_project(self, selector: str | None = None) -> dict[str, Any]:
        """Full UUIDs select historical identity; names and slots retain active-project semantics."""
        if selector is None:
            return self.project()
        archived = list(self.archived_projects())
        active = list(self.state["projects"].values())
        identities = [p for p in (*active, *archived) if selector == p.get("uuid")]
        if identities:
            if len({str(self.artifact_directory(p)) for p in identities}) != 1:
                raise ProjectError(f"Project UUID {selector} is ambiguous.")
            return identities[-1]
        if selector in SLOT_NAMES:
            return self.project(selector)
        try:
            return self.project(selector)
        except NoProject:
            pass
        matches = [p for p in archived
                   if selector == p["name"] or selector in p.get("previous_names", [])]
        unique = {p["uuid"]: p for p in matches}
        if len(unique) > 1:
            raise ProjectError(f"Project name {selector} matches several projects; use the full project UUID.")
        if unique:
            return next(iter(unique.values()))
        raise NoProject(f"No project found for {selector}.")

    def base(self, source: Path, refresh: bool, use_cached_default: bool = False) -> str:
        if "origin" in git(source, "remote").splitlines():
            ref = git(source, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
            if refresh and use_cached_default and ref.startswith("refs/remotes/origin/"):
                default = ref.removeprefix("refs/remotes/origin/")
                try:
                    git(source, "fetch", "origin", f"refs/heads/{default}:{ref}")
                    return ref
                except ProjectError:
                    # A renamed/deleted remote branch invalidates the cached default.
                    pass
            if refresh or not ref:
                if not refresh:
                    raise ProjectError(f"No cached origin default branch for {source}.")
                output = git(source, "ls-remote", "--symref", "origin", "HEAD")
                match = re.search(r"^ref: refs/heads/(.+)\s+HEAD$", output, re.MULTILINE)
                if not match:
                    raise ProjectError(f"Cannot discover default branch for {source}")
                ref = "refs/remotes/origin/" + match.group(1)
            default = ref.removeprefix("refs/remotes/origin/")
            if refresh:
                git(source, "fetch", "origin", f"refs/heads/{default}:{ref}")
                git(source, "symbolic-ref", "refs/remotes/origin/HEAD", ref)
            return ref
        candidates = [name for name in ("main", "master") if git(source, "rev-parse", "--verify", f"refs/heads/{name}", check=False)]
        if len(candidates) != 1:
            raise ProjectError(f"Local-only repository needs an unambiguous main or master branch: {source}")
        return f"refs/heads/{candidates[0]}"

    def prepare(self, names: list[str], project: dict[str, Any], replacing: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        prepared = self.resolve_repos(names, project, replacing)
        self.reserve(set().union(*(entry_claims(r["common_dir"], r["branch"], r["path"]) for r in prepared)))
        for repo in prepared:
            root = Path(repo["source"])
            with self.repo_lock(repo):
                base = self.base(root, refresh=True)
            repo.update(base_ref=base, base_sha=git(root, "rev-parse", base), step="pending")
        return prepared

    def member_branch_and_path(self, project: dict[str, Any], repo_id: str,
                               name: str) -> tuple[str, Path]:
        if not re.fullmatch(MEMBER_NAME_PATTERN, name):
            raise ProjectError("Use a simple member slug containing letters/digits/dashes/underscores.")
        target = f"{project['branch']}--{name}"
        return target, self.worktrees / repo_id / branch_suffix(target, project["branch_prefix"])

    def resolve_repos(self, names: list[str], project: dict[str, Any],
                      replacing: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Validate repository arguments locally, without fetching."""
        prepared: list[dict[str, Any]] = []
        for name in names:
            supplied = Path(self.resolve_repo(name)).expanduser()
            source = (supplied if supplied.is_absolute() else self.sources / supplied).resolve()
            root = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
            if replacing and any(inside(root, Path(r["path"])) for r in project_entries(replacing)):
                raise ProjectError(f"Replacement source would be removed by cleanup; use the primary checkout: {root}")
            identity = str(common_dir(root))
            repo_id = root.name
            if not re.fullmatch(REPOSITORY_NAME_PATTERN, repo_id):
                raise ProjectError(f"Unsupported repository name: {repo_id}")
            if any(r["id"] == repo_id or r["common_dir"] == identity for r in project["repos"] + prepared):
                raise ProjectError(f"Duplicate repository identity or name: {repo_id}")
            historical_links = {link_name(entry) for entry in project_entries(project, include_retired=True)}
            historical_links.update(f"{r['id']}--{member_name(r)}" for r in project["repos"] if member_name(r))
            if repo_id in managed_links(project) or repo_id in historical_links:
                raise ProjectError(f"Repository name collides with a member link: {repo_id}")
            path = self.worktrees / repo_id / branch_suffix(project["branch"], project["branch_prefix"])
            owned = replacing is not None and any(
                r["common_dir"] == identity and r["path"] == str(path) and r["branch"] == project["branch"]
                for r in replacing["repos"]
            )
            if (path.exists() or path.is_symlink()) and not owned:
                raise ProjectError(f"Worktree path already exists: {path}")
            if not owned and git(root, "rev-parse", "--verify", f"refs/heads/{project['branch']}", check=False):
                raise ProjectError(f"Branch already exists in {root}: {project['branch']}")
            prepared.append({"id": repo_id, "source": str(root), "common_dir": identity,
                             "path": str(path), "branch": project["branch"],
                             "branch_prefix": project["branch_prefix"]})
        return prepared

    def check_coordinator(self, project: dict[str, Any], idle: bool = False, allow_dirty: bool = False) -> Path:
        path = self.slots / project["slot"]
        if common_dir(path) != common_dir(self.repo):
            raise ProjectError(f"Coordinator identity changed: {path}")
        expected = project["slot"] if idle else project["branch"]
        if branch(path) != expected:
            raise ProjectError(f"Expected branch {expected} at {path}")
        if not allow_dirty and git(path, "status", "--porcelain", "--untracked-files=all"):
            raise ProjectError(f"Coordinator has uncommitted files: {path}")
        return path

    def validate_repo(self, repo: dict[str, Any]) -> None:
        source, path = Path(repo["source"]), Path(repo["path"])
        if common_dir(source) != Path(repo["common_dir"]) or common_dir(path) != Path(repo["common_dir"]):
            raise ProjectError(f"Repository identity changed: {path}")
        if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
            raise ProjectError(f"Not an exact worktree root: {path}")
        if branch(path) != repo["branch"]:
            raise ProjectError(f"Unexpected branch at {path}")
        expected = self.worktrees / repo["id"] / branch_suffix(repo["branch"], repo["branch_prefix"])
        if path != expected or not inside(path.resolve(), self.worktrees):
            raise ProjectError(f"Unsafe worktree path: {path}")

    def validate_clean_repo(self, repo: dict[str, Any], allow_dirty: bool = False) -> None:
        self.validate_repo(repo)
        path = Path(repo["path"])
        if not allow_dirty and git(path, "status", "--porcelain", "--untracked-files=all"):
            raise ProjectError(f"Worktree has dirty or untracked files: {path}")
        ignored = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        ignored_dirs = {entry.rstrip("/") for entry in ignored.split("\0") if entry.endswith("/")}
        for directory, dirs, files in os.walk(path):
            if Path(directory) != path and (".git" in dirs or ".git" in files):
                relative = str(Path(directory).relative_to(path))
                if git(path, "check-ignore", "--", relative, check=False):
                    dirs.clear()
                    continue
                raise ProjectError(f"Nested repository in {path}: {directory}")
            dirs[:] = [d for d in dirs if d != ".git"
                       and str((Path(directory) / d).relative_to(path)) not in ignored_dirs]

    if TYPE_CHECKING:
        # Implemented by the command mixins that make up manager.Manager; declared so they can call each other.
        def artifacts_git(self, directory: Path) -> Path: ...
        def validate_retro_directory(self, project: dict[str, Any]) -> Path: ...
        def preflight_blackboard(self, project: dict[str, Any]) -> None: ...
        def initialize_blackboard(self, project: dict[str, Any]) -> None: ...
        def artifact_checkout(self, root: Path, relative: list[str]) -> str: ...
        def sync_artifact_checkout(self, root: Path) -> None: ...
        def commit_artifact_paths(self, root: Path, message: str, relative: list[str], push: bool = True,
                                  sync: bool = True, artifacts_root: Path | None = None,
                                  prepare: Callable[[], None] | None = None,
                                  after_commit: Callable[[], None] | None = None) -> dict[str, bool]: ...
        def materialize(self, project: dict[str, Any]) -> None: ...
        def reconcile_coordinator(self, project: dict[str, Any], previous_links: dict[str, Path] | None = None) -> None: ...
        def check_member_destination(self, project: dict[str, Any], repo: dict[str, Any], name: str,
                                     target: str, path: Path, *, allow_existing: bool = False) -> str: ...
        def prepare_first_member(self, project: dict[str, Any], repo: dict[str, Any], member: str,
                                current: dict[str, Any] | None) -> dict[str, Any] | None: ...
        def finish_first_member(self, project: dict[str, Any], repo: dict[str, Any],
                               pending: dict[str, Any]) -> None: ...
        def cleanup(self, selector: str | None, dry_run: bool, allow_unmerged: list[str], *,
                    abandonment_verified: bool = False, force: bool = False) -> None: ...
        def resume_command(self, project: dict[str, Any]) -> str | None: ...
