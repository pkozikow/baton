"""Require CI reports and execution of dependency-sensitive integration coverage."""

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REQUIRED_CORE_TESTS = frozenset({
    "test_build_provenance.BuildProvenanceTest.test_wheel_contains_runtime_and_working_entrypoints",
    "test_build_provenance.BuildProvenanceTest.test_sdist_contains_focus_guidance",
    "test_build_provenance.BuildProvenanceTest.test_sdist_stamp_survives_when_git_is_unavailable",
    "test_build_provenance.BuildProvenanceTest.test_missing_or_invalid_stamp_is_unverifiable_without_git",
    "test_build_provenance.BuildProvenanceTest.test_real_checkout_overrides_a_preserved_stamp",
    "test_native_retirement_compat.NativeRetirementCompatibilityTest.test_missing_merged_members_have_native_position_constraints",
    "test_native_retirement_compat.NativeRetirementCompatibilityTest.test_local_unstack_preserves_sibling_group_with_missing_merged_root",
    "test_native_retirement_compat.NativeRetirementCompatibilityTest.test_normal_rebase_supports_missing_merged_root_after_local_default_fetch",
})


def check_reports(directory: Path) -> list[str]:
    errors: list[str] = []
    for name, required in (("core", REQUIRED_CORE_TESTS), ("skills", frozenset())):
        path = directory / f"{name}.json"
        try:
            report: dict[str, Any] = json.loads(path.read_text())
            tests = report["tests"]
            if not report["successful"] or report["tests_run"] <= 0:
                errors.append(f"{path}: no successful test run")
            if report["selected_tests"] != report["tests_run"] or len(tests) != report["tests_run"]:
                errors.append(f"{path}: selected/executed test counts differ")
            if not any(test['status'] == 'passed' for test in tests):
                errors.append(f"{path}: no passing tests")
            outcomes: dict[str, list[str]] = {}
            for test in tests:
                outcomes.setdefault(test["id"], []).append(test["status"])
            for test_id in sorted(required):
                if outcomes.get(test_id) != ["passed"]:
                    errors.append(f"{path}: required test {test_id} must pass exactly once; got {outcomes.get(test_id, [])}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{path}: invalid or missing report: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    errors = check_reports(parser.parse_args().directory)
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
