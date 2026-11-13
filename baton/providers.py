"""Built-in registry of the agent CLIs Baton launches.

Every provider-dependent feature (ownership claims, owner resume/fork, interactive
and unattended retrospectives, reviews, model configuration and skill installation)
resolves its CLI here instead of branching on provider names.

Contract for a provider (enforced by tests/test_providers.py):

- ``resume``, ``start`` and, when ``supports_fork``, ``fork`` return interactive argv.
  The prompt is always an argument because stdin is the user's terminal.
- ``fork`` raises ``UnsupportedProviderOperation`` without ``supports_fork``; callers
  then use ``start`` with the fresh-session prompt instead. Baton never resumes the
  real owner session to emulate a fork.
- ``exec_unattended`` returns ``(argv, prompt_delivery)`` for a headless run whose
  working directory is a private workspace. It must keep writes outside that
  workspace blocked by the CLI itself, never use blanket approval or sandbox bypass
  flags, deny rather than prompt, and pass no extra directories. A provider without
  such a mode raises ``UnsupportedProviderOperation``; this is the only operation
  allowed to raise it unconditionally. Settings files can grant tools, so the
  invocation must also keep user and project settings from widening it (Claude's
  ``--restricted``). ``read_confinement`` documents whether the CLI enforces reads
  (``enforced``) or only the prompt asks (``best_effort``).
- ``session_env`` names the variable holding the current session UUID inside the
  agent's shell, or ``None`` when no verified variable exists. Baton scrubs every
  registered ``session_env`` from the CLIs it launches so a child cannot inherit and
  claim the owner's identity.
- ``skill_dir`` is the one home-relative directory Baton installs skills into for
  the harness; ``skill_reads`` lists every user-level skill directory it reads, and
  ``dedupes_skills`` whether it collapses same-named skills found in several of them.
- ``guidance`` gives claim and fresh-subagent instructions served at runtime through
  ``provider_guidance`` so the bundled skills stay provider-neutral.
- ``review`` runs one headless reviewer for ``baton review run`` and the doctor smoke
  test. The base class owns process-group launch, the timeout (SIGTERM, then SIGKILL
  after ``REVIEW_KILL_GRACE_SECS``), empty-output failure, optional output validation
  (``validates_review``; a rejected output becomes ``<output>.invalid``) and the
  ``.failed`` rename of non-empty output from a failed run. Its default invocation is
  ``<binary> <args> [<model_flag> MODEL] <prompt_separator>`` followed by the prompt
  (``prompt_delivery`` argv, with the raw input on stdin) or with the combined
  prompt and input on stdin. Providers override ``run_review`` for another protocol,
  ``review_output`` to name the output file, and ``review_options`` to read
  provider-specific review settings from the environment. Reviewers read untrusted
  diffs and plans, so every review invocation must be read-only (a read-only
  sandbox, permission mode or tool allow-list) and never use blanket approval or
  sandbox bypass flags. ``read_dir_flag`` passes ``request.read_dirs`` (the
  directories a reviewer may read: project repositories and per-file diffs) as ``<flag> DIR`` pairs before
  the prompt separator; providers whose read-only mode already reads any path
  leave it ``None``. agy's added directories rely on headless permissions: it
  auto-denies write and command tools that no ``permissions.allow`` rule in the
  user's agy settings grants, and a denied call ends the run with an empty
  response, which fails the review.
- Every review attempt appends an entry to ``request.attempts`` with the reviewer
  session ID when the CLI exposes one (``review_session`` pre-assigns it; Codex and
  agy report it in their log or stream), and a failed review sets
  ``request.failure`` to one of ``FAILURE_REASONS``.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, ClassVar, TextIO
import uuid

from .errors import ProjectError


class UnsupportedProviderOperation(ProjectError):
    """The provider's CLI cannot perform this operation within Baton's safety floor."""


PROMPT_ARGV = "argv"
PROMPT_STDIN = "stdin"
REVIEW_KILL_GRACE_SECS = 5
REVIEW_SHUTDOWN_GRACE_SECS = 5


class ProcessGroups:
    """Reviewer process groups that a signal handler can kill; once closed, new launches are refused."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pids: set[int] = set()
        self.closed = False

    def launch(self, argv: list[str], **options: Any) -> subprocess.Popen[bytes] | None:
        # Launching under the lock means kill_all either sees a process or prevents it from starting.
        with self._lock:
            if self.closed:
                return None
            process = subprocess.Popen(argv, start_new_session=True, **options)
            self._pids.add(process.pid)
            return process

    def release(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._pids.discard(process.pid)

    def kill_all(self) -> None:
        with self._lock:
            self.closed = True
            for pid in self._pids:
                _kill_group(pid, signal.SIGKILL)


@contextmanager
def kill_groups_on_signals(processes: ProcessGroups, exit_codes: Mapping[int, int]) -> Iterator[None]:
    """While active, each signal in ``exit_codes`` kills every group in ``processes`` and exits with its code.

    Reviewer CLIs run in their own process groups, so a signal to the launching process never reaches
    them; a terminated launcher must kill them itself. Signal handlers belong to the main thread, so
    elsewhere this installs nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(signum: int, frame: object) -> None:
        processes.kill_all()
        raise SystemExit(exit_codes[signum])

    previous = {signum: signal.signal(signum, terminate) for signum in exit_codes}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def _kill_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_unreaped(pid: int, seconds: float) -> None:
    """Waits up to ``seconds`` for a child to exit without reaping it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return
        except ChildProcessError:
            return
        time.sleep(0.05)


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]", "-", re.sub(r"[/\s]", "-", value)))


