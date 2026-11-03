"""Cheap archive discovery and crash-safe, bounded retrospective execution."""

import argparse
import contextlib
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import uuid

from .doctor_freshness import require_trusted_runtime
from .errors import ProjectError
from .gitutil import write_json, require_agent_write_path
from .providers import PROMPT_ARGV, PROMPT_STDIN


DRAFT_BEGIN = "BATON_RETRO_DRAFT_BEGIN"
DRAFT_END = "BATON_RETRO_DRAFT_END"


def create_draft_workspace() -> Path:
    root = Path(tempfile.gettempdir()).resolve()
    require_agent_write_path(root)
    cutoff = time.time() - 7 * 24 * 60 * 60
    for previous in root.glob("baton-retro-*"):
        try:
            if (not previous.is_symlink() and previous.is_dir()
                    and previous.stat().st_uid == os.getuid()
                    and (previous / ".failed").is_file()
                    and (previous / ".failed").stat().st_mtime < cutoff):
                shutil.rmtree(previous)
        except OSError:
            pass
    return Path(tempfile.mkdtemp(prefix="baton-retro-", dir=root))


def recover_draft(log: Path, output: Path) -> bool:
    with log.open(errors="replace") as stream:
        transcript = stream.read(2 * 1024 * 1024 + 1)
    if len(transcript) > 2 * 1024 * 1024:
        raise ProjectError("Fork output exceeds the 2 MiB draft recovery limit; inspect the attempt log.")
    lines = transcript.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == DRAFT_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == DRAFT_END]
    if not starts and not ends:
        return False
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ProjectError("Fork output has incomplete or ambiguous draft markers; inspect the attempt log.")
    with output.open("x") as target:
        target.write("".join(lines[starts[0] + 1:ends[0]]))
    return True


