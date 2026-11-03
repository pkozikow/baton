"""Blackboard setup and commits of project artifacts in the shared artifacts checkout."""

import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from blackboard import Blackboard, revision_of
from .blackboard_sqlite import SQLiteBlackboard, SCHEMA_VERSION
from .errors import ProjectError
from .gitutil import git, common_dir, branch, file_lock, inside, require_agent_write_path
from .manifest import project_entries, subplan_directory, coordination_context
from .core import ManagerCore


def unsafe_artifact_path(path: Path) -> bool:
    return not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents))


class ArtifactsMixin(ManagerCore):
    def artifacts_git(self, directory: Path) -> Path:
        require_agent_write_path(directory)
        current = directory
        while not current.exists():
            current = current.parent
        try:
            root = Path(git(current, "rev-parse", "--show-toplevel")).resolve()
        except ProjectError as error:
            raise ProjectError(f"Project artifacts directory {directory} is not inside a Git checkout ({error}); "
                               "configure paths.artifacts_root inside one or run git init in its checkout root.") from error
        if not inside(directory, root):
            raise ProjectError("Project artifacts must be inside a Git checkout.")
        if directory.exists() and common_dir(directory) != common_dir(root):
            raise ProjectError("Project artifacts belong to an unexpected nested repository.")
        return root

    def preflight_blackboard(self, project: dict[str, Any]) -> None:
        directory = self.artifact_directory(project)
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ProjectError(f"Unsafe project artifacts directory: {directory}")
        root = self.artifacts_git(directory)
        for name in ("blackboard-lock.md", "blackboard.sqlite", "blackboard.sqlite-wal", "blackboard.sqlite-shm"):
            path = directory / name
            if git(root, "ls-files", "--", str(path.relative_to(root))):
                raise ProjectError(f"Refusing tracked blackboard file {path}; move it aside "
                                   "(git mv to another name and commit) before reusing this project name.")
            if not git(root, "check-ignore", "--no-index", "--", str(path), check=False):
                raise ProjectError(f"Blackboard file must be Git-ignored: {path}")
            Blackboard._regular(path, missing=True)
        if directory.exists():
            info = project.get("blackboard")
            board = self.board_for(project) if info else SQLiteBlackboard(directory, project["name"], "", project["uuid"])
            board.validate_initialization()

    def board_for(self, project: dict[str, Any]) -> SQLiteBlackboard:
        info = project.get("blackboard")
        if not info:
            raise ProjectError("Blackboard is not initialized; run baton blackboard init.")
        if info.get("backend") != "sqlite":
            raise ProjectError("Unsupported blackboard backend.")
        return SQLiteBlackboard(Path(info["directory"]), info.get("project", project["name"]), info["generation"], project["uuid"])

    def initialize_blackboard(self, project: dict[str, Any]) -> None:
        info = project.get("blackboard")
        if info and info.get("initialized"):
            return
        self.preflight_blackboard(project)
        if info is None:
            info = {"directory": str(self.artifact_directory(project)), "generation": uuid.uuid4().hex, "backend": "sqlite", "schema_version": SCHEMA_VERSION}
            project["blackboard"] = info
            self.save()
        self.board_for(project).initialize()
        info["initialized"] = True
        self.save()

    def export_blackboard(self, project: dict[str, Any], destination: Path | None = None, *, final: bool = False) -> dict[str, Any]:
        board = self.board_for(project)
        checkpoint = project.get("coordination_archive") if final else None
        context = checkpoint.get("context", {}) if checkpoint else coordination_context(project, self.artifact_directory(project))
        destination = destination or board.directory / ".blackboard-export"
        coordination = self.artifact_directory(project) / "coordination"
        if not final and (destination.resolve() == coordination.resolve() or (project.get("coordination_archive", {}).get("directory") and Path(project["coordination_archive"]["directory"]).resolve() == destination.resolve())):
            raise ProjectError("Final coordination archives cannot be overwritten by an ordinary export.")
        return board.export(destination, replace_sealed=final, context=context)

    def preflight_coordination_export(self, project: dict[str, Any]) -> None:
        info = project.get("blackboard")
        if not info:
            return
        directory = self.artifact_directory(project)
        names = ("blackboard.sqlite", "blackboard-lock.md")
        if not info.get("initialized") and not any((directory / name).exists() or (directory / name).is_symlink() for name in names):
            return
        root = self.artifacts_git(directory)
        for name in ("board.sqlite", "overview.md", "history.md", "manifest.json"):
            path = directory / "coordination" / name
            if git(root, "check-ignore", "--no-index", "--", str(path), check=False):
                raise ProjectError(f"Update the artifact checkout .gitignore before cleanup: final coordination evidence must not be ignored: {path}")
        for name in ("blackboard-lock.md", ".blackboard-preflight.tmp", ".snapshot-preflight.sqlite"):
            path = directory / "coordination" / name
            if not git(root, "check-ignore", "--no-index", "--", str(path), check=False):
                raise ProjectError("Update the artifact checkout .gitignore before cleanup: ignore coordination/blackboard-lock.md, "
                                   "coordination/.blackboard-*.tmp and coordination/.snapshot-*.sqlite*; keep the four final evidence files tracked.")

    def preserve_blackboard(self, project: dict[str, Any]) -> None:
        info = project.get("blackboard")
        if not info:
            return
        directory = self.artifact_directory(project) / "coordination"
        archive = project.get("coordination_archive")
        root = self.artifacts_git(directory)
        names = ("board.sqlite", "overview.md", "history.md", "manifest.json")
        relative = [str((directory / name).relative_to(root)) for name in names]
        if archive and not archive.get("uninitialized"):
            if (archive.get("uuid"), archive.get("generation")) != (project["uuid"], info["generation"]) or archive.get("schema_version") != SCHEMA_VERSION:
                raise ProjectError("Coordination archive identity/schema differs from this project; cleanup blocked.")
        board = self.board_for(project)
        if archive and not archive.get("uninitialized") and not archive.get("commit"):
            previous = git(root, "log", "-1", "--format=%H", "--", *relative)
            if previous:
                try:
                    self.verify_archive_commit(root, previous, relative, archive.get("checksums", {}))
                except ProjectError:
                    pass
                else:
                    archive["commit"] = previous
                    self.save()
        if archive and archive.get("commit"):
            evidence = self.verify_archive_commit(root, archive["commit"], relative, archive.get("checksums", {}))
            output = Blackboard(directory, project["name"], info["generation"])
            with output._locked(create=True):
                for suffix in ("-wal", "-shm", "-journal"):
                    if output._regular(directory / ("board.sqlite" + suffix), missing=True) is not None:
                        raise ProjectError("Recover archive SQLite sidecars before restoring committed evidence.")
                for name, content in evidence.items():
                    if output._data(name, missing=True) != content:
                        output._replace(name, content)
            if board.path.exists():
                with board._locked(read_only=True), contextlib.closing(board._connect(readonly=True)) as connection:
                    if board._metadata(connection)["status"] != "sealed":
                        raise ProjectError("Committed archive source is no longer sealed; cleanup blocked.")
            return
        if not info.get("initialized"):
            evidence = (board.path, board.directory / "blackboard-lock.md")
            if not any(path.exists() or path.is_symlink() for path in evidence):
                project["coordination_archive"] = {"uninitialized": True, "uuid": project["uuid"], "generation": info["generation"]}
                self.save()
                return
            board.initialize()
            info["initialized"] = True
            self.save()
        board.freeze()
        if archive and not archive.get("uninitialized"):
            with board._locked(read_only=True), contextlib.closing(board._connect(readonly=True)) as connection:
                digest = board.logical_digest(board.snapshot_data(connection))
            if digest != archive.get("logical_sha256"):
                raise ProjectError("Sealed database differs from the recorded logical snapshot; restore it before retrying.")
        manifest = self.export_blackboard(project, directory, final=True)
        if archive and not archive.get("uninitialized") and (manifest["sequence"] != archive.get("sequence") or manifest["logical_sha256"] != archive.get("logical_sha256")):
            raise ProjectError("Archive retry changed the sealed logical snapshot; cleanup blocked.")
        project["coordination_archive"] = {"directory": str(directory), "sequence": manifest["sequence"],
            "logical_sha256": manifest["logical_sha256"], "uuid": project["uuid"], "generation": info["generation"],
            "schema_version": manifest["schema_version"], "context": manifest.get("context", {}),
            "checksums": {name: revision_of((directory / name).read_bytes()) for name in names}}
        self.save()
        self.commit_artifact_paths(root, "Preserve sealed project coordination", relative, push=False, sync=False,
                                   artifacts_root=self.artifact_directory(project).parent)
        commit = git(root, "rev-parse", "HEAD")
        archive = project["coordination_archive"]
        self.verify_archive_commit(root, commit, relative, archive["checksums"])
        archive["commit"] = commit
        self.save()
        # Remote publication may fail; the verified local preservation commit remains authoritative.
        self.commit_artifact_paths(root, "Preserve sealed project coordination", relative, push=True,
                                   artifacts_root=self.artifact_directory(project).parent)

    @staticmethod
    def verify_archive_commit(root: Path, commit: str, paths: list[str], checksums: dict[str, str]) -> dict[str, bytes]:
        evidence = {}
        for path in paths:
            expected = checksums.get(Path(path).name)
            if not isinstance(expected, str) or len(expected) != 64:
                raise ProjectError("Coordination archive checkpoint is missing a required checksum; cleanup blocked.")
            result = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{path}"], capture_output=True)
            if result.returncode or revision_of(result.stdout) != expected:
                raise ProjectError("Local commit does not preserve the coordination archive; restore it before resuming cleanup.")
            evidence[Path(path).name] = result.stdout
        return evidence

    def commit_artifacts(self, project: dict[str, Any], message: str, paths: list[str], push: bool = True) -> None:
        """Commit only these project artifact paths in the shared checkout, leaving other staged work alone."""
        directory = self.artifact_directory(project)
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ProjectError(f"Unsafe project artifacts directory: {directory}")
        root = self.artifacts_git(directory)
        owned = self.owned_artifacts(project, directory)
        relative = []
        for supplied in paths:
            path = Path(supplied).resolve()
            if not inside(path, directory):
                raise ProjectError(f"Artifact path is outside {directory}: {supplied}")
            # Master, retro and archived notes stay committable everywhere; only sibling subplans are off limits.
            if owned and inside(path, directory / "branches") and not inside(path, owned):
                raise ProjectError(f"{supplied} is another branch's subplan; from this branch checkout commit only "
                                   f"{owned}, and leave other subplans to their workers or the parent.")
            relative.append(str(path.relative_to(root)))
        self.commit_artifact_paths(root, message, relative, push, artifacts_root=directory.parent)

    @staticmethod
    def owned_artifacts(project: dict[str, Any], directory: Path) -> Path | None:
        """The only subplan directory a branch checkout may commit, when the project has several branches."""
        if project.get("completed_at") or project.get("_archived"):
            return None
        members = project_entries(project, include_retired=True)
        cwd = Path.cwd().resolve()
        here = [entry for entry in members if inside(cwd, Path(entry["path"]).resolve())]
        if len(members) < 2 or not here:
            return None
        entry = max(here, key=lambda e: len(e["path"]))
        return subplan_directory(directory, entry["id"], entry["branch"], project.get("branch_prefix"))

    def artifact_checkout(self, root: Path, relative: list[str]) -> str:
        """Check that the shared checkout can take a commit of these paths and return its default branch."""
        default = self.base(root, refresh=False).removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
        if branch(root) != default:
            raise ProjectError(f"Project artifacts are committed only on {default}; {root} is on "
                               f"{branch(root) or 'a detached HEAD'}.")
        # The index restore after a failed commit cannot recreate conflict stages.
        if git(root, "--literal-pathspecs", "ls-files", "-u", "--", *relative):
            raise ProjectError("Resolve the merge conflicts in these artifact paths first.")
        return default

    @staticmethod
    def artifacts_lock(root: Path) -> contextlib.AbstractContextManager[bool]:
        # The index is per worktree, so the lock lives in this checkout's Git directory.
        return file_lock(Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "project-artifacts.lock")),
                         f"artifacts commit in {root}")

    def sync_artifact_checkout(self, root: Path) -> None:
        """Fast-forward the shared checkout before a caller moves files in it (rename)."""
        if "origin" in git(root, "remote").splitlines():
            default = self.artifact_checkout(root, [])
            if self.fetch_artifacts(root, default):
                with self.artifacts_lock(root):
                    self.fast_forward_artifacts(root, default)

    def commit_artifact_paths(self, root: Path, message: str, relative: list[str], push: bool = True,
                              sync: bool = True, artifacts_root: Path | None = None,
                              prepare: Callable[[], None] | None = None,
                              after_commit: Callable[[], None] | None = None) -> dict[str, bool]:
        default = self.artifact_checkout(root, relative)
        remote = push and "origin" in git(root, "remote").splitlines()
        requested = relative

        def index() -> dict[str, tuple[str, str]]:
            entries = {}
            for record in filter(None, git(root, "--literal-pathspecs", "ls-files", "-s", "-z", "--", *relative).split("\0")):
                info, name = record.split("\t", 1)
                mode, sha, _stage = info.split()
                entries[name] = (mode, sha)
            return entries

        def restore(before: dict[str, tuple[str, str]]) -> None:
            # Put back exactly what the index held for these paths, including content the user had staged.
            after = index()
            for name in sorted(after.keys() - before.keys()):
                git(root, "--literal-pathspecs", "rm", "--cached", "-q", "--", name)
            for name, (mode, sha) in before.items():
                if after.get(name) != (mode, sha):
                    git(root, "update-index", "--cacheinfo", mode, sha, name)

        def present(path: str) -> bool:
            return bool((root / path).exists() or git(root, "--literal-pathspecs", "ls-files", "--", path))

        def in_head(path: str) -> bool:
            return bool(git(root, "--literal-pathspecs", "ls-tree", "-r", "--name-only", "HEAD", "--", path))

        # Network calls stay outside the lock so parallel plan commits do not queue behind them.
        fetched = remote and sync and self.fetch_artifacts(root, default)
        pushable = False
        with self.artifacts_lock(root):
            if fetched:
                self.fast_forward_artifacts(root, default, artifacts_root=artifacts_root)
            if prepare is not None:
                prepare()
            # Paths are classified after the sync, which can add or remove files.
            unknown = [path for path in requested if not present(path) and not in_head(path)
                       and not git(root, "--literal-pathspecs", "rev-list", "-1", "HEAD", "--", path)]
            if unknown:
                raise ProjectError(f"Artifact paths do not exist and have no history: {' '.join(unknown)}")
            # A path moved away (git mv, or a rename already committed) exists in neither the worktree nor the index;
            # git add rejects it, while the commit must still include its deletion when HEAD has it.
            staged = [path for path in requested if present(path)]
            relative = [path for path in requested if present(path) or in_head(path)]
            unchanged = True
            if relative:
                before = index()
                try:
                    if staged:
                        git(root, "--literal-pathspecs", "add", "--", *staged)
                    unchanged = subprocess.run(["git", "-C", str(root), "--literal-pathspecs", "diff", "--cached",
                                                "--quiet", "HEAD", "--", *relative], capture_output=True).returncode == 0
                    if not unchanged:
                        # Hooks could stage unrelated files into the temporary --only index.
                        git(root, "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "commit", "--only", "-m",
                            message, "--", *relative)
                except ProjectError as error:
                    restore(before)
                    raise ProjectError(f"Artifact commit failed; restored the index for its paths. {error}") from error
                if unchanged:
                    restore(before)
            if unchanged:
                print(f"Nothing to commit in {root}: {' '.join(requested)}", flush=True)
            else:
                print(f"Committed {git(root, 'rev-parse', '--short', 'HEAD')} in {root}: {' '.join(relative)}", flush=True)
            if after_commit is not None:
                after_commit()
            if remote:
                if branch(root) != default:
                    self.artifact_warning(root, default, f"{root} left {default} during the commit; nothing was pushed.")
                elif not sync or fetched:
                    pushable = True
        published = False
        if pushable:
            # Also pushes earlier artifact commits whose push failed, e.g. when a retried commit has nothing new.
            pushed = self.push_artifacts(root, default, artifacts_root=artifacts_root)
            published = pushed or bool(fetched and self.artifact_divergence(root, default) == (0, 0))
        elif remote and sync and not fetched:
            self.artifact_warning(root, default, "Push skipped because fetch failed; artifact commits are saved locally. "
                                                 "Retry artifacts commit to publish them.")
        return {"published": published, "fetch_succeeded": bool(fetched)}

    @staticmethod
    def remote_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        # GIT_SSH_COMMAND would override a configured core.sshCommand.
        if not git(root, "config", "--get", "core.sshCommand", check=False):
            env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        started = time.monotonic()
        print(f"baton: {args[0]} origin in {root} (timeout 60s)", file=sys.stderr, flush=True)
        try:
            result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=60, env=env)
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess(args, 124, "", "timed out after 60s")
        print(f"baton: {args[0]} {'completed' if result.returncode == 0 else 'failed'} "
              f"after {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
        return result

    @staticmethod
    def artifact_divergence(root: Path, default: str) -> tuple[int, int] | None:
        counts = git(root, "rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{default}", check=False)
        if not counts:
            return None
        ahead, behind = counts.split()
        return int(ahead), int(behind)

    @staticmethod
    def artifact_warning(root: Path, default: str, detail: str, *, reconcile: bool = False) -> None:
        action = (f" Artifact commits in {root} stay local until {default} is reconciled with origin/{default}; "
                  "ask the user to do that. Agents never rebase, merge or reset this shared checkout." if reconcile else "")
        print(f"baton: warning: {detail}{action}", file=sys.stderr, flush=True)

    def non_artifact_paths(self, root: Path, old: str, new: str, *,
                           artifacts_root: Path | None = None) -> list[str]:
        """Use the selected project's historical artifact root, even after configuration changes."""
        allowed = artifacts_root if artifacts_root is not None else self.artifacts
        names = [name for name in git(root, "diff", "--name-only", "--no-renames", "-z", old, new).split("\0") if name]
        try:
            prefix = allowed.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            # A different configured checkout cannot authorize publishing this one.
            return names
        if prefix == ".":
            return []
        return [name for name in names if not name.startswith(prefix + "/")]

    def fetch_artifacts(self, root: Path, default: str) -> bool:
        fetched = self.remote_git(root, "fetch", "--quiet", "origin", f"refs/heads/{default}:refs/remotes/origin/{default}")
        if fetched.returncode:
            self.artifact_warning(root, default, f"Fetch failed; sync skipped for origin/{default}. "
                                                 f"Retry when remote access is restored.\n{fetched.stderr.strip()}")
        return not fetched.returncode

    def fast_forward_artifacts(self, root: Path, default: str, *, artifacts_root: Path | None = None) -> None:
        """Fast-forward the shared checkout to origin when that only brings in project artifacts."""
        counts = self.artifact_divergence(root, default)
        if not counts or not counts[1] or counts[0]:
            return
        upstream = f"refs/remotes/origin/{default}"
        # The shared checkout is the live framework code and guidance for every agent; deploying it is the user's call.
        allowed = artifacts_root if artifacts_root is not None else self.artifacts
        code = self.non_artifact_paths(root, "HEAD", upstream, artifacts_root=allowed)
        if code:
            self.artifact_warning(root, default, f"origin/{default} changes files outside {allowed} "
                                                 f"({self.summarize(code)}); fast-forwarding would deploy them to every "
                                                 "agent, so this commit stays local and diverges.", reconcile=True)
            return
        # Git refuses a fast-forward that would overwrite another agent's uncommitted files.
        merged = subprocess.run(["git", "-C", str(root), "merge", "--ff-only", "--quiet", upstream],
                                text=True, capture_output=True)
        if merged.returncode:
            self.artifact_warning(root, default, f"Cannot fast-forward to origin/{default}: "
                                                 f"{(merged.stderr or merged.stdout).strip()}; this commit stays local "
                                                 "and diverges.", reconcile=True)

    @staticmethod
    def summarize(paths: list[str]) -> str:
        return ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")

    def push_artifacts(self, root: Path, default: str, *, artifacts_root: Path | None = None) -> bool:
        counts = self.artifact_divergence(root, default)
        if counts is None or not counts[0]:
            return False
        ahead, behind = counts
        if behind:
            self.artifact_warning(root, default, f"{default} has diverged from origin/{default} "
                                                 f"({ahead} ahead, {behind} behind).", reconcile=True)
            return False
        # Other agents merge their own work into this checkout; publishing it is not an artifact commit's decision.
        allowed = artifacts_root if artifacts_root is not None else self.artifacts
        code = self.non_artifact_paths(root, f"refs/remotes/origin/{default}", "HEAD", artifacts_root=allowed)
        if code:
            self.artifact_warning(root, default, f"Local {default} has unpushed changes outside {allowed} "
                                                 f"({self.summarize(code)}); nothing was pushed. "
                                                 "Ask the user to publish or reconcile these changes.")
            return False
        pushed = self.remote_git(root, "push", "--quiet", "origin", f"HEAD:refs/heads/{default}")
        if pushed.returncode:
            self.artifact_warning(root, default, f"Push to origin/{default} failed; artifact commits are saved locally. "
                                                 f"Retry artifacts commit to publish them.\n{pushed.stderr.strip()}")
        else:
            print(f"Pushed {ahead} artifact commit{'s' if ahead != 1 else ''} to origin/{default}.", flush=True)
        return pushed.returncode == 0