FAILURE_REASONS = ("timeout", "rate_limited", "auth", "startup_denied", "invalid_output", "empty", "missing_cli",
                   "error")
_AUTH = re.compile(r"\b401\b|unauthori[sz]ed|unauthenticated|not (logged|signed) in|not authenticated|"
                   r"please (run|use) \S*\s*/?login|authentication (failed|required|error)|invalid (x-)?api[ _-]?key|expired (token|credentials)|token (has )?expired|"
                   r"credentials? (are |is )?(missing|invalid|expired)", re.IGNORECASE)
_RATE_LIMITED = re.compile(r"\b429\b|rate[ _-]?limit|too many requests|resource[ _]exhausted|quota|usage limit|"
                           r"overloaded|\b529\b", re.IGNORECASE)
_TRANSIENT = re.compile(r"\b50[0234]\b|service unavailable|bad gateway|gateway time-?out|internal server error|"
                        r"connection (reset|refused|closed|error)|econnreset|stream (error|disconnected)|"
                        r"network error|temporarily unavailable", re.IGNORECASE)


def classify_failure(status: int, lines: list[str]) -> tuple[str, str | None, bool]:
    """Classifies a failed review from its exit status and error lines: ``(reason, detail, retryable)``.

    The most recent recognizable line decides. A rate limit that ran into the deadline is reported as
    ``rate_limited``: it is the root cause. Only rate limits and transient server or network errors are retryable.
    """
    patterns = ((_RATE_LIMITED, "rate_limited"),) if status == 124 else (
        (_AUTH, "auth"), (_RATE_LIMITED, "rate_limited"), (_TRANSIENT, "error"))
    for line in reversed(lines):
        for pattern, reason in patterns:
            if pattern.search(line):
                return reason, line.strip()[:300], reason != "auth"
    if status == 124:
        return "timeout", None, False
    detail = next((line.strip() for line in reversed(lines) if line.strip()), None)
    return "error", detail[:300] if detail else None, False


def _tail_lines(path: Path, limit: int = 65536) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit))
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


@dataclass
class ReviewRequest:
    """One reviewer run. ``input`` is the raw reviewed text; ``combined_input`` holds the prompt plus input."""

    executable: str
    model: str
    input: Path
    combined_input: Path
    prompt: str
    output: Path
    log: Path
    timeout: float
    # A monotonic deadline shared by every attempt (doctor smoke); otherwise each attempt gets ``timeout``.
    deadline: float | None = None
    # Without it, Codex accepts output from a non-zero exit with a warning.
    require_zero_exit: bool = False
    # Returns None for acceptable output, else what the output failed to be ("a complete plan review; ...").
    validator: Callable[[Path], str | None] | None = None
    options: Mapping[str, str] = field(default_factory=dict)
    read_dirs: tuple[Path, ...] = ()
    cwd: Path | None = None
    messages: TextIO | None = None
    processes: ProcessGroups | None = None
    # One entry per launched CLI: session_id, log, cwd and status, plus provider details such as reasoning.
    attempts: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None
    failure_detail: str | None = None
    retryable: bool = False

    def remaining(self) -> float:
        return self.timeout if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def fail(self, reason: str, detail: str | None = None, retryable: bool = False) -> None:
        """Records the first failure reason of this review; later, less specific failures do not replace it."""
        if self.failure is None:
            self.failure, self.failure_detail, self.retryable = reason, detail, retryable

    def attempt(self, session_id: str | None, log: Path, cwd: Path | None, status: int, **details: Any) -> None:
        self.attempts.append({"session_id": session_id, "log": str(log.absolute()),
                              "cwd": str((cwd or self.cwd or Path.cwd()).absolute()), "status": status, **details})

    def say(self, text: str) -> None:
        stream = self.messages or sys.stderr
        stream.write(text + "\n")
        stream.flush()


