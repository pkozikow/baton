import json
from pathlib import Path
import shutil
import unittest

import test_project_manager as manager_fixtures
import test_project_sessions as fixtures
import provider_fixtures
from baton.providers import PROVIDERS


class ProjectRetroTest(unittest.TestCase):
    setUp = fixtures.ProjectSessionsTest.setUp
    run_command = fixtures.ProjectSessionsTest.run_command
    git = fixtures.ProjectSessionsTest.git
    cli = fixtures.ProjectSessionsTest.cli
    make_repo = fixtures.ProjectSessionsTest.make_repo
    checkout = fixtures.ProjectSessionsTest.checkout
    create = fixtures.ProjectSessionsTest.create
    project = fixtures.ProjectSessionsTest.project
    owner = fixtures.ProjectSessionsTest.owner
    claim = fixtures.ProjectSessionsTest.claim
    start_project = fixtures.ProjectSessionsTest.start_project
    fake_agents = fixtures.ProjectSessionsTest.fake_agents
    extra_cli = fixtures.ProjectSessionsTest.extra_cli
    member = fixtures.ProjectSessionsTest.member
    init_members = fixtures.ProjectSessionsTest.init_members
    add_member = fixtures.ProjectSessionsTest.add_member

    def check_retro_launch(self, provider: str) -> None:
        checkout = self.start_project()
        self.claim(provider=provider)
        coordinator = self.slots / "p3"
        state_path = manager_fixtures.state_file(self.projects)
        before = state_path.read_bytes(), (coordinator / "project.json").read_bytes()
        tip = self.git(coordinator, "rev-parse", "HEAD")
        log = self.fake_agents()
        self.cli("retro", cwd=checkout)
        invocation = json.loads(log.read_text())
        self.assertEqual(invocation["provider"], provider)
        self.assertEqual(Path(invocation["cwd"]).resolve(), coordinator.resolve())
        expected = (["fork", fixtures.SESSION_ID, "--cd", str(coordinator)] if provider == "codex"
                    else ["--resume", fixtures.SESSION_ID, "--fork-session"])
        self.assertEqual(invocation["args"][:-1], expected)
        prompt = invocation["args"][-1]
        source = Path(__file__).resolve().parents[1] / "agent-skills" / "baton-retro" / "SKILL.md"
        self.assertIn(source.read_text(), prompt)
        for value in ("$baton-retro", "example", self.project()["uuid"], provider,
                      fixtures.SESSION_ID, str(Path(self.project()["blackboard"]["directory"]) / "retro.md")):
            self.assertIn(value, prompt)
        self.assertIn("not the owner", prompt.lower())
        self.assertIn("claim", prompt.lower())
        self.assertIn("authoriz", prompt.lower())
        self.assertNotIn("Wait for the user's questions.", prompt)
        self.assertEqual(state_path.read_bytes(), before[0])
        self.assertEqual((coordinator / "project.json").read_bytes(), before[1])
        self.assertEqual(self.git(coordinator, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(coordinator, "status", "--porcelain"), "")
        self.assertFalse((Path(self.project()["blackboard"]["directory"]) / "retro.md").exists())

    def check_fresh_retro(self, provider: str) -> None:
        entry = PROVIDERS.get(provider) or provider_fixtures.EXTRA
        checkout = self.start_project()
        self.extra_cli("session", "claim", "--provider", provider, "--session-id", fixtures.SESSION_ID,
                       "--selector", "example")
        coordinator = self.slots / "p3"
        log = self.fake_agents()
        self.extra_cli("retro", cwd=checkout)
        invocation = json.loads(log.read_text())
        self.assertEqual(invocation["provider"], entry.binary)
        self.assertEqual(Path(invocation["cwd"]).resolve(), coordinator.resolve())
        self.assertEqual(invocation["args"][:-1], entry.start(coordinator, "PROMPT")[1:-1])
        prompt = invocation["args"][-1]
        preamble = prompt.partition("Read and follow the complete skill")[0]
        for value in ("$baton-retro", "fresh retrospective session", "no owner history", "not the owner",
                      self.project()["uuid"], str(Path(self.project()["artifacts_path"]) / "retro.md")):
            self.assertIn(value, preamble)
        for value in ("inherited", "Git history", "fork of the project's owner session"):
            self.assertNotIn(value, preamble)

    def test_agy_retro_starts_fresh_session_without_owner_history(self) -> None:
        self.check_fresh_retro("agy")

    def test_extra_provider_retro_starts_fresh_session_without_owner_history(self) -> None:
        self.check_fresh_retro(provider_fixtures.EXTRA.name)

    def test_retro_exports_readable_selected_manifest_context(self) -> None:
        self.start_project()
        self.claim()
        log = self.fake_agents()
        self.cli("retro", "--selector", "example")
        prompt = json.loads(log.read_text())["args"][-1]
        for value in ("Project context", "Owner provider: codex", "Repository: alpha", "Branch plan:",
                      "task assignments", "only done", "Snapshot sequence:"):
            self.assertIn(value, prompt)
        directory = Path(self.project()["blackboard"]["directory"]) / ".blackboard-export"
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["context"]["uuid"], self.project()["uuid"])
        overview = (directory / "overview.md").read_text()
        for value in (fixtures.SESSION_ID, "Repository: alpha", "plan.md"):
            self.assertIn(value, overview)

    def test_codex_retro_forks_owner_with_skill_context_without_lock_or_mutation(self) -> None:
        self.check_retro_launch("codex")

    def test_claude_retro_forks_owner_with_skill_context_without_lock_or_mutation(self) -> None:
        self.check_retro_launch("claude")

    def test_infers_coordinator_code_members_and_subdirectories(self) -> None:
        first = self.init_members()
        other = self.add_member("docs")
        self.claim()
        log = self.fake_agents()
        for directory in (first, other, self.slots / "p3", first / "subdirectory"):
            directory.mkdir(exist_ok=True)
            with self.subTest(directory=directory):
                self.cli("retro", cwd=directory)
                invocation = json.loads(log.read_text().splitlines()[-1])
                self.assertEqual(invocation["args"][:2], ["fork", fixtures.SESSION_ID])

    def test_selector_required_outside_project_and_unrelated_repository_on_same_branch(self) -> None:
        self.start_project()
        self.claim()
        log = self.fake_agents()
        unrelated = self.make_repo("unrelated")
        self.git(unrelated, "switch", "-c", "baton/example")
        for directory in (self.root, self.repos / "alpha", unrelated):
            with self.subTest(directory=directory):
                result = self.cli("retro", cwd=directory, ok=False)
                self.assertIn("--selector is required", result.stderr)
        self.assertFalse(log.exists())
        for selector in ("example", "p3"):
            with self.subTest(selector=selector):
                self.cli("retro", "--selector", selector, cwd=self.root)
                self.assertEqual(json.loads(log.read_text().splitlines()[-1])["args"][:2],
                                 ["fork", fixtures.SESSION_ID])

    def test_switched_detached_and_nested_repositories_require_selector(self) -> None:
        checkout = self.start_project()
        self.claim()
        log = self.fake_agents()
        self.git(checkout, "switch", "-c", "unregistered")
        result = self.cli("retro", cwd=checkout, ok=False)
        self.assertIn("--selector is required", result.stderr)
        self.git(checkout, "checkout", "--detach")
        result = self.cli("retro", cwd=checkout, ok=False)
        self.assertIn("--selector is required", result.stderr)
        self.git(checkout, "switch", "baton/example")
        nested = checkout / "nested"
        self.git(self.root, "init", "-b", "baton/example", str(nested))
        (nested / "README.md").write_text("Independent repository\n")
        self.git(nested, "add", "README.md")
        self.git(nested, "commit", "-m", "Initialize nested repository")
        result = self.cli("retro", cwd=nested, ok=False)
        self.assertIn("--selector is required", result.stderr)
        self.assertFalse(log.exists())

    def test_requires_owner_and_completed_project_operations_before_launch(self) -> None:
        checkout = self.start_project()
        log = self.fake_agents()
        result = self.cli("retro", cwd=checkout, ok=False)
        self.assertIn("has no owner; the first agent must run baton session claim.", result.stderr)
        self.claim()
        state_path = manager_fixtures.state_file(self.projects)
        state = self.project()
        state.update(state="adding", pending_add=["beta"])
        state_path.write_text(json.dumps(state))
        result = self.cli("retro", "--selector", "example", ok=False)
        self.assertIn("outstanding project operation", result.stderr)
        self.assertFalse(log.exists())

    def test_agent_exit_status_is_propagated(self) -> None:
        self.start_project()
        self.claim()
        self.fake_agents()
        self.env["AGENT_EXIT_CODE"] = "23"
        result = self.cli("retro", "--selector", "example", ok=False)
        self.assertEqual(result.returncode, 23)

    def test_skill_is_loaded_from_launcher_checkout_and_unavailable_skill_prevents_launch(self) -> None:
        self.start_project()
        self.claim()
        log = self.fake_agents()
        source = manager_fixtures.SCRIPT.parent.parent
        isolated = self.root / "isolated" / "baton"
        isolated.mkdir(parents=True)
        shutil.copytree(source / "baton", isolated / "baton",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(source / "bin", isolated / "bin")
        shutil.copy2(source / "blackboard.py", isolated / "blackboard.py")
        command = ["bash", str(isolated / "bin" / "baton"), "retro", "--selector", "example"]
        missing = self.run_command(command, self.root, ok=False)
        self.assertIn("baton-retro", missing.stderr)
        self.assertFalse(log.exists())
        skill = isolated / "agent-skills" / "baton-retro" / "SKILL.md"
        skill.mkdir(parents=True)
        unreadable = self.run_command(command, self.root, ok=False)
        self.assertIn("baton-retro", unreadable.stderr)
        self.assertFalse(log.exists())
        skill.rmdir()
        skill.write_text("---\nname: baton-retro\ndisable-model-invocation: true\n---\n"
                         "Isolated checkout retro skill marker.\n")
        self.run_command(command, self.root)
        self.assertIn(skill.read_text(), json.loads(log.read_text())["args"][-1])


if __name__ == "__main__":
    unittest.main()
