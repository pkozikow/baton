"""Built-in focused review guidance and scoped schedule resolution."""

from pathlib import Path
from typing import Any

from .providers import PROVIDERS, effective_models, resolve_review_model, validate_review_models

DEFAULT_FOCUSED_REVIEWS = ("performance", "complexity", "security")
DEFAULT_GENERAL_ROUNDS = 10


def review_focus_catalog() -> dict[str, str]:
    catalog = {}
    for path in sorted(Path(__file__).with_name("review_focuses").glob("*.md")):
        text = path.read_text()
        if not text.strip():
            raise ValueError(f"Empty review focus guidance: {path.name}")
        catalog[path.stem] = text
    if not catalog or any(name not in catalog for name in DEFAULT_FOCUSED_REVIEWS):
        raise ValueError("Missing built-in review focus guidance")
    return catalog


def _scope(settings: Any, config: dict[str, Any], label: str, repair: str) -> dict[str, Any]:
    try:
        if not isinstance(settings, dict):
            raise ValueError("review_loop must be an object")
        unknown = settings.keys() - {"focused_reviews", "general_rounds", "focused_review_models"}
        if unknown:
            raise ValueError("Unknown review_loop settings: " + ", ".join(sorted(map(str, unknown))))
        catalog = review_focus_catalog()
        if "focused_reviews" in settings:
            focuses = settings["focused_reviews"]
            if not isinstance(focuses, list) or any(not isinstance(name, str) or name not in catalog for name in focuses):
                raise ValueError("review_loop.focused_reviews must be a list of catalog names; valid names: " + ", ".join(catalog))
        if "general_rounds" in settings and (type(settings["general_rounds"]) is not int or settings["general_rounds"] < 0):
            raise ValueError("review_loop.general_rounds must be a nonnegative integer")
        models = settings.get("focused_review_models", {})
        if not isinstance(models, dict):
            raise ValueError("review_loop.focused_review_models must be an object")
        for area, pairs in models.items():
            if area not in catalog:
                raise ValueError(f"Unknown review_loop.focused_review_models area {area!r}; valid names: {', '.join(catalog)}")
            validate_review_models(f"review_loop.focused_review_models.{area}", pairs)
            for provider, model in pairs.items():
                resolve_review_model(provider, model, config.get("model_tiers", {}))
        return settings
    except ValueError as error:
        raise ValueError(f"{label}: {error}. Repair with {repair}") from error


def effective_review_loop(config: dict[str, Any], project: dict[str, Any] | None = None) -> dict[str, Any]:
    global_settings = _scope(config.get("review_loop", {}), config, "Global review_loop",
                             "baton config set --review-focus performance --review-focus complexity --review-focus security; "
                             "for model errors use --remove-focused-review-model AREA.PROVIDER or --clear-focused-review-models")
    local = {} if project is None else _scope(project.get("review_loop", {}), config,
                f"Project {project.get('name', '<project>')} review_loop",
                f"baton config set --selector {project.get('name', '<project>')} --inherit-review-loop")
    merged = {"focused_reviews": list(DEFAULT_FOCUSED_REVIEWS), "general_rounds": DEFAULT_GENERAL_ROUNDS}
    for settings in (global_settings, local):
        for key in ("focused_reviews", "general_rounds"):
            if key in settings:
                merged[key] = list(settings[key]) if key == "focused_reviews" else settings[key]
    models: dict[str, dict[str, str]] = {}
    for settings in (global_settings, local):
        for area, pairs in settings.get("focused_review_models", {}).items():
            models.setdefault(area, {}).update(pairs)
    merged["focused_review_models"] = models
    return merged


def resolved_focused_review_models(config: dict[str, Any], project: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    settings = effective_review_loop(config, project)
    normal = effective_models(config, "implementation_review_model")
    return {area: {provider: resolve_review_model(provider,
                settings["focused_review_models"].get(area, {}).get(provider, normal.get(provider, "default")),
                config.get("model_tiers", {})) for provider in PROVIDERS} for area in review_focus_catalog()}


def review_project(selector: str | None = None) -> dict[str, Any] | None:
    from .manager import Manager
    from .errors import NoProject
    if selector is not None and not selector.strip():
        raise ValueError("--selector must name a project or coordinator slot")
    manager = Manager()
    if selector is None and not manager.initialized():
        return None
    manager.load()
    try:
        return manager.project(selector)
    except NoProject:
        if selector is not None:
            raise
        return None