@dataclass(frozen=True)
class Provider:
    name: str
    binary: str
    model_flag: str = "--model"
    args: tuple[str, ...] = ()
    prompt_separator: tuple[str, ...] = ()
    prompt_delivery: str = PROMPT_ARGV
    default_enabled: bool = True
    session_env: str | None = None
    skill_dir: str = ".agents/skills"
    skill_reads: tuple[str, ...] = (".agents/skills",)
    dedupes_skills: bool = False
    supports_fork: bool = False
    read_confinement: str = "best_effort"
    session_id_source: str = ""
    subagent_guidance: str = ""
    read_dir_flag: str | None = None
    validates_review: ClassVar[bool] = False

    @property
    def env_name(self) -> str:
        """The provider name as it appears in environment variables such as ``<ENV_NAME>_BIN``."""
        return self.name.upper().replace("-", "_")

    @property
    def binary_env(self) -> str:
        return self.env_name + "_BIN"

    def executable(self, environ: Mapping[str, str] | None = None) -> str:
        environment = os.environ if environ is None else environ
        return environment.get(self.binary_env) or self.binary

    def skill_path(self, home: Path | None = None) -> Path:
        return (home or Path.home()) / self.skill_dir

    def skill_read_paths(self, home: Path | None = None) -> tuple[Path, ...]:
        return tuple((home or Path.home()) / directory for directory in self.skill_reads)

    def guidance(self) -> dict[str, str]:
        claim = f"baton session claim --provider {self.name}"
        if self.session_env is None:
            claim += " --session-id <current-session-uuid>"
        return {"claim_command": claim, "session_id_source": self.session_id_source,
                "fresh_subagent": self.subagent_guidance}

    def resume(self, session_id: str, directory: Path) -> list[str]:
        raise NotImplementedError

    def fork(self, session_id: str, directory: Path, prompt: str) -> list[str]:
        raise UnsupportedProviderOperation(f"{self.name} cannot fork a session; start a fresh session instead.")

    def start(self, directory: Path, prompt: str) -> list[str]:
        raise NotImplementedError

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        raise UnsupportedProviderOperation(f"{self.name} has no unattended mode that meets Baton's safety floor.")

    def require_fresh(self, session_id: str | None) -> None:
        if session_id is not None and not self.supports_fork:
            raise UnsupportedProviderOperation(f"{self.name} cannot fork a session; run unattended without one.")

    def review_options(self, prefix: str, environ: Mapping[str, str]) -> dict[str, str]:
        """Provider-specific review settings from ``<prefix>_*`` variables; raises ValueError naming the variable."""
        return {}

    def review_output(self, prefix: str, model: str) -> Path:
        return Path(f"{prefix}-{self.name}.md")

    def read_dir_args(self, request: ReviewRequest) -> list[str]:
        if self.read_dir_flag is None:
            return []
        return [value for directory in request.read_dirs for value in (self.read_dir_flag, str(directory))]

    def review_session(self) -> tuple[list[str], str | None]:
        """Arguments that pre-assign a reviewer session, and its ID; ``([], None)`` when the CLI reports its own."""
        return [], None

    def error_lines(self, request: ReviewRequest) -> list[str]:
        """Lines of the last attempt's diagnostics that may explain a failure (never the echoed prompt)."""
        return _tail_lines(request.log)

    def review(self, request: ReviewRequest) -> int:
        """Runs one reviewer and returns its exit status; non-zero means no usable review at ``request.output``."""
        request.failure = request.failure_detail = None
        request.retryable = False
        status = self.run_review(request)
        output = request.output
        if status == 0 and self.validates_review and request.validator is not None and _nonempty(output):
            problem = request.validator(output)
            if problem is not None:
                invalid = output.with_name(output.name + ".invalid")
                output.replace(invalid)
                request.say(f"Error: {self.name} output is not {problem}.")
                request.say(f"Raw {self.name} output preserved at: {invalid}")
                request.fail("invalid_output", problem)
                status = 1
        if status == 0 and not _nonempty(output):
            request.say(f"Error: {self.name} produced empty output.")
            request.fail("empty")
            status = 1
        if status != 0 and request.failure is None:
            request.fail(*classify_failure(status, self.error_lines(request)))
        if status != 0 and _nonempty(output):
            output.replace(output.with_name(output.name + ".failed"))
        return status

    def run_review(self, request: ReviewRequest) -> int:
        session_args, session_id = self.review_session()
        command = [request.executable, *self.args, *session_args]
        if request.model != "default":
            command += [self.model_flag, request.model]
        command += self.read_dir_args(request)
        command += self.prompt_separator
        stdin = request.input
        if self.prompt_delivery == PROMPT_STDIN:
            stdin = request.combined_input
        else:
            command.append(request.prompt)
        with request.output.open("wb") as output, request.log.open("wb") as log:
            status = run_reviewer_command(command, request, stdin=stdin, stdout=output, stderr=log)
        request.attempt(session_id, request.log, None, status)
        return status


def run_reviewer_command(command: list[str], request: ReviewRequest, *, stdin: Path | None,
                         stdout: IO[bytes], stderr: IO[bytes], cwd: Path | None = None,
                         completed: Callable[[], bool] | None = None) -> int:
    """Runs one CLI in its own process group with the request's timeout; returns 124 on timeout.

    ``cwd`` defaults to the request's working directory.
    """
    timeout = request.remaining()
    deadline = request.deadline if request.deadline is not None else time.monotonic() + timeout
    if completed is not None and timeout <= 0:
        stderr.write(f"Timed out after {_seconds(timeout)}s: {' '.join(command)}\n".encode())
        return 124
    processes = request.processes or ProcessGroups()
    with open(stdin if stdin is not None else os.devnull, "rb") as handle:
        process = processes.launch(command, stdin=handle, stdout=stdout, stderr=stderr,
                                   env=scrubbed_environment(), cwd=cwd or request.cwd)
        if process is None:
            return 124
        if completed is not None:
            return _wait_for_review_completion(process, processes, completed, deadline, timeout, command, stderr)
        try:
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid, signal.SIGTERM)
                _wait_unreaped(process.pid, REVIEW_KILL_GRACE_SECS)
                # Also kills children that ignored TERM after the CLI exited. The unreaped leader keeps
                # the group ID allocated, so this cannot hit a reused process group.
                _kill_group(process.pid, signal.SIGKILL)
                process.wait()
                stderr.write(f"Timed out after {_seconds(math.ceil(timeout))}s: {' '.join(command)}\n".encode())
                stderr.flush()
                return 124
        finally:
            processes.release(process)


