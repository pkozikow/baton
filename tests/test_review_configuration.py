import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from baton.review_loop import effective_review_loop
from baton.providers import (MODEL_OPTIONS, REVIEW_MODEL_OPTIONS, PROVIDERS, Provider,
                             MODEL_TIERS, default_review_models, effective_review_models, provider_settings,
                             resolve_review_model, resolved_review_models, effective_worker_models, resolved_worker_models)


ROOT = Path(__file__).resolve().parents[1]


class ReviewConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "settings/config.json"
        self.env = os.environ | {"BATON_CONFIG_FILE": str(self.config), "BATON_ALLOW_UNTRUSTED_WRITES": "1"}
        for variable in ("BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT",
                         "BATON_REPOS_ROOT", "BATON_ARTIFACTS_ROOT"):
            self.env[variable] = str(self.root / variable.lower())

    def cli(self, *args: str, ok: bool = True, module: bool = False) -> subprocess.CompletedProcess[str]:
        command = ([sys.executable, "-m", "baton.configuration"] if module else
                   [str(ROOT / "bin/baton"), "config"])
        result = subprocess.run([*command, *args], cwd=ROOT, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        return result

    def save(self, data: dict) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(data))

    def test_show_defaults_without_creating_config(self) -> None:
        data = json.loads(self.cli("show").stdout)
        for key in MODEL_OPTIONS:
            self.assertEqual(data[key], {"codex": "default", "claude": "default", "agy": "default"})
        self.assertNotIn("review_providers", data)
        self.assertEqual(data["providers"]["claude"]["review"]["prompt_separator"], ["--"])
        self.assertEqual(data["providers"]["cursor"]["review"]["prompt_delivery"], "stdin")
        self.assertEqual(data["providers"]["codex"]["model_flag"], "--model")
        self.assertEqual(set(data["providers"]), set(PROVIDERS))
        self.assertEqual(data["providers"]["agy"]["skill_dir"], ".gemini/antigravity-cli/skills")
        self.assertEqual(data["providers"]["agy"]["session_env"], "ANTIGRAVITY_CONVERSATION_ID")
        for name, entry in data["providers"].items():
            self.assertEqual(entry["guidance"], PROVIDERS[name].guidance())
        self.assertEqual(data["model_tiers"], {})
        for key in MODEL_OPTIONS:
            self.assertEqual(data["resolved_" + key], data[key])
        self.assertEqual(data["review_loop"], effective_review_loop({}))
        self.assertFalse(self.config.exists())

    def test_review_kinds_and_providers_are_configured_independently(self) -> None:
        self.cli("set", "--repo-alias", "api=backend", "--worktrees-root", str(self.root / "trees"),
                 "--plan-review-model", "codex=plan-codex", "--plan-review-model", "agy=plan-agy",
                 "--implementation-review-model", "claude=implementation-claude")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["plan_review_model"], {"codex": "plan-codex", "claude": "default", "agy": "plan-agy"})
        self.assertEqual(data["implementation_review_model"],
                         {"codex": "default", "claude": "implementation-claude", "agy": "default"})
        self.cli("set", "--plan-review-model", "codex=default", "--implementation-review-model", "agy=org/model-v2")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["plan_review_model"], {"codex": "default", "claude": "default", "agy": "plan-agy"})
        self.assertEqual(saved["implementation_review_model"], {"codex": "default", "claude": "implementation-claude", "agy": "org/model-v2"})
        self.assertEqual(saved["paths"]["worktrees_root"], str(self.root / "trees"))
        self.assertEqual(saved["repo_aliases"], {"api": "backend"})

    def test_init_fills_missing_defaults_preserves_settings_and_is_idempotent(self) -> None:
        self.save({"paths": {"artifacts_root": str(self.root / "artifacts")}, "repo_aliases": {"api": "backend"},
                   "plan_review_model": {"codex": "pinned-model"}, "custom_setting": {"retained": True}, "branch_prefix": "team"})
        self.cli("init", module=True)
        data = json.loads(self.config.read_text())
        self.assertEqual(data["plan_review_model"], {"codex": "pinned-model"})
        self.assertEqual(data["implementation_review_model"], default_review_models())
        self.assertEqual(data["repo_aliases"], {"api": "backend"})
        self.assertEqual(data["paths"]["artifacts_root"], str(self.root / "artifacts"))
        self.assertEqual(data["custom_setting"], {"retained": True})
        self.assertEqual(data["branch_prefix"], "team")
        original = self.config.read_bytes()
        modified = self.config.stat().st_mtime_ns
        self.cli("init")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mtime_ns, modified)

    def test_saved_review_provider_sets_are_authoritative_and_independent(self) -> None:
        config = {"plan_review_model": {"codex": "default", "claude": "plan-model"},
                  "implementation_review_model": {"agy": "implementation-model"}}
        for expected in (config, {"plan_review_model": {"codex": "default", "cursor": "default"},
                                  "implementation_review_model": {"claude": "default"}}):
            self.save(expected)
            data = json.loads(self.cli("show").stdout)
            for key in REVIEW_MODEL_OPTIONS:
                self.assertEqual(data[key], expected[key])
                self.assertEqual(data["resolved_" + key], expected[key])
            self.assertEqual(data["worker_model"], default_review_models())

    def test_empty_review_provider_sets_survive_initialization_and_updates(self) -> None:
        self.save({"plan_review_model": {}, "implementation_review_model": {},
                   "worker_model": {"codex": "worker-model"}})
        self.cli("init", module=True)
        data = json.loads(self.cli("show").stdout)
        for key in REVIEW_MODEL_OPTIONS:
            self.assertEqual(data[key], {})
            self.assertEqual(data["resolved_" + key], {})
        self.assertEqual(data["worker_model"], default_review_models() | {"codex": "worker-model"})
        original = self.config.read_bytes()
        modified = self.config.stat().st_mtime_ns
        self.cli("init")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mtime_ns, modified)
        self.cli("set", "--plan-review-model", "codex=default")
        data = json.loads(self.cli("init").stdout)
        self.assertEqual(data["plan_review_model"], {"codex": "default"})
        self.assertEqual(data["implementation_review_model"], {})

    def test_setting_review_models_preserves_existing_provider_sets(self) -> None:
        self.save({"plan_review_model": {"codex": "plan-model"},
                   "implementation_review_model": {"claude": "implementation-model"}})
        self.cli("set", "--plan-review-model", "cursor=default")
        data = json.loads(self.cli("init").stdout)
        self.assertEqual(data["plan_review_model"], {"codex": "plan-model", "cursor": "default"})
        self.assertEqual(data["implementation_review_model"], {"claude": "implementation-model"})

    def test_init_creates_all_install_defaults(self) -> None:
        self.cli("init", module=True)
        self.assertEqual(json.loads(self.config.read_text()),
                         {**{key: default_review_models() for key in MODEL_OPTIONS},
                          "review_loop": effective_review_loop({})})

    def test_invalid_assignments_never_replace_saved_configuration(self) -> None:
        self.cli("set", "--repo-alias", "api=backend")
        original = self.config.read_bytes()
        for assignment in ("codex", "=model", "unknown=model", "codex=", "claude= ", "codex=foo\nbar", "agy=foo\tbar"):
            for key in MODEL_OPTIONS:
                with self.subTest(assignment=assignment, key=key):
                    self.cli("set", "--repo-alias", "web=frontend", "--" + key.replace("_", "-"), assignment, ok=False)
                    self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_saved_models_fail_closed(self) -> None:
        for key in MODEL_OPTIONS:
            for invalid in ([], None, "default", {"unknown": "model"}, {"codex": []}, {"agy": " "}, {"claude": "model\nnext"}, {"agy": "nul\0model"}):
                with self.subTest(key=key, invalid=invalid):
                    self.save({key: invalid})
                    original = self.config.read_bytes()
                    for action in ("show", "init"):
                        failed = self.cli(action, ok=False)
                        self.assertIn(key, failed.stderr)
                        self.assertNotIn("Traceback", failed.stderr)
                    self.assertEqual(self.config.read_bytes(), original)

    def test_cursor_is_available_without_adding_it_to_default_configuration(self) -> None:
        self.cli("init")
        saved = self.config.read_bytes()
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["providers"]["cursor"]["binary"], "agent")
        self.assertFalse(data["providers"]["cursor"]["default_enabled"])
        for key in MODEL_OPTIONS:
            self.assertNotIn("cursor", data[key])
        self.cli("init")
        self.assertEqual(self.config.read_bytes(), saved)
        self.cli("set", "--plan-review-model", "cursor=plan-cursor",
                 "--implementation-review-model", "cursor=default")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["plan_review_model"]["cursor"], "plan-cursor")
        self.assertEqual(data["implementation_review_model"]["cursor"], "default")

    def test_all_provider_tiers_resolve_for_both_review_kinds(self) -> None:
        for provider in PROVIDERS:
            for tier in MODEL_TIERS:
                with self.subTest(provider=provider, tier=tier):
                    model = f"{provider}-actual-{tier}"
                    self.cli("set", "--model-tier", f"{provider}.{tier}={model}",
                             "--plan-review-model", f"{provider}={tier}",
                             "--implementation-review-model", f"{provider}={tier}",
                             "--worker-model", f"{provider}={tier}")
                    data = json.loads(self.cli("show").stdout)
                    for key in MODEL_OPTIONS:
                        self.assertEqual(data[key][provider], tier)
                        self.assertEqual(data["resolved_" + key][provider], model)
                    self.assertEqual(data["model_tiers"][provider][tier], model)

    def test_tiers_are_provider_specific_and_never_fall_back(self) -> None:
        self.cli("set", "--model-tier", "codex.strong=codex-actual")
        original = self.config.read_bytes()
        for key in MODEL_OPTIONS:
            for provider in PROVIDERS:
                tier = "medium" if provider == "codex" else "strong"
                with self.subTest(key=key, provider=provider):
                    failed = self.cli("set", "--" + key.replace("_", "-"), f"{provider}={tier}", ok=False)
                    self.assertIn(f"{provider}.{tier}=MODEL", failed.stderr)
                    self.assertNotIn("Traceback", failed.stderr)
                    self.assertEqual(self.config.read_bytes(), original)

    def test_default_and_actual_model_ids_ignore_tier_mappings(self) -> None:
        self.cli("set", "--model-tier", "codex.strong=tier-model",
                 "--plan-review-model", "codex=default",
                 "--implementation-review-model", "codex=org/direct-model")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["resolved_plan_review_model"]["codex"], "default")
        self.assertEqual(data["resolved_implementation_review_model"]["codex"], "org/direct-model")
        for model in ("default", "org/direct-model"):
            self.assertEqual(resolve_review_model("codex", model, {}), model)

    def test_mappings_can_be_changed_independently_and_preserve_settings(self) -> None:
        self.cli("set", "--model-tier", "codex.strong=first-model", "--model-tier", "codex.weak=weak-model",
                 "--plan-review-model", "codex=strong", "--repo-alias", "api=backend",
                 "--artifacts-root", str(self.root / "artifacts"))
        before = json.loads(self.config.read_text())
        self.cli("set", "--model-tier", "codex.strong=second-model")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["plan_review_model"], before["plan_review_model"])
        self.assertEqual(saved["repo_aliases"], before["repo_aliases"])
        self.assertEqual(saved["paths"], before["paths"])
        self.assertEqual(saved["model_tiers"]["codex"], {"strong": "second-model", "weak": "weak-model"})
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["resolved_plan_review_model"]["codex"], "second-model")

    def test_invalid_tier_assignments_never_write_even_when_unused(self) -> None:
        self.cli("set", "--repo-alias", "api=backend")
        original = self.config.read_bytes()
        invalid = ("codex.strong", "codex=actual", ".strong=actual", "unknown.strong=actual",
                   "codex.other=actual", "codex.strong=", "codex.strong= ", "codex.strong=foo\nbar",
                   "codex.strong=foo\x7fbar", "codex.strong=default", "codex.strong=strong",
                   "claude.medium=weak", "agy.weak=medium")
        for assignment in invalid:
            with self.subTest(assignment=assignment):
                failed = self.cli("set", "--model-tier", assignment, "--repo-alias", "web=frontend", ok=False)
                self.assertNotIn("Traceback", failed.stderr)
                self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_saved_tier_mappings_fail_closed_without_writes(self) -> None:
        invalid = ([], None, "strong", {"unknown": {}}, {"codex": []}, {"codex": {"other": "model"}},
                   {"claude": {"strong": "default"}}, {"agy": {"weak": "strong"}},
                   {"cursor": {"medium": ""}}, {"codex": {"strong": "bad\x7fmodel"}})
        for tiers in invalid:
            with self.subTest(tiers=tiers):
                self.save({"model_tiers": tiers})
                original = self.config.read_bytes()
                for action in ("show", "init"):
                    failed = self.cli(action, ok=False)
                    self.assertIn("model_tiers", failed.stderr)
                    self.assertNotIn("Traceback", failed.stderr)
                self.assertEqual(self.config.read_bytes(), original)

    def test_unmapped_saved_tier_fails_closed_and_can_be_repaired_atomically(self) -> None:
        for key in MODEL_OPTIONS:
            with self.subTest(key=key):
                self.save({key: {"codex": "strong"}})
                original = self.config.read_bytes()
                for action in ("show", "init"):
                    failed = self.cli(action, ok=False)
                    self.assertIn(f"{key}.codex", failed.stderr)
                    self.assertNotIn("Traceback", failed.stderr)
                self.cli("set", "--repo-alias", "api=backend", ok=False)
                self.assertEqual(self.config.read_bytes(), original)
                data = json.loads(self.cli("set", "--model-tier", "codex.strong=repaired-model").stdout)
                self.assertEqual(data["resolved_" + key]["codex"], "repaired-model")
                self.assertEqual(data[key]["codex"], "strong")

    def test_cursor_mapping_does_not_enable_cursor_or_change_install_defaults(self) -> None:
        self.cli("set", "--model-tier", "cursor.strong=cursor-model")
        self.cli("init")
        data = json.loads(self.cli("show").stdout)
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["model_tiers"], {"cursor": {"strong": "cursor-model"}})
        for key in MODEL_OPTIONS:
            self.assertEqual(saved[key], default_review_models())
            self.assertNotIn("cursor", data[key])
            self.assertNotIn("cursor", data["resolved_" + key])
        original = self.config.read_bytes()
        self.cli("init")
        self.assertEqual(self.config.read_bytes(), original)

    def test_init_preserves_existing_tier_selections_and_mappings(self) -> None:
        self.save({"model_tiers": {"codex": {"strong": "selected-model"}},
                   "plan_review_model": {"codex": "strong"}})
        data = json.loads(self.cli("init").stdout)
        self.assertEqual(data["plan_review_model"]["codex"], "strong")
        self.assertEqual(data["resolved_plan_review_model"]["codex"], "selected-model")
        self.assertEqual(data["resolved_implementation_review_model"], default_review_models())
        original = self.config.read_bytes()
        self.cli("init")
        self.assertEqual(self.config.read_bytes(), original)

    def test_worker_models_are_guidance_only_and_independent_of_reviews(self) -> None:
        self.cli("set", "--worker-model", "codex=gpt-6.1-sol", "--worker-model", "cursor=cursor-worker")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["worker_model"], {"codex": "gpt-6.1-sol", "claude": "default", "agy": "default",
                                              "cursor": "cursor-worker"})
        self.assertEqual(data["resolved_worker_model"], data["worker_model"])
        for key in REVIEW_MODEL_OPTIONS:
            self.assertEqual(data[key], default_review_models())
            self.assertNotIn("cursor", data[key])
        self.cli("init")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["worker_model"], data["worker_model"])
        self.cli("set", "--worker-model", "codex=default", "--worker-model", "cursor=default")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["resolved_worker_model"]["codex"], "default")
        self.assertEqual(data["resolved_worker_model"]["cursor"], "default")
        self.assertNotIn("cursor", data["plan_review_model"])

    def test_worker_tier_mapping_changes_apply_without_changing_reviews(self) -> None:
        self.cli("set", "--worker-model", "codex=medium", "--model-tier", "codex.medium=first-worker")
        self.cli("set", "--model-tier", "codex.medium=second-worker")
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["worker_model"]["codex"], "medium")
        self.assertEqual(data["resolved_worker_model"]["codex"], "second-worker")
        for key in REVIEW_MODEL_OPTIONS:
            self.assertEqual(data[key], default_review_models())
        self.assertEqual(effective_worker_models({})["codex"], "default")
        self.assertEqual(resolved_worker_models({"worker_model": {"codex": "literal"}})["codex"], "literal")

    def test_model_entries_can_be_removed_from_saved_and_implicit_maps(self) -> None:
        self.cli("set", "--remove-plan-review-model", "agy", "--remove-implementation-review-model", "agy",
                 "--remove-implementation-review-model", "cursor")
        saved = json.loads(self.config.read_text())
        expected = {name: model for name, model in default_review_models().items() if name != "agy"}
        for key in REVIEW_MODEL_OPTIONS:
            self.assertEqual(saved[key], expected)
        self.cli("set", "--worker-model", "codex=worker", "--worker-model", "claude=worker")
        self.cli("set", "--remove-worker-model", "codex", "--remove-plan-review-model", "claude",
                 "--remove-plan-review-model", "codex")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["worker_model"], {"claude": "worker"})
        self.assertEqual(saved["plan_review_model"], {})
        data = json.loads(self.cli("show").stdout)
        self.assertEqual(data["plan_review_model"], {})
        self.assertEqual(data["worker_model"]["codex"], "default")

    def test_model_removal_conflicts_and_selector_scope_never_write(self) -> None:
        self.cli("set", "--repo-alias", "api=backend")
        original = self.config.read_bytes()
        for args in (("--plan-review-model", "codex=x", "--remove-plan-review-model", "codex"),
                     ("--remove-worker-model", "unknown"),
                     ("--model-tier", "codex.strong=x", "--remove-model-tier", "codex"),
                     ("--model-tier", "codex.strong=x", "--remove-model-tier", "codex.strong"),
                     ("--remove-model-tier", "codex.other"), ("--remove-model-tier", "codex."),
                     ("--remove-model-tier", "unknown"),
                     ("--selector", "anything", "--remove-plan-review-model", "codex"),
                     ("--selector", "anything", "--remove-model-tier", "codex")):
            with self.subTest(args=args):
                failed = self.cli("set", *args, ok=False)
                self.assertNotIn("Traceback", failed.stderr)
                self.assertEqual(self.config.read_bytes(), original)

    def test_model_tiers_can_be_removed_per_tier_or_per_provider(self) -> None:
        self.cli("set", "--model-tier", "codex.strong=a", "--model-tier", "codex.weak=b",
                 "--model-tier", "claude.strong=c", "--model-tier", "agy.medium=d")
        self.cli("set", "--remove-model-tier", "codex.strong", "--remove-model-tier", "claude",
                 "--remove-model-tier", "cursor", "--model-tier", "codex.medium=e")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["model_tiers"], {"codex": {"weak": "b", "medium": "e"}, "agy": {"medium": "d"}})
        self.cli("set", "--remove-model-tier", "agy.medium")
        self.assertEqual(json.loads(self.config.read_text())["model_tiers"], {"codex": {"weak": "b", "medium": "e"}})
        self.cli("set", "--plan-review-model", "codex=weak")
        original = self.config.read_bytes()
        failed = self.cli("set", "--remove-model-tier", "codex.weak", ok=False)
        self.assertIn("plan_review_model.codex", failed.stderr)
        self.assertEqual(self.config.read_bytes(), original)

    def test_registry_extension_supplies_defaults_validation_and_runner_metadata(self) -> None:
        provider = Provider("future", "future-cli", args=("--print",))
        PROVIDERS[provider.name] = provider
        try:
            self.assertEqual(default_review_models()[provider.name], "default")
            self.assertEqual(effective_review_models({"plan_review_model": {"future": "chosen"}}, "plan_review_model")["future"], "chosen")
            tiers = {"future": {"medium": "future-medium-model"}}
            config = {"model_tiers": tiers, "plan_review_model": {"future": "medium"}}
            self.assertEqual(resolved_review_models(config, "plan_review_model")["future"], "future-medium-model")
            self.assertEqual(resolved_worker_models({"worker_model": {"future": "medium"}, "model_tiers": tiers})["future"],
                             "future-medium-model")
            self.assertEqual(provider_settings()[provider.name]["binary"], "future-cli")
            self.assertEqual(provider_settings()[provider.name]["review"]["args"], ["--print"])
        finally:
            del PROVIDERS[provider.name]


if __name__ == "__main__":
    unittest.main()
