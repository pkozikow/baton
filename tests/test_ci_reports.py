import ast
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_ci_reports', ROOT / 'check_ci_reports.py')
assert SPEC is not None and SPEC.loader is not None
REPORTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTS)


class CIReportsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.core = self.report(REPORTS.REQUIRED_CORE_TESTS)
        self.skills = self.report(['example.Cases.test_skill'])

    def report(self, ids: list[str] | frozenset[str]) -> dict:
        return {'successful': True, 'selected_tests': len(ids), 'tests_run': len(ids),
                'tests': [{'id': name, 'status': 'passed'} for name in sorted(ids)]}

    def write_reports(self) -> None:
        for name, report in (('core', self.core), ('skills', self.skills)):
            (self.directory / f'{name}.json').write_text(json.dumps(report))

    def test_required_ids_match_current_modules(self) -> None:
        expected = set()
        for module in ('test_build_provenance', 'test_native_retirement_compat'):
            tree = ast.parse((ROOT / 'tests' / f'{module}.py').read_text())
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef):
                    for method in cls.body:
                        if isinstance(method, ast.FunctionDef) and method.name.startswith('test_'):
                            expected.add(f'{module}.{cls.name}.{method.name}')
        self.assertEqual(REPORTS.REQUIRED_CORE_TESTS, expected)

    def test_cli_accepts_passed_reports_and_unrelated_skips(self) -> None:
        self.skills = self.report(['example.Cases.test_skill', 'example.Cases.test_optional'])
        self.skills['tests'][0]['status'] = 'skipped'
        self.write_reports()
        result = subprocess.run([sys.executable, str(ROOT / 'check_ci_reports.py'), str(self.directory)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_required_case_must_pass_once(self) -> None:
        original = copy.deepcopy(self.core)
        for status in ('skipped', 'failed', 'error', 'expected_failure', 'unexpected_success', 'missing', 'duplicate'):
            with self.subTest(status=status):
                self.core = copy.deepcopy(original)
                required_id = self.core['tests'][0]['id']
                if status == 'missing':
                    self.core['tests'].pop(0)
                elif status == 'duplicate':
                    self.core['tests'].append(self.core['tests'][0])
                else:
                    self.core['tests'][0]['status'] = status
                self.core['selected_tests'] = self.core['tests_run'] = len(self.core['tests'])
                self.write_reports()
                errors = REPORTS.check_reports(self.directory)
                self.assertTrue(any(f'required test {required_id} must pass exactly once' in error
                                    for error in errors), errors)
                self.assertFalse(any('counts differ' in error for error in errors), errors)

    def test_both_reports_must_exist_and_be_valid(self) -> None:
        for name in ('core', 'skills'):
            for content in (None, '{', '{}'):
                with self.subTest(name=name, content=content):
                    self.write_reports()
                    path = self.directory / f'{name}.json'
                    if content is None:
                        path.unlink()
                    else:
                        path.write_text(content)
                    self.assertTrue(REPORTS.check_reports(self.directory))

    def test_all_skipped_reports_fail(self) -> None:
        self.skills['tests'][0]['status'] = 'skipped'
        self.write_reports()
        errors = REPORTS.check_reports(self.directory)
        self.assertTrue(any('no passing tests' in error for error in errors), errors)
        self.assertFalse(any('counts differ' in error for error in errors), errors)

    def test_unsuccessful_empty_and_partial_runs_fail(self) -> None:
        original = copy.deepcopy(self.skills)
        cases = (
            ({'successful': False}, 'no successful test run'),
            ({'tests_run': 0, 'selected_tests': 0, 'tests': []}, 'no successful test run'),
            ({'selected_tests': 2}, 'selected/executed test counts differ'),
        )
        for update, expected in cases:
            with self.subTest(update=update):
                self.skills = {**original, **update}
                self.write_reports()
                errors = REPORTS.check_reports(self.directory)
                self.assertTrue(any(expected in error for error in errors), errors)