def _wait_for_review_completion(process: subprocess.Popen[bytes], processes: ProcessGroups,
                                completed: Callable[[], bool], deadline: float, timeout: float,
                                command: list[str], stderr: IO[bytes]) -> int:
    shutdown_at: float | None = None
    forced = timed_out = False
    try:
        while not processes.closed:
            now = time.monotonic()
            if shutdown_at is None and now >= deadline:
                timed_out = True
                _kill_group(process.pid, signal.SIGTERM)
                _wait_unreaped(process.pid, REVIEW_KILL_GRACE_SECS)
                break
            ready = completed()
            now = time.monotonic()
            if ready and shutdown_at is None:
                if now >= deadline:
                    timed_out = True
                    _kill_group(process.pid, signal.SIGTERM)
                    _wait_unreaped(process.pid, REVIEW_KILL_GRACE_SECS)
                    break
                shutdown_at = min(deadline, now + REVIEW_SHUTDOWN_GRACE_SECS)
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                break
            if shutdown_at is not None and now >= shutdown_at:
                forced = True
                break
            time.sleep(min(0.05, max(0.0, (shutdown_at or deadline) - now)))
    finally:
        # The unreaped leader pins the group ID; unregister it before reaping so cancellation cannot
        # accidentally signal a reused group. This also cleans descendants after an ordinary CLI exit.
        _kill_group(process.pid, signal.SIGKILL)
        processes.release(process)
        status = process.wait()
    if processes.closed:
        return 124
    if timed_out:
        stderr.write(f"Timed out after {_seconds(math.ceil(timeout))}s: {' '.join(command)}\n".encode())
        return 124
    if forced and status == -signal.SIGKILL:
        stderr.write(b"AGY review completed; killed lingering CLI process group after shutdown grace.\n")
        return 0
    return status


