"""Validate provider model references and smoke-test each provider's review invocation."""

from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import tempfile
import time
from typing import Any

from .providers import (REVIEW_MODEL_OPTIONS, PROVIDERS, ProcessGroups, ReviewRequest, kill_groups_on_signals,
                        resolved_review_models, resolved_worker_models, tier_effort, validate_model_tier_effort,
                        validate_model_tiers)
from .review_runner import KINDS

# Smoke tests run reviewers like implementation reviews, so they honor the same provider settings.
SMOKE_ENV_PREFIX = KINDS["implementation"].env_prefix


def _snippet(text: str) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*)\S+", r"\1[redacted]", text)
    return text.strip()[-2000:]


SmokeCase = tuple[str, str, str | None]


def _case_name(case: SmokeCase) -> str:
    provider, model, effort = case
    return f"model:{provider}:{model}" + (f"@{effort}" if effort else "")


def _run_smoke_case(case: SmokeCase, timeout: int, processes: ProcessGroups) -> dict[str, Any]:
    provider, model, effort = case
    entry = PROVIDERS[provider]
    name = _case_name(case)
    binary = entry.executable()
    executable = shutil.which(binary)
    if executable is None:
        return {"name": name, "status": "fail", "message": f"CLI not found: {binary}"}
    # Absolute but not resolved: a symlinked CLI may depend on the name it is invoked by.
    executable = str(Path(executable).absolute())
    # A tier case checks whether the model accepts the tier's own effort, so neither a per-run review
    # setting nor a lower fallback may stand in for it.
    environ = os.environ if effort is None else {
        key: value for key, value in os.environ.items() if not key.startswith(SMOKE_ENV_PREFIX + "_")}
    try:
        options = entry.review_options(SMOKE_ENV_PREFIX, environ, effort)
    except ValueError as error:
        return {"name": name, "status": "fail", "message": f"{provider} smoke test cannot run: {error}"}
    if effort is not None:
        options = {**options, "auto_fallback": "0"}
    at_effort = f" at reasoning effort {effort}" if effort is not None else ""
    nonce = "BATON_DOCTOR_" + secrets.token_hex(12)
    prompt = ("This is a CLI connectivity smoke test. Do not use tools, read other files, or modify files. "
              f"Answer with exactly this text and nothing else: {nonce}")
    messages = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="baton-doctor-") as directory:
        root = Path(directory)
        prompt_path = root / "input.txt"
        prompt_path.write_text(prompt + "\n")
        output_path = root / "output.txt"
        request = ReviewRequest(
            executable=executable, model=model, input=prompt_path, combined_input=prompt_path, prompt=prompt,
            output=output_path, log=root / "smoke.log", timeout=timeout, deadline=time.monotonic() + timeout,
            require_zero_exit=True, options=options, cwd=root, messages=messages, processes=processes,
            validator=lambda path: None if path.read_text(errors="replace").strip() == nonce else "the expected smoke response")
        status = entry.review(request)
        output = output_path.read_text(errors="replace") if output_path.is_file() else ""
        if status == 0 and output.strip() == nonce:
            return {"name": name, "status": "pass",
                    "message": f"{provider} accepted {model!r}{at_effort} and answered the smoke prompt"}
        logs = [messages.getvalue()]
        for path in sorted(root.glob("*.log")):
            logs.append(path.read_text(errors="replace"))
        message = (f"{provider} smoke test failed for {model!r}{at_effort} (exit {status})"
                   if status else
                   f"{provider} smoke response did not match the expected nonce for {model!r}{at_effort}")
        diagnostic = _snippet("\n".join(logs))
        record = {"name": name, "status": "fail", "message": message}
        if diagnostic:
            record["details"] = diagnostic
        return record


def _smoke_case(case: SmokeCase, timeout: int, processes: ProcessGroups) -> dict[str, Any]:
    try:
        return _run_smoke_case(case, timeout, processes)
    except OSError as error:
        provider = case[0]
        return {"name": _case_name(case), "status": "fail",
                "message": f"Cannot run {provider} smoke test: {error}"}


def model_checks(config: dict[str, Any], *, smoke: bool = True, timeout: int = 60) -> list[dict[str, Any]]:
    """Check every effective selection and configured tier target once per provider/model.

    A tier target runs with its tier's reasoning effort, so tiers that share a model at different efforts
    are separate cases.
    """
    try:
        if not isinstance(config, dict):
            raise ValueError("Model configuration must be an object")
        tiers = config.get("model_tiers", {})
        validate_model_tiers(tiers)
        efforts = config.get("model_tier_effort", {})
        validate_model_tier_effort(efforts)
        cases: dict[SmokeCase, None] = {}
        for provider, mapping in tiers.items():
            for tier, model in mapping.items():
                cases[provider, model, tier_effort(provider, tier, efforts)] = None
        targets = {(provider, model) for provider, model, _ in cases}
        selections = [pair for key in REVIEW_MODEL_OPTIONS for pair in resolved_review_models(config, key).items()]
        selections += [pair for pair in resolved_worker_models(config).items() if pair[1] != "default"]
        for provider, model in selections:
            # A tier target is already checked at its tier's effort.
            if (provider, model) not in targets:
                cases[provider, model, None] = None
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("Smoke timeout must be a positive integer")
    except (ValueError, TypeError) as error:
        return [{"name": "models:configuration", "status": "fail", "message": str(error)}]
    checks: list[dict[str, Any]] = [{"name": "models:configuration", "status": "pass",
                                    "message": "Review and worker models and tier mappings are valid"}]
    if not smoke:
        for provider in dict.fromkeys(provider for provider, _, _ in cases):
            binary = PROVIDERS[provider].executable()
            available = shutil.which(binary) is not None
            checks.append({"name": f"command:{provider}", "status": "pass" if available else "fail",
                           "message": f"CLI available: {binary}" if available else f"CLI not found: {binary}"})
        checks.append({"name": "models:smoke", "status": "skip", "message": "CLI/model smoke tests were disabled"})
        return checks
    processes = ProcessGroups()
    with kill_groups_on_signals(processes, {signal.SIGTERM: 124, signal.SIGINT: 130}):
        with ThreadPoolExecutor(max_workers=4) as executor:
            checks.extend(executor.map(lambda case: _smoke_case(case, timeout, processes), cases))
    return checks
