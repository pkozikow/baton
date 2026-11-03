import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from baton.doctor_freshness import compare_revision, freshness_check


class DoctorFreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'framework'
        self.source.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Doctor test')
        self.git('config', 'user.email', 'doctor@example.test')
        (self.source / 'baton').mkdir()
        (self.source / 'baton' / 'cli.py').write_text('')
        (self.source / 'pyproject.toml').write_text('[project]\nname = "baton"\n')
        self.first = self.commit('Initial framework')

    def git(self, *arguments: str) -> str:
        return subprocess.run(['git', '-C', str(self.source), *arguments],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message: str) -> str:
        self.git('add', '.')
        self.git('commit', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def check(self, revision: str | None = None, dirty: bool = False, ref: str | None = None) -> dict:
        return compare_revision(self.source, {'revision': revision or self.first, 'dirty': dirty}, ref)

    def test_clean_build_matches_main_without_changing_worktree(self) -> None:
        before = self.git('status', '--porcelain')
        result = self.check()
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(result['details']['ref'], 'refs/heads/main')
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertIn('without fetching', result['message'])

    def test_build_becomes_stale_when_main_advances(self) -> None:
        (self.source / 'next.py').write_text('change\n')
        self.commit('Advance main')
        result = self.check()
        self.assertEqual(result['status'], 'fail')
        self.assertIn('out of date', result['message'])

    def test_master_fallback(self) -> None:
        self.git('branch', '-m', 'master')
        self.assertEqual(self.check()['details']['ref'], 'refs/heads/master')
        self.assertEqual(self.check()['status'], 'pass')

    def test_explicit_ref_is_used_instead_of_main(self) -> None:
        self.git('branch', 'release')
        (self.source / 'next.py').write_text('change\n')
        self.commit('Advance main')
        self.assertEqual(self.check(ref='refs/heads/release')['status'], 'pass')
        self.assertEqual(self.check(ref='absent')['status'], 'fail')

    def test_remote_head_fallback_uses_only_existing_local_refs(self) -> None:
        self.git('update-ref', 'refs/remotes/origin/trunk', self.first)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        self.git('branch', '-m', 'topic')
        result = self.check()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['details']['ref'], 'refs/remotes/origin/trunk')

    def test_build_ahead_of_main_is_a_mismatch(self) -> None:
        self.git('checkout', '-b', 'feature')
        (self.source / 'next.py').write_text('change\n')
        tip = self.commit('Feature change')
        result = self.check(tip)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('differs from', result['message'])

    def test_build_dirty_or_unverifiable_fails(self) -> None:
        self.assertIn('dirty checkout', self.check(dirty=True)['message'])
        for provenance in ({}, {'revision': self.first}, {'revision': self.first, 'dirty': 'false'}):
            result = compare_revision(self.source, provenance)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('provenance', result['message'])
        result = self.check('a' * 40)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('unavailable', result['message'])

    def test_missing_source_git_and_framework_fail_with_remediation(self) -> None:
        result = compare_revision(self.source / 'absent', {})
        self.assertIn('--source PATH', result['message'])
        plain = Path(self.temporary.name) / 'plain'
        plain.mkdir()
        self.assertIn('not a Git checkout root', compare_revision(plain, {})['message'])
        self.assertIn('not a Git checkout root', compare_revision(self.source / 'baton', {})['message'])
        (self.source / 'baton' / 'cli.py').unlink()
        self.assertIn('not a baton', self.check()['message'])

    def test_no_mainline_is_an_actionable_failure(self) -> None:
        self.git('branch', '-m', 'feature')
        result = self.check()
        self.assertEqual(result['status'], 'fail')
        self.assertIn('--ref REF', result['message'])

    def installed_check(self, *, stamp: bool = True, direct_url: dict | None = None,
                        explicit_source: bool = False) -> dict:
        installed = Path(self.temporary.name) / 'site-packages'
        package = installed / 'baton'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        runtime = Path(__file__).resolve().parents[1] / 'baton'
        for name in ('doctor_freshness.py', 'gitutil.py', 'errors.py'):
            shutil.copyfile(runtime / name, package / name)
        if stamp:
            (package / '_build_info.json').write_text(json.dumps({'revision': self.first, 'dirty': False}))
        distribution = installed / 'baton-0.1.0.dist-info'
        distribution.mkdir()
        (distribution / 'METADATA').write_text('Name: baton\nVersion: 0.1.0\n')
        (distribution / 'direct_url.json').write_text(json.dumps(
            direct_url if direct_url is not None else {'url': self.source.as_uri(), 'dir_info': {}}))
        script = ('import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                  'from baton.doctor_freshness import freshness_check; '
                  'print(json.dumps(freshness_check(Path(sys.argv[2]) if len(sys.argv)>2 else None)))')
        arguments = [sys.executable, '-I', '-c', script, str(installed)]
        if explicit_source:
            arguments.append(str(self.source))
        result = subprocess.run(arguments, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_installed_metadata_discovers_original_local_checkout(self) -> None:
        result = self.installed_check()
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(result['details']['source'], str(self.source.resolve()))

    def test_old_install_without_stamp_requires_reinstallation(self) -> None:
        result = self.installed_check(stamp=False)
        self.assertEqual(result['status'], 'fail', result)
        self.assertIn('no verifiable Git provenance', result['message'])

    def test_remote_install_without_local_source_requires_explicit_source(self) -> None:
        result = self.installed_check(direct_url={'url': 'https://example.test/framework.whl'})
        self.assertEqual(result['status'], 'fail', result)
        self.assertIn('--source PATH', result['message'])

    def test_explicit_source_supports_installs_without_local_direct_url(self) -> None:
        result = self.installed_check(direct_url={'url': 'https://example.test/framework.whl'}, explicit_source=True)
        self.assertEqual(result['status'], 'pass', result)

    def test_current_source_execution_is_reported_without_tracebacks(self) -> None:
        result = freshness_check()
        self.assertIn(result['status'], ('pass', 'fail'))
        self.assertEqual(result['name'], 'version')
        json.dumps(result)


if __name__ == '__main__':
    unittest.main()
