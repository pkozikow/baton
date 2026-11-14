"""Persistent paths, repository aliases, and provider models; environment variables override paths."""

import argparse
import copy
from contextlib import redirect_stdout
from collections.abc import Mapping
import json
import os
import re
from pathlib import Path
import sys
from typing import Any

from .doctor_freshness import require_trusted_runtime
from .errors import ProjectError
from .gitutil import write_json
from .review_loop import effective_review_loop, review_focus_catalog, resolved_focused_review_models, review_project
from .providers import (MODEL_OPTIONS, MODEL_TIERS, REVIEW_MODEL_OPTIONS, default_review_models, effective_models,
                        provider_settings, resolved_models, resolved_worker_efforts, validate_model_tier_effort,
                        validate_model_tiers, validate_review_models, validate_tier_names)


PATH_OPTIONS = ("projects_repo", "slots_root", "worktrees_root", "repos_root", "artifacts_root")
REPOSITORY_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"


def validate_repo_aliases(aliases: Any) -> None:
    if not isinstance(aliases, dict) or any(
        not isinstance(alias, str) or not isinstance(target, str)
        or not re.fullmatch(REPOSITORY_NAME_PATTERN, alias)
        or not re.fullmatch(REPOSITORY_NAME_PATTERN, target)
        for alias, target in aliases.items()
    ):
        raise ValueError("repo_aliases must map simple repository names to simple repository names")


def configured_branch_prefix(config: dict[str, Any]) -> str:
    value = config.get("branch_prefix", "baton")
    if not isinstance(value, str):
        raise ValueError("branch_prefix must be a slash-separated namespace of letters/digits/dashes/underscores")
    value = value.removesuffix("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*", value):
        raise ValueError("branch_prefix must be a slash-separated namespace of letters/digits/dashes/underscores")
    return value


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    home = Path(environment.get("HOME", str(Path.home())))
    if "BATON_CONFIG_FILE" in environment:
        return Path(environment["BATON_CONFIG_FILE"]).expanduser()
    directory = Path(environment.get("XDG_CONFIG_HOME", str(home / ".config"))).expanduser()
    return directory / "baton" / "config.json"


def read_raw_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError(f"Expected an object in {path}")
    return config


def validate_paths(paths: Any) -> None:
    if (not isinstance(paths, dict) or any(key not in PATH_OPTIONS or not isinstance(value, str)
                                          or not value.strip() for key, value in paths.items())):
        raise ValueError("Invalid paths configuration")


def effective_paths(config: dict[str, Any], environ: Mapping[str, str] | None = None,
                    *, keys: tuple[str, ...] = PATH_OPTIONS) -> dict[str, Path]:
    environment = os.environ if environ is None else environ
    paths = config.get("paths", {})
    validate_paths(paths)
    dev = Path(environment.get("HOME", str(Path.home()))) / "dev"
    defaults = {"projects_repo": dev / "baton-projects", "slots_root": dev / "baton-wt",
                "worktrees_root": dev / "baton-worktrees", "repos_root": dev,
                "artifacts_root": dev / "baton-artifacts/projects"}
    return {key: Path(environment.get("BATON_REPO" if key == "projects_repo" else
                                     "BATON_" + key.upper(), paths.get(key, default))).expanduser().resolve()
            for key, default in defaults.items() if key in keys}


def read_config(*, validate_model_references: bool = True) -> dict[str, Any]:
    config = read_raw_config()
    try:
        validate_paths(config.get("paths", {}))
    except ValueError as error:
        raise ValueError(f"{error} in {config_path()}") from error
    configured_branch_prefix(config)
    validate_repo_aliases(config.get("repo_aliases", {}))
    validate_model_tiers(config.get("model_tiers", {}))
    validate_model_tier_effort(config.get("model_tier_effort", {}))
    for key in MODEL_OPTIONS:
        validate_review_models(key, config.get(key, {}))
        if validate_model_references:
            effective_models(config, key)
    return config


def _tier_changes(key: str, flag: str, value_name: str, assignments: list[str],
                  removals: list[str]) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """Parses repeatable ``PROVIDER.TIER=VALUE`` assignments and ``PROVIDER[.TIER]`` removals of per-tier map ``key``."""
    changes: dict[str, dict[str, str]] = {}
    for value in assignments:
        name, separator, target = value.partition("=")
        provider, dot, tier = name.partition(".")
        if not separator or not dot:
            raise ValueError(f"--{flag} must use PROVIDER.TIER={value_name}")
        changes.setdefault(provider, {})[tier] = target
    removed: dict[str, set[str]] = {}
    for value in removals:
        provider, dot, tier = value.partition(".")
        if dot and not tier:
            raise ValueError(f"--remove-{flag} must use PROVIDER or PROVIDER.TIER")
        tiers = {tier} if dot else set(MODEL_TIERS)
        validate_tier_names(key, {provider: dict.fromkeys(tiers)})
        removed.setdefault(provider, set()).update(tiers)
        if tiers & set(changes.get(provider, {})):
            raise ValueError(f"Cannot set and remove the same {flag.replace('-', ' ')} for {provider} in one command")
    return changes, removed


