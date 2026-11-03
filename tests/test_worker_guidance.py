import json
from pathlib import Path
import unittest

import test_project_sessions as fixtures
from baton.providers import PROVIDERS, provider_guidance


class WorkerGuidanceTest(unittest.TestCase):
    setUp = fixtures.ProjectSessionsTest.setUp
    run_command = fixtures.ProjectSessionsTest.run_command
    git = fixtures.ProjectSessionsTest.git
    cli = fixtures.ProjectSessionsTest.cli
    make_repo = fixtures.ProjectSessionsTest.make_repo
    checkout = fixtures.ProjectSessionsTest.checkout
    create = fixtures.ProjectSessionsTest.create
    start_project = fixtures.ProjectSessionsTest.start_project
    claim = fixtures.ProjectSessionsTest.claim

    def status(self) -> dict:
        return json.loads(self.cli("status", "example", "--json").stdout)

    def test_missing_and_explicit_default_provide_no_guidance(self) -> None:
        self.start_project()
        for config in ({}, {"worker_model": {"codex": "default", "cursor": "default"}}):
            Path(self.env["BATON_CONFIG_FILE"]).write_text(json.dumps(config))
            data = self.status()
            self.assertEqual(data["worker_model_guidance"], {})
            self.assertEqual(data["worker_model"]["codex"], "default")
            self.assertEqual(data["resolved_worker_model"], data["worker_model"])
            self.assertNotIn("Worker model guidance:", self.cli("status", "example").stdout)

    def test_literal_tier_and_reset_apply_live_without_manifest_changes(self) -> None:
        self.start_project()
        self.claim()
        coordinator = self.slots / "p3"
        tip = self.git(coordinator, "rev-parse", "HEAD")
        manifest = (coordinator / "project.json").read_bytes()
        owner = self.status()["owner"]
        self.cli("config", "set", "--worker-model", "codex=gpt-6.1-sol")
        data = self.status()
        self.assertEqual(data["worker_model"]["codex"], "gpt-6.1-sol")
        self.assertIn("gpt-6.1-sol", data["worker_model_guidance"]["codex"])
        self.assertIn("gpt-6.1-sol", self.cli("status", "example").stdout)
        self.cli("config", "set", "--model-tier", "codex.medium=gpt-6-luna", "--worker-model", "codex=medium")
        data = self.status()
        self.assertEqual(data["worker_model"]["codex"], "medium")
        self.assertEqual(data["resolved_worker_model"]["codex"], "gpt-6-luna")
        config = json.loads(self.cli("config", "show").stdout)
        for key in ("worker_model", "resolved_worker_model"):
            self.assertEqual(data[key], config[key])
        self.assertIn("gpt-6-luna", data["worker_model_guidance"]["codex"])
        self.assertEqual(data["owner"], owner)
        self.cli("config", "set", "--worker-model", "codex=default")
        self.assertEqual(self.status()["worker_model_guidance"], {})
        self.assertEqual((coordinator / "project.json").read_bytes(), manifest)
        self.assertEqual(self.git(coordinator, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(coordinator, "status", "--porcelain"), "")

    def test_codex_owner_only_sees_codex_guidance(self) -> None:
        self.check_owner_guidance("codex", "gpt-6.1-sol")

    def test_claude_owner_only_sees_claude_guidance(self) -> None:
        self.check_owner_guidance("claude", "claude-opus-5-5")

    def check_owner_guidance(self, provider: str, model: str) -> None:
        self.start_project()
        self.cli("config", "set", "--worker-model", "codex=gpt-6.1-sol",
                 "--worker-model", "claude=claude-opus-5-5", "--worker-model", "cursor=cursor-worker")
        self.claim(provider=provider)
        data = self.status()
        self.assertEqual(set(data["worker_model_guidance"]), {provider})
        self.assertIn(model, data["worker_model_guidance"][provider])
        output = self.cli("status", "example").stdout
        self.assertEqual(output.count("Worker model guidance:"), 1)
        self.assertIn(model, output)
        for key in ("worker_model", "resolved_worker_model"):
            self.assertEqual(data[key]["codex"], "gpt-6.1-sol")
            self.assertEqual(data[key]["claude"], "claude-opus-5-5")
            self.assertEqual(data[key]["cursor"], "cursor-worker")
            for other, other_model in data[key].items():
                if other != provider and other_model != "default":
                    self.assertNotIn(other_model, output)

        self.cli("config", "set", "--worker-model", f"{provider}=default")
        self.assertEqual(self.status()["worker_model_guidance"], {})
        self.assertNotIn("Worker model guidance:", self.cli("status", "example").stdout)

    def test_unclaimed_owner_sees_harness_specific_guidance(self) -> None:
        self.start_project()
        self.cli("config", "set", "--worker-model", "claude=claude-worker", "--worker-model", "cursor=cursor-worker")
        data = self.status()
        self.assertIsNone(data["owner"])
        self.assertEqual(set(data["worker_model_guidance"]), {"claude", "cursor"})
        self.assertIn("claude-worker", data["worker_model_guidance"]["claude"])
        self.assertIn("cursor-worker", data["worker_model_guidance"]["cursor"])
        self.assertNotIn("cursor-worker", data["worker_model_guidance"]["claude"])

    def test_provider_guidance_covers_every_provider_and_is_not_owner_filtered(self) -> None:
        self.start_project()
        unowned = self.status()["provider_guidance"]
        self.assertEqual(unowned, provider_guidance())
        self.assertEqual(set(unowned), set(PROVIDERS))
        self.assertIn("CURSOR_CONVERSATION_ID", unowned["cursor"]["session_id_source"])
        self.assertEqual(unowned["agy"]["claim_command"], "baton session claim --provider agy")
        self.claim()
        self.assertEqual(self.status()["provider_guidance"], unowned)
        config = json.loads(self.cli("config", "show").stdout)
        self.assertEqual({name: entry["guidance"] for name, entry in config["providers"].items()}, unowned)


if __name__ == "__main__":
    unittest.main()