class CodexProvider(Provider):
    def resume(self, session_id: str, directory: Path) -> list[str]:
        return [self.executable(), "resume", session_id, "--cd", str(directory)]

    def fork(self, session_id: str, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), "fork", session_id, "--cd", str(directory), prompt]

    def start(self, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), "--cd", str(directory), prompt]

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        # read-only rather than workspace-write: workspace-write also allows writes to /tmp. The approval
        # policy goes through exec's --config: exec ignores a top-level -a, and a resolved on-request policy
        # would route escalations to a configured approvals_reviewer instead of denying them.
        command = [self.executable(), "exec", "--config", 'approval_policy="never"', "-C", str(workspace),
                   "-s", "read-only"]
        if session_id is not None:
            command.append("fork")
        command.append("--skip-git-repo-check")
        if session_id is not None:
            command.append(session_id)
        return [*command, "-"], PROMPT_STDIN

    def review_options(self, prefix: str, environ: Mapping[str, str]) -> dict[str, str]:
        variable = f"{prefix}_CODEX_REASONING"
        reasoning = (environ.get(variable) or environ.get("CODEX_REASONING_EFFORT")
                     or codex_config_reasoning(environ) or "high")
        if reasoning not in CODEX_REASONING:
            raise ValueError(f"{variable} must be one of: {', '.join(CODEX_REASONING)}")
        fallback = environ.get(f"{prefix}_CODEX_AUTO_FALLBACK") or "1"
        if fallback not in ("0", "1"):
            raise ValueError(f"{prefix}_CODEX_AUTO_FALLBACK must be 0 or 1")
        return {"reasoning": reasoning, "auto_fallback": fallback}

    def review_output(self, prefix: str, model: str) -> Path:
        return Path(f"{prefix}-{self.name}-{slugify(model)}.md")

    def error_lines(self, request: ReviewRequest) -> list[str]:
        # The log echoes the prompt and the reviewed text, so only Codex's own error lines count: error-like lines
        # that do not also appear in that input.
        try:
            echoed = set(request.combined_input.read_text(errors="replace").splitlines())
        except OSError:
            echoed = set()
        return [line for line in _tail_lines(request.log)
                if re.match(r"(?:ERROR|Error|error)\b", line) and line not in echoed]

    def run_review(self, request: ReviewRequest) -> int:
        reasoning = request.options.get("reasoning", "high")
        candidates = [reasoning]
        if request.options.get("auto_fallback", "1") == "1":
            candidates += {"high": ["medium", "low"], "medium": ["low"], "minimal": ["low"]}.get(reasoning, [])
        model = [] if request.model == "default" else [self.model_flag, request.model]
        stem = str(request.log).removesuffix(".log")
        status, attempt_log = 1, None
        if request.deadline is not None and request.remaining() <= 0:
            request.say(f"Error: {self.name} timed out after {_seconds(request.timeout)}s.")
            request.fail("timeout")
            return 124
        for index, effort in enumerate(candidates):
            # The reviewer's failure is the last attempt's.
            request.failure = request.failure_detail = None
            request.retryable = False
            attempt_log = Path(f"{stem}-{effort}.log")
            request.output.unlink(missing_ok=True)
            # Reviewers read untrusted diffs and plans, so they get a read-only sandbox and an explicit
            # never policy: without one, exec inherits the user's sandbox and (possibly auto-reviewed) approvals.
            command = [request.executable, "exec", *model, "--config", f'model_reasoning_effort="{effort}"',
                       "--config", 'approval_policy="never"', "-s", "read-only", "--skip-git-repo-check",
                       "--output-last-message", str(request.output), "--color", "never", "-"]
            with attempt_log.open("wb") as log:
                status = run_reviewer_command(command, request, stdin=request.combined_input, stdout=log, stderr=log)
            diagnostics = attempt_log.read_text(errors="replace")
            session = re.search(r"^session id:\s*(\S+)\s*$", diagnostics, re.MULTILINE)
            request.attempt(session.group(1) if session else None, attempt_log, None, status, reasoning=effort)
            # From here on the last attempt's log is the reviewer's log, whatever ends the loop.
            shutil.copyfile(attempt_log, request.log)
            if _nonempty(request.output) and (status == 0 or not request.require_zero_exit):
                Path(f"{stem}-reasoning.txt").write_text(effort + "\n")
                if status != 0:
                    request.say(f"Warning: {self.name} exited with code {status} at reasoning={effort} but produced "
                                "review output; accepting output.")
                return 0
            if status == 124:
                # One deadline covers every attempt, so a timed-out attempt leaves no time for a fallback.
                request.fail(*classify_failure(status, self.error_lines(request)))
                return status
            if request.processes is not None and request.processes.closed:
                # Interrupted: a retry could not launch and its empty log would replace this attempt's.
                break
            if status != 0 and not _nonempty(request.output):
                if (re.search(r"^Error: failed to initialize in-process app-server client: "
                              r"(?:Operation not permitted \(os error 1\)|Permission denied \(os error 13\))$",
                              diagnostics, re.MULTILINE)
                        and not re.search(r"^OpenAI Codex v|^model:", diagnostics, re.MULTILINE)):
                    request.say("Error: codex could not initialize its app-server client because access was denied. "
                                "Outer launch permissions may block startup; retry through the user or harness's "
                                "normal approved launch mechanism. Reasoning fallbacks cannot resolve this; "
                                "the reviewer still requires read-only sandboxing and approval_policy=never.")
                    request.fail("startup_denied")
                    break
                reason, detail, retryable = classify_failure(status, self.error_lines(request))
                if reason == "auth" or retryable:
                    # Lower reasoning cannot fix these; the runner owns the one retry of a rate limit or outage.
                    request.fail(reason, detail, retryable)
                    break
            if status == 0:
                status = 1
                request.say(f"Warning: {self.name} finished with reasoning={effort} but produced empty review output.")
                request.fail("empty")
            if index + 1 < len(candidates):
                if request.remaining() <= 0:
                    request.fail("timeout")
                    return 124
                request.say(f"Warning: {self.name} review failed with reasoning={effort}; "
                            f"retrying with {candidates[index + 1]}.")
        return status


CODEX_REASONING = ("minimal", "low", "medium", "high")


def codex_config_reasoning(environ: Mapping[str, str]) -> str | None:
    """The top-level ``model_reasoning_effort`` from Codex's config.toml; profiles are ignored."""
    home = environ.get("CODEX_HOME") or os.path.join(environ.get("HOME", str(Path.home())), ".codex")
    try:
        lines = (Path(home) / "config.toml").read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if re.match(r"\s*\[", line):
            return None
        if re.match(r"\s*model_reasoning_effort\s*=", line):
            value = re.sub(r"\s*(#.*)?$", "", line.partition("=")[2].lstrip(), count=1)
            return value[1:-1] if re.fullmatch(r'".*"', value) else value
    return None


# --restricted ignores user, project and local settings (whose allow rules would otherwise still apply,
# including a reviewed checkout's own .claude/settings*.json), removes command-running tools and confines
# file tools to the working directories; --strict-mcp-config skips configured MCP servers. Within that,
# dontAsk denies every tool outside the read-only --tools list instead of prompting.
CLAUDE_READ_ONLY = ("--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk",
                    "--permission-prompts", "none", "--tools", "Read,Glob,Grep")


class ClaudeProvider(Provider):
    def resume(self, session_id: str, directory: Path) -> list[str]:
        return [self.executable(), "--resume", session_id]

    def fork(self, session_id: str, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), "--resume", session_id, "--fork-session", prompt]

    def start(self, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), prompt]

    def review_session(self) -> tuple[list[str], str | None]:
        session = str(uuid.uuid4())
        return ["--session-id", session], session

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        command = [self.executable(), "--print"]
        if session_id is not None:
            command += ["--resume", session_id, "--fork-session"]
        # --tools is variadic, so the prompt goes on stdin.
        return [*command, *CLAUDE_READ_ONLY], PROMPT_STDIN


