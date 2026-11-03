"""Run plan and implementation reviewers in parallel (`baton review run`).

The review scripts assemble the prompt and input files; this module selects the reviewers,
resolves their CLIs, models and timeouts, and runs each through ``Provider.review``. It only
reads configuration and optional project metadata, and writes only inside the caller's
run directory. No trusted runtime is required.
"""

import argparse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
from typing import TextIO

from .configuration import read_config
from .errors import ProjectError
from .providers import (PROVIDERS, ProcessGroups, Provider, ReviewRequest, _nonempty, kill_groups_on_signals,
                        resolved_review_models)


@dataclass(frozen=True)
class ReviewKind:
    env_prefix: str
    default_timeout: int
    label: str


# Plan reviewers also inspect the project repositories, so they get more time than diff reviewers.
KINDS = {"plan": ReviewKind("PLAN_REVIEW", 420, "plan review"),
         "implementation": ReviewKind("PEER_REVIEW", 300, "peer review")}


# An interrupted or terminated run kills its reviewers and exits like a shell job killed by that signal.
SIGNAL_EXIT_CODES = {signal.SIGINT: 130, signal.SIGTERM: 143}


class ReviewError(Exception):
    """A reviewer selection or environment problem reported before any reviewer starts."""


@dataclass(frozen=True)
class Reviewer:
    provider: Provider
    executable: str
    model: str
    timeout: int
    options: Mapping[str, str]


