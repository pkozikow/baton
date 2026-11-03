"""Best-effort project cleanup with explicit confirmation and ownership fences."""

import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid
from typing import Any, Callable

from blackboard import BlackboardError

from .core import ManagerCore
from .errors import ProjectError
from .gitutil import branch, common_dir, git, inside, write_json
from .manifest import branch_suffix, managed_links, project_claims, project_entries
from .native import registered_stacks


FORCE_FAILURES = (ProjectError, BlackboardError, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired)


def nested_repository(path: Path, dirs: list[str], files: list[str]) -> bool:
    if '.git' in dirs or '.git' in files:
        return True
    if 'HEAD' not in files or 'objects' not in dirs:
        return False
    # Explicit gitdir prevents discovery of the enclosing managed worktree.
    return git(path, '--git-dir=.', 'rev-parse', '--is-bare-repository', check=False) == 'true'


class ForceCleanupMixin(ManagerCore):
    def force_cleanup(self, selector: str | None, dry_run: bool) -> None:
        from .cleanup import unarchived_notes

        project = self.project(selector)
        original = copy.deepcopy(project)
        warnings: list[str] = []

        def warn(message: str) -> None:
            warnings.append(message)
            print(f"Warning: {message}", file=sys.stderr)

        coordinator, current_branch, coordinator_tip = self.force_coordinator(project)
        print(f"{'Would force clean' if dry_run else 'Force cleanup'} {project['name']} ({project['slot']}):")
        print(f"  Reset coordinator {coordinator} to {project['slot']} and release its active registry record.")
        print(f"  Retain shared artifacts and the live board at {self.artifact_directory(project)}.")
        if project['state'] not in ('active', 'cleaning'):
            warn(f"Project is {project['state']}; unfinished operations will not be resumed.")
        if any(key.startswith('pending_') and value for key, value in project.items()):
            warn("Pending lifecycle operations will be abandoned locally; PR closures are not completed. No PRs will be closed.")
        artifacts_safe = False
        try:
            self.force_artifact_directory(project)
            artifacts_safe = True
        except FORCE_FAILURES as error:
            warn(f"Shared artifacts will be retained without archival: {error}")
        try:
            self.preflight_coordination_export(project)
            if project.get('blackboard'):
                self.board_for(project)
        except FORCE_FAILURES as error:
            warn(f"Coordination export preflight failed: {error}. Shared artifacts will remain in place.")
        status = git(coordinator, 'status', '--porcelain', '--untracked-files=all')
        if status:
            warn(f"Coordinator has uncommitted files; preserve its contents before resetting:\n{status}")
        links = coordinator / 'repos'
        known = managed_links(project)
        if links.is_symlink() or (links.exists() and not links.is_dir()):
            warn(f"Unexpected coordinator repos entry will be relocated without following symlinks: {links}")
        elif links.exists():
            for link in links.iterdir():
                if link.name not in known or not link.is_symlink() or link.readlink() != known[link.name]:
                    warn(f"Unmanaged coordinator entry will be preserved: {link}")
        for repo in project['repos']:
            try:
                for record, _scope, native in registered_stacks(repo, allow_missing=True):
                    if native is not None:
                        warn(f"Native stack {record['root']} in {repo['id']} will be retained; native metadata may require later repair.")
            except FORCE_FAILURES as error:
                warn(f"Cannot inspect native stack metadata in {repo['id']}: {error}; retained in place.")
        snapshots: dict[str, tuple[bool, tuple[int, int] | None, str]] = {}
        for entry in project_entries(project):
            path, source = Path(entry['path']), Path(entry['source'])
            print(f"  Remove owned worktree {path} and local branch {entry['branch']} (preserve Git tip first).")
            safe, identity, tip = False, None, ''
            try:
                self.force_worktree_identity(project, entry)
                safe = True
                if path.exists():
                    info = path.stat()
                    identity = (info.st_dev, info.st_ino)
                else:
                    warn(f"Registered worktree is missing: {path}")
            except FORCE_FAILURES as error:
                warn(f"Retaining unsafe or ambiguous worktree {path}: {error}")
            try:
                if common_dir(source) != Path(entry['common_dir']):
                    raise ProjectError('Source repository identity changed')
                tip = git(source, 'rev-parse', '--verify', f"refs/heads/{entry['branch']}", check=False)
                if not tip:
                    warn(f"Missing project branch {entry['branch']} in {source}.")
                else:
                    target = self.base(source, refresh=False, use_cached_default=True)
                    result = subprocess.run(['git', '-C', str(source), 'merge-base', '--is-ancestor', tip, target], capture_output=True)
                    if result.returncode:
                        warn(f"Unmerged commits in {entry['id']} ({entry['branch']} versus {target}); preserve tip {tip} in a local archive ref.")
            except FORCE_FAILURES as error:
                warn(f"Cannot inspect source repository {source}: {error}. Git history may not be preservable; retain any unverifiable branch.")
            if safe and path.exists():
                try:
                    dirty = git(path, 'status', '--porcelain', '--untracked-files=all')
                    if dirty:
                        warn(f"Dirty or untracked files in {path} will be permanently discarded:\n{dirty}")
                    notes = unarchived_notes(path, self.artifact_directory(project))
                    if notes:
                        warn(f"Unarchived ignored note files in {path} will be deleted:\n" + '\n'.join(notes))
                except FORCE_FAILURES as error:
                    warn(f"Cannot inspect files in {path}: {error}. Removing this owned directory can discard unpreserved files.")
            snapshots[str(path)] = (safe, identity, tip)
        warn('Force cleanup can permanently discard code-worktree files. Unremovable or unverifiable resources remain as reported leftovers; force never closes PRs.')
        warn('Coordinator tracked, untracked and ignored contents can be permanently discarded if their recovery copy fails.')
        if dry_run:
            return
        if not sys.stdin.isatty():
            raise ProjectError('Interactive confirmation is required for force cleanup.')
        print('Continue force cleanup? [y/N] ', end='', file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() not in ('y', 'yes'):
            raise ProjectError('Cleanup cancelled; project unchanged.')
        if self.force_coordinator(project) != (coordinator, current_branch, coordinator_tip):
            raise ProjectError('Coordinator changed during confirmation; retry force cleanup.')
        os.chdir(coordinator)
        if artifacts_safe:
            try:
                self.force_artifact_directory(project)
                self.preserve_blackboard(project)
            except FORCE_FAILURES as error:
                warn(f"Coordination archive failed: {error}. Shared artifacts and the live board remain in place.")
        for entry in reversed(project_entries(project)):
            safe, identity, tip = snapshots[str(entry['path'])]
            if not safe:
                continue
            try:
                with self.repo_lock(entry, blocking=False):
                    self.force_remove_entry(project, entry, identity, tip, warn)
            except FORCE_FAILURES as error:
                warn(f"Leftover {entry['path']} / {entry['branch']}: {error}")
        self.force_reset_coordinator(project, coordinator, current_branch, coordinator_tip, warn)
        project['state'] = 'archived'
        project['force_cleanup'] = {'original': original, 'warnings': warnings}
        try:
            self.archive_project(project)
        except FORCE_FAILURES as error:
            warn(f"Registry archive failed: {error}; attempting to retain the record separately and release the slot.")
            with self.global_lock() as directory:
                active = directory / 'v2' / 'projects' / f"{project['slot']}.json"
                if active.exists():
                    if json.loads(active.read_text()).get('uuid') != project['uuid']:
                        raise ProjectError('Active slot identity changed; refusing to release it.')
                try:
                    fallback = directory / 'force-cleanup' / f"{project['uuid']}.json"
                    fallback.parent.mkdir(parents=True, exist_ok=True)
                    write_json(fallback, project)
                    print(f"Preserved project record: {fallback}")
                except FORCE_FAILURES as preservation_error:
                    warn(f"Could not preserve the registry record: {preservation_error}")
                active.unlink(missing_ok=True)
                self.state['projects'].pop(project['slot'], None)
        print(f"Slot {project['slot']} is ready for another project.")

    def force_artifact_directory(self, project: dict[str, Any]) -> Path:
        directory = self.validate_retro_directory(project)
        if any(inside(directory.resolve(), Path(entry['path']).resolve())
               for record in self.state['projects'].values() for entry in project_entries(record)):
            raise ProjectError('Artifact directory is inside a managed code worktree; retain it without archival.')
        info = project.get('blackboard')
        if info:
            board_directory = Path(info['directory'])
            if board_directory.is_symlink() or board_directory.resolve() != directory.resolve():
                raise ProjectError('Live board directory differs from the owned artifact directory.')
        return directory

    def force_coordinator(self, project: dict[str, Any]) -> tuple[Path, str, str]:
        coordinator = self.slots / project['slot']
        if coordinator.is_symlink() or common_dir(coordinator) != common_dir(self.repo):
            raise ProjectError(f"Cannot safely reuse coordinator: identity changed at {coordinator}")
        if coordinator.resolve() == self.repo.resolve() or not (coordinator / '.git').is_file() or (coordinator / '.git').is_symlink():
            raise ProjectError(f"Coordinator is not a linked managed worktree: {coordinator}")
        pointer = (coordinator / '.git').read_text().strip()
        if not pointer.startswith('gitdir: '):
            raise ProjectError(f"Invalid coordinator worktree pointer: {coordinator}")
        metadata = Path(pointer.removeprefix('gitdir: '))
        if not metadata.is_absolute():
            metadata = coordinator / metadata
        if metadata.parent.resolve() != common_dir(self.repo) / 'worktrees' or metadata.is_symlink():
            raise ProjectError(f"Foreign coordinator worktree pointer: {coordinator}")
        if Path((metadata / 'gitdir').read_text().strip()).resolve() != (coordinator / '.git').resolve():
            raise ProjectError(f"Coordinator worktree backlink changed: {coordinator}")
        if Path(git(coordinator, 'rev-parse', '--show-toplevel')).resolve() != coordinator.resolve():
            raise ProjectError(f"Coordinator is not an exact worktree root: {coordinator}")
        current = branch(coordinator)
        pending = project.get('pending_rename') or {}
        if current not in (project['branch'], project['slot'], pending.get('branch'), pending.get('old_branch')):
            raise ProjectError(f"Cannot safely reuse coordinator on unexpected branch {current}: {coordinator}")
        git(coordinator, 'rev-parse', '--verify', f"refs/heads/{project['slot']}")
        if git(coordinator, 'ls-tree', '--name-only', f"refs/heads/{project['slot']}", '--', 'project.json', 'repos'):
            raise ProjectError('Idle coordinator branch contains project metadata or repos; slot cannot be reused safely.')
        self.force_coordinator_contents(project, coordinator)
        return coordinator, current, git(coordinator, 'rev-parse', 'HEAD')

    def force_protected_directories(self, project: dict[str, Any]) -> list[Path]:
        protected = [self.artifacts]
        for record in (*self.state['projects'].values(), *self.archived_projects()):
            protected.append(self.artifact_directory(record))
            if record.get('blackboard'):
                protected.append(Path(record['blackboard']['directory']))
            if record['uuid'] != project['uuid'] and record.get('state') != 'archived':
                protected.extend(Path(entry['path']) for entry in project_entries(record))
        return protected

    def force_coordinator_contents(self, project: dict[str, Any], coordinator: Path) -> None:
        def unreadable(error: OSError) -> None:
            raise ProjectError(f"Cannot inspect coordinator contents for safe reuse: {error}")

        for path in self.force_protected_directories(project):
            if path.exists() and inside(path.resolve(), coordinator.resolve()):
                raise ProjectError(f"Cannot safely reuse coordinator containing a protected artifact or worktree directory: {path}")
        for directory, dirs, files in os.walk(coordinator, followlinks=False, onerror=unreadable):
            if Path(directory) != coordinator and nested_repository(Path(directory), dirs, files):
                raise ProjectError(f"Cannot safely reuse coordinator containing a foreign repository: {directory}")
            dirs[:] = [name for name in dirs if name != '.git' and not (Path(directory) / name).is_symlink()]

    def force_worktree_identity(self, project: dict[str, Any], entry: dict[str, Any]) -> None:
        path = Path(entry['path'])
        expected = self.worktrees / entry['id'] / branch_suffix(entry['branch'], entry['branch_prefix'])
        if path != expected or path.is_symlink() or not inside(path.resolve(), self.worktrees.resolve()):
            raise ProjectError(f"Unsafe worktree path: {path}")
        if path.resolve() != path.absolute() or path.resolve() == Path(entry['source']).resolve():
            raise ProjectError(f"Worktree path traverses a symlink or names its source: {path}")
        claims = {('path', str(path)), ('branch', entry['common_dir'], entry['branch'])}
        for other in self.state['projects'].values():
            if other['uuid'] != project['uuid'] and claims & project_claims(other):
                raise ProjectError(f"Resource belongs to another project: {other['name']}")
        for protected in self.force_protected_directories(project):
            if protected.exists() and inside(protected.resolve(), path.resolve()):
                raise ProjectError(f"Worktree contains a protected artifact or other project worktree directory: {protected}")
        if not path.exists():
            return
        pointer = path / '.git'
        if pointer.is_symlink() or not pointer.is_file():
            raise ProjectError('Missing worktree ownership pointer')

        def unreadable(error: OSError) -> None:
            raise ProjectError(f"Cannot inspect worktree contents safely: {error}")

        for directory, dirs, files in os.walk(path, followlinks=False, onerror=unreadable):
            if Path(directory) != path and nested_repository(Path(directory), dirs, files):
                raise ProjectError(f"Nested repository: {directory}")
            dirs[:] = [name for name in dirs if name != '.git' and not (Path(directory) / name).is_symlink()]
        try:
            self.validate_clean_repo(entry, allow_dirty=True)
            return
        except ProjectError:
            if git(path, 'rev-parse', '--git-common-dir', check=False):
                raise
        text = pointer.read_text().strip()
        if not text.startswith('gitdir: '):
            raise ProjectError('Invalid worktree ownership pointer')
        metadata = Path(text.removeprefix('gitdir: '))
        if not metadata.is_absolute():
            metadata = path / metadata
        expected_parent = Path(entry['common_dir']) / 'worktrees'
        if metadata.parent != expected_parent or metadata.is_symlink() or metadata.resolve() != metadata.absolute():
            raise ProjectError('Foreign worktree ownership pointer')
        backlink = metadata / 'gitdir'
        if backlink.exists() and Path(backlink.read_text().strip()) != pointer:
            raise ProjectError('Worktree ownership backlink changed')

    def force_remove_entry(self, project: dict[str, Any], entry: dict[str, Any],
                           identity: tuple[int, int] | None, tip: str, warn: Callable[[str], None]) -> None:
        path, source = Path(entry['path']), Path(entry['source'])
        self.force_worktree_identity(project, entry)
        if path.exists():
            info = path.stat()
            if identity != (info.st_dev, info.st_ino):
                raise ProjectError('Worktree changed during confirmation; retained')
        elif identity is not None:
            raise ProjectError('Worktree disappeared during confirmation; branch retained')
        source_valid = False
        try:
            source_valid = common_dir(source) == Path(entry['common_dir'])
        except ProjectError:
            pass
        if not source_valid:
            if path.exists():
                shutil.rmtree(path)
                warn(f"Removed owned directory {path} without Git metadata; its missing source prevented Git-tip preservation.")
            return
        current = git(source, 'rev-parse', '--verify', f"refs/heads/{entry['branch']}", check=False)
        if current != tip:
            raise ProjectError('Branch changed during confirmation; retained')
        records = git(source, 'worktree', 'list', '--porcelain').split('\n\n')
        if any(f"branch refs/heads/{entry['branch']}" in record.splitlines()
               and f"worktree {path}" not in record.splitlines() for record in records):
            raise ProjectError('Branch is checked out in an unregistered worktree; retained')
        if tip:
            ref = f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}"
            git(source, 'update-ref', ref, tip, '')
            entry['backup_ref'] = ref
            print(f"Preserved {entry['branch']}: {ref} ({tip})")
        if path.exists():
            git(source, 'worktree', 'remove', '--force', str(path))
        if tip:
            git(source, 'update-ref', '-d', f"refs/heads/{entry['branch']}", tip)
        entry['step'] = 'removed'

    def force_reset_coordinator(self, project: dict[str, Any], coordinator: Path, current: str,
                                tip: str, warn: Callable[[str], None]) -> None:
        if self.force_coordinator(project) != (coordinator, current, tip):
            raise ProjectError('Coordinator changed while cleaning worktrees; cannot safely reset it.')
        # Include ignored files and unexpected directories without traversing symlink targets.
        destination = self.state_dir / 'force-cleanup' / f"{project['uuid']}-{uuid.uuid4().hex}"
        project['force_coordinator_recovery'] = str(destination)
        try:
            destination.mkdir(parents=True, mode=0o700)
            for child in coordinator.iterdir():
                if child.name == '.git':
                    continue
                target = destination / child.name
                if child.is_symlink():
                    target.symlink_to(child.readlink())
                elif child.is_dir():
                    shutil.copytree(child, target, symlinks=True)
                else:
                    shutil.copy2(child, target, follow_symlinks=False)
            print(f"Preserved coordinator contents: {destination}")
        except FORCE_FAILURES as error:
            warn(f"Coordinator recovery failed at {destination}: {error}. Any partial copy remains there; unpreserved contents will be discarded.")
        if self.force_coordinator(project) != (coordinator, current, tip):
            raise ProjectError('Coordinator changed while preserving its contents; cannot safely reset it.')
        preserved_tip = False
        if current != project['slot']:
            ref = f"refs/project-archive/{project['name']}/{uuid.uuid4().hex}"
            try:
                git(self.repo, 'update-ref', ref, tip, '')
                project['archive_ref'] = ref
                project['coordinator_sha'] = tip
                preserved_tip = True
            except FORCE_FAILURES as error:
                warn(f"Cannot archive coordinator tip {tip}: {error}; retaining project branch {current}.")
        for child in coordinator.iterdir():
            if child.name == '.git':
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        git(coordinator, 'reset', '--hard', 'HEAD')
        if current != project['slot']:
            git(coordinator, 'switch', project['slot'])
            if preserved_tip:
                try:
                    git(self.repo, 'update-ref', '-d', f"refs/heads/{current}", tip)
                except FORCE_FAILURES as error:
                    warn(f"Retained coordinator branch {current}: {error}")
        self.check_coordinator(project, idle=True)
        if (coordinator / 'project.json').exists() or (coordinator / 'project.json').is_symlink():
            raise ProjectError('Coordinator project.json remains; slot cannot be reused.')
        if (coordinator / 'repos').exists() or (coordinator / 'repos').is_symlink():
            raise ProjectError('Coordinator repos entry remains; slot cannot be reused.')