AGY_UNATTENDED_NOTE = (
    "Use only file reading, listing and search tools on files inside your working directory. "
    "Never run shell commands and never write or edit files: any denied tool call aborts this run "
    "without output.\n\n"
)


AGY_REVIEW_NOTE = (
    "The plan or diff is already embedded below, possibly only in part; do not reopen it or follow artifact "
    "links outside the explicitly allowed read roots (your working directory and the supplied repository and "
    "diff directories, where the input lists any per-file diffs that were not embedded). "
    "Use only native file reading, listing, and search tools for this review, including in the supplied "
    "repository directories. Never run shell commands, including read-only git commands or tests, and "
    "never write or edit files. Any denied tool call can end this headless run without a review. If "
    "file tools cannot verify a claim or inspect a Git revision, report that inspection gap and finish "
    "the review from the available evidence.\n\n"
)


def _agy_stream_facts(path: Path) -> tuple[str | None, str | None]:
    """The conversation ID (from the first event) and terminal error payload (near the end) of an agy stream."""
    conversation = error = None
    try:
        with path.open("rb") as handle:
            first = handle.readline(1 << 20).decode("utf-8", errors="replace")
    except OSError:
        first = ""
    for line in [first, *_tail_lines(path)]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        nested = event.get(event.get("event")) if isinstance(event.get("event"), str) else None
        for source in (event, nested if isinstance(nested, dict) else {}):
            if conversation is None and isinstance(source.get("conversation_id"), str):
                conversation = source["conversation_id"]
        result = event.get("result")
        if event.get("event") == "result" and isinstance(result, dict) and result.get("status") != "SUCCESS":
            error = str(result.get("error") or result.get("status"))
    return conversation, error


class AgyProvider(Provider):
    # agy may wrap or truncate the review, so its output must pass the caller's validator.
    validates_review = True

    @staticmethod
    def stream_path(request: ReviewRequest) -> Path:
        return Path(str(request.log.absolute()).removesuffix(".log") + "-stream.jsonl")

    def error_lines(self, request: ReviewRequest) -> list[str]:
        # A rate-limited agy keeps retrying until it is stopped, then reports the error in its terminal result.
        error = _agy_stream_facts(self.stream_path(request))[1]
        return _tail_lines(request.log) + ([error] if error else [])

    def run_review(self, request: ReviewRequest) -> int:
        # The sandbox may write its working directory, so keep it separate from other reviewers' artifacts.
        work = request.output.absolute().with_name(request.output.stem + ".work")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        launched = False
        status = 1
        try:
            status, launched = self._run_review(request, work)
            return status
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if launched:
                conversation = _agy_stream_facts(self.stream_path(request))[0]
                request.attempt(conversation, request.log, work, status)

    def _run_review(self, request: ReviewRequest, work: Path) -> tuple[int, bool]:
        stem = str(request.log.absolute()).removesuffix(".log")
        stream_path = self.stream_path(request)
        with request.output.open("wb") as output, request.log.open("wb") as log, stream_path.open("wb") as stream:
            try:
                content = request.combined_input.read_bytes().decode("utf-8")
            except UnicodeError:
                log.write(b"Error: agy review input is not valid UTF-8.\n")
                request.fail("error", "agy review input is not valid UTF-8")
                return 1, False
            message = {"event": "user", "message": {"content": AGY_REVIEW_NOTE + content}}
            stdin = work / "stdin.jsonl"
            stdin.write_text(json.dumps(message, ensure_ascii=False) + "\n", encoding="utf-8")
            # Retries share the reviewer's deadline, so agy's own timeout is what remains of it.
            command = [request.executable, "--print-timeout", f"{_seconds(math.ceil(request.remaining()))}s",
                       "--log-file", stem + "-cli.log", "--input-format", "stream-json",
                       "--output-format", "stream-json"]
            if request.model != "default":
                command += [self.model_flag, request.model]
            command += self.read_dir_args(request)
            command.append("--sandbox")
            result: dict[str, Any] | None = None
            encoded: bytes | None = None
            pending = b""
            failure = "invalid_output"
            with stream_path.open("rb") as events:
                def observe(*, final: bool = False) -> bool:
                    nonlocal result, encoded, pending, failure
                    pending += events.read()
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    if final and pending:
                        lines.append(pending)
                        pending = b""
                    for line in lines:
                        if not line.strip():
                            continue
                        event = json.loads(line.decode("utf-8"))
                        if not isinstance(event, dict):
                            raise ValueError("expected a JSON event object")
                        if result is not None:
                            raise ValueError("unexpected event after terminal result")
                        if event.get("event") != "result":
                            continue
                        result = event.get("result")
                        if not isinstance(result, dict):
                            raise ValueError("expected a terminal result object")
                        if result.get("status") != "SUCCESS":
                            # Keep the payload: it names the real cause, such as a rate limit.
                            failure = "error"
                            error = result.get("error")
                            raise ValueError(f"terminal result status is not SUCCESS ({result.get('status')!r})"
                                             + (f": {error}" if error else ""))
                        response = result.get("response")
                        if not isinstance(response, str) or not response.strip():
                            failure = "empty"
                            denied = result.get("denied_actions")
                            if isinstance(denied, list) and denied:
                                actions = [{key: item[key] for key in ("action", "display_name")
                                            if isinstance(item.get(key), str)}
                                           for item in denied if isinstance(item, dict)]
                                raise ValueError("terminal result has no nonempty response; headless tool permission "
                                                 "denial: denied_actions=" + json.dumps(actions))
                            raise ValueError("terminal result has no nonempty response")
                        encoded = response.encode("utf-8")
                    return encoded is not None

                try:
                    status = run_reviewer_command(command, request, stdin=stdin, stdout=stream, stderr=log,
                                                  cwd=work, completed=observe)
                    if status != 0:
                        return status, True
                    if not observe(final=True):
                        raise ValueError("missing terminal result")
                except ValueError as error:
                    log.write(f"Error: agy returned an invalid review stream: {error}\n".encode())
                    log.flush()
                    if failure != "error":
                        request.fail(failure, str(error)[:300])
                    return 1, True
            assert encoded is not None
            output.write(encoded)
            return 0, True

    def resume(self, session_id: str, directory: Path) -> list[str]:
        return [self.executable(), "--conversation", session_id]

    def start(self, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), "-i", prompt]

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        self.require_fresh(session_id)
        # Headless agy reads prompts from stdin only as stream-json, so the prompt is an argument.
        return [self.executable(), "--print", AGY_UNATTENDED_NOTE + prompt, "--sandbox"], PROMPT_ARGV