def _apply_tier_changes(config: dict[str, Any], key: str, changes: dict[str, dict[str, str]],
                        removed: dict[str, set[str]]) -> None:
    saved = config.get(key, {})
    for provider, tiers in removed.items():
        if provider in saved:
            for tier in tiers:
                saved[provider].pop(tier, None)
            if not saved[provider]:
                saved.pop(provider)
    for provider, mappings in changes.items():
        config.setdefault(key, {}).setdefault(provider, {}).update(mappings)


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton config", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    shower = commands.add_parser("show", help="Show effective paths, branch prefix, aliases, models, providers, and review loop")
    initializer = commands.add_parser("init", help="Initialize model and review-loop defaults while preserving saved settings")
    setter = commands.add_parser("set", help="Save paths, branch prefix, aliases, models, and review-loop settings")
    shower.add_argument("--selector")
    shower.add_argument("--section", action="append", choices=("paths", "branch_prefix", "providers", "worker_model",
                                                               "resolved_worker_model", "resolved_worker_effort"))
    initializer.add_argument("--preserve-review-loop", action="store_true")
    setter.add_argument("--selector")
    setter.add_argument("--inherit-review-loop", action="store_true")
    setter.add_argument("--focused-review-model", action="append", default=[], metavar="AREA.PROVIDER=MODEL")
    setter.add_argument("--remove-focused-review-model", action="append", default=[], metavar="AREA.PROVIDER")
    setter.add_argument("--clear-focused-review-models", action="store_true")
    setter.add_argument("--repo-alias", action="append", default=[], metavar="NAME=REPO",
                        help="Add or replace a repository alias (repeatable; targets are canonical names)")
    setter.add_argument("--remove-repo-alias", action="append", default=[], metavar="NAME",
                        help="Remove a repository alias (repeatable)")
    setter.add_argument("--model-tier", action="append", default=[], metavar="PROVIDER.TIER=MODEL",
                        help="Map strong, medium, or weak to an actual provider model (repeatable)")
    setter.add_argument("--remove-model-tier", action="append", default=[], metavar="PROVIDER[.TIER]",
                        help="Remove one tier mapping, or all of a provider's tiers (repeatable)")
    setter.add_argument("--model-tier-effort", action="append", default=[], metavar="PROVIDER.TIER=EFFORT",
                        help="Set the reasoning effort a tier carries (repeatable; default high)")
    setter.add_argument("--remove-model-tier-effort", action="append", default=[], metavar="PROVIDER[.TIER]",
                        help="Remove one tier's effort, or all of a provider's tier efforts (repeatable)")
    setter.add_argument("--branch-prefix", help="Namespace for new projects (default: baton)")
    focused_reviews = setter.add_mutually_exclusive_group()
    focused_reviews.add_argument("--review-focus", action="append", metavar="NAME",
                                 help="Replace focused review catalog names (repeatable; order is preserved)")
    focused_reviews.add_argument("--no-focused-reviews", action="store_true", help="Skip focused reviews")
    setter.add_argument("--general-review-rounds", type=int, metavar="COUNT",
                        help="Maximum general review rounds; stop early when clean; zero skips general reviews")
    for key in PATH_OPTIONS:
        setter.add_argument("--" + key.replace("_", "-"), type=Path)
    for key in MODEL_OPTIONS:
        setter.add_argument("--" + key.replace("_", "-"), action="append", default=[], metavar="PROVIDER=MODEL",
                            help=("Guide owner sessions to use this worker model (repeatable; default gives no guidance)"
                                  if key == "worker_model" else
                                  "Set a provider review model (repeatable; default uses the provider's own default)"))
        setter.add_argument("--remove-" + key.replace("_", "-"), action="append", default=[], metavar="PROVIDER",
                            help="Remove a provider from this model map (repeatable)")
    args = parser.parse_args(argv)
    try:
        selector = getattr(args, "selector", None)
        if selector is not None and not selector.strip():
            raise ValueError("--selector must name a project or coordinator slot")
        if args.action != "show":
            require_trusted_runtime()
        if args.action == "show" and args.section:
            if args.selector:
                raise ValueError("--section cannot be combined with --selector")
            raw = read_raw_config()
            metadata = {}
            if "paths" in args.section:
                metadata["paths"] = {key: str(value) for key, value in effective_paths(raw).items()}
            if "branch_prefix" in args.section:
                metadata["branch_prefix"] = configured_branch_prefix(raw)
            if "providers" in args.section:
                metadata["providers"] = provider_settings()
            if "worker_model" in args.section:
                metadata["worker_model"] = effective_models(raw, "worker_model")
            if "resolved_worker_model" in args.section:
                metadata["resolved_worker_model"] = resolved_models(raw, "worker_model")
            if "resolved_worker_effort" in args.section:
                metadata["resolved_worker_effort"] = resolved_worker_efforts(raw)
            print(json.dumps(metadata, indent=2))
            return 0
        config = read_config(validate_model_references=args.action != "set")
        project = None
        if args.action == "show" and selector is not None:
            project = review_project(selector)
        preserve = args.action == "init" and args.preserve_review_loop
        if args.action == "init":
            original = json.dumps(config, sort_keys=True)
            if not preserve or "review_loop" not in config:
                config["review_loop"] = effective_review_loop(config)
            for key in MODEL_OPTIONS:
                if key in REVIEW_MODEL_OPTIONS:
                    config.setdefault(key, default_review_models())
                else:
                    config[key] = default_review_models() | config.get(key, {})
            if json.dumps(config, sort_keys=True) != original:
                save_config(config)
        if args.action == "set":
            loop_mutation = (args.review_focus is not None or args.no_focused_reviews
                             or args.general_review_rounds is not None or args.focused_review_model
                             or args.remove_focused_review_model or args.clear_focused_review_models)
            if args.inherit_review_loop and (not args.selector or loop_mutation):
                raise ValueError("--inherit-review-loop requires --selector and is exclusive with review-loop changes")
            if args.clear_focused_review_models and (args.focused_review_model or args.remove_focused_review_model):
                raise ValueError("--clear-focused-review-models is exclusive with model assignment/removal")
            if args.selector and (args.repo_alias or args.remove_repo_alias or args.model_tier
                    or args.remove_model_tier or args.model_tier_effort or args.remove_model_tier_effort
                    or args.branch_prefix is not None or any(getattr(args, key) is not None for key in PATH_OPTIONS)
                    or any(getattr(args, key) or getattr(args, "remove_" + key) for key in MODEL_OPTIONS)):
                raise ValueError("Project --selector accepts only review-loop flags")
            changes = {key: str(getattr(args, key).expanduser().resolve())
                       for key in PATH_OPTIONS if getattr(args, key) is not None}
            model_changes = {}
            model_removals = {}
            for key in MODEL_OPTIONS:
                additions = {}
                for value in getattr(args, key):
                    provider, separator, model = value.partition("=")
                    if not separator:
                        raise ValueError(f"--{key.replace('_', '-')} must use PROVIDER=MODEL")
                    additions[provider] = model
                validate_review_models(key, additions)
                removals = getattr(args, "remove_" + key)
                validate_review_models(key, dict.fromkeys(removals, "default"))
                if set(additions) & set(removals):
                    raise ValueError(f"Cannot set and remove the same provider in {key} in one command")
                if additions:
                    model_changes[key] = additions
                if removals:
                    model_removals[key] = removals
            tier_changes, tier_removals = _tier_changes("model_tiers", "model-tier", "MODEL", args.model_tier,
                                                        args.remove_model_tier)
            validate_model_tiers(tier_changes)
            effort_changes, effort_removals = _tier_changes("model_tier_effort", "model-tier-effort", "EFFORT",
                                                            args.model_tier_effort, args.remove_model_tier_effort)
            validate_model_tier_effort(effort_changes)
            if (not changes and not args.repo_alias and not args.remove_repo_alias and not model_changes
                    and not model_removals and not tier_changes and not tier_removals and not effort_changes
                    and not effort_removals and args.branch_prefix is None
                    and not loop_mutation and not args.inherit_review_loop):
                parser.error("specify at least one path, branch prefix, alias, model, tier, or review-loop change")
            aliases = dict(config.get("repo_aliases", {}))
            additions = {}
            for value in args.repo_alias:
                name, separator, target = value.partition("=")
                if not separator:
                    raise ValueError("--repo-alias must use NAME=REPO")
                additions[name] = target
            validate_repo_aliases(additions)
            validate_repo_aliases({name: name for name in args.remove_repo_alias})
            if set(additions) & set(args.remove_repo_alias):
                raise ValueError("Cannot set and remove the same repository alias in one command")
            for name in args.remove_repo_alias:
                aliases.pop(name, None)
            aliases.update(additions)
            if args.branch_prefix is not None:
                config["branch_prefix"] = configured_branch_prefix({"branch_prefix": args.branch_prefix})
            if changes:
                config.setdefault("paths", {}).update(changes)
            if args.repo_alias or args.remove_repo_alias:
                config["repo_aliases"] = aliases
            for key in MODEL_OPTIONS:
                if key in model_changes or key in model_removals:
                    # An absent review map means the defaults, so removal must start from them to stick.
                    models = config.setdefault(key, default_review_models() if key in REVIEW_MODEL_OPTIONS else {})
                    for provider in model_removals.get(key, ()):
                        models.pop(provider, None)
                    models.update(model_changes.get(key, {}))
            _apply_tier_changes(config, "model_tiers", tier_changes, tier_removals)
            _apply_tier_changes(config, "model_tier_effort", effort_changes, effort_removals)
            def apply_loop(target: dict[str, Any]) -> None:
                if args.inherit_review_loop:
                    target.pop("review_loop", None)
                    return
                if not loop_mutation:
                    return
                old = target.get("review_loop", {})
                if not isinstance(old, dict):
                    raise ValueError("review_loop must be an object; project overrides can be repaired with --inherit-review-loop")
                loop = copy.deepcopy(old)
                if args.review_focus is not None:
                    loop["focused_reviews"] = args.review_focus
                elif args.no_focused_reviews:
                    loop["focused_reviews"] = []
                if args.general_review_rounds is not None:
                    loop["general_rounds"] = args.general_review_rounds
                if args.clear_focused_review_models:
                    loop.pop("focused_review_models", None)
                if args.focused_review_model or args.remove_focused_review_model:
                    models = loop.setdefault("focused_review_models", {})
                    if not isinstance(models, dict):
                        raise ValueError("Malformed review_loop.focused_review_models; use --clear-focused-review-models")
                    assignments = {}
                    for value in args.focused_review_model:
                        name, separator, model = value.partition("=")
                        if not separator:
                            raise ValueError("--focused-review-model requires AREA.PROVIDER=MODEL")
                        assignments[name] = model
                    if assignments.keys() & set(args.remove_focused_review_model):
                        raise ValueError("Cannot set and remove the same focused review model pair")
                    for name in [*args.remove_focused_review_model, *assignments]:
                        area, dot, provider = name.partition(".")
                        if not dot or not area or not provider:
                            raise ValueError("Focused review model pair requires AREA.PROVIDER")
                        if name in assignments:
                            pairs = models.setdefault(area, {})
                            if not isinstance(pairs, dict):
                                raise ValueError("Malformed focus model map; use --clear-focused-review-models")
                            pairs[provider] = assignments[name]
                        elif isinstance(models.get(area), dict):
                            models[area].pop(provider, None)
                            if not models[area]:
                                models.pop(area)
                    loop["focused_review_models"] = models
                target["review_loop"] = loop
            if selector is None:
                apply_loop(config)
            else:
                from .manager import Manager
                manager = Manager()
                manager.load()
                with manager.command_lock(args.selector):
                    selected = manager.project(args.selector)
                    if selected["state"] != "active":
                        raise ProjectError("Finish the outstanding project operation first.")
                    if selected.get("pending_manifest"):
                        with redirect_stdout(sys.stderr):
                            manager.materialize(selected, create_worktrees=False)
                    manager.check_coordinator(selected)
                    prospective = copy.deepcopy(selected)
                    apply_loop(prospective)
                    effective_review_loop(config, prospective)
                    for key in MODEL_OPTIONS:
                        effective_models(config, key)
                    selected.clear()
                    selected.update(prospective)
                    with redirect_stdout(sys.stderr):
                        manager.materialize(selected, create_worktrees=False)
                    project = selected
            for key in MODEL_OPTIONS:
                effective_models(config, key)
            effective_review_loop(config, project)
            if project is None:
                save_config(config)
        from .manager import Manager
        manager = Manager()
        data = {"config_file": str(config_path()), "paths": {
            "projects_repo": str(manager.repo), "slots_root": str(manager.slots),
            "worktrees_root": str(manager.worktrees), "repos_root": str(manager.sources),
            "artifacts_root": str(manager.artifacts)}, "repo_aliases": dict(manager.repo_aliases),
            "branch_prefix": manager.branch_prefix,
            "providers": provider_settings(), "model_tiers": config.get("model_tiers", {}),
            "model_tier_effort": config.get("model_tier_effort", {}),
            "resolved_worker_effort": resolved_worker_efforts(config),
            "review_loop": config.get("review_loop", {}) if preserve else effective_review_loop(config, project),
            "project_review_loop": None if project is None else project.get("review_loop", {}),
            **({} if preserve else {"review_focus_catalog": review_focus_catalog(),
               "resolved_focused_review_models": resolved_focused_review_models(config, project)}),
            **{key: effective_models(config, key) for key in MODEL_OPTIONS},
            **{"resolved_" + key: resolved_models(config, key) for key in MODEL_OPTIONS}}
        print(json.dumps(data, indent=2))
        return 0
    except (OSError, ValueError, ProjectError) as error:
        print(f"baton config: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
