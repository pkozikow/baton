"""Blackboard setup and commits of project artifacts in the shared artifacts checkout."""

import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from blackboard import Blackboard, revision_of
from .blackboard_sqlite import SQLiteBlackboard, SCHEMA_VERSION
from .errors import ProjectError
from .gitutil import git, common_dir, branch, file_lock, inside, require_agent_write_path
from .manifest import (project_entries, subplan_directory, coordination_context, artifacts_branch_name,
                       artifacts_final_path)
from .native import github_repository
from .slots import read_active_project
from .core import ManagerCore


# The sealed coordination files preserve_blackboard exports and commits.
EVIDENCE_FILES = ("board.sqlite", "overview.md", "history.md", "manifest.json")


def unsafe_artifact_path(path: Path) -> bool:
    return not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents))


class NonRegularArtifact(OSError):
    pass


def open_artifact(root: Path, relative: PurePosixPath) -> int:
    """Open a file or directory beneath root, refusing a symlink at every component; returns the descriptor."""
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError(f"Unsafe artifact path: {relative}")
    # O_NONBLOCK keeps a FIFO from blocking the open; callers reject non-regular files by type.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in relative.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_artifact(root: Path, relative: PurePosixPath, limit: int | None = None) -> bytes:
    """A regular file's bytes, read through open_artifact; with a limit, at most limit + 1 bytes."""
    descriptor = open_artifact(root, relative)
    try:
        # fdopen() refuses a directory without closing the descriptor it was given.
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NonRegularArtifact(f"Not a regular file: {root / relative}")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        return stream.read() if limit is None else stream.read(limit + 1)


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

    def preflight_blackboard(self, project: dict[str, Any], directory: Path | None = None) -> None:
        """Check the board's ignore rules in the project's directory, or in the given one before it exists."""
        directory = self.artifact_directory(project) if directory is None else directory
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
        # A sealed archive describes where the project lives once archived, which for an artifacts branch is
        # its directory in the shared checkout after the branch is merged.
        context = checkpoint.get("context", {}) if checkpoint else coordination_context(
            project, artifacts_final_path(project) if final else self.artifact_directory(project))
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
        for name in EVIDENCE_FILES:
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
        names = EVIDENCE_FILES
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
        target = artifacts_branch_name(project)
        self.commit_artifact_paths(root, "Preserve sealed project coordination", relative, push=False, sync=False,
                                   artifacts_root=self.artifact_directory(project).parent, expected_branch=target)
        commit = git(root, "rev-parse", "HEAD")
        archive = project["coordination_archive"]
        self.verify_archive_commit(root, commit, relative, archive["checksums"])
        archive["commit"] = commit
        self.save()
        # Remote publication may fail; the verified local preservation commit remains authoritative.
        self.commit_artifact_paths(root, "Preserve sealed project coordination", relative, push=True,
                                   artifacts_root=self.artifact_directory(project).parent, expected_branch=target)

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
        """Commit only these project artifact paths, leaving other staged work alone.

        They go on the project's artifacts branch when it has one, otherwise on the shared checkout's default branch.
        """
        target = artifacts_branch_name(project)
        if target is not None:
            self.require_open_artifacts(project)
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
        self.commit_artifact_paths(root, message, relative, push, artifacts_root=directory.parent,
                                   prepare=None if target is None else lambda: self.require_open_artifacts(project),
                                   expected_branch=target)

    def require_open_artifacts(self, project: dict[str, Any]) -> None:
        """Refuse new artifacts on a sealed project branch, judged from the registry as it is now.

        Cleanup seals the branch when it starts, and force cleanup archives the project with the branch retained.
        Cleanup's own seal commits go through preserve_blackboard, not here.
        """
        current = None
        try:
            record = read_active_project(self.layout / "projects" / f"{project['slot']}.json")
        except FileNotFoundError:
            record = {}
        if record.get("uuid") == project["uuid"]:
            current = record
        if current is None:
            current = next((p for p in self.archived_projects() if p["uuid"] == project["uuid"]), project)
        state = "archived" if current.get("_archived") else current.get("state")
        target = artifacts_branch_name(current)
        if target is not None and state in ("cleaning", "archived"):
            raise ProjectError(f"Project {current['name']} is {state} and its artifacts branch {target} is sealed; "
                               "it takes no new artifacts.")

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

    def artifact_checkout(self, root: Path, relative: list[str], expected_branch: str | None = None) -> str:
        """Check that the checkout can take a commit of these paths and return the branch it commits on.

        Without an expected branch, that is the shared checkout's default branch.
        """
        if expected_branch is None:
            default = self.base(root, refresh=False).removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
        else:
            default = expected_branch
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
                              after_commit: Callable[[], None] | None = None,
                              expected_branch: str | None = None) -> dict[str, bool]:
        """Commit on the default branch of the shared checkout, or on a project's artifacts branch.

        A project branch has a single writer checkout, so it is neither synced nor compared with origin: the commit
        is pushed to origin/<branch>, which creates it on the first push.
        """
        default = self.artifact_checkout(root, relative, expected_branch)
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
        fetched = remote and sync and expected_branch is None and self.fetch_artifacts(root, default)
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
                elif expected_branch is not None or not sync or fetched:
                    pushable = True
        published = False
        if pushable and expected_branch is not None:
            published = self.push_artifacts_branch(root, expected_branch)
        elif pushable:
            # Also pushes earlier artifact commits whose push failed, e.g. when a retried commit has nothing new.
            pushed = self.push_artifacts(root, default, artifacts_root=artifacts_root)
            published = pushed or bool(fetched and self.artifact_divergence(root, default) == (0, 0))
        elif remote and sync and not fetched and expected_branch is None:
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

    @staticmethod
    def outside_directory(root: Path, old: str, new: str, directory: Path) -> list[str]:
        """Paths changed between two commits that are not under directory, judged by path names alone.

        Baton merges pull requests on this answer, so unlike non_artifact_paths it never resolves the checkout's
        files: a symlink there must not be able to widen what is allowed.
        """
        names = [name for name in git(root, "diff", "--name-only", "--no-renames", "-z", old, new).split("\0") if name]
        try:
            prefix = directory.relative_to(root).as_posix()
        except ValueError:
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

    def fast_forward_artifacts(self, root: Path, default: str, *, artifacts_root: Path | None = None,
                               any_change: bool | None = None) -> None:
        """Fast-forward the shared checkout to origin.

        With dedicated_artifacts_branch (or any_change) any pure fast-forward is taken; with it off, only one that
        brings in project artifacts alone.
        """
        # A project's artifacts branch is never fast-forwarded to the default branch.
        if branch(root) != default:
            return
        counts = self.artifact_divergence(root, default)
        if not counts or not counts[1] or counts[0]:
            return
        upstream = f"refs/remotes/origin/{default}"
        if not (self.dedicated_artifacts_branch if any_change is None else any_change):
            # With dedicated_artifacts_branch off, the shared checkout is the live framework code and guidance for
            # every agent; deploying it is the user's call.
            allowed = artifacts_root if artifacts_root is not None else self.artifacts
            code = self.non_artifact_paths(root, "HEAD", upstream, artifacts_root=allowed)
            if code:
                self.artifact_warning(root, default, f"origin/{default} changes files outside {allowed} "
                                                     f"({self.summarize(code)}); fast-forwarding would deploy them to "
                                                     "every agent (dedicated_artifacts_branch is off), so this commit "
                                                     "stays local and diverges.", reconcile=True)
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

    def push_artifacts_branch(self, root: Path, name: str) -> bool:
        """Publish a project's artifacts branch, including earlier commits whose push failed."""
        pushed = self.remote_git(root, "push", "--quiet", "origin", f"refs/heads/{name}:refs/heads/{name}")
        if pushed.returncode:
            self.artifact_warning(root, name, f"Push to origin/{name} failed; artifact commits are saved locally. "
                                              f"Retry artifacts commit to publish them.\n{pushed.stderr.strip()}")
        else:
            print(f"Pushed artifacts branch {name} to origin/{name}.", flush=True)
        return pushed.returncode == 0

    def push_artifacts(self, root: Path, default: str, *, artifacts_root: Path | None = None) -> bool:
        """Publish the shared checkout's local commits; with dedicated_artifacts_branch, through a pull request."""
        if self.dedicated_artifacts_branch:
            return self.publish_artifacts(root, default, artifacts_root=artifacts_root)
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

    def publish_artifacts(self, root: Path, default: str, *, artifacts_root: Path | None = None) -> bool:
        """Publish the shared checkout's HEAD through one merged pull request, then fast-forward the checkout.

        Returns whether that HEAD is an ancestor of the fetched origin/<default>, even when the fast-forward is
        blocked. Nothing is ever pushed to the default branch itself.
        """
        sha = git(root, "rev-parse", "HEAD")
        allowed = artifacts_root if artifacts_root is not None else self.artifacts
        published = self.merge_artifacts_pr(
            root, default, f"{self.branch_prefix}/artifacts-{sha}", sha, allowed,
            title=f"Publish artifact commits up to {sha[:12]}",
            body=f"Opened by Baton to publish artifact commits from the shared checkout {root.name}. "
                 "Baton merges it itself only because every change is under the artifacts root.")
        if published:
            with self.artifacts_lock(root):
                # A commit that landed meanwhile is published, and synchronizes the checkout, on its own turn.
                if git(root, "rev-parse", "HEAD") == sha:
                    self.fast_forward_artifacts(root, default, artifacts_root=allowed, any_change=True)
        return published

    @staticmethod
    def is_ancestor(root: Path, commit: str, ref: str) -> bool:
        return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", commit, ref],
                              capture_output=True).returncode == 0

    @staticmethod
    def gh(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        """Run gh without prompts; a missing or hung gh is an ordinary failed call."""
        env = dict(os.environ, GH_PROMPT_DISABLED="1")
        try:
            return subprocess.run(["gh", *args], cwd=root, text=True, capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            return subprocess.CompletedProcess(["gh", *args], 124, "", str(error))

    def artifacts_prs(self, root: Path, repository: str, name: str) -> list[dict[str, Any]]:
        """Every pull request, in any state, whose head is this branch of the repository itself, not of a fork."""
        listed = self.gh(root, "pr", "list", "--repo", repository, "--head", name, "--state", "all", "--limit", "100",
                         "--json", "number,url,state,headRefName,headRefOid,baseRefName,isCrossRepository",
                         timeout=30)
        if listed.returncode:
            raise ProjectError(f"Cannot list pull requests for {name} in {repository} with gh; check that gh is "
                               f"installed and authenticated for it. {listed.stderr.strip()}")
        try:
            prs = json.loads(listed.stdout)
            if not isinstance(prs, list) or any(
                    not isinstance(pr, dict) or type(pr.get("number")) is not int
                    or pr.get("state") not in ("OPEN", "CLOSED", "MERGED")
                    or not all(isinstance(pr.get(key), str) for key in ("headRefName", "headRefOid", "baseRefName"))
                    for pr in prs):
                raise ValueError("unexpected pull request list")
        except ValueError as error:
            raise ProjectError(f"Cannot read pull requests for {name} in {repository}: {error}") from error
        # gh matches --head by branch name alone, so a fork's branch of the same name is listed too.
        return [pr for pr in prs if pr["headRefName"] == name and not pr.get("isCrossRepository")]

    def merge_artifacts_pr(self, root: Path, default: str, name: str, sha: str, allowed: Path, *,
                           title: str, body: str, keep_branch: bool = False) -> bool:
        """Get commit sha into origin/<default> through a pull request from branch name, merged with a merge commit.

        Baton merges only these pull requests, so every file changed since the merge base must be under allowed, and
        the merge is pinned to sha. Returns whether sha is an ancestor of origin/<default>; failures are warnings
        and leave the commits where they were.
        """
        upstream = f"refs/remotes/origin/{default}"
        # Already published as far as the last fetch knows: nothing to do, and no network call.
        if self.is_ancestor(root, sha, upstream):
            return True
        if not self.fetch_artifacts(root, default):
            return False
        if self.is_ancestor(root, sha, upstream):
            return True
        base = git(root, "merge-base", upstream, sha, check=False)
        outside = self.outside_directory(root, base, sha, allowed) if base else ["(no merge base)"]
        if outside:
            self.artifact_warning(root, default, f"Local commits up to {sha[:12]} change files outside {allowed} "
                                                 f"({self.summarize(outside)}); nothing was published. "
                                                 "Ask the user to publish or reconcile these changes.")
            return False
        repository = github_repository(root)
        if repository is None:
            self.artifact_warning(root, default, f"origin of {root} is not a GitHub repository; pull request "
                                                 "publication is unavailable and the commits stay local.")
            return False
        failure: ProjectError | None = None
        try:
            # Listed before the push, so an unusable gh leaves no branch behind on origin.
            listed = self.artifacts_prs(root, repository, name)
            pushed = self.remote_git(root, "push", "--quiet", "origin", f"{sha}:refs/heads/{name}")
            if pushed.returncode:
                self.artifact_warning(root, default, f"Push to origin/{name} failed; artifact commits are saved "
                                                     f"locally. Retry artifacts commit to publish them.\n"
                                                     f"{pushed.stderr.strip()}")
                return False
            # That push was not forced, so the branch, and with it any open pull request from it, is now at sha;
            # the pinned merge below refuses anything else.
            reusable = [pr for pr in listed if pr["state"] == "OPEN" and pr["baseRefName"] == default]
            if reusable:
                number, url = str(reusable[0]["number"]), reusable[0].get("url", "")
            else:
                created = self.gh(root, "pr", "create", "--repo", repository, "--head", name, "--base", default,
                                  "--title", title, "--body", body)
                url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
                match = re.search(r"/pull/(\d+)$", url)
                if created.returncode or not match:
                    raise ProjectError(f"gh pr create failed: {(created.stderr or created.stdout).strip()}")
                number = match.group(1)
            print(f"baton: merging {url or name} into {default} (head pinned to {sha[:12]})", file=sys.stderr, flush=True)
            merged = self.gh(root, "pr", "merge", number, "--repo", repository, "--merge", "--match-head-commit", sha)
            if merged.returncode:
                raise ProjectError(f"gh pr merge failed: {(merged.stderr or merged.stdout).strip()}")
        except ProjectError as error:
            failure = error
        # A concurrent publisher of the same commit, or a merge whose response was lost, still published it.
        if not (self.fetch_artifacts(root, default) and self.is_ancestor(root, sha, upstream)):
            self.artifact_warning(root, default, (
                f"Pull request publication of {sha[:12]} from {name} failed; the commits stay local and the next "
                f"artifact commit retries. {failure}" if failure else
                f"Merged {name}, but {sha[:12]} is not yet in origin/{default}; the next artifact commit checks again."))
            return False
        print(f"Published {sha[:12]} to origin/{default} through a pull request from {name}.", flush=True)
        if not keep_branch:
            self.delete_remote_branch(root, name, sha)
        return True

    def delete_remote_branch(self, root: Path, name: str, sha: str) -> bool:
        """Best-effort removal of a merged branch from origin, only while it still points at the merged commit."""
        deleted = self.remote_git(root, "push", "--quiet", f"--force-with-lease=refs/heads/{name}:{sha}", "origin",
                                  f":refs/heads/{name}")
        if deleted.returncode == 0:
            return True
        present = self.remote_git(root, "ls-remote", "--heads", "origin", f"refs/heads/{name}")
        if present.returncode == 0 and not present.stdout.strip():
            return True
        print(f"baton: warning: could not delete origin/{name}; origin is unreachable or the branch is no longer at "
              f"the merged commit {sha[:12]}, so check it before deleting it by hand. {deleted.stderr.strip()}",
              file=sys.stderr, flush=True)
        return False
