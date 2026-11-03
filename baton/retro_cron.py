"""Independent daily retro and weekly synthesis runners with opt-in cron installation."""

import argparse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .doctor_freshness import require_trusted_runtime
from .configuration import PATH_OPTIONS, config_path
from .errors import ProjectError
from .gitutil import file_lock
from .providers import PROVIDERS


BEGIN = "# BEGIN baton retrospective"
END = "# END baton retrospective"
DEFAULT_SCHEDULE = "0 10 * * *"
WEEKLY_BEGIN = "# BEGIN baton weekly retrospective"
WEEKLY_END = "# END baton weekly retrospective"
DEFAULT_WEEKLY_SCHEDULE = "0 11 * * 1"


def validate_schedule(value: str) -> str:
    fields = value.split()
    if len(fields) != 5:
        raise ValueError("Cron schedule must have five numeric fields: minute hour day month weekday")
    for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        for item in field.split(","):
            if not re.fullmatch(r"(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[1-9][0-9]*)?", item):
                raise ValueError(f"Invalid cron schedule field: {field}")
            base = item.split("/", 1)[0]
            if base != "*":
                numbers = [int(number) for number in base.split("-")]
                if any(number < low or number > high for number in numbers) or numbers != sorted(numbers):
                    raise ValueError(f"Cron field {field!r} must be within {low}..{high}")
    return " ".join(fields)


def _quote(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Cron paths and environment settings must not contain control characters")
    # Cron interprets percent signs before invoking the shell, even inside quotes.
    return '"$(printf \'\\045\')"'.join(shlex.quote(part) for part in value.split("%"))


def state_directory(environment: Mapping[str, str]) -> Path:
    home = Path(environment.get("HOME", str(Path.home())))
    return Path(environment.get("XDG_STATE_HOME", str(home / ".local/state"))).expanduser().resolve() / "baton"


@contextmanager
def installation_lock(environment: Mapping[str, str], label: str) -> Iterator[None]:
    directory = state_directory(environment)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(directory / "retro-cron-install.lock", label):
        yield


def cron_entry(schedule: str, *, executable: Path, environment: Mapping[str, str],
               configuration: Path, log: Path, since: str = "3d", provider: str | None = None) -> str:
    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown weekly retrospective provider: {provider}")
        action = ["run-weekly", "--provider", provider]
    else:
        from .retro_automation import duration
        try:
            duration(since)
        except ProjectError as error:
            raise ValueError(str(error)) from error
        action = ["run", "--since", since]
    settings = {"PATH": environment.get("PATH", os.defpath), "BATON_CONFIG_FILE": str(configuration)}
    # Claude's keychain-backed login needs USER; cron does not always provide it.
    overrides = ["USER", "XDG_STATE_HOME", "BATON_REPO", *("BATON_" + key.upper() for key in PATH_OPTIONS if key != "projects_repo"),
                 *(entry.binary_env for entry in PROVIDERS.values())]
    settings.update({key: environment[key] for key in overrides if key in environment})
    arguments = ["/usr/bin/env", *(key + "=" + value for key, value in settings.items()), str(executable),
                 "-I", "-m", "baton.retro_cron", *action]
    command = "exec " + " ".join(_quote(argument) for argument in arguments) + " >> " + _quote(str(log)) + " 2>&1"
    return validate_schedule(schedule) + " /bin/sh -c " + shlex.quote(command)


def _managed_block(previous: str, begin: str = BEGIN, end: str = END) -> tuple[list[str], int | None, int | None]:
    lines = previous.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == begin]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == end]
    if len(starts) != len(ends) or len(starts) > 1 or (starts and ends[0] < starts[0]):
        raise ValueError("Malformed or duplicate managed retrospective cron block; reconcile it before installing")
    return lines, starts[0] if starts else None, ends[0] if ends else None


def update_crontab(previous: str, entry: str, *, weekly: bool = False) -> str:
    daily_block = _managed_block(previous)
    weekly_block = _managed_block(previous, WEEKLY_BEGIN, WEEKLY_END)
    _, daily_start, daily_end = daily_block
    _, weekly_start, weekly_end = weekly_block
    if (daily_start is not None and daily_end is not None and weekly_start is not None and weekly_end is not None
            and not (daily_end < weekly_start or weekly_end < daily_start)):
        raise ValueError("Malformed overlapping managed retrospective cron blocks; reconcile them before installing")
    lines, start, end = weekly_block if weekly else daily_block
    begin_marker, end_marker = (WEEKLY_BEGIN, WEEKLY_END) if weekly else (BEGIN, END)
    block = begin_marker + "\n" + entry + "\n" + end_marker + "\n"
    if start is not None and end is not None:
        return "".join(lines[:start]) + block + "".join(lines[end + 1:])
    separator = "" if not previous or previous.endswith("\n") else "\n"
    return previous + separator + block