class CursorProvider(Provider):
    def resume(self, session_id: str, directory: Path) -> list[str]:
        return [self.executable(), "--resume", session_id]

    def start(self, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), prompt]

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        self.require_fresh(session_id)
        # Ask mode is read-only and --force is never passed; --trust only accepts the workspace.
        return [self.executable(), "-p", "--mode", "ask", "--sandbox", "enabled", "--trust",
                "--workspace", str(workspace), prompt], PROMPT_ARGV


PROVIDERS: dict[str, Provider] = {
    # No read_dir_flag: the read-only sandbox already reads any path, and codex's --add-dir grants writes.
    "codex": CodexProvider(
        "codex", "codex", session_env="CODEX_THREAD_ID", supports_fork=True,
        skill_dir=".agents/skills", skill_reads=(".agents/skills",),
        session_id_source="CODEX_THREAD_ID in the agent shell holds the current thread ID; claim reads it "
                          "when --session-id is omitted.",
        subagent_guidance="Spawn a `worker` subagent with `fork_context: false` (or `fork_turns: none`) so it "
                          "starts without the parent's conversation."),
    "claude": ClaudeProvider(
        # Reviewers read untrusted input, so they run with the read-only restrictions. --tools is
        # variadic, so the prompt follows the "--" separator.
        "claude", "claude", args=("-p", *CLAUDE_READ_ONLY),
        prompt_separator=("--",), read_dir_flag="--add-dir", session_env="CLAUDE_CODE_SESSION_ID",
        supports_fork=True,
        read_confinement="enforced", skill_dir=".claude/skills", skill_reads=(".claude/skills",),
        session_id_source="CLAUDE_CODE_SESSION_ID in the agent shell holds the current session ID, including "
                          "inside a forked session; claim reads it when --session-id is omitted.",
        subagent_guidance="Use the Agent tool with a named `subagent_type` such as `general-purpose`; never "
                          "use `fork`, which inherits the parent's conversation."),
    "agy": AgyProvider(
        "agy", "agy", read_confinement="enforced", read_dir_flag="--add-dir",
        skill_dir=".gemini/antigravity-cli/skills",
        skill_reads=(".gemini/antigravity-cli/skills", ".gemini/config/skills"),
        session_env="ANTIGRAVITY_CONVERSATION_ID",
        session_id_source="ANTIGRAVITY_CONVERSATION_ID in the agent shell holds the current conversation ID "
                          "(the ID `agy --conversation` resumes); claim reads it when --session-id is omitted.",
        subagent_guidance="Use the `invoke_subagent` tool to start a fresh subagent."),
    "cursor": CursorProvider(
        # Fresh doctor/review directories need workspace trust even in read-only ask mode.
        "cursor", "agent", args=("--print", "--output-format", "text", "--mode", "ask", "--trust"),
        prompt_delivery=PROMPT_STDIN, default_enabled=False, read_dir_flag="--add-dir",
        session_env="CURSOR_CONVERSATION_ID",
        skill_dir=".agents/skills",
        skill_reads=(".cursor/skills", ".agents/skills", ".claude/skills", ".codex/skills"), dedupes_skills=True,
        session_id_source="CURSOR_CONVERSATION_ID in the agent shell holds the current chat ID (the ID "
                          "/copy-conversation-id copies); claim reads it when --session-id is omitted.",
        subagent_guidance="Use a Task-tool subagent, which starts with a clean context."),
}
REVIEW_MODEL_OPTIONS = ("plan_review_model", "implementation_review_model")
MODEL_OPTIONS = (*REVIEW_MODEL_OPTIONS, "worker_model")
MODEL_TIERS = ("strong", "medium", "weak")


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ProjectError(f"Unsupported provider: {name}; expected {', '.join(PROVIDERS)}") from None


