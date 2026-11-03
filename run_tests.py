#!/usr/bin/env python3
"""Run unittest cases in bounded, isolated processes without extra dependencies."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections.abc import Iterator
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import traceback
import unittest
from types import TracebackType
from typing import Any


ExcInfo = tuple[type[BaseException], BaseException, TracebackType]


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.timings: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.started = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self.started = time.perf_counter()
        self.current = {"id": test.id(), "status": "passed"}

    def stopTest(self, test: unittest.TestCase) -> None:
        assert self.current is not None
        self.current["seconds"] = time.perf_counter() - self.started
        self.timings.append(self.current)
        self.current = None
        super().stopTest(test)

    def mark(self, status: str) -> None:
        priorities = {"passed": 0, "skipped": 1, "expected_failure": 2,
                      "unexpected_success": 3, "failed": 4, "error": 5}
        if self.current is not None and priorities[status] > priorities[self.current["status"]]:
            self.current["status"] = status

    def addFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        self.mark("failed")
        super().addFailure(test, err)

    def addError(self, test: unittest.TestCase, err: ExcInfo) -> None:
        self.mark("error")
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.mark("skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        self.mark("expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self.mark("unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: ExcInfo | None) -> None:
        if err is not None:
            self.mark("failed" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)


def cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def task_groups(tests: list[unittest.TestCase]) -> list[list[str]]:
    modules: dict[str, list[unittest.TestCase]] = {}
    for test in tests:
        modules.setdefault(test.__class__.__module__, []).append(test)
    groups = []
    for name, module_tests in modules.items():
        module = sys.modules[name]
        shared = any(callable(getattr(module, attr, None)) for attr in ("setUpModule", "tearDownModule"))
        for test in module_tests:
            for attr in ("setUpClass", "tearDownClass"):
                method = getattr(test.__class__, attr)
                if getattr(method, "__func__", method) is not getattr(unittest.TestCase, attr).__func__:
                    shared = True
        # Keep fixture lifetimes intact if a future module adds shared setup.
        ids = [test.id() for test in module_tests]
        groups.extend([ids] if shared else [[test_id] for test_id in ids])
    return groups


def run_group(ids: list[str], start_directory: str, verbosity: int) -> dict[str, Any]:
    sys.path.insert(0, start_directory)
    suite = unittest.defaultTestLoader.loadTestsFromNames(ids)
    return run_suite(suite, verbosity)


def run_suite(suite: unittest.TestSuite, verbosity: int) -> dict[str, Any]:
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=verbosity, resultclass=TimedResult, buffer=True
    ).run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": [{"id": test.id(), "traceback": detail} for test, detail in result.failures],
        "errors": [{"id": test.id(), "traceback": detail} for test, detail in result.errors],
        "skips": [{"id": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": [{"id": test.id(), "traceback": detail} for test, detail in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "successful": result.wasSuccessful(),
        "tests": result.timings,
        "output": stream.getvalue(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test_ids", nargs="*", help="explicit unittest IDs (identical arguments run once)")
    parser.add_argument("--start-directory", type=Path, default=Path(__file__).parent / "tests")
    parser.add_argument("--pattern", default="test_*.py", help="discovery filename pattern")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    started = time.perf_counter()
    start_directory = str(args.start_directory.resolve())
    sys.path.insert(0, start_directory)
    loader = unittest.TestLoader()
    try:
        suite = (loader.loadTestsFromNames(list(dict.fromkeys(args.test_ids))) if args.test_ids else
                 loader.discover(start_directory, pattern=args.pattern))
    except Exception as exc:
        parser.error(str(exc))
    selected = list(cases(suite))
    if not selected:
        parser.error("no tests selected")
    ids = [test.id() for test in selected]
    if args.jobs > 1 and len(set(ids)) != len(ids):
        parser.error("duplicate discovered test IDs are unsupported; select distinct loadable cases")
    # Discovery creates synthetic cases for import errors and module-level skips.
    discovery_results = [test for test in selected if type(test).__module__ == unittest.loader.__name__]
    tests = [test for test in selected if type(test).__module__ != unittest.loader.__name__]
    if args.jobs > 1:
        for test in tests:
            cls = type(test)
            if (isinstance(test, unittest.FunctionTestCase) or
                callable(getattr(sys.modules[cls.__module__], "load_tests", None)) or
                test.id() != f"{cls.__module__}.{cls.__qualname__}.{test._testMethodName}"):
                parser.error(f"parallel execution requires ordinary loadable TestCases: {test.id()}; use --jobs 1")
    report = {
        "jobs": args.jobs,
        "selected_tests": len(selected),
        "tests_run": 0,
        "failures": [],
        "errors": [],
        "skips": [],
        "expected_failures": [],
        "unexpected_successes": [],
        "tests": [],
        "successful": True,
    }

    def collect(result: dict[str, Any]) -> None:
        report["tests_run"] += result["tests_run"]
        for key in ("failures", "errors", "skips", "expected_failures", "unexpected_successes", "tests"):
            report[key].extend(result[key])
        report["successful"] = report["successful"] and result["successful"]
        if args.verbose or not result["successful"]:
            print(result["output"], end="", file=sys.stderr)

    verbosity = 2 if args.verbose else 1
    if args.jobs == 1:
        collect(run_suite(suite, verbosity))
    elif discovery_results:
        collect(run_suite(unittest.TestSuite(discovery_results), verbosity))
    if args.jobs > 1 and tests:
        groups = task_groups(tests)
        try:
            with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
                pending = {pool.submit(run_group, group, start_directory, verbosity): group
                           for group in groups}
                for future in as_completed(pending):
                    try:
                        collect(future.result())
                    except Exception:
                        report["successful"] = False
                        error = {"id": ", ".join(pending[future]), "traceback": traceback.format_exc()}
                        report["errors"].append(error)
                        print(error["traceback"], file=sys.stderr)
        except Exception:
            report["successful"] = False
            error = {"id": "worker pool", "traceback": traceback.format_exc()}
            report["errors"].append(error)
            print(error["traceback"], file=sys.stderr)
    report["wall_seconds"] = time.perf_counter() - started
    print(f"Ran {report['tests_run']} tests in {report['wall_seconds']:.3f}s "
          f"(jobs={args.jobs}, failures={len(report['failures'])}, errors={len(report['errors'])}, "
          f"skipped={len(report['skips'])}, expected failures={len(report['expected_failures'])}, "
          f"unexpected successes={len(report['unexpected_successes'])})", file=sys.stderr)
    print("OK" if report["successful"] else "FAILED", file=sys.stderr)
    if args.json_report:
        args.json_report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
