import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from typing import Any
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "run_tests.py"


class RunTestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.modules = self.root / "modules"
        self.modules.mkdir()
        self.runs = 0

    def module(self, name: str, code: str) -> None:
        (self.modules / f"{name}.py").write_text(textwrap.dedent(code))

    def run_suite(self, jobs: int, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path]:
        self.runs += 1
        events = self.root / f"events-{self.runs}"
        events.mkdir()
        report = self.root / f"report-{self.runs}.json"
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--start-directory", str(self.modules),
             "--jobs", str(jobs), "--json-report", str(report), *args],
            text=True, capture_output=True, timeout=60,
            env={**os.environ, "RUNNER_EVENTS": str(events)},
        )
        return result, json.loads(report.read_text()) if report.exists() else {}, events

    def test_serial_and_parallel_run_each_selected_case_once(self) -> None:
        for name in ("test_one", "test_two"):
            self.module(name, """
                import os
                from pathlib import Path
                import unittest
                class Cases(unittest.TestCase):
                    def record(self):
                        with (Path(os.environ['RUNNER_EVENTS']) / self.id()).open('a') as stream:
                            stream.write('ran\\n')
                    def test_a(self): self.record()
                    def test_b(self): self.record()
                    @unittest.skip('intentional')
                    def test_skip(self): pass
                    @unittest.expectedFailure
                    def test_expected(self): self.fail('expected')
            """)
        reports = []
        for jobs in (1, 3):
            result, report, events = self.run_suite(jobs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["selected_tests"], 8)
            self.assertEqual(report["tests_run"], 8)
            self.assertEqual(len(report["skips"]), 2)
            self.assertEqual(len(report["expected_failures"]), 2)
            self.assertEqual(len(list(events.iterdir())), 4)
            for event in events.iterdir():
                self.assertEqual(event.read_text(), "ran\n")
            self.assertEqual(len({test["id"] for test in report["tests"]}), 8)
            self.assertTrue(all(test["seconds"] >= 0 for test in report["tests"]))
            reports.append({test["id"]: test["status"] for test in report["tests"]})
        self.assertEqual(*reports)
        result, report, _ = self.run_suite(
            2, "test_one.Cases.test_a", "test_one.Cases.test_a", "test_two.Cases.test_b"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["tests_run"], 2)
        self.assertEqual({test["id"] for test in report["tests"]},
                         {"test_one.Cases.test_a", "test_two.Cases.test_b"})
        result, report, _ = self.run_suite(2, "--pattern", "test_one.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["tests_run"], 4)

    def test_failures_errors_subtests_and_unexpected_successes_fail(self) -> None:
        self.module("test_outcomes", """
            import unittest
            class Cases(unittest.TestCase):
                def test_failure(self): self.fail('normal failure')
                def test_error(self): raise RuntimeError('normal error')
                def test_subtests(self):
                    with self.subTest(value=1): self.fail('subtest failure')
                    with self.subTest(value=2): raise ValueError('subtest error')
                    with self.subTest(value=3): self.skipTest('subtest skip')
                @unittest.expectedFailure
                def test_unexpected(self): pass
        """)
        for jobs in (1, 3):
            result, report, _ = self.run_suite(jobs)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(report["successful"])
            self.assertEqual(report["tests_run"], 4)
            self.assertEqual(len(report["failures"]), 2)
            self.assertEqual(len(report["errors"]), 2)
            self.assertEqual(len(report["unexpected_successes"]), 1)
            self.assertEqual(len(report["skips"]), 1)
            statuses = {test["id"]: test["status"] for test in report["tests"]}
            self.assertEqual(statuses["test_outcomes.Cases.test_subtests"], "error")
            self.assertIn("subtest failure", result.stderr)
            self.assertIn("normal error", result.stderr)

    def test_discovery_and_explicit_selection_errors_keep_original_details(self) -> None:
        self.module("test_broken", "raise RuntimeError('deliberate import failure')")
        self.module("test_good", """
            import unittest
            class Cases(unittest.TestCase):
                def test_ok(self): pass
        """)
        for jobs in (1, 2):
            result, report, _ = self.run_suite(jobs)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(report["tests_run"], 2)
            self.assertEqual(len(report["errors"]), 1)
            self.assertIn("deliberate import failure", report["errors"][0]["traceback"])
        result, report, _ = self.run_suite(2, "missing_module.Cases.test_missing")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("missing_module", report["errors"][0]["traceback"])

    def test_shared_module_and_inherited_class_fixtures_stay_together(self) -> None:
        self.module("test_shared", """
            import os
            from pathlib import Path
            import unittest
            events = Path(os.environ['RUNNER_EVENTS'])
            ready = False
            def record(event):
                with (events / 'fixtures').open('a') as stream: stream.write(event + '\\n')
            def setUpModule():
                global ready
                ready = True
                record('module setup')
            def tearDownModule(): record('module teardown')
            class Base(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    cls.ready = True
                    record('class setup')
                @classmethod
                def tearDownClass(cls): record('class teardown')
            class Cases(Base):
                def test_a(self): self.assertTrue(ready and self.ready)
                def test_b(self): self.assertTrue(ready and self.ready)
        """)
        self.module("test_class_only", """
            import os
            from pathlib import Path
            import unittest
            class Base(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    cls.ready = True
                    with (Path(os.environ['RUNNER_EVENTS']) / 'class_only').open('a') as stream:
                        stream.write('setup\\n')
            class Cases(Base):
                def test_a(self): self.assertTrue(self.ready)
                def test_b(self): self.assertTrue(self.ready)
        """)
        for jobs in (1, 3):
            result, report, events = self.run_suite(jobs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["tests_run"], 4)
            self.assertEqual((events / "fixtures").read_text().splitlines(),
                             ["module setup", "class setup", "class teardown", "module teardown"])
            self.assertEqual((events / "class_only").read_text(), "setup\n")

    def test_module_fixture_failure_is_reported(self) -> None:
        self.module("test_setup_error", """
            import unittest
            def setUpModule(): raise RuntimeError('module setup failed')
            class Cases(unittest.TestCase):
                def test_a(self): pass
                def test_b(self): pass
        """)
        for jobs in (1, 2):
            result, report, _ = self.run_suite(jobs)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(report["tests_run"], 0)
            self.assertEqual(len(report["errors"]), 1)
            self.assertIn("module setup failed", report["errors"][0]["traceback"])

    def test_worker_exit_fails_the_run(self) -> None:
        self.module("test_exit", """
            import os
            import unittest
            class Cases(unittest.TestCase):
                def test_exit(self): os._exit(7)
                def test_ok(self): pass
        """)
        result, report, _ = self.run_suite(2)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report["successful"])
        self.assertTrue(report["errors"])
        self.assertIn("BrokenProcessPool", result.stderr)

    def test_custom_loaded_instances_keep_state_serially_and_are_rejected_in_parallel(self) -> None:
        self.module("test_custom", """
            import unittest
            class Cases(unittest.TestCase):
                def test_state(self): self.assertTrue(self.loaded_state)
            def load_tests(loader, suite, pattern):
                case = Cases('test_state')
                case.loaded_state = True
                return unittest.TestSuite([case])
        """)
        result, report, _ = self.run_suite(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["tests_run"], 1)
        result, _, _ = self.run_suite(2)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("requires ordinary loadable TestCases", result.stderr)

    def test_module_import_skip_stays_a_skip(self) -> None:
        self.module("test_skipped", "import unittest; raise unittest.SkipTest('module unavailable')")
        for jobs in (1, 2):
            result, report, _ = self.run_suite(jobs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report["selected_tests"], 1)
            self.assertEqual(report["tests_run"], 1)
            self.assertEqual(len(report["skips"]), 1)
            self.assertEqual(report["skips"][0]["reason"], "module unavailable")
            self.assertEqual(report["errors"], [])

    def test_serial_execution_preserves_custom_suite_behavior(self) -> None:
        self.module("test_custom_suite", """
            import unittest
            class Cases(unittest.TestCase):
                def test_state(self): self.assertTrue(self.prepared)
            class CustomSuite(unittest.TestSuite):
                def run(self, result, debug=False):
                    for case in self: case.prepared = True
                    return super().run(result, debug)
            def load_tests(loader, suite, pattern):
                return CustomSuite([Cases('test_state')])
        """)
        result, report, _ = self.run_suite(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["tests_run"], 1)

    def test_duplicate_discovered_instances_are_not_dropped(self) -> None:
        self.module("test_duplicates", """
            import unittest
            class Cases(unittest.TestCase):
                def test_state(self): self.assertTrue(self.loaded_state)
            def load_tests(loader, suite, pattern):
                cases = [Cases('test_state'), Cases('test_state')]
                for case in cases: case.loaded_state = True
                return unittest.TestSuite(cases)
        """)
        result, report, _ = self.run_suite(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["selected_tests"], 2)
        self.assertEqual(report["tests_run"], 2)
        result, _, _ = self.run_suite(2)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate discovered test IDs", result.stderr)

    def test_empty_selection_and_invalid_jobs_are_rejected(self) -> None:
        for jobs, args, message in ((2, (), "no tests selected"),
                                    (0, (), "--jobs must be at least 1")):
            result, _, _ = self.run_suite(jobs, *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
