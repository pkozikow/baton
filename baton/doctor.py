"""Installation, path, and review-provider diagnostics without project-state changes."""

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import sys
from typing import Any

from .configuration import (PATH_OPTIONS, config_path, effective_paths, read_raw_config,
                            validate_paths, validate_repo_aliases)
from .errors import ProjectError
from .gitutil import common_dir, git
from .slots import SLOT_NAMES, read_active_project
from .storage_names import registry_directory


def repository_check(name: str, path: Path, *, root: bool = False,
                     expected_common: Path | None = None) -> dict[str, Any]:
    try:
        if git(path, "rev-parse", "--is-inside-work-tree") != "true":
            raise ProjectError("expected a non-bare Git worktree")
        top = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
        if root and top != path.resolve():
            raise ProjectError(f"expected a Git checkout root; this path belongs to {top}")
        if expected_common is not None and common_dir(path) != expected_common:
            raise ProjectError("worktree belongs to a different coordinator repository")
        return {"name": name, "status": "pass", "message": str(path)}
    except (OSError, ProjectError) as error:
        return {"name": name, "status": "fail", "message": f"{path}: {error}"}


def path_checks(config: dict[str, Any], environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    environment = os.environ if environ is None else environ
    results: list[dict[str, Any]] = []
    supplied = config.get("paths", {})
    try:
        validate_paths(supplied)
    except ValueError as error:
        results.append({"name": "paths.config", "status": "fail", "message": str(error)})
        supplied = {key: value for key, value in supplied.items()
                    if key in PATH_OPTIONS and isinstance(value, str) and value.strip()} if isinstance(supplied, dict) else {}
    roots = {}
    for key in PATH_OPTIONS:
        try:
            roots.update(effective_paths({"paths": supplied}, environment, keys=(key,)))
        except (OSError, ValueError, RuntimeError) as error:
            results.append({"name": f"paths.{key}", "status": "fail",
                            "message": f"Cannot resolve configured path: {error}"})
    available = set()
    for key, path in roots.items():
        variable = "BATON_REPO" if key == "projects_repo" else "BATON_" + key.upper()
        if variable in environment and not environment[variable].strip():
            results.append({"name": f"paths.{key}", "status": "fail", "message": f"{variable} is blank"})
        elif not path.is_dir():
            hint = "run baton init or correct this path" if key in ("projects_repo", "slots_root") else "create the directory or correct this path"
            results.append({"name": f"paths.{key}", "status": "fail", "message": f"{path}: directory does not exist; {hint}"})
        else:
            available.add(key)
            if key in ("projects_repo", "artifacts_root"):
                results.append(repository_check(f"paths.{key}", path, root=key == "projects_repo"))
            else:
                results.append({"name": f"paths.{key}", "status": "pass", "message": str(path)})
    registry = None
    if "projects_repo" in available:
        try:
            registry = common_dir(roots["projects_repo"])
        except (ProjectError, OSError):
            pass
    invalid_slots = set()
    if registry is not None:
        try:
            records = sorted((registry_directory(registry) / "v2/projects").glob("*.json"))
        except (OSError, ProjectError) as error:
            results.append({"name": "paths.registry", "status": "fail", "message": str(error)})
            records = []
        for record in records:
            try:
                read_active_project(record)
            except FileNotFoundError:
                continue  # Another slot's cleanup archived it after the glob.
            except (ProjectError, OSError) as error:
                invalid_slots.add(record.stem)
                results.append({"name": f"slots.{record.stem}", "status": "fail",
                                "message": str(error)})
    if "slots_root" in available:
        for slot in SLOT_NAMES:
            if slot in invalid_slots:
                continue
            path = roots["slots_root"] / slot
            if not path.is_dir():
                results.append({"name": f"slots.{slot}", "status": "fail",
                                "message": f"{path}: missing coordinator; run baton init"})
            else:
                results.append(repository_check(f"slots.{slot}", path, root=True, expected_common=registry))
    aliases = config.get("repo_aliases", {})
    try:
        validate_repo_aliases(aliases)
    except ValueError as error:
        results.append({"name": "paths.repo_aliases", "status": "fail", "message": str(error)})
    else:
        for target in sorted(set(aliases.values())):
            if "repos_root" in available:
                results.append(repository_check(f"repos.{target}", roots["repos_root"] / target, root=True))
            else:
                results.append({"name": f"repos.{target}", "status": "skip",
                                "message": "Cannot check repository until repos_root is valid"})
    return results


def diagnose(*, source: Path | None = None, ref: str | None = None,
             smoke: bool = True, timeout: int = 60) -> dict[str, Any]:
    from .doctor_freshness import freshness_check
    from .doctor_smoke import model_checks

    results = [freshness_check(source, ref)]
    try:
        config = read_raw_config()
    except (OSError, ValueError) as error:
        results.append({"name": "config", "status": "fail", "message": str(error)})
        results.append({"name": "paths", "status": "skip", "message": "Cannot validate paths until configuration parses"})
        results.append({"name": "models", "status": "skip", "message": "Cannot validate models until configuration parses"})
    else:
        results.append({"name": "config", "status": "pass", "message": str(config_path())})
        results.extend(path_checks(config))
        results.extend(model_checks(config, smoke=smoke, timeout=timeout))
    return {"ok": all(row["status"] != "fail" for row in results),
            "complete": all(row["status"] != "skip" for row in results), "checks": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton doctor", description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit structured diagnostics")
    parser.add_argument("--source", type=Path, help="Framework source checkout (default: installation provenance)")
    parser.add_argument("--ref", help="Local comparison ref (default: main, otherwise master); does not fetch")
    parser.add_argument("--skip-smoke", action="store_true", help="Check static configuration and executable paths without model requests")
    parser.add_argument("--timeout", type=int, default=60, metavar="SECONDS", help="Timeout per model smoke test (default: 60)")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be a positive integer")
    report = diagnose(source=args.source, ref=args.ref, smoke=not args.skip_smoke, timeout=args.timeout)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in report["checks"]:
            print(f"{row['status'].upper():4} {row['name']}: {row['message']}")
        state = "passed" if report["ok"] else "failed"
        suffix = " (some checks skipped)" if not report["complete"] else ""
        print(f"Doctor {state}{suffix}.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
