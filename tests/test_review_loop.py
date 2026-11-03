import copy
import json
import subprocess
import unittest

from baton.review_loop import (DEFAULT_FOCUSED_REVIEWS, DEFAULT_GENERAL_ROUNDS, effective_review_loop,
                               review_focus_catalog, resolved_focused_review_models)
import test_review_configuration as fixtures


class ReviewLoopSettingsTest(unittest.TestCase):
    def test_defaults_and_partial_settings_are_independent_copies(self) -> None:
        defaults = effective_review_loop({})
        self.assertEqual(defaults, {"focused_reviews": list(DEFAULT_FOCUSED_REVIEWS),
                                    "general_rounds": DEFAULT_GENERAL_ROUNDS, "focused_review_models": {}})
        defaults["focused_reviews"].clear()
        self.assertEqual(effective_review_loop({})["focused_reviews"], list(DEFAULT_FOCUSED_REVIEWS))
        original = {"review_loop": {"focused_reviews": ["security"]}}
        before = copy.deepcopy(original)
        resolved = effective_review_loop(original)
        self.assertEqual(resolved["focused_reviews"], original["review_loop"]["focused_reviews"])
        self.assertEqual(resolved["general_rounds"], DEFAULT_GENERAL_ROUNDS)
        resolved["focused_reviews"].append("performance")
        self.assertEqual(original, before)
        self.assertEqual(effective_review_loop({"review_loop": {"general_rounds": 0, "focused_review_models": {}}}),
                         {"focused_reviews": list(DEFAULT_FOCUSED_REVIEWS), "general_rounds": 0, "focused_review_models": {}})

    def test_zero_phases_and_unbounded_counts(self) -> None:
        for rounds in (0, 1, 27, 10000):
            with self.subTest(rounds=rounds):
                self.assertEqual(effective_review_loop({"review_loop": {"focused_reviews": [],
                                                                         "general_rounds": rounds, "focused_review_models": {}}}),
                                 {"focused_reviews": [], "general_rounds": rounds, "focused_review_models": {}})
        focuses = ["security", "complexity", "security"]
        self.assertEqual(effective_review_loop({"review_loop": {"focused_reviews": focuses}})["focused_reviews"],
                         focuses)

    def test_invalid_settings_are_rejected(self) -> None:
        invalid = (None, [], "default", False, {"unknown": 1}, {"focused_review": []},
                   *({"focused_reviews": value} for value in (None, "security", [None], [1], [""], [" \n "], ["bad\0prompt"])),
                   *({"general_rounds": value} for value in (None, True, False, -1, 1.5, "3")))
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "review_loop"):
                effective_review_loop({"review_loop": settings})


