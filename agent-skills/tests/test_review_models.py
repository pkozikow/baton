import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_peer_review as peer_fixtures
from provider_fixtures import LAUNCHER  # noqa: E402  (tests/ is on sys.path via test_peer_review)

ROOT = peer_fixtures.ROOT
artifacts_dir = peer_fixtures.artifacts_dir


class ReviewModelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = peer_fixtures.PeerReviewTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.env = self.fixture.repo, self.fixture.env
        self.config = self.fixture.project_config
        self.plan = self.repo / "plan.md"
        self.plan.write_text("# Test plan\nA bounded change.\n")
        self.env["PLAN_REVIEW_PROJECTS_ROOT"] = str(self.repo)
        self.env["REVIEW_TEST_ROOT"] = str(self.repo)
        self.env["PLAN_REVIEW_REVIEWERS"] = "claude,codex,agy"
        self.env["PEER_REVIEW_REVIEWERS"] = "claude,codex,agy"
        self.cli = [str(ROOT / "bin/baton")]
        for name in ("claude", "codex", "agy", "extra", "cursor"):
            binary = self.repo / f"fake-{name}"
            binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name.removeprefix("fake-")
args = sys.argv[1:]
if args[-3:] == ["mcp", "list", "--json"]:
    print("[]")
    raise SystemExit(0)
root = pathlib.Path(os.environ["REVIEW_TEST_ROOT"])
with (root / (name + ".calls")).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if name == "agy":
    message = json.loads(sys.stdin.read())
    assert message["event"] == "user" and message["message"]["content"]
    assert "--print" not in args
else:
    supplied = sys.stdin.read()
    assert supplied
    if name == "cursor":
        assert "You are a senior" in supplied
        assert "## Findings" in supplied
        assert "--mode" in args and args[args.index("--mode") + 1] == "ask"
        assert "--output-format" in args and args[args.index("--output-format") + 1] == "text"
        assert "--print" in args
        assert "--trust" in args
        assert len(args) == 6 + 2 * ("--model" in args) + 2 * args.count("--add-dir"), args
if name == "codex" and os.environ.get("REVIEW_TEST_FAIL_REASONING"):
    effort = args[args.index("--config") + 1]
    if os.environ["REVIEW_TEST_FAIL_REASONING"] in effort:
        raise SystemExit(1)
review = "## Summary\\nReview\\n## Findings\\nNone\\n## Suggested Fixes\\nNone\\n## Suggested Plan Changes\\nNone\\n## Verdict\\nReady\\n"
if name == "codex":
    pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(review)
elif name == "agy":
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": review}}), flush=True)
else:
    print(review, flush=True)
if os.environ.get("REVIEW_TEST_SLEEP"):
    time.sleep(float(os.environ["REVIEW_TEST_SLEEP"]))
if os.environ.get("REVIEW_TEST_FAIL_PROVIDER") == name:
    raise SystemExit(1)
