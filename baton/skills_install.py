"""Install bundled skills without replacing unmanaged user files."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Callable, Iterable

from .configuration import effective_paths, read_raw_config
from .doctor_freshness import require_trusted_runtime
from .providers import PROVIDERS, Provider


MANIFEST = ".baton-skills.json"
WORKFLOW = "baton-workflow"
SLOTS_ROOT_PLACEHOLDER = "{{BATON_SLOTS_ROOT}}"
# Today's targets stay installed even when their harness is absent.
FLOOR = (".agents/skills", ".claude/skills")
SKILLS = ("baton-plan", "baton-peer-review", "baton-review-executor", "baton-review-loop", "baton-retro", WORKFLOW)
# Skills earlier releases installed; a reinstall removes their links when they still point where the manifest says.
RETIRED_SKILLS = ("baton-plan-review-executor", "baton-peer-review-executor")


def bundled_skills() -> Path:
    bundled = Path(str(files("baton").joinpath("skills")))
    if bundled.is_dir():
        return bundled
    source = Path(__file__).resolve().parents[1] / "agent-skills"
    if source.is_dir():
        return source
    raise RuntimeError("Bundled skills are missing; reinstall baton")


def _manifest(target: Path) -> dict[str, Any]:
    path = target / MANIFEST
    if not path.exists():
        return {"links": {}, "workflow": None}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("format") not in (1, 2) or not isinstance(data.get("links"), dict):
        raise ValueError(f"Invalid skill ownership manifest: {path}")
    links = data["links"]
    if any(name not in (*SKILLS, *RETIRED_SKILLS) or not isinstance(source, str) for name, source in links.items()):
        raise ValueError(f"Invalid managed skill links: {path}")
    workflow = data.get("workflow") if data["format"] == 2 else None
    if data["format"] == 2:
        if (WORKFLOW in links or not isinstance(workflow, dict)
                or not isinstance(workflow.get("sha256"), str)
                or len(workflow["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in workflow["sha256"])
                or not isinstance(workflow.get("links"), dict)
                or any(not isinstance(name, str) or name in ("", ".", "..", "SKILL.md")
                       or Path(name).name != name or not isinstance(destination, str)
                       for name, destination in workflow["links"].items())):
            raise ValueError(f"Invalid managed workflow: {path}")
    return {"links": links, "workflow": workflow}


def _check_workflow(path: Path, ownership: dict[str, Any]) -> None:
    if (path.is_symlink() or not path.is_dir()
            or {child.name for child in path.iterdir()} != {"SKILL.md", *ownership["links"]}
            or (path / "SKILL.md").is_symlink() or not (path / "SKILL.md").is_file()
            or hashlib.sha256((path / "SKILL.md").read_bytes()).hexdigest() != ownership["sha256"]
            or any(not (path / name).is_symlink() or os.readlink(path / name) != destination
                   for name, destination in ownership["links"].items())):
        raise ValueError(f"Refusing to replace modified managed skill: {path}; move it explicitly before installing")


def _workflow_content(source: Path) -> str:
    slots = effective_paths(read_raw_config(), keys=("slots_root",))["slots_root"]
    content = (source / WORKFLOW / "SKILL.md").read_text()
    header, separator, body = content.partition("\n---\n")
    if not header.startswith("---\n") or not separator:
        raise ValueError(f"Invalid workflow frontmatter: {source / WORKFLOW / 'SKILL.md'}")
    lines = header.splitlines()
    descriptions = [index for index, line in enumerate(lines) if line.startswith("description:")]
    if len(descriptions) != 1:
        raise ValueError(f"Expected one workflow description: {source / WORKFLOW / 'SKILL.md'}")
    description = json.loads(lines[descriptions[0]].partition(":")[2])
    if not isinstance(description, str) or description.count(SLOTS_ROOT_PLACEHOLDER) != 1:
        raise ValueError(f"Expected one {SLOTS_ROOT_PLACEHOLDER} placeholder in workflow description: "
                         f"{source / WORKFLOW / 'SKILL.md'}")
    lines[descriptions[0]] = "description: " + json.dumps(description.replace(SLOTS_ROOT_PLACEHOLDER, str(slots)))
    return "\n".join(lines) + separator + body


def install_skills(targets: list[Path], source: Path | None = None) -> list[Path]:
    source = (source or bundled_skills()).resolve()
    for name in SKILLS:
        if not (source / name / "SKILL.md").is_file():
            raise ValueError(f"Missing skill: {source / name / 'SKILL.md'}")
    content = _workflow_content(source)
    workflow = {"sha256": hashlib.sha256(content.encode()).hexdigest(), "links": {
        path.name: str(path) for path in sorted((source / WORKFLOW).iterdir()) if path.name != "SKILL.md"}}
    targets = sorted({target.expanduser().resolve() for target in targets})
    installed = []
    with ExitStack() as stack:
        for target in targets:
            target.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context((target / ".baton-skills.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        manifests = {target: _manifest(target) for target in targets}
        for target in targets:
            for name in SKILLS:
                path = target / name
                if path.exists() or path.is_symlink():
                    if name == WORKFLOW and manifests[target]["workflow"] is not None:
                        _check_workflow(path, manifests[target]["workflow"])
                        continue
                    previous = manifests[target]["links"].get(name)
                    if not path.is_symlink() or previous is None or os.readlink(path) != previous:
                        raise ValueError(f"Refusing to replace unmanaged skill: {path}; move it explicitly before installing")
        for target in targets:
            for name in RETIRED_SKILLS:
                path = target / name
                previous = manifests[target]["links"].get(name)
                if previous is not None and path.is_symlink() and os.readlink(path) == previous:
                    path.unlink()
            links = {}
            for name in SKILLS:
                path = target / name
                if name == WORKFLOW:
                    with tempfile.TemporaryDirectory(dir=target, prefix=".workflow-install-") as directory:
                        staged = Path(directory) / WORKFLOW
                        staged.mkdir()
                        (staged / "SKILL.md").write_text(content)
                        for child, destination in workflow["links"].items():
                            (staged / child).symlink_to(destination, target_is_directory=(source / WORKFLOW / child).is_dir())
                        if path.is_symlink():
                            path.unlink()
                        elif path.exists():
                            for child in ("SKILL.md", *manifests[target]["workflow"]["links"]):
                                (path / child).unlink()
                            path.rmdir()
                        staged.replace(path)
                else:
                    destination = str(source / name)
                    temporary = target / f".{name}.install-{os.getpid()}"
                    try:
                        temporary.symlink_to(destination, target_is_directory=True)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    links[name] = destination
                installed.append(path)
            with tempfile.NamedTemporaryFile(mode="w", dir=target, prefix=".skills-manifest-", delete=False) as handle:
                temporary_manifest = Path(handle.name)
                json.dump({"format": 2, "links": links, "workflow": workflow}, handle, indent=2)
                handle.write("\n")
            try:
                temporary_manifest.replace(target / MANIFEST)
            finally:
                temporary_manifest.unlink(missing_ok=True)
    return installed


def provider_present(provider: Provider, home: Path) -> bool:
    return shutil.which(provider.executable()) is not None or provider.skill_path(home).is_dir()


def default_targets(home: Path | None = None, present: Callable[[Provider, Path], bool] = provider_present
                    ) -> tuple[list[Path], list[str]]:
    """Return the default targets: the floor plus the skill_dir of every present provider, and skip notes."""
    home = home or Path.home()
    targets = [home / directory for directory in FLOOR]
    skipped: dict[Path, list[str]] = {}
    for provider in PROVIDERS.values():
        directory = provider.skill_path(home)
        if directory in targets:
            continue
        if present(provider, home):
            targets.append(directory)
            skipped.pop(directory, None)
        else:
            skipped.setdefault(directory, []).append(provider.name)
    notes = [f"Skipped {directory}: {', '.join(names)} not found ({' or '.join(PROVIDERS[name].executable() for name in names)} "
             "is not on PATH and the directory does not exist)"
             for directory, names in skipped.items() if directory not in targets]
    return targets, notes


def discovery_conflicts(targets: Iterable[Path], home: Path | None = None) -> list[str]:
    """Report harnesses that would discover a Baton skill more than once from these targets."""
    resolved = {target.expanduser().resolve() for target in targets}
    conflicts = []
    for provider in PROVIDERS.values():
        seen = sorted({path for path in (read.resolve() for read in provider.skill_read_paths(home)) if path in resolved})
        if len(seen) > 1 and not provider.dedupes_skills:
            conflicts.append(f"{provider.name} reads Baton skills from several targets without deduplicating them: "
                             + ", ".join(map(str, seen)))
    return conflicts


def unmanaged_copies(targets: Iterable[Path], home: Path | None = None) -> list[str]:
    """Report baton-* skills in harness skill directories that this install does not manage."""
    resolved = {target.expanduser().resolve() for target in targets}
    readers: dict[Path, list[str]] = {}
    for provider in PROVIDERS.values():
        for directory in provider.skill_read_paths(home):
            readers.setdefault(directory, []).append(provider.name)
    warnings = []
    for directory, names in readers.items():
        if directory.resolve() in resolved or (directory / MANIFEST).exists() or not directory.is_dir():
            continue
        copies = sorted(path.name for path in directory.iterdir() if path.name.startswith("baton-"))
        if copies:
            warnings.append(f"{directory} contains baton-* skills Baton does not manage ({', '.join(copies)}). "
                            f"{', '.join(names)} read that directory, so a stale copy can shadow the installed skills; "
                            "remove it explicitly if it is outdated.")
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton skills", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install or update bundled baton-* skills")
    install.add_argument("--target", action="append", type=Path,
                         help="Skill directory; repeat for multiple targets (default: ~/.agents/skills, "
                              "~/.claude/skills, and the skill directory of every other detected provider)")
    args = parser.parse_args(argv)
    if args.target:
        targets, notes = args.target, []
    else:
        targets, notes = default_targets()
    try:
        require_trusted_runtime()
        for path in install_skills(targets):
            print(f"Installed {path}")
        for note in notes:
            print(note)
        # Both checks return bare messages; the prefix is added only here.
        for warning in discovery_conflicts(targets) + unmanaged_copies(targets):
            print(f"Warning: {warning}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