def session_variables() -> set[str]:
    return {entry.session_env for entry in PROVIDERS.values() if entry.session_env}


def scrubbed_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a launched CLI without any inherited session identity."""
    variables = session_variables()
    return {key: value for key, value in (os.environ if environ is None else environ).items() if key not in variables}


def provider_guidance() -> dict[str, dict[str, str]]:
    return {name: entry.guidance() for name, entry in PROVIDERS.items()}


def provider_settings() -> dict[str, dict[str, Any]]:
    return {name: {"binary": entry.binary, "binary_env": entry.binary_env, "session_env": entry.session_env,
                   "supports_fork": entry.supports_fork, "read_confinement": entry.read_confinement,
                   "default_enabled": entry.default_enabled, "skill_dir": entry.skill_dir,
                   "skill_reads": list(entry.skill_reads), "dedupes_skills": entry.dedupes_skills,
                   "model_flag": entry.model_flag,
                   "review": {"args": list(entry.args), "prompt_separator": list(entry.prompt_separator),
                              "prompt_delivery": entry.prompt_delivery},
                   "guidance": entry.guidance()}
            for name, entry in PROVIDERS.items()}


def default_review_models() -> dict[str, str]:
    return {name: "default" for name, provider in PROVIDERS.items() if provider.default_enabled}


def validate_review_models(key: str, models: Any) -> None:
    if not isinstance(models, dict):
        raise ValueError(f"{key} must map providers to nonblank model names")
    for provider, model in models.items():
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider in {key}: {provider}; expected {', '.join(PROVIDERS)}")
        if (not isinstance(model, str) or not model.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in model)):
            raise ValueError(f"{key}.{provider} must be a nonblank model name without control characters")


def validate_model_tiers(tiers: Any) -> None:
    if not isinstance(tiers, dict):
        raise ValueError("model_tiers must map providers to model tier mappings")
    for provider, mapping in tiers.items():
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider in model_tiers: {provider}")
        if not isinstance(mapping, dict):
            raise ValueError(f"model_tiers.{provider} must map strong, medium, or weak to model names")
        for tier, model in mapping.items():
            if tier not in MODEL_TIERS:
                raise ValueError(f"Unknown model tier in model_tiers.{provider}: {tier}; expected {', '.join(MODEL_TIERS)}")
            validate_review_models("model_tiers", {provider: model})
            if model in (*MODEL_TIERS, "default"):
                raise ValueError(f"model_tiers.{provider}.{tier} must name an actual model, not {model}")


def resolve_review_model(provider: str, model: str, model_tiers: dict[str, dict[str, str]]) -> str:
    validate_review_models("review_model", {provider: model})
    validate_model_tiers(model_tiers)
    return _resolve_model(provider, model, model_tiers)


def _resolve_model(provider: str, model: str, model_tiers: dict[str, dict[str, str]]) -> str:
    if model not in MODEL_TIERS:
        return model
    mapped = model_tiers.get(provider, {}).get(model)
    if mapped is None:
        raise ValueError(f"Model tier {model!r} has no mapping for provider {provider}; "
                         f"set --model-tier {provider}.{model}=MODEL")
    return mapped


def _checked_models(config: dict[str, Any], key: str) -> tuple[dict[str, str], dict[str, str]]:
    """Validates one model setting once and returns its effective selections and their resolved models."""
    if key not in MODEL_OPTIONS:
        raise ValueError(f"Unknown model configuration: {key}")
    models = config.get(key, {})
    validate_review_models(key, models)
    tiers = config.get("model_tiers", {})
    validate_model_tiers(tiers)
    resolved = {}
    for provider, model in models.items():
        try:
            resolved[provider] = _resolve_model(provider, model, tiers)
        except ValueError as error:
            raise ValueError(f"{key}.{provider}: {error}") from error
    effective = dict(models) if key in REVIEW_MODEL_OPTIONS and key in config else default_review_models() | models
    # Built-in defaults are "default", which needs no resolution.
    return effective, {provider: resolved.get(provider, model) for provider, model in effective.items()}


def effective_models(config: dict[str, Any], key: str) -> dict[str, str]:
    return _checked_models(config, key)[0]


def resolved_models(config: dict[str, Any], key: str) -> dict[str, str]:
    return _checked_models(config, key)[1]


def _require_review_key(key: str) -> None:
    if key not in REVIEW_MODEL_OPTIONS:
        raise ValueError(f"Unknown review model configuration: {key}")


def effective_review_models(config: dict[str, Any], key: str) -> dict[str, str]:
    _require_review_key(key)
    return effective_models(config, key)


def resolved_review_models(config: dict[str, Any], key: str) -> dict[str, str]:
    _require_review_key(key)
    return resolved_models(config, key)


def effective_worker_models(config: dict[str, Any]) -> dict[str, str]:
    return effective_models(config, "worker_model")


def resolved_worker_models(config: dict[str, Any]) -> dict[str, str]:
    return resolved_models(config, "worker_model")
