import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import unittest

import test_project_manager as fixtures
from test_project_manager import SCRIPT, read_state, state_file


class ForceCleanupTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create

    def force(self, reply: bytes = b'y\n', ok: bool = True,
              command: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(command or ['bash', str(SCRIPT), 'cleanup', 'p3', '--force'],
                                       cwd=self.root, env=self.env, stdin=slave,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.write(master, reply)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode == 0, ok, f'{stdout}\n{stderr}')
            return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def edit_record(self, change: dict) -> None:
        path = state_file(self.projects)
        project = json.loads(path.read_text())
        project.update(change)
        path.write_text(json.dumps(project))

    def test_clean_project_still_confirms_and_can_reuse_slot(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        before = state_file(self.projects).read_bytes()
        failed = self.cli('cleanup', 'p3', '--force', ok=False)
        self.assertIn('Interactive confirmation is required', failed.stderr)
        self.force(b'n\n', ok=False)
        self.assertEqual(state_file(self.projects).read_bytes(), before)
        result = self.force()
        self.assertIn('Continue force cleanup?', result.stderr)
        self.assertFalse(self.checkout('alpha').exists())
        self.assertEqual(read_state(self.projects)['projects'], {})
        self.assertEqual(self.git(self.slots / 'p3', 'branch', '--show-current'), 'p3')
        self.create('alpha', name='next')

    def test_missing_source_and_unsupported_board_do_not_block_release(self) -> None:
        source = self.make_repo('alpha')
        self.create('alpha')
        record = json.loads(state_file(self.projects).read_text())
        board = Path(record['artifacts_path']) / 'blackboard.sqlite'
        before = board.read_bytes()
        record['blackboard']['backend'] = 'unsupported'
        state_file(self.projects).write_text(json.dumps(record))
        shutil.rmtree(source)
        dry = self.cli('cleanup', 'p3', '--force', '--dry-run')
        self.assertIn('Unsupported blackboard backend', dry.stderr)
        self.assertIn('Cannot inspect source repository', dry.stderr)
        self.assertTrue(self.checkout('alpha').exists())
        result = self.force()
        self.assertIn('Coordination archive failed', result.stderr)
        self.assertIn('missing source prevented Git-tip preservation', result.stderr)
        self.assertFalse(self.checkout('alpha').exists())
        self.assertEqual(board.read_bytes(), before)
        self.assertFalse(state_file(self.projects).exists())

    def test_dry_run_aggregates_repositories_and_changes_nothing(self) -> None:
        self.make_repo('alpha')
        self.make_repo('beta')
        self.create('alpha', 'beta')
        for name in ('alpha', 'beta'):
            path = self.checkout(name)
            (path / 'new.txt').write_text('new\n')
            self.git(path, 'add', 'new.txt')
            self.git(path, 'commit', '-m', 'Unmerged change')
            (path / 'dirty.txt').write_text('discard\n')
        before = state_file(self.projects).read_bytes()
        dry = self.cli('cleanup', 'p3', '--force', '--dry-run')
        for name in ('alpha', 'beta'):
            self.assertIn(f'Unmerged commits in {name}', dry.stderr)
            self.assertIn(f'Dirty or untracked files in {self.checkout(name)}', dry.stderr)
        self.assertEqual(state_file(self.projects).read_bytes(), before)
        self.assertNotIn('Continue force cleanup?', dry.stderr)
        for name in ('alpha', 'beta'):
            self.assertEqual(self.git(self.repos / name, 'for-each-ref', 'refs/project-archive'), '')

    def test_unmerged_tip_is_preserved_and_dirty_files_are_removed(self) -> None:
        source = self.make_repo('alpha')
        self.create('alpha')
        path = self.checkout('alpha')
        (path / 'new.txt').write_text('new\n')
        self.git(path, 'add', 'new.txt')
        self.git(path, 'commit', '-m', 'Unmerged change')
        tip = self.git(path, 'rev-parse', 'HEAD')
        (path / 'dirty').write_text('discard\n')
        self.force()
        self.assertIn(tip, self.git(source, 'for-each-ref', '--format=%(objectname)', 'refs/project-archive'))
        self.git(source, 'show-ref', '--verify', 'refs/heads/baton/example', ok=False)
        self.assertFalse(path.exists())

    def test_unsafe_path_is_retained_and_other_repositories_continue(self) -> None:
        source = self.make_repo('alpha')
        self.make_repo('beta')
        self.create('alpha', 'beta')
        record = json.loads(state_file(self.projects).read_text())
        record['repos'][0]['path'] = str(source)
        state_file(self.projects).write_text(json.dumps(record))
        result = self.force()
        self.assertIn('Retaining unsafe or ambiguous worktree', result.stderr)
        self.assertTrue((source / '.git').is_dir())
        self.assertTrue(self.checkout('alpha').exists())
        self.assertFalse(self.checkout('beta').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_unknown_coordinator_branch_is_fatal_without_mutation(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        self.git(self.slots / 'p3', 'switch', '-c', 'foreign')
        before = state_file(self.projects).read_bytes()
        result = self.cli('cleanup', 'p3', '--force', '--dry-run', ok=False)
        self.assertIn('unexpected branch foreign', result.stderr)
        self.assertEqual(state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout('alpha').exists())

    def test_unmanaged_coordinator_contents_and_symlink_targets_are_preserved(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        coordinator = self.slots / 'p3'
        external = self.root / 'external'
        external.mkdir()
        (external / 'keep').write_text('retain\n')
        (coordinator / 'repos' / 'foreign').symlink_to(external)
        (coordinator / 'repos' / 'notes').mkdir()
        (coordinator / 'repos' / 'notes' / 'important').write_text('save\n')
        self.force()
        archive = read_state(self.projects)['archive'][0]
        recovery = Path(archive['force_coordinator_recovery'])
        self.assertTrue((recovery / 'repos' / 'foreign').is_symlink())
        self.assertEqual((recovery / 'repos' / 'notes' / 'important').read_text(), 'save\n')
        self.assertEqual((external / 'keep').read_text(), 'retain\n')
        self.assertFalse((coordinator / 'repos').exists())

    def test_pending_abandon_is_released_without_closing_prs(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        self.edit_record({'state': 'abandoning', 'pending_abandon': {'reason': 'interrupted'}})
        result = self.force()
        self.assertIn('PR closures are not completed', result.stderr)
        archive = read_state(self.projects)['archive'][0]
        self.assertIn('pending_abandon', archive['force_cleanup']['original'])
        self.assertFalse(state_file(self.projects).exists())

    def test_locked_worktree_failure_does_not_prevent_other_cleanup_or_release(self) -> None:
        source = self.make_repo('alpha')
        self.make_repo('beta')
        self.create('alpha', 'beta')
        self.git(source, 'worktree', 'lock', str(self.checkout('alpha')))
        result = self.force()
        self.assertIn('Leftover', result.stderr)
        self.assertTrue(self.checkout('alpha').exists())
        self.assertFalse(self.checkout('beta').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_registry_archive_failure_falls_back_and_releases_slot(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        script = self.root / 'fail-archive.py'
        script.write_text(
            f'import sys\nsys.path.insert(0, {str(SCRIPT.parent.parent)!r})\n'
            'from baton.manager import Manager\n'
            'from baton.errors import ProjectError\n'
            'class FailingArchive(Manager):\n'
            '    def archive_project(self, project):\n'
            '        raise ProjectError("injected archival failure")\n'
            'manager = FailingArchive()\n'
            'manager.load()\n'
            'with manager.command_lock("p3"):\n'
            '    manager.force_cleanup("p3", False)\n'
        )
        result = self.force(command=['python3', str(script)])
        self.assertIn('Registry archive failed', result.stderr)
        self.assertFalse(state_file(self.projects).exists())
        fallbacks = list((self.projects / '.git' / 'baton' / 'force-cleanup').glob('*.json'))
        self.assertEqual(len(fallbacks), 1)
        record = json.loads(fallbacks[0].read_text())
        self.assertIn('force_cleanup', record)
        self.assertIn('injected archival failure', '\n'.join(record['force_cleanup']['warnings']))
        self.create('alpha', name='next')

    def test_missing_worktree_pointer_is_retained_and_slot_released(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        (self.checkout('alpha') / '.git').unlink()
        result = self.force()
        self.assertIn('Missing worktree ownership pointer', result.stderr)
        self.assertTrue(self.checkout('alpha').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_idle_branch_with_project_manifest_cannot_claim_slot_is_reusable(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        tip = self.git(self.slots / 'p3', 'rev-parse', 'HEAD')
        self.git(self.projects, 'update-ref', 'refs/heads/p3', tip)
        before = state_file(self.projects).read_bytes()
        result = self.cli('cleanup', 'p3', '--force', '--dry-run', ok=False)
        self.assertIn('Idle coordinator branch contains project metadata', result.stderr)
        self.assertEqual(state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout('alpha').exists())

    def test_failed_tip_preservation_retains_branch_and_slot_can_be_reused(self) -> None:
        source = self.make_repo('alpha')
        self.make_repo('beta')
        self.create('alpha', 'beta')
        tip = self.git(self.checkout('alpha'), 'rev-parse', 'HEAD')
        self.git(source, 'update-ref', 'refs/project-archive', tip)
        result = self.force()
        self.assertIn('Leftover', result.stderr)
        self.assertTrue(self.checkout('alpha').exists())
        self.assertFalse(self.checkout('beta').exists())
        self.assertEqual(self.git(source, 'rev-parse', 'refs/heads/baton/example'), tip)
        self.create('alpha', name='next')

    def test_shared_artifact_directory_is_not_archived_or_modified(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        self.create('alpha', name='foreign', slot='p4')
        foreign = json.loads(state_file(self.projects, 'p4').read_text())
        directory = Path(foreign['artifacts_path'])
        before = {path.relative_to(directory): path.read_bytes()
                  for path in directory.rglob('*') if path.is_file()}
        self.edit_record({'artifacts_path': str(directory), 'blackboard': foreign['blackboard']})
        result = self.force()
        self.assertIn('Shared artifacts will be retained without archival', result.stderr)
        self.assertIn('shared by multiple project UUIDs', result.stderr)
        self.assertEqual(before, {path.relative_to(directory): path.read_bytes()
                                  for path in directory.rglob('*') if path.is_file()})
        self.assertTrue(state_file(self.projects, 'p4').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_corrupt_board_does_not_prevent_slot_reuse(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        record = json.loads(state_file(self.projects).read_text())
        board = Path(record['artifacts_path']) / 'blackboard.sqlite'
        board.write_bytes(b'corrupt sqlite database')
        result = self.force()
        self.assertIn('Coordination archive failed', result.stderr)
        self.assertEqual(board.read_bytes(), b'corrupt sqlite database')
        self.assertFalse(state_file(self.projects).exists())
        self.create('alpha', name='next')

    def test_corrupt_permanent_board_lock_is_warned_and_left_in_place(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        record = json.loads(state_file(self.projects).read_text())
        lock = Path(record['artifacts_path']) / 'blackboard-lock.md'
        lock.write_text('invalid journal\n')
        result = self.force()
        self.assertIn('Coordination archive failed', result.stderr)
        self.assertIn('corrupt journal record', result.stderr)
        self.assertEqual(lock.read_text(), 'invalid journal\n')
        self.assertFalse(state_file(self.projects).exists())

    def test_coordinator_backlink_mismatch_blocks_release(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        coordinator = self.slots / 'p3'
        metadata = Path(self.git(coordinator, 'rev-parse', '--absolute-git-dir'))
        (metadata / 'gitdir').write_text(str(self.root / 'foreign' / '.git') + '\n')
        before = state_file(self.projects).read_bytes()
        result = self.cli('cleanup', 'p3', '--force', '--dry-run', ok=False)
        self.assertIn('Coordinator worktree backlink changed', result.stderr)
        self.assertEqual(state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout('alpha').exists())

    def test_nested_primary_repository_is_never_removed(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        foreign = self.slots / 'p3' / 'repos' / 'foreign'
        self.git(self.root, 'init', '-b', 'main', str(foreign))
        (foreign / 'valuable').write_text('keep original\n')
        self.git(foreign, 'add', 'valuable')
        self.git(foreign, 'commit', '-m', 'Foreign data')
        tip = self.git(foreign, 'rev-parse', 'HEAD')
        result = self.force(ok=False)
        self.assertIn('containing a foreign repository', result.stderr)
        self.assertEqual(self.git(foreign, 'rev-parse', 'HEAD'), tip)
        self.assertEqual((foreign / 'valuable').read_text(), 'keep original\n')
        self.assertTrue(self.checkout('alpha').exists())
        self.assertTrue(state_file(self.projects).exists())

    def test_foreign_linked_worktree_is_never_removed(self) -> None:
        source = self.make_repo('alpha')
        self.create('alpha')
        foreign = self.slots / 'p3' / 'repos' / 'foreign'
        self.git(source, 'worktree', 'add', '-b', 'foreign', str(foreign), 'main')
        pointer = (foreign / '.git').read_bytes()
        result = self.force(ok=False)
        self.assertIn('containing a foreign repository', result.stderr)
        self.assertEqual((foreign / '.git').read_bytes(), pointer)
        self.assertEqual(self.git(foreign, 'branch', '--show-current'), 'foreign')
        self.assertTrue(self.checkout('alpha').exists())
        self.assertTrue(state_file(self.projects).exists())

    def test_live_artifacts_inside_coordinator_are_never_copied_or_removed(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        record = json.loads(state_file(self.projects).read_text())
        original = Path(record['artifacts_path'])
        nested = self.slots / 'p3' / 'repos' / 'board'
        shutil.move(str(original), nested)
        record['artifacts_path'] = str(nested)
        record['blackboard']['directory'] = str(nested)
        state_file(self.projects).write_text(json.dumps(record))
        before = {path.relative_to(nested): (path.stat().st_ino, path.read_bytes())
                  for path in nested.rglob('*') if path.is_file()}
        result = self.force(ok=False)
        self.assertIn('containing a protected artifact or worktree directory', result.stderr)
        self.assertEqual(before, {path.relative_to(nested): (path.stat().st_ino, path.read_bytes())
                                  for path in nested.rglob('*') if path.is_file()})
        self.assertTrue(self.checkout('alpha').exists())
        self.assertTrue(state_file(self.projects).exists())

    def test_live_artifacts_inside_code_worktree_retain_worktree_and_release_slot(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        record = json.loads(state_file(self.projects).read_text())
        original = Path(record['artifacts_path'])
        nested = self.checkout('alpha') / 'artifacts'
        shutil.move(str(original), nested)
        record['artifacts_path'] = str(nested)
        record['blackboard']['directory'] = str(nested)
        state_file(self.projects).write_text(json.dumps(record))
        before = {path.relative_to(nested): (path.stat().st_ino, path.read_bytes())
                  for path in nested.rglob('*') if path.is_file()}
        result = self.force()
        self.assertIn('Worktree contains a protected artifact', result.stderr)
        self.assertEqual(before, {path.relative_to(nested): (path.stat().st_ino, path.read_bytes())
                                  for path in nested.rglob('*') if path.is_file()})
        self.assertTrue(self.checkout('alpha').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_coordinator_backup_failure_does_not_block_reusable_slot(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        recovery = self.projects / '.git' / 'baton' / 'force-cleanup'
        recovery.write_text('blocking file\n')
        notes = self.slots / 'p3' / 'repos' / 'notes'
        notes.mkdir()
        (notes / 'scratch').write_text('explicitly discard if preservation fails\n')
        result = self.force()
        self.assertIn('Coordinator recovery failed', result.stderr)
        self.assertIn('unpreserved contents will be discarded', result.stderr)
        self.assertFalse((self.slots / 'p3' / 'repos').exists())
        self.assertFalse(state_file(self.projects).exists())
        self.assertEqual(recovery.read_text(), 'blocking file\n')
        self.create('alpha', name='next')

    def test_unreadable_coordinator_directory_blocks_unsafe_release(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        hidden = self.slots / 'p3' / 'repos' / 'unreadable'
        hidden.mkdir()
        (hidden / 'valuable').write_text('retain\n')
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        if os.access(hidden, os.R_OK | os.X_OK):
            self.skipTest('Process can read directories despite their permission mode')
        result = self.force(ok=False)
        self.assertIn('Cannot inspect coordinator contents for safe reuse', result.stderr)
        hidden.chmod(0o700)
        self.assertEqual((hidden / 'valuable').read_text(), 'retain\n')
        self.assertTrue(state_file(self.projects).exists())
        self.assertTrue(self.checkout('alpha').exists())

    def test_ignored_nested_primary_repository_retains_worktree_and_slot_is_reusable(self) -> None:
        source = self.make_repo('alpha')
        self.create('alpha')
        checkout = self.checkout('alpha')
        (checkout / '.gitignore').write_text('ignored-repo/\n')
        foreign = checkout / 'ignored-repo'
        self.git(self.root, 'init', '-b', 'main', str(foreign))
        (foreign / 'valuable').write_text('retain original primary\n')
        self.git(foreign, 'add', 'valuable')
        self.git(foreign, 'commit', '-m', 'Foreign primary data')
        tip = self.git(foreign, 'rev-parse', 'HEAD')
        project_tip = self.git(checkout, 'rev-parse', 'HEAD')
        result = self.force()
        self.assertIn(f'Nested repository: {foreign}', result.stderr)
        self.assertTrue(checkout.exists())
        self.assertEqual(self.git(foreign, 'rev-parse', 'HEAD'), tip)
        self.assertEqual((foreign / 'valuable').read_text(), 'retain original primary\n')
        self.assertEqual(self.git(source, 'rev-parse', 'refs/heads/baton/example'), project_tip)
        self.assertFalse(state_file(self.projects).exists())
        self.create('alpha', name='next')

    def test_busy_source_lock_is_retained_without_blocking_slot_release(self) -> None:
        source = self.make_repo('alpha')
        self.make_repo('beta')
        self.create('alpha', 'beta')
        record = json.loads(state_file(self.projects).read_text())
        alpha = next(entry for entry in record['repos'] if entry['id'] == 'alpha')
        key = hashlib.sha256(alpha['common_dir'].encode()).hexdigest()[:16]
        lock = self.projects / '.git' / 'baton' / 'locks' / f'repo-{key}.lock'
        tip = self.git(source, 'rev-parse', 'refs/heads/baton/example')
        with lock.open('r+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                result = self.force()
                self.assertIn('Repository alpha is busy with another project command', result.stderr)
                self.assertTrue(self.checkout('alpha').exists())
                self.assertEqual(self.git(source, 'rev-parse', 'refs/heads/baton/example'), tip)
                self.assertFalse(self.checkout('beta').exists())
                self.assertFalse(state_file(self.projects).exists())
                self.create('beta', name='next')
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def make_bare_repository(self, path: Path) -> str:
        source = self.make_repo('unique-history', remote=False)
        tip = self.git(source, 'rev-parse', 'HEAD')
        self.git(self.root, 'clone', '--bare', str(source), str(path))
        shutil.rmtree(source)
        return tip

    def test_ignored_nested_bare_repository_retains_worktree_and_releases_slot(self) -> None:
        source = self.make_repo('alpha')
        self.create('alpha')
        checkout = self.checkout('alpha')
        (checkout / '.gitignore').write_text('ignored-history/\n')
        foreign = checkout / 'ignored-history'
        tip = self.make_bare_repository(foreign)
        project_tip = self.git(source, 'rev-parse', 'refs/heads/baton/example')
        result = self.force()
        self.assertIn(f'Nested repository: {foreign}', result.stderr)
        self.assertEqual(self.git(foreign, 'rev-parse', 'HEAD'), tip)
        self.assertEqual(self.git(foreign, 'show', 'HEAD:README.md'), '# unique-history')
        self.assertEqual(self.git(source, 'rev-parse', 'refs/heads/baton/example'), project_tip)
        self.assertFalse(state_file(self.projects).exists())
        self.create('alpha', name='next')

    def test_nested_bare_repository_blocks_coordinator_reset_before_mutation(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        foreign = self.slots / 'p3' / 'repos' / 'bare-history'
        tip = self.make_bare_repository(foreign)
        record = state_file(self.projects).read_bytes()
        result = self.force(ok=False)
        self.assertIn(f'containing a foreign repository: {foreign}', result.stderr)
        self.assertEqual(self.git(foreign, 'rev-parse', 'HEAD'), tip)
        self.assertEqual(self.git(foreign, 'show', 'HEAD:README.md'), '# unique-history')
        self.assertEqual(state_file(self.projects).read_bytes(), record)
        self.assertTrue(self.checkout('alpha').exists())

    def test_bare_repository_symlinks_preserve_targets_without_blocking_cleanup(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        target = self.root / 'external-history'
        tip = self.make_bare_repository(target)
        (self.checkout('alpha') / 'history-link').symlink_to(target)
        (self.slots / 'p3' / 'repos' / 'history-link').symlink_to(target)
        self.force()
        self.assertEqual(self.git(target, 'rev-parse', 'HEAD'), tip)
        self.assertFalse(self.checkout('alpha').exists())
        self.assertFalse(state_file(self.projects).exists())

    def test_bare_shaped_plain_directories_do_not_block_cleanup(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        coordinator = self.slots / 'p3'
        for root in (self.checkout('alpha'), coordinator / 'repos'):
            directory = root / 'ordinary-data'
            directory.mkdir()
            (directory / 'HEAD').write_text('ordinary text\n')
            (directory / 'objects').mkdir()
            (directory / 'refs').mkdir()
        self.force()
        self.assertFalse(self.checkout('alpha').exists())
        archive = read_state(self.projects)['archive'][0]
        recovery = Path(archive['force_coordinator_recovery'])
        self.assertEqual((recovery / 'repos' / 'ordinary-data' / 'HEAD').read_text(), 'ordinary text\n')
        self.assertFalse(state_file(self.projects).exists())

    def test_empty_bare_repository_in_coordinator_is_protected(self) -> None:
        self.make_repo('alpha')
        self.create('alpha')
        foreign = self.slots / 'p3' / 'repos' / 'empty-history'
        self.git(self.root, 'init', '--bare', str(foreign))
        self.force(ok=False)
        self.assertEqual(self.git(foreign, 'rev-parse', '--is-bare-repository'), 'true')
        self.assertTrue(state_file(self.projects).exists())