def _read_crontab(binary: str, environment: dict[str, str]) -> str:
    result = subprocess.run([binary, "-l"], capture_output=True, text=True, env=environment, timeout=30)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and re.fullmatch(r"(?:crontab:\s*)?no crontab for [^\n]+", result.stderr.strip()):
        return ""
    raise ValueError(f"Cannot read existing crontab; nothing changed: {result.stderr.strip() or result.stdout.strip()}")


def install_cron(schedule: str | None = None, *, since: str = "3d", weekly_provider: str | None = None,
                 environ: Mapping[str, str] | None = None, executable: Path | None = None,
                 crontab: str = "crontab") -> str:
    environment = dict(os.environ if environ is None else environ)
    directory = state_directory(environment)
    configuration = config_path(environment).resolve()
    if schedule is None:
        schedule = DEFAULT_WEEKLY_SCHEDULE if weekly_provider is not None else DEFAULT_SCHEDULE
    log_name = "retro-weekly-cron.log" if weekly_provider is not None else "retro-cron.log"
    entry = cron_entry(schedule, executable=executable or Path(sys.executable).absolute(), environment=environment,
                       configuration=configuration, log=directory / log_name, since=since, provider=weekly_provider)
    environment["LC_ALL"] = "C"
    with installation_lock(environment, "retro cron installation"):
        previous = _read_crontab(crontab, environment)
        updated = update_crontab(previous, entry, weekly=weekly_provider is not None)
        if updated == previous:
            return entry
        _write_crontab(crontab, environment, previous, updated)
    return entry


def _write_crontab(binary: str, environment: dict[str, str], previous: str, updated: str) -> None:
    if _read_crontab(binary, environment) != previous:
        raise ValueError("Crontab changed during installation; nothing changed, retry installation")
    result = subprocess.run([binary, "-"], input=updated, capture_output=True, text=True,
                            env=environment, timeout=30)
    if result.returncode:
        raise ValueError(f"Cannot install retrospective cron job: {result.stderr.strip() or result.stdout.strip()}")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["run"]:
        from .retro_automation import pending_main
        return pending_main(arguments[1:])
    if arguments[:1] == ["run-weekly"]:
        from .retro_summary import summary_main
        return summary_main(arguments[1:])
    parser = argparse.ArgumentParser(prog="baton-retro-cron", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Scan and run pending retros; forwards options to baton retro pending")
    commands.add_parser("run-weekly", help="Synthesize weekly retros; forwards options to baton retro summarize")
    installer = commands.add_parser("install", help="Install or update the managed daily user-crontab job")
    installer.add_argument("--schedule", default=DEFAULT_SCHEDULE, help="Five-field numeric cron schedule (default: daily 10:00)")
    installer.add_argument("--since", default="3d", help="Completion age window (default: 3d)")
    weekly_installer = commands.add_parser("install-weekly", help="Install or update the independent weekly synthesis job")
    weekly_installer.add_argument("--schedule", default=DEFAULT_WEEKLY_SCHEDULE,
                                  help="Five-field numeric cron schedule (default: Monday 11:00, local cron timezone)")
    weekly_installer.add_argument("--provider", required=True, choices=tuple(PROVIDERS))
    weekly_installer.add_argument("--check", action="store_true", help="Validate weekly options without changing cron")
    args = parser.parse_args(arguments)
    try:
        if args.command == "install-weekly" and args.check:
            validate_schedule(args.schedule)
            return 0
        require_trusted_runtime()
        if (Path(__file__).resolve().parents[1] / "pyproject.toml").is_file():
            raise ValueError("Cron installation requires the installed distribution; run install.sh --install-cron")
        print("Installed retrospective cron job:\n" + install_cron(
            args.schedule, since=getattr(args, "since", "3d"), weekly_provider=getattr(args, "provider", None)))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"baton-retro-cron: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