def _non_negative(variable: str, value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise ReviewError(f"{variable} must be a non-negative integer.")
    return int(value)


def prepare(kind: str, reviewers_csv: str, environ: Mapping[str, str], warnings: TextIO,
            focus_area: str | None = None, selector: str | None = None) -> list[Reviewer]:
    """Validates the run and returns the available reviewers in registry order.

    Reviewers default to the configured model map; empty CSV entries are ignored. Missing CLIs
    are skipped with a warning written to ``warnings`` and the run fails if none remain.
    """
    settings = KINDS[kind]
    prefix = settings.env_prefix
    default_timeout = _non_negative(f"{prefix}_TIMEOUT_SECS",
                                    environ.get(f"{prefix}_TIMEOUT_SECS") or str(settings.default_timeout))
    key = f"{kind}_review_model"
    try:
        config = read_config()
        models = resolved_review_models(config, key)
        if focus_area is not None:
            if kind != "implementation":
                raise ValueError("--focus-area is only supported for implementation reviews")
            focused = focused_context(focus_area, selector, config)["resolved_models"]
        else:
            focused = None
    except (OSError, ValueError, ProjectError) as error:
        raise ReviewError(f"baton config: {error}") from None
    requested = [name for name in reviewers_csv.split(",") if name] if reviewers_csv else list(models)
    for name in requested:
        if name not in PROVIDERS:
            raise ReviewError(f"unknown reviewer '{name}'; configured providers: {','.join(PROVIDERS)}.")
    if not requested:
        raise ReviewError("reviewer list must contain at least one reviewer.")
    reviewers = []
    for name, provider in PROVIDERS.items():
        if name not in requested:
            continue
        variable = f"{prefix}_{provider.env_name}_TIMEOUT_SECS"
        timeout = _non_negative(variable, environ[variable]) if environ.get(variable) else default_timeout
        try:
            options = provider.review_options(prefix, environ)
        except ValueError as error:
            raise ReviewError(str(error)) from None
        executable = provider.executable(environ)
        found = shutil.which(executable)
        if found is None:
            print(f"Warning: skipping {name} review; CLI not found: {executable}", file=warnings, flush=True)
            continue
        # Absolute because some reviewers (agy) run in another directory; not resolved, since a
        # symlinked CLI may depend on the name it is invoked by.
        reviewers.append(Reviewer(provider, str(Path(found).absolute()), (focused or models).get(name, "default"), timeout,
                                  options))
    if not reviewers:
        raise ReviewError("no requested reviewer CLI is available.")
    return reviewers


def focused_context(area: str, selector: str | None = None, config: dict | None = None) -> dict:
    from .review_loop import review_focus_catalog, resolved_focused_review_models, review_project

    catalog = review_focus_catalog()
    if area not in catalog:
        raise ValueError(f"unknown focus area {area!r}; valid names: {', '.join(catalog)}")
    models = resolved_focused_review_models(config if config is not None else read_config(),
                                            review_project(selector))
    return {"focus_area": area, "guidance": catalog[area], "resolved_models": models[area]}


def heading_validator(label: str, headings: list[str]) -> Callable[[Path], str | None] | None:
    if not headings:
        return None

    def validate(path: Path) -> str | None:
        lines = {line.rstrip(" \t\r\f\v") for line in path.read_text(errors="replace").splitlines()}
        missing = [heading for heading in headings if heading not in lines]
        return f"a complete {label}; missing headings: {' '.join(missing)}" if missing else None
    return validate


def run(kind: str, reviewers: list[Reviewer], prefix: str, input_path: Path, combined_input: Path,
        prompt: str, headings: list[str], read_dirs: tuple[Path, ...] = ()) -> int:
    validator = heading_validator(KINDS[kind].label, headings)
    processes = ProcessGroups()
    requests = [ReviewRequest(executable=reviewer.executable, model=reviewer.model, input=input_path,
                              combined_input=combined_input, prompt=prompt,
                              output=reviewer.provider.review_output(prefix, reviewer.model),
                              log=Path(f"{prefix}-{reviewer.provider.name}.log"), timeout=reviewer.timeout,
                              validator=validator, options=reviewer.options, read_dirs=read_dirs,
                              processes=processes)
                for reviewer in reviewers]

    def review(reviewer: Reviewer, request: ReviewRequest) -> int:
        try:
            return reviewer.provider.review(request)
        except OSError as error:
            request.say(f"Error: cannot run {reviewer.provider.name} review: {error}")
            return 1

    with kill_groups_on_signals(processes, SIGNAL_EXIT_CODES):
        with ThreadPoolExecutor(max_workers=len(reviewers)) as executor:
            statuses = list(executor.map(review, reviewers, requests))
    failed = []
    for reviewer, status in zip(reviewers, statuses):
        if status != 0:
            failed.append(reviewer.provider.name)
            print(f"Error: {reviewer.provider.name} review failed.", file=sys.stderr, flush=True)
    if len(failed) == len(reviewers):
        print("All review jobs failed.", file=sys.stderr)
        for request, reviewer in zip(requests, reviewers):
            print(f"{reviewer.provider.name} log: {request.log}", file=sys.stderr)
        return 1
    if failed:
        print(f"Warning: partial review results. Failed reviewers: {' '.join(failed)}", file=sys.stderr, flush=True)
    for reviewer, request in zip(reviewers, requests):
        name = reviewer.provider.name
        print(f"{name} review: {request.output if _nonempty(request.output) else 'unavailable'}")
        print(f"{name} log: {request.log}")
        print(f"{name} model: {reviewer.model}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton review", description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="action", required=True)
    focus = commands.add_parser("focus", help="Resolve named focused review guidance and models")
    focus.add_argument("name")
    focus.add_argument("--selector", metavar="PROJECT")
    command = commands.add_parser(
        "run", help="Run the selected reviewers in parallel",
        description="Run the selected reviewers in parallel. Environment overrides: <PREFIX>_TIMEOUT_SECS, "
                    "<PREFIX>_<PROVIDER>_TIMEOUT_SECS and <PROVIDER>_BIN, where PREFIX is PLAN_REVIEW for plan "
                    "reviews and PEER_REVIEW for implementation reviews, plus provider-specific settings such as "
                    "<PREFIX>_CODEX_REASONING and <PREFIX>_CODEX_AUTO_FALLBACK.")
    command.add_argument("--kind", choices=tuple(KINDS), required=True)
    command.add_argument("--focus-area", metavar="NAME", help="Named implementation review focus")
    command.add_argument("--selector", metavar="PROJECT", help="Project context for a named focus")
    command.add_argument("--check", action="store_true",
                         help="Only validate the reviewers and resolve their CLIs and models")
    command.add_argument("--reviewers", default="", metavar="CSV",
                         help="Reviewers to run (default: the configured review model map)")
    command.add_argument("--prefix", help="Output path prefix; each reviewer writes <prefix>-<provider>.md and .log")
    command.add_argument("--input", type=Path, help="Reviewed text, on stdin for prompt-argument reviewers")
    command.add_argument("--combined-input", type=Path, help="Prompt followed by the reviewed text")
    command.add_argument("--prompt-file", type=Path, help="Review prompt")
    command.add_argument("--required-heading", action="append", default=[], metavar="HEADING",
                         help="Heading a validated reviewer output must contain (repeatable)")
    command.add_argument("--read-dir", action="append", default=[], type=Path, metavar="PATH", dest="read_dirs",
                         help="Directory reviewers may read, such as a project repository (repeatable)")
    args = parser.parse_args(argv)
    if args.action == "focus":
        try:
            print(json.dumps(focused_context(args.name, args.selector), indent=2))
            return 0
        except (OSError, ValueError, ProjectError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
    if not args.check:
        missing = [option for option in ("prefix", "input", "combined_input", "prompt_file")
                   if getattr(args, option) is None]
        if missing:
            parser.error("review run requires " + ", ".join("--" + option.replace("_", "-") for option in missing))
        # Some reviewers (agy) run in another directory, where relative paths would resolve elsewhere.
        args.prefix = os.path.join(os.getcwd(), args.prefix)
        args.input, args.combined_input, args.prompt_file = (
            path.absolute() for path in (args.input, args.combined_input, args.prompt_file))
        args.read_dirs = tuple(path.absolute() for path in args.read_dirs)
    # The scripts check before they run, so each skip warning is printed once: by the run, or by a
    # failing check (no run follows it).
    skipped = io.StringIO() if args.check else sys.stderr
    try:
        reviewers = prepare(args.kind, args.reviewers, os.environ, skipped, args.focus_area, args.selector)
        if args.check:
            return 0
        if args.focus_area is not None:
            print(f"Focus area: {args.focus_area}")
            for reviewer in reviewers:
                model = "default (provider-selected)" if reviewer.model == "default" else reviewer.model
                print(f"Focused {reviewer.provider.name} model: {model}")
        prompt = args.prompt_file.read_text()
    except ReviewError as error:
        if isinstance(skipped, io.StringIO):
            sys.stderr.write(skipped.getvalue())
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Error: cannot read the review prompt: {error}", file=sys.stderr)
        return 1
    return run(args.kind, reviewers, args.prefix, args.input, args.combined_input, prompt, args.required_heading,
               args.read_dirs)


if __name__ == "__main__":
    raise SystemExit(main())
