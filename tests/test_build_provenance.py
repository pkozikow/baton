import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
import configparser
import zipfile
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('hatchling'), 'requires the build backend')
class BuildProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'baton'
        self.package.mkdir()
        self.stamp = {'revision': 'a' * 40, 'dirty': False, 'branch': 'refs/heads/main'}
        (self.package / '_build_info.json').write_text(json.dumps(self.stamp))

    def build_info(self) -> dict:
        from hatch_build import CustomBuildHook
        hook = CustomBuildHook(str(self.root), {}, None, None, str(self.root), 'wheel')
        data = {}
        hook.initialize('0.1.0', data)
        try:
            path, = data['force_include']
            return json.loads(Path(path).read_text())
        finally:
            hook.finalize('0.1.0', data, '')

    def test_wheel_contains_runtime_and_working_entrypoints(self) -> None:
        from hatchling.builders.wheel import WheelBuilder
        source = Path(__file__).resolve().parents[1]
        wheel = Path(next(WheelBuilder(str(source)).build(directory=str(self.root))))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assertIn('baton/cli.py', names)
            self.assertIn('baton/skills/baton-plan/SKILL.md', names)
            reference = 'baton-plan/references/validation.md'
            self.assertEqual(archive.read(f'baton/skills/{reference}'),
                             (source / 'agent-skills' / reference).read_bytes())
            for name in ('performance', 'complexity', 'security'):
                resource = f'baton/review_focuses/{name}.md'
                self.assertEqual(archive.read(resource), (source / resource).read_bytes())
            workflow = source / 'agent-skills/baton-workflow'
            for resource in workflow.rglob('*'):
                if resource.is_file():
                    relative = resource.relative_to(workflow).as_posix()
                    packaged = f'baton/skills/baton-workflow/{relative}'
                    self.assertEqual(archive.read(packaged), resource.read_bytes())
            self.assertIn('baton/_build_info.json', names)
            self.assertIn('blackboard.py', names)
            entrypoints = configparser.ConfigParser()
            entrypoints.read_string(archive.read(next(
                name for name in names if name.endswith('.dist-info/entry_points.txt'))).decode())
            archive.extractall(self.root / 'installed')
        self.assertEqual(dict(entrypoints['console_scripts']), {
            'baton': 'baton.cli:main',
            'baton-retro-cron': 'baton.retro_cron:main',
        })
        code = (
            "import importlib.util, sys; sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[1:]; "
            "import baton.cli, blackboard; "
            "from baton.skills_install import bundled_skills, install_skills; "
            "from baton.review_loop import review_focus_catalog, effective_review_loop; "
            "catalog = review_focus_catalog(); "
            "assert all(catalog[name].strip() for name in effective_review_loop({})['focused_reviews']); "
            "from pathlib import Path; "
            "resources = bundled_skills() / 'baton-workflow'; "
            "assert (resources / 'references/startup.md').is_file(); "
            "install_skills([Path('skill-target')]); "
            "assert (Path('skill-target') / 'baton-workflow/references/coordination.md').is_file(); "
            "reference = Path('skill-target/baton-plan/references/validation.md'); "
            "assert reference.read_bytes() == (bundled_skills() / 'baton-plan/references/validation.md').read_bytes(); "
            "assert (reference.parent / '../../baton-workflow/references/coordination.md').resolve().is_file(); "
            "sys.exit(baton.cli.main())"
        )
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code,
                                 str(self.root / 'installed'), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('blackboard', result.stdout)

    def test_sdist_contains_focus_guidance(self) -> None:
        from hatchling.builders.sdist import SdistBuilder
        source = Path(__file__).resolve().parents[1]
        distribution = Path(next(SdistBuilder(str(source)).build(directory=str(self.root))))
        with tarfile.open(distribution) as archive:
            for name in ('performance', 'complexity', 'security'):
                suffix = f'/baton/review_focuses/{name}.md'
                member = next(member for member in archive.getmembers() if member.name.endswith(suffix))
                content = archive.extractfile(member)
                self.assertIsNotNone(content)
                self.assertEqual(content.read(), (source / 'baton/review_focuses' / f'{name}.md').read_bytes())

    def test_sdist_stamp_survives_when_git_is_unavailable(self) -> None:
        with patch.dict(os.environ, {'PATH': str(self.root / 'missing-bin')}):
            self.assertEqual(self.build_info(), self.stamp)

    def test_missing_or_invalid_stamp_is_unverifiable_without_git(self) -> None:
        stamp = self.package / '_build_info.json'
        with patch.dict(os.environ, {'PATH': str(self.root / 'missing-bin')}):
            stamp.write_text('{')
            self.assertEqual(self.build_info(), {})
            stamp.unlink()
            self.assertEqual(self.build_info(), {})

    def test_real_checkout_overrides_a_preserved_stamp(self) -> None:
        binary = shutil.which('git')
        self.assertIsNotNone(binary)
        def git(*args: str) -> str:
            return subprocess.run([binary, '-C', str(self.root), *args], text=True,
                                  capture_output=True, check=True).stdout.strip()
        git('init', '-b', 'main')
        git('add', '.')
        git('-c', 'user.name=Build test', '-c', 'user.email=build@example.test', 'commit', '-m', 'Initial')
        self.assertEqual(self.build_info(), {'revision': git('rev-parse', 'HEAD'), 'dirty': False, 'branch': 'refs/heads/main'})
        git('switch', '-c', 'feature')
        self.assertEqual(self.build_info()['branch'], 'refs/heads/feature')
        git('switch', '--detach', 'main')
        self.assertEqual(self.build_info()['branch'], '')
        (self.package / 'local.py').write_text('uncommitted\n')
        self.assertTrue(self.build_info()['dirty'])


if __name__ == '__main__':
    unittest.main()