def output_excerpt(stdout: Path, stderr: Path, limit: int = 1000) -> str:
    """The start of stdout and the end of stderr (where harness errors land), bounded for state.json.

    When both are present, the stderr tail keeps up to half the budget so a long stdout cannot crowd
    out the harness error.
    """
    head = tail = ""
    with contextlib.suppress(OSError), stdout.open(errors="replace") as stream:
        head = "\n".join(stream.read(limit).splitlines()[:10])
    with contextlib.suppress(OSError), stderr.open("rb") as stream:
        stream.seek(max(0, stream.seek(0, os.SEEK_END) - 4 * limit))
        tail = "\n".join(stream.read().decode(errors="replace").splitlines()[-10:])
    head = head if head.strip() else ""
    section = ""
    if tail.strip():
        label = "stderr:\n"
        budget = (limit // 2 if head else limit) - len(label)
        section = label + tail[max(0, len(tail) - budget):]
    if not section:
        return head[:limit]
    if not head:
        return section
    return head[:max(0, limit - len(section) - 1)] + "\n" + section


class PublicationError(ProjectError):
    """A verified retro exists but its artifact commit needs retrying."""


def identity(project: dict[str, Any]) -> str:
    value = project.get("uuid")
    if not isinstance(value, str):
        raise ProjectError("Archived project has no UUID.")
    try:
        uuid.UUID(value)
    except ValueError:
        raise ProjectError(f"Invalid project UUID: {value}") from None
    return value


def read_record(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ProjectError(f"Expected a JSON object: {path}")
    return value


def duration(value: str) -> timedelta:
    match = re.fullmatch(r"([0-9]+)([dh])", value)
    if not match or int(match[1]) <= 0:
        raise ProjectError("--since must be a positive duration such as 3d or 12h.")
    return timedelta(**{"days" if match[2] == "d" else "hours": int(match[1])})


def state_directory(layout: Path, project: dict[str, Any]) -> Path:
    return layout / "retros" / identity(project)


def retro_file(project: dict[str, Any], artifact_directory: Callable[[dict[str, Any]], Path]) -> Path:
    return artifact_directory(project) / "retro.md"


def conflicting_project_uuid(text: str, project_id: str) -> bool:
    markers = re.findall(
        r"(?im)^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?project[ \t]+uuid(?:\*\*)?[ \t]*:"
        r"(?:\*\*)?[ \t]*`?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{32})\b",
        text,
    )
    return any(uuid.UUID(marker) != uuid.UUID(project_id) for marker in markers)


def existing_result(layout: Path, project: dict[str, Any], output: Path) -> tuple[str, str]:
    state_path = state_directory(layout, project) / "state.json"
    state = read_record(state_path) if state_path.exists() else {}
    if state and state.get("uuid") != identity(project):
        return "blocked", "retrospective state belongs to another project UUID"
    if state.get("status") in ("running", "interrupted") and isinstance(state.get("process_group"), int):
        try:
            os.killpg(state["process_group"], 0)
            return "running", "recorded owner-fork process group is still alive"
        except ProcessLookupError:
            pass
        except PermissionError:
            return "running", "recorded owner-fork process group cannot be inspected"
    if output.is_symlink():
        return "blocked", "retro.md is a symlink; inspect it manually"
    if output.exists():
        if not output.is_file():
            return "blocked", "retro.md is not a regular file"
        text = output.read_text()
        if not text.strip():
            return "blocked", "retro.md is empty; inspect the partial artifact before retrying"
        if conflicting_project_uuid(text, identity(project)):
            return "blocked", "retro.md explicitly records a different project UUID"
        if state.get("status") in ("running", "failed", "interrupted"):
            return "blocked", "an earlier attempt left retro.md; inspect it before retrying"
        if state.get("status") == "publication-failed":
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            if identity(project) in text and digest == state.get("sha256"):
                return "pending", "verified retrospective needs artifact commit retry"
            return "blocked", "retrospective changed after publication failed; inspect it manually"
        if identity(project) in text:
            return "complete", "retro.md records this project UUID"
        if state.get("status") == "completed":
            return "blocked", "completed retro.md no longer records this project UUID"
        return "blocked", "retro.md does not record this project UUID"
    return "pending", "no retrospective artifact"


def archive_scan(layout: Path, artifact_directory: Callable[[dict[str, Any]], Path], since: str,
                 *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - duration(since)
    records, errors = [], []
    owners: dict[str, set[str]] = {}
    for category in ("archive", "projects"):
        for path in sorted((layout / category).glob("*.json")):
            try:
                project = read_record(path)
                project_id = identity(project)
                if not isinstance(project.get("name"), str) or not project["name"]:
                    raise ProjectError("Project metadata has no name.")
                if "artifacts_path" in project and (not isinstance(project["artifacts_path"], str)
                                                     or not project["artifacts_path"].strip()):
                    raise ProjectError("artifacts_path must be a nonempty absolute path string.")
                directory = artifact_directory(project)
                if not directory.is_absolute():
                    raise ProjectError("Project artifacts path is not absolute.")
                if directory.is_symlink():
                    raise ProjectError("Project artifacts directory is a symlink; inspect it manually.")
                resolved = directory.resolve()
                owners.setdefault(str(resolved), set()).add(project_id)
                if category == "archive":
                    records.append((project, directory, resolved))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, ProjectError) as error:
                errors.append({"path": str(path), "error": str(error)})
    candidates, skipped = [], []
    counts = Counter(identity(project) for project, _, _ in records)
    seen = set()
    for project, directory, resolved in records:
        project_id = identity(project)
        entry = {"uuid": project_id, "name": project.get("name"), "artifacts_path": str(directory)}
        if counts[project_id] > 1:
            if project_id not in seen:
                errors.append({"uuid": project_id, "error": "duplicate archived project UUID"})
            seen.add(project_id)
            continue
        try:
            completed = datetime.fromisoformat(project.get("completed_at", ""))
            if completed.tzinfo is None:
                raise ValueError("completion time has no timezone")
        except (ValueError, TypeError):
            skipped.append({**entry, "status": "unknown-date", "reason": "no valid timezone-aware completed_at"})
            continue
        if completed > now or completed < cutoff:
            skipped.append({**entry, "status": "outside-window", "reason": "completion outside requested window"})
            continue
        try:
            status, reason = existing_result(layout, project, directory / "retro.md")
            if status == "pending" and len(owners.get(str(resolved), set())) > 1:
                status, reason = "blocked", "artifact directory is shared by multiple project UUIDs"
        except (OSError, ValueError, ProjectError) as error:
            status, reason = "blocked", str(error)
        if status == "pending" and project.get("owner") is None:
            status, reason = "unowned", "no owner session is registered"
        target = candidates if status == "pending" else skipped
        target.append({**entry, "status": status, "reason": reason, "completed_at": completed.isoformat()})
    candidates.sort(key=lambda item: (item["completed_at"], item["uuid"]))
    return {"candidates": candidates, "skipped": skipped, "errors": errors}


def _execute(command: list[str], cwd: Path, prompt: str, directory: Path, project: dict[str, Any],
             output: Path, timeout: int, publish: Callable[[Path], None] | None = None,
             environment: dict[str, str] | None = None, *, private_draft: bool = False,
             managed_workspace: bool = False, prompt_delivery: str = PROMPT_STDIN) -> int:
    state_path = directory / "state.json"
    previous = read_record(state_path) if state_path.exists() else {}
    attempt = int(previous.get("attempt", 0)) + 1
    state = {"uuid": identity(project), "status": "running", "pid": os.getpid(), "attempt": attempt,
             "started_at": datetime.now(timezone.utc).isoformat(), "output": str(output),
             "log": str(directory / f"attempt-{attempt}.log"),
             "stderr_log": str(directory / f"attempt-{attempt}.stderr.log")}
    write_json(state_path, state)
    code, error = 1, None
    process = None
    try:
        # The draft is recovered from stdout alone: harnesses such as codex exec echo the prompt (which
        # quotes the marker lines) and repeat the final message on stderr.
        with Path(state["log"]).open("xb") as log, Path(state["stderr_log"]).open("xb") as errors:
            def record_child() -> None:
                # Popen waits for exec, so saving identity before exec closes the orphan-launch gap.
                state["process_group"] = os.getpid()
                write_json(state_path, state)

            # An argv-delivered prompt is already part of the command; stdin then stays empty.
            piped = prompt_delivery == PROMPT_STDIN
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                       stdout=log, stderr=errors, start_new_session=True,
                                       preexec_fn=record_child, env=environment)
            state["process_group"] = process.pid
            try:
                process.communicate(prompt.encode() if piped else None, timeout=timeout)
                code = process.returncode
            except subprocess.TimeoutExpired:
                error = f"retrospective session exceeded {timeout} seconds"
                code = 124
            finally:
                # Kill surviving descendants too, including when the harness parent exited first.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if code == 0:
            if private_draft and not output.exists() and not output.is_symlink():
                state["recovered_from_output"] = recover_draft(Path(state["log"]), output)
            if output.is_symlink() or not output.is_file():
                code, error = 1, "fork exited successfully without a regular retro.md recording the project UUID"
            elif identity(project) not in output.read_text() or conflicting_project_uuid(output.read_text(), identity(project)):
                code, error = 1, "retro.md does not identify the selected project UUID consistently"
            else:
                state["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
                if publish is not None:
                    with Path(state["log"]).open("a") as publication_log:
                        with contextlib.redirect_stdout(publication_log), contextlib.redirect_stderr(publication_log):
                            publish(output)
    except PublicationError as failure:
        code, error = 1, str(failure)
        state["status"] = "publication-failed"
    except (OSError, ValueError, ProjectError) as failure:
        code, error = 1, str(failure)
    if code != 0 and not error:
        error = f"retrospective session exited with code {code}; see output_excerpt and the attempt logs"
    final_status = "completed" if code == 0 else "failed"
    if state.get("status") == "publication-failed":
        final_status = "publication-failed"
    state.update(status=final_status, returncode=code,
                 finished_at=datetime.now(timezone.utc).isoformat())
    if error:
        state["error"] = error
    if code != 0:
        excerpt = output_excerpt(Path(state["log"]), Path(state["stderr_log"]))
        if excerpt:
            state["output_excerpt"] = excerpt
    if managed_workspace:
        if code == 0:
            shutil.rmtree(cwd, ignore_errors=True)
        else:
            try:
                (cwd / ".failed").touch()
            except OSError:
                pass
    write_json(state_path, state)
    return code


def execute_retro(layout: Path, project: dict[str, Any], output: Path, command: list[str], cwd: Path,
                  prompt: str, *, timeout: int = 1800,
                  draft: Path | None = None, publish: Callable[[Path], None] | None = None,
                  retry_publish: Callable[[], None] | None = None,
                  environment: dict[str, str] | None = None,
                  managed_workspace: bool = False, prompt_delivery: str = PROMPT_STDIN) -> dict[str, Any]:
    if timeout <= 0:
        raise ProjectError("--timeout must be positive.")
    if prompt_delivery not in (PROMPT_ARGV, PROMPT_STDIN):
        raise ProjectError(f"Unknown prompt delivery: {prompt_delivery}")
    directory = state_directory(layout, project)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"uuid": identity(project), "status": "running", "returncode": 0,
                    "reason": "another retrospective attempt holds the project lock"}
        status, reason = existing_result(layout, project, output)
        if status != "pending":
            return {"uuid": identity(project), "status": status, "reason": reason,
                    "returncode": 0 if status in ("complete", "running") else 1}
        state_path = directory / "state.json"
        previous = read_record(state_path) if state_path.exists() else {}
        if reason == "verified retrospective needs artifact commit retry" and retry_publish is not None:
            try:
                with Path(previous["log"]).open("a") as log:
                    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                        retry_publish()
                previous.update(status="completed", returncode=0, finished_at=datetime.now(timezone.utc).isoformat())
                previous.pop("error", None)
            except (OSError, ProjectError) as error:
                previous.update(returncode=1, error=str(error))
            write_json(state_path, previous)
            return previous
        # A supervisor inherits the lock across caller death. It keeps it through harness exit,
        # timeout cleanup and the durable state write; the caller never explicitly unlocks it.
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            try:
                os._exit(_execute(command, cwd, prompt, directory, project, draft or output, timeout, publish, environment,
                                 private_draft=draft is not None, managed_workspace=managed_workspace,
                                 prompt_delivery=prompt_delivery))
            except BaseException:
                os._exit(1)
        _, wait_status = os.waitpid(pid, 0)
        state_path = directory / "state.json"
        state = read_record(state_path) if state_path.exists() else {"uuid": identity(project)}
        exit_code = os.waitstatus_to_exitcode(wait_status)
        if exit_code != 0 and state.get("status") not in ("failed", "publication-failed"):
            state.update(status="interrupted", returncode=1, error="retrospective supervisor was interrupted")
            write_json(state_path, state)
        elif exit_code == 0 and state.get("status") != "completed":
            state.update(status="interrupted", returncode=1, error="retrospective supervisor left no completion record")
            write_json(state_path, state)
        return state


def pending_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton retro pending",
                                     description="Run missing retros for recently completed archived projects.")
    parser.add_argument("--since", default="3d", help="Completion window, e.g. 3d or 12h (default: 3d)")
    parser.add_argument("--dry-run", action="store_true", help="Discover candidates without launching agents or writing state")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per owner fork (default: 1800)")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            require_trusted_runtime()
        if args.timeout <= 0:
            raise ProjectError("--timeout must be positive.")
        from .manager import Manager
        manager = Manager()
        layout = manager.layout
        if not layout.is_dir():
            raise ProjectError("Project manager v2 registry is unavailable; run baton init explicitly.")
        report = archive_scan(layout, manager.artifact_directory, args.since)
        report["runs"] = []
        if not args.dry_run:
            # Do not load/revalidate active slots. Archived selection reads only their frozen JSON.
            for item in report["candidates"]:
                try:
                    report["runs"].append(manager.retro_noninteractive(item["uuid"], timeout=args.timeout))
                except (ProjectError, OSError, ValueError) as error:
                    report["runs"].append({"uuid": item["uuid"], "status": "failed", "returncode": 1, "error": str(error)})
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for item in report["candidates"]:
                print(f"{'Would run' if args.dry_run else 'Selected'}: {item['name']} ({item['uuid']})")
            for item in report["runs"]:
                detail = item.get("error") or (item.get("reason") if item["status"] == "blocked" else None)
                print(f"{item['uuid']}: {item['status']}" + (f" — {detail}" if detail else ""))
            for item in report["skipped"]:
                if item["status"] in ("blocked", "unowned", "running"):
                    print(f"Skipped {item['name']} ({item['uuid']}): {item['reason']}")
            for item in report["errors"]:
                print(f"Error: {item}", file=sys.stderr)
            print(f"{len(report['candidates'])} candidates; {len(report['skipped'])} skipped; {len(report['errors'])} metadata errors")
        return 1 if report["errors"] or any(item.get("returncode", 0) for item in report["runs"]) else 0
    except (ProjectError, OSError, ValueError) as error:
        print(f"baton retro pending: {error}", file=sys.stderr)
        return 1
