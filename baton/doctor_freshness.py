"""Build provenance, write authorization, and local mainline freshness checks."""

from importlib import metadata, resources
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .errors import ProjectError
from .gitutil import git


def _check(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"name": "version", "status": status, "message": message, "details": details}


def installed_provenance() -> tuple[dict[str, Any], Path | None]:
    """Read wheel metadata, or identify direct execution from a source checkout."""
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "pyproject.toml").is_file() or (source_root / ".git").exists():
        checkout = git(source_root, "rev-parse", "--show-toplevel")
        if Path(checkout).resolve() != source_root:
            raise ProjectError(f"Baton source is not an exact Git checkout root: {source_root}")
        revision = git(source_root, "rev-parse", "--verify", "HEAD", check=False)
        dirty = bool(git(source_root, "status", "--porcelain", "--untracked-files=all"))
        branch = git(source_root, "symbolic-ref", "--quiet", "HEAD", check=False)
        return {"revision": revision, "dirty": dirty, "branch": branch, "execution": "source"}, source_root
    try:
        info = json.loads(resources.files("baton").joinpath("_build_info.json").read_text())
    except (FileNotFoundError, ValueError):
        info = {}
    source = None
    try:
        direct = json.loads(metadata.distribution("baton").read_text("direct_url.json") or "{}")
        location = urlsplit(direct.get("url", ""))
        if location.scheme == "file" and location.netloc in ("", "localhost"):
            source = Path(unquote(location.path))
    except (metadata.PackageNotFoundError, ValueError, TypeError, AttributeError):
        pass
    return info if isinstance(info, dict) else {}, source


def require_trusted_runtime() -> None:
    if os.environ.get("BATON_ALLOW_UNTRUSTED_WRITES") == "1":
        return
    try:
        provenance, _source = installed_provenance()
    except (OSError, ValueError, ProjectError) as error:
        raise ValueError(f"Cannot verify Baton runtime for writes: {error}") from error
    revision = provenance.get("revision")
    if (isinstance(revision, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision)
            and provenance.get("dirty") is False and provenance.get("branch") == "refs/heads/main"):
        return
    raise ValueError("Refusing writes from an untrusted Baton runtime; requires a clean revision of main. "
                     "Run install.sh from a clean main checkout to install a trusted build.")


def compare_revision(source: Path, provenance: dict[str, Any], ref: str | None = None) -> dict[str, Any]:
    """Compare a known build with a real Git repository without fetching or updating it."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        return _check("fail", f"Framework source directory does not exist: {source}; use --source PATH.")
    root = git(source, "rev-parse", "--show-toplevel", check=False)
    if not root or Path(root).resolve() != source:
        return _check("fail", f"Framework source is not a Git checkout root: {source}; use --source PATH.")
    if not (source / "baton" / "cli.py").is_file() or not (source / "pyproject.toml").is_file():
        return _check("fail", f"Source is not a baton framework checkout: {source}; use --source PATH.")
    selected = ref
    if selected is None:
        selected = next((candidate for candidate in ("refs/heads/main", "refs/heads/master")
                         if git(source, "rev-parse", "--verify", candidate, check=False)), None)
        if selected is None:
            selected = git(source, "symbolic-ref", "refs/remotes/origin/HEAD", check=False) or None
    if selected is None:
        return _check("fail", "Cannot find local main or master; use --ref REF with --source PATH.", source=str(source))
    target = git(source, "rev-parse", "--verify", "--end-of-options", f"{selected}^{{commit}}", check=False)
    if not target:
        return _check("fail", f"Cannot resolve mainline ref {selected!r} in {source}.", source=str(source), ref=selected)
    revision = provenance.get("revision")
    details = {"source": str(source), "ref": selected, "mainline_revision": target,
               "installed_revision": revision, "built_dirty": provenance.get("dirty"),
               "comparison": "local refs only; no fetch performed"}
    if not isinstance(revision, str) or not revision or type(provenance.get("dirty")) is not bool:
        return _check("fail", "Installed build has no verifiable Git provenance; reinstall with the framework's install.sh.", **details)
    if provenance["dirty"]:
        return _check("fail", "Framework was built from a dirty checkout; commit changes and reinstall to verify its version.", **details)
    known = git(source, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", check=False)
    if known != revision:
        return _check("fail", "Installed revision is unavailable in the source checkout; select the correct --source or reinstall.", **details)
    if revision == target:
        return _check("pass", f"Installed framework matches {selected} ({target[:12]}); compared local refs without fetching.", **details)
    mainline_only = git(source, "rev-list", "--count", f"{revision}..{target}", check=False)
    installed_only = git(source, "rev-list", "--count", f"{target}..{revision}", check=False)
    if mainline_only not in ("", "0") and installed_only == "0":
        return _check("fail", f"Installed framework is out of date with {selected}; run the framework's install.sh.", **details)
    return _check("fail", f"Installed framework differs from {selected}; install from that ref to use the mainline version.", **details)


def freshness_check(source: Path | None = None, ref: str | None = None) -> dict[str, Any]:
    try:
        provenance, discovered = installed_provenance()
        selected = source if source is not None else discovered
        if selected is None:
            return _check("fail", "Cannot locate the framework's source checkout; use --source PATH to compare with local main.")
        return compare_revision(Path(selected), provenance, ref)
    except (OSError, ValueError, ProjectError) as error:
        return _check("fail", f"Cannot verify installed framework version: {error}")