class ReviewLoopConfigurationTest(unittest.TestCase):
    setUp = fixtures.ReviewConfigurationTest.setUp
    cli = fixtures.ReviewConfigurationTest.cli
    save = fixtures.ReviewConfigurationTest.save

    def test_cli_sets_custom_ordered_focuses_and_general_rounds(self) -> None:
        focuses = ["security", "complexity", "security"]
        result = self.cli("set", "--review-focus", focuses[0], "--review-focus", focuses[1],
                          "--review-focus", focuses[2], "--general-review-rounds", "27",
                          "--repo-alias", "api=backend")
        expected = {"focused_reviews": focuses, "general_rounds": 27, "focused_review_models": {}}
        self.assertEqual(json.loads(result.stdout)["review_loop"], expected)
        saved = json.loads(self.config.read_text())
        self.assertEqual(effective_review_loop(saved), expected)
        self.assertEqual(saved["repo_aliases"], {"api": "backend"})

    def test_zero_phases_survive_init_and_independent_updates(self) -> None:
        self.save({"review_loop": {"focused_reviews": [], "general_rounds": 0, "focused_review_models": {}},
                   "plan_review_model": {"codex": "default"}, "custom_setting": "preserved"})
        self.cli("init")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["review_loop"], {"focused_reviews": [], "general_rounds": 0, "focused_review_models": {}})
        self.assertEqual(saved["plan_review_model"], {"codex": "default"})
        self.assertEqual(saved["custom_setting"], "preserved")
        before, modified = self.config.read_bytes(), self.config.stat().st_mtime_ns
        self.cli("init", module=True)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.config.stat().st_mtime_ns, modified)
        data = json.loads(self.cli("set", "--review-focus", "security").stdout)
        self.assertEqual(data["review_loop"], {"focused_reviews": ["security"], "general_rounds": 0, "focused_review_models": {}})
        data = json.loads(self.cli("set", "--no-focused-reviews").stdout)
        self.assertEqual(data["review_loop"], {"focused_reviews": [], "general_rounds": 0, "focused_review_models": {}})
        data = json.loads(self.cli("set", "--general-review-rounds", "4").stdout)
        self.assertEqual(data["review_loop"], {"focused_reviews": [], "general_rounds": 4, "focused_review_models": {}})

    def test_partial_settings_resolve_and_initialize_missing_fields(self) -> None:
        for settings, expected in (({"focused_reviews": []}, {"focused_reviews": [], "general_rounds": 10, "focused_review_models": {}}),
                                   ({"general_rounds": 0, "focused_review_models": {}}, {"focused_reviews": list(DEFAULT_FOCUSED_REVIEWS), "general_rounds": 0, "focused_review_models": {}})):
            with self.subTest(settings=settings):
                self.save({"review_loop": settings})
                before = self.config.read_bytes()
                self.assertEqual(json.loads(self.cli("show").stdout)["review_loop"], expected)
                self.assertEqual(self.config.read_bytes(), before)
                self.cli("init")
                self.assertEqual(json.loads(self.config.read_text())["review_loop"], expected)

    def test_invalid_cli_updates_never_write_unrelated_settings(self) -> None:
        self.save({"review_loop": {"focused_reviews": ["security"], "general_rounds": 0, "focused_review_models": {}},
                   "repo_aliases": {"api": "backend"}})
        before = self.config.read_bytes()
        for args in (("--general-review-rounds", "-1"),
                     *(("--review-focus", name) for name in ("", " \n ", "Security", "security.md", "../security", "/tmp/security"))):
            with self.subTest(args=args):
                result = self.cli("set", *args, "--repo-alias", "web=frontend", ok=False)
                self.assertIn("review_loop", result.stderr)
                self.assertEqual(self.config.read_bytes(), before)
        result = subprocess.run([str(fixtures.ROOT / "bin/baton"), "config", "set", "--no-focused-reviews",
                                 "--review-focus", "Security"], env=self.env, cwd=fixtures.ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.config.read_bytes(), before)

    def test_invalid_saved_loop_blocks_show_init_and_updates_without_writes(self) -> None:
        for settings in (None, [], {"focused_reviews": "security"}, {"focused_reviews": ["bad\0prompt"]},
                         {"general_rounds": True}, {"general_rounds": -1}, {"general_round": 0}):
            with self.subTest(settings=settings):
                self.save({"review_loop": settings})
                before = self.config.read_bytes()
                for args in (("show",), ("init",), ("set", "--repo-alias", "api=backend")):
                    result = self.cli(*args, ok=False)
                    self.assertIn("review_loop", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(self.config.read_bytes(), before)


class FocusedModelTest(unittest.TestCase):
    def test_pair_overlay_and_dormant_fallback(self) -> None:
        config = {"implementation_review_model": {"codex": "normal"},
                  "model_tiers": {"codex": {"strong": "large"}},
                  "review_loop": {"focused_reviews": ["security", "security"],
                                  "focused_review_models": {"security": {"codex": "strong", "cursor": "dormant"}}}}
        project = {"name": "demo", "review_loop": {"focused_reviews": [], "general_rounds": 0,
                    "focused_review_models": {"security": {"codex": "default"}}}}
        resolved = effective_review_loop(config, project)
        self.assertEqual(resolved["focused_reviews"], [])
        self.assertEqual(resolved["general_rounds"], 0)
        models = resolved_focused_review_models(config, project)
        self.assertEqual(models["security"]["codex"], "default")
        self.assertEqual(models["security"]["cursor"], "dormant")
        self.assertEqual(models["performance"]["codex"], "normal")
        self.assertEqual(config["implementation_review_model"], {"codex": "normal"})
        self.assertEqual(resolved_focused_review_models(config)["security"]["codex"], "large")
        self.assertTrue(all(text.strip() for text in review_focus_catalog().values()))

    def test_invalid_global_cannot_be_hidden_by_project(self) -> None:
        with self.assertRaisesRegex(ValueError, "Global review_loop"):
            effective_review_loop({"review_loop": {"focused_reviews": ["legacy prompt"]}},
                                  {"name": "demo", "review_loop": {"focused_reviews": []}})
        with self.assertRaisesRegex(ValueError, "model-tier"):
            effective_review_loop({"review_loop": {"focused_reviews": [],
                                  "focused_review_models": {"security": {"cursor": "strong"}}}})


class ReviewLoopRepairTest(unittest.TestCase):
    setUp = fixtures.ReviewConfigurationTest.setUp
    cli = fixtures.ReviewConfigurationTest.cli
    save = fixtures.ReviewConfigurationTest.save

    def test_empty_selector_never_falls_back_to_global_updates(self) -> None:
        self.save({"review_loop": {"focused_reviews": ["performance"]}})
        before = self.config.read_bytes()
        for selector in ("", " \t "):
            result = self.cli("set", "--selector", selector, "--review-focus", "security", ok=False)
            self.assertIn("--selector", result.stderr)
            self.assertEqual(self.config.read_bytes(), before)

    def test_repair_legacy_and_metadata_preservation(self) -> None:
        for settings in ({"focused_reviews": ["legacy prompt"]}, "malformed", {"unknown": True},
                         {"focused_review_models": {"security": {"cursor": "strong"}}}):
            self.save({"review_loop": settings})
            before = self.config.read_bytes()
            data = json.loads(self.cli("show", "--section", "paths", "--section", "branch_prefix").stdout)
            self.assertEqual(set(data), {"paths", "branch_prefix"})
            self.cli("init", "--preserve-review-loop")
            self.assertEqual(json.loads(self.config.read_text())["review_loop"], settings)
            self.cli("show", ok=False)
        self.save({"review_loop": {"focused_reviews": ["legacy prompt"]}})
        self.cli("set", "--review-focus", "security")
        self.assertEqual(json.loads(self.cli("show").stdout)["review_loop"]["focused_reviews"], ["security"])

    def test_model_repairs_and_conflicts_are_atomic(self) -> None:
        self.save({"review_loop": {"focused_review_models": "malformed"}})
        self.cli("set", "--clear-focused-review-models")
        self.cli("set", "--focused-review-model", "security.cursor=default")
        before = self.config.read_bytes()
        self.cli("set", "--focused-review-model", "security.cursor=literal",
                 "--remove-focused-review-model", "security.cursor", ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.save({"review_loop": {"focused_review_models": {"invalid": {"unknown": "bad"}}}})
        self.cli("set", "--remove-focused-review-model", "invalid.unknown")
        self.assertEqual(json.loads(self.cli("show").stdout)["review_loop"]["focused_review_models"], {})


class ReviewMetadataTest(unittest.TestCase):
    setUp = fixtures.ReviewConfigurationTest.setUp
    cli = fixtures.ReviewConfigurationTest.cli
    save = fixtures.ReviewConfigurationTest.save

    def test_worker_metadata_ignores_loop_and_unrequested_settings(self) -> None:
        self.save({"review_loop": "legacy", "paths": "invalid", "branch_prefix": "!",
                   "worker_model": {"codex": "strong"}, "model_tiers": {"codex": {"strong": "chosen"}}})
        before = self.config.read_bytes()
        data = json.loads(self.cli("show", "--section", "providers", "--section", "worker_model",
                                   "--section", "resolved_worker_model").stdout)
        self.assertEqual(set(data), {"providers", "worker_model", "resolved_worker_model"})
        self.assertEqual(data["worker_model"]["codex"], "strong")
        self.assertEqual(data["resolved_worker_model"]["codex"], "chosen")
        self.assertEqual(self.config.read_bytes(), before)
        self.save({"review_loop": "legacy", "worker_model": {"codex": "strong"}})
        self.cli("show", "--section", "providers")
        result = self.cli("show", "--section", "resolved_worker_model", ok=False)
        self.assertIn("worker_model.codex", result.stderr)