''')
            binary.chmod(0o755)
            self.env[name.upper() + "_BIN"] = str(binary)

    def run_review(self, kind: str, success: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, list[list[str]]]]:
        command = [*self.cli, "review", "run", f"--kind={kind}", str(self.plan) if kind == "plan" else "main"]
        result = subprocess.run(command, cwd=self.repo, env=self.env, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        calls = {}
        for path in self.repo.glob("*.calls"):
            calls[path.stem] = [json.loads(line) for line in path.read_text().splitlines()]
            path.unlink()
        return result, calls

    def test_tiers_resolve_per_harness_in_both_review_workflows(self) -> None:
        providers = ("codex", "claude", "agy", "cursor")
        config = {"model_tiers": {name: {tier: f"{name}-{tier}-model" for tier in ("strong", "medium", "weak")}
                                  for name in providers}}
        self.env.update(PLAN_REVIEW_REVIEWERS=",".join(providers), PEER_REVIEW_REVIEWERS=",".join(providers))
        for tier in ("strong", "medium", "weak"):
            for key in ("plan_review_model", "implementation_review_model"):
                config[key] = {name: tier for name in providers}
            self.config.write_text(json.dumps(config))
            for kind in ("plan", "implementation"):
                with self.subTest(tier=tier, kind=kind):
                    result, calls = self.run_review(kind)
                    self.assertEqual(set(calls), set(providers))
                    for name, entries in calls.items():
                        args = entries[0]
                        self.assertEqual(args[args.index("--model") + 1], f"{name}-{tier}-model")
                    self.assertTrue(list(artifacts_dir(self, result).glob(f"*-codex-codex-{tier}-model.md")))

    def test_missing_tier_mapping_never_invokes_any_harness(self) -> None:
        for provider in ("codex", "claude", "agy", "cursor"):
            config = {"model_tiers": {name: {"strong": f"{name}-actual"} for name in
                                      ("codex", "claude", "agy", "cursor") if name != provider}}
            for key in ("plan_review_model", "implementation_review_model"):
                config[key] = {provider: "strong"}
            self.config.write_text(json.dumps(config))
            for kind in ("plan", "implementation"):
                with self.subTest(provider=provider, kind=kind):
                    result, calls = self.run_review(kind, success=False)
                    self.assertEqual(calls, {})
                    self.assertIn("strong", result.stderr)
                    self.assertIn(provider, result.stderr)

    def test_codex_retry_uses_resolved_tier_model(self) -> None:
        self.config.write_text(json.dumps({"model_tiers": {"codex": {"strong": "pinned-strong"}},
                                          "plan_review_model": {"codex": "strong"},
                                          "implementation_review_model": {"codex": "strong"}}))
        self.env.update(PLAN_REVIEW_REVIEWERS="codex", PEER_REVIEW_REVIEWERS="codex",
                        PLAN_REVIEW_CODEX_REASONING="high", PEER_REVIEW_CODEX_REASONING="high",
                        REVIEW_TEST_FAIL_REASONING="high")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                _, calls = self.run_review(kind)
                self.assertEqual(len(calls["codex"]), 2)
                for args in calls["codex"]:
                    self.assertEqual(args[args.index("--model") + 1], "pinned-strong")

    def test_cursor_is_not_run_or_saved_by_default(self) -> None:
        self.config.write_text("{}")
        original = self.config.read_bytes()
        for key in ("PLAN_REVIEW_REVIEWERS", "PEER_REVIEW_REVIEWERS"):
            self.env.pop(key)
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                _, calls = self.run_review(kind)
                self.assertEqual(set(calls), {"codex", "claude", "agy"})
                self.assertEqual(self.config.read_bytes(), original)

    def test_cursor_can_be_selected_without_a_model_setting(self) -> None:
        self.config.write_text("{}")
        self.env.update(PLAN_REVIEW_REVIEWERS="cursor", PEER_REVIEW_REVIEWERS="cursor")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind)
                self.assertEqual(set(calls), {"cursor"})
                self.assertNotIn("--model", calls["cursor"][0])
                self.assertTrue(list(artifacts_dir(self, result).glob("*-cursor.md")))
                self.assertEqual(self.config.read_text(), "{}")

    def test_cursor_model_configuration_enables_only_that_review_kind(self) -> None:
        self.config.write_text(json.dumps({"plan_review_model": {"cursor": "plan-cursor"}}))
        for key in ("PLAN_REVIEW_REVIEWERS", "PEER_REVIEW_REVIEWERS"):
            self.env.pop(key)
        _, calls = self.run_review("plan")
        self.assertEqual(set(calls), {"cursor"})
        args = calls["cursor"][0]
        self.assertEqual(args[args.index("--model") + 1], "plan-cursor")
        _, calls = self.run_review("implementation")
        self.assertEqual(set(calls), {"codex", "claude", "agy"})

    def test_default_reviewers_follow_config_additions_and_removals(self) -> None:
        for key in ("PLAN_REVIEW_REVIEWERS", "PEER_REVIEW_REVIEWERS"):
            self.env.pop(key)
        configurations = (
            {"plan_review_model": {"codex": "default", "claude": "plan-claude"},
             "implementation_review_model": {"agy": "default"}},
            {"plan_review_model": {"codex": "default", "cursor": "default"},
             "implementation_review_model": {"claude": "default", "codex": "implementation-codex"}},
            {"plan_review_model": {"claude": "plan-claude"},
             "implementation_review_model": {"cursor": "implementation-cursor"}},
        )
        for config in configurations:
            self.config.write_text(json.dumps(config))
            original = self.config.read_bytes()
            for kind in ("plan", "implementation"):
                expected = config[f"{kind}_review_model"]
                with self.subTest(kind=kind, providers=expected):
                    _, calls = self.run_review(kind)
                    self.assertEqual(set(calls), set(expected))
                    for provider, model in expected.items():
                        args = calls[provider][0]
                        if model == "default":
                            self.assertNotIn("--model", args)
                        else:
                            self.assertEqual(args[args.index("--model") + 1], model)
                    self.assertEqual(self.config.read_bytes(), original)

    def test_empty_review_maps_do_not_fall_back_to_default_providers(self) -> None:
        self.config.write_text(json.dumps({"plan_review_model": {}, "implementation_review_model": {},
                                          "worker_model": {"codex": "worker-model"}}))
        for key in ("PLAN_REVIEW_REVIEWERS", "PEER_REVIEW_REVIEWERS"):
            self.env.pop(key)
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind, success=False)
                self.assertEqual(calls, {})
                self.assertIn("reviewer list must contain at least one reviewer", result.stderr)
                self.assertNotIn("REVIEW_ARTIFACTS=", result.stdout)

    def test_explicit_reviewer_override_can_select_an_unconfigured_provider(self) -> None:
        self.config.write_text(json.dumps({"plan_review_model": {"claude": "default"},
                                          "implementation_review_model": {"claude": "default"}}))
        self.env.update(PLAN_REVIEW_REVIEWERS="codex", PEER_REVIEW_REVIEWERS="codex")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                _, calls = self.run_review(kind)
                self.assertEqual(set(calls), {"codex"})
                self.assertNotIn("--model", calls["codex"][0])

    def test_cursor_honors_each_review_kind_model_and_default(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS="cursor", PEER_REVIEW_REVIEWERS="cursor")
        for model in ("default", "vendor/model with spaces"):
            self.config.write_text(json.dumps({key: {"cursor": model} for key in
                                              ("plan_review_model", "implementation_review_model")}))
            for kind in ("plan", "implementation"):
                with self.subTest(kind=kind, model=model):
                    _, calls = self.run_review(kind)
                    args = calls["cursor"][0]
                    if model == "default":
                        self.assertNotIn("--model", args)
                    else:
                        self.assertEqual(args[args.index("--model") + 1], model)

    def test_distinct_configured_models_for_each_provider_and_review_kind(self) -> None:
        config = {f"{kind}_review_model": {name: f"{kind}-{name}" for name in ("claude", "codex", "agy")}
                  for kind in ("plan", "implementation")}
        self.config.write_text(json.dumps(config))
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind)
                self.assertEqual(set(calls), {"claude", "codex", "agy"})
                for name, entries in calls.items():
                    self.assertEqual(len(entries), 1)
                    args = entries[0]
                    self.assertEqual(args[args.index("--model") + 1], f"{kind}-{name}")
                artifacts_dir(self, result)

    def test_default_ignores_harness_configuration(self) -> None:
        (self.fixture.config / "config.toml").write_text('model = "harness-codex"\n')
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                _, calls = self.run_review(kind)
                for entries in calls.values():
                    self.assertNotIn("--model", entries[0])

    def test_models_with_spaces_and_shell_characters_are_single_arguments(self) -> None:
        model = "vendor/model with 'quotes' $(touch UNEXPECTED)"
        self.config.write_text(json.dumps({f"{kind}_review_model": {name: model for name in ("claude", "codex", "agy")}
                                          for kind in ("plan", "implementation")}))
        slug = re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9._-]+", "-", re.sub(r"[/\s]", "-", model)))
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind)
                for entries in calls.values():
                    self.assertEqual(entries[0][entries[0].index("--model") + 1], model)
                self.assertTrue(list(artifacts_dir(self, result).glob(f"*-codex-{slug}.md")))
                self.assertFalse((self.repo / "UNEXPECTED").exists())

    def test_invalid_configuration_stops_before_invoking_reviewers(self) -> None:
        for invalid in ('{', '{"plan_review_model": []}', '{"implementation_review_model": {"agy": " "}}'):
            self.config.write_text(invalid)
            for kind in ("plan", "implementation"):
                with self.subTest(kind=kind, invalid=invalid):
                    result, calls = self.run_review(kind, success=False)
                    self.assertIn("baton config:", result.stderr)
                    self.assertEqual(calls, {})
                    self.assertNotIn("REVIEW_ARTIFACTS=", result.stdout)

    def test_unknown_reviewer_is_rejected(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS="codex,cluade", PEER_REVIEW_REVIEWERS="codex,cluade")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind, success=False)
                self.assertIn("unknown reviewer 'cluade'", result.stderr)
                self.assertEqual(calls, {})

    def test_new_provider_needs_only_a_registry_entry(self) -> None:
        # The launcher registers the test-only provider before running the branch CLI.
        self.config.write_text(json.dumps({"plan_review_model": {"extra": "plan-extra"},
                                          "implementation_review_model": {"extra": "medium"},
                                          "model_tiers": {"extra": {"medium": "extra-medium"}}}))
        for key in ("PLAN_REVIEW_REVIEWERS", "PEER_REVIEW_REVIEWERS"):
            self.env.pop(key)
        self.cli = LAUNCHER
        for kind, model in (("plan", "plan-extra"), ("implementation", "extra-medium")):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind)
                self.assertEqual(set(calls), {"extra"})
                args = calls["extra"][0]
                self.assertEqual(args[:2], ["--model", model])
                self.assertEqual(len(args), 3)
                self.assertTrue(args[2].startswith("You are a senior"))
                self.assertTrue(list(artifacts_dir(self, result).glob("*-extra.md")))
                self.assertIn(f"extra model: {model}", result.stdout)

    def test_comma_only_reviewer_list_reports_invalid_selection(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS=",", PEER_REVIEW_REVIEWERS=",")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind, success=False)
                self.assertIn("reviewer list must contain at least one reviewer", result.stderr)
                self.assertEqual(calls, {})

    def test_failed_stdout_is_a_failure_in_partial_results(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS="claude,agy", PEER_REVIEW_REVIEWERS="claude,agy",
                        REVIEW_TEST_FAIL_PROVIDER="claude")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, _ = self.run_review(kind)
                self.assertIn("claude review: failed (error)", result.stdout)
                run_dir = artifacts_dir(self, result)
                self.assertFalse(list(run_dir.glob("*-claude.md")))
                self.assertTrue(list(run_dir.glob("*-claude.md.failed")))

    def test_nonzero_stdout_harness_is_not_counted_as_success(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS="claude", PEER_REVIEW_REVIEWERS="claude",
                        REVIEW_TEST_FAIL_PROVIDER="claude")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind, success=False)
                self.assertIn("All review jobs failed.", result.stderr)
                self.assertEqual(set(calls), {"claude"})
                run_dir = artifacts_dir(self, result)
                self.assertFalse(list(run_dir.glob("*-claude.md")))
                self.assertTrue(list(run_dir.glob("*-claude.md.failed")))
                self.assertIn("claude log:", result.stderr)
                self.assertNotIn("Review file:", result.stderr)

    def test_harness_timeout_is_not_counted_as_success(self) -> None:
        self.env.update(PLAN_REVIEW_REVIEWERS="claude", PEER_REVIEW_REVIEWERS="claude",
                        # Long enough for the stub to start under parallel test load.
                        PLAN_REVIEW_TIMEOUT_SECS="4", PEER_REVIEW_TIMEOUT_SECS="4", REVIEW_TEST_SLEEP="15")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind, success=False)
                self.assertIn("All review jobs failed.", result.stderr)
                self.assertEqual(set(calls), {"claude"})
                logs = list(artifacts_dir(self, result).glob("*-claude.log"))
                self.assertIn("Timed out after 4s", logs[0].read_text())
                self.assertFalse(list(logs[0].parent.glob("*-claude.md")))
                self.assertTrue(list(logs[0].parent.glob("*-claude.md.failed")))
                self.assertNotIn("Review file:", result.stderr)

    def test_codex_retry_preserves_configured_model(self) -> None:
        self.config.write_text(json.dumps({"plan_review_model": {"codex": "pinned"},
                                          "implementation_review_model": {"codex": "pinned"}}))
        self.env.update(PLAN_REVIEW_REVIEWERS="codex", PEER_REVIEW_REVIEWERS="codex",
                        PLAN_REVIEW_CODEX_REASONING="high", PEER_REVIEW_CODEX_REASONING="high",
                        REVIEW_TEST_FAIL_REASONING="high")
        for kind in ("plan", "implementation"):
            with self.subTest(kind=kind):
                result, calls = self.run_review(kind)
                self.assertEqual(len(calls["codex"]), 2)
                for args in calls["codex"]:
                    self.assertEqual(args[args.index("--model") + 1], "pinned")
                self.assertIn("retrying with medium", result.stderr)


if __name__ == "__main__":
    unittest.main()
