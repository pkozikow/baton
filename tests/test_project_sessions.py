import datetime
import json
from pathlib import Path
import shlex
import sys
import unittest
from typing import Any

import test_project_branches as fixtures
import provider_fixtures
from baton.providers import PROVIDERS, session_variables


SESSION_ID = "2fac417b-93f7-4d24-b236-61b37672c72f"
OTHER_SESSION_ID = "4769f45b-d7c0-45ec-a712-05b38ce9044f"


class ProjectSessionsTest(unittest.TestCase):
    setUp = fixtures.ProjectBranchesTest.setUp
    run_command = fixtures.ProjectBranchesTest.run_command
    git = fixtures.ProjectBranchesTest.git
    cli = fixtures.ProjectBranchesTest.cli
    make_repo = fixtures.ProjectBranchesTest.make_repo
    checkout = fixtures.ProjectBranchesTest.checkout
    create = fixtures.ProjectBranchesTest.create
    native = fixtures.ProjectBranchesTest.native
    commit_file = fixtures.ProjectBranchesTest.commit_file
    member = fixtures.ProjectBranchesTest.member
    init_members = fixtures.ProjectBranchesTest.init_members
    add_member = fixtures.ProjectBranchesTest.add_member
    manager_dir = fixtures.fixtures.ProjectManagerTest.manager_dir
    held = fixtures.fixtures.ProjectManagerTest.held
    spawn = fixtures.fixtures.ProjectManagerTest.spawn
    wait_for = fixtures.fixtures.ProjectManagerTest.wait_for
    finish = fixtures.fixtures.ProjectManagerTest.finish

    def project(self) -> dict[str, Any]:
        return json.loads(fixtures.fixtures.state_file(self.projects).read_text())

    def owner(self, selector: str = "example") -> dict[str, Any] | None:
        return json.loads(self.cli("session", "show", "--selector", selector).stdout)

    def claim(self, provider: str = "codex", session_id: str = SESSION_ID,
              selector: str = "example") -> None:
        self.cli("session", "claim", "--provider", provider, "--session-id", session_id,
                 "--selector", selector)

    def start_project(self) -> Path:
        self.make_repo("alpha")
        self.create("alpha")
        return self.checkout("alpha")

    def extra_cli(self, *args: str, cwd: Path | None = None, ok: bool = True) -> Any:
        return self.run_command([*provider_fixtures.LAUNCHER, *args], cwd or self.root, ok=ok)

    def fake_agents(self) -> Path:
        directory = self.root / "bin"
        directory.mkdir()
        log = self.root / "agent-invocations.jsonl"
        variables = sorted(session_variables() | {provider_fixtures.EXTRA.session_env})
        script = "#!" + sys.executable + "\n" + f"SESSION_VARIABLES = {variables!r}\n" + '''import fcntl
import json
import os
from pathlib import Path
import sys

lock = Path(os.environ["BATON_REPO"]) / ".git/baton/locks/p3.lock"
with lock.open("a+b") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {"provider": Path(sys.argv[0]).name, "args": sys.argv[1:], "cwd": os.getcwd(),
              "session_env": sorted(name for name in SESSION_VARIABLES if name in os.environ)}
    with open(os.environ["AGENT_INVOCATIONS"], "a") as output:
        output.write(json.dumps(record) + "\\n")
sys.exit(int(os.environ.get("AGENT_EXIT_CODE", "0")))
'''
        for binary in {entry.binary for entry in PROVIDERS.values()} | {provider_fixtures.EXTRA.binary}:
            executable = directory / binary
            executable.write_text(script)
            executable.chmod(0o755)
        self.env["PATH"] = str(directory) + ":" + self.env["PATH"]
        self.env["AGENT_INVOCATIONS"] = str(log)
        return log

    def test_unowned_project_show_and_status(self) -> None:
        self.start_project()
        self.assertIsNone(self.owner())
        status = json.loads(self.cli("status", "example", "--json").stdout)
        self.assertIsNone(status["owner"])
        self.assertEqual(status["uuid"], self.project()["uuid"])

    def test_claim_is_immutable_idempotent_and_committed_to_manifest(self) -> None:
        self.start_project()
        self.claim()
        owner = self.owner()
        self.assertEqual(set(owner), {"provider", "session_id", "claimed_at"})
        self.assertEqual(owner["provider"], "codex")
        self.assertEqual(owner["session_id"], SESSION_ID)
        self.assertIsNotNone(datetime.datetime.fromisoformat(owner["claimed_at"]))
        coordinator = self.slots / "p3"
        committed = json.loads(self.git(coordinator, "show", "HEAD:project.json"))
        self.assertEqual(committed["owner"], owner)
        self.assertEqual(json.loads(self.cli("status", "example", "--json").stdout)["owner"], owner)
        tip = self.git(coordinator, "rev-parse", "HEAD")
        self.claim(session_id=SESSION_ID.upper().replace("-", ""))
        self.assertEqual(self.owner(), owner)
        self.assertEqual(self.git(coordinator, "rev-parse", "HEAD"), tip)
        for provider, session_id in (("codex", OTHER_SESSION_ID), ("claude", SESSION_ID)):
            with self.subTest(provider=provider, session_id=session_id):
                result = self.cli("session", "claim", "--provider", provider, "--session-id", session_id,
                                  "--selector", "example", ok=False)
                self.assertIn("owner", result.stderr.lower())
                self.assertEqual(self.owner(), owner)
        self.assertEqual(self.git(coordinator, "status", "--porcelain"), "")

    def test_codex_claim_uses_current_thread_and_explicit_id_takes_precedence(self) -> None:
        checkout = self.start_project()
        self.env["CODEX_THREAD_ID"] = OTHER_SESSION_ID
        self.cli("session", "claim", "--provider", "codex", "--session-id", SESSION_ID, cwd=checkout)
        self.assertEqual(self.owner()["session_id"], SESSION_ID)
        self.env["CODEX_THREAD_ID"] = SESSION_ID
        self.cli("session", "claim", "--provider", "codex", cwd=checkout)
        self.assertEqual(self.owner()["session_id"], SESSION_ID)

    def test_codex_claim_without_explicit_id_registers_thread_environment(self) -> None:
        checkout = self.start_project()
        self.env["CODEX_THREAD_ID"] = SESSION_ID.upper()
        self.cli("session", "claim", "--provider", "codex", cwd=checkout)
        self.assertEqual(self.owner()["session_id"], SESSION_ID)

    def test_invalid_or_missing_session_identity_does_not_claim(self) -> None:
        self.start_project()
        self.env.pop("CODEX_THREAD_ID", None)
        for args in (("--provider", "codex"), ("--provider", "claude"),
                     ("--provider", "codex", "--session-id", "not-a-session"),
                     ("--provider", "claude", "--session-id", ""),
                     ("--provider", "unknown", "--session-id", SESSION_ID)):
            with self.subTest(args=args):
                self.cli("session", "claim", "--selector", "example", *args, ok=False)
                self.assertIsNone(self.owner())
                self.assertEqual(self.project()["state"], "active")
        self.env["CODEX_THREAD_ID"] = SESSION_ID
        self.cli("session", "claim", "--provider", "claude", "--selector", "example", ok=False)
        self.assertIsNone(self.owner())

    def test_optional_selector_infers_coordinator_code_members_and_subdirectories(self) -> None:
        first = self.init_members()
        docs = self.add_member("docs")
        self.cli("session", "claim", "--provider", "codex", "--session-id", SESSION_ID, cwd=first)
        for directory in (first, docs, self.slots / "p3", first / "subdirectory"):
            directory.mkdir(exist_ok=True)
            with self.subTest(directory=directory):
                self.assertEqual(json.loads(self.cli("session", "show", cwd=directory).stdout), self.owner())

    def test_optional_selector_rejects_unrelated_same_branch_and_nonproject_directory(self) -> None:
        self.start_project()
        self.claim()
        unrelated = self.make_repo("unrelated")
        self.git(unrelated, "switch", "-c", "baton/example")
        for directory in (self.root, self.repos / "alpha", unrelated):
            with self.subTest(directory=directory):
                result = self.cli("session", "show", cwd=directory, ok=False)
                self.assertIn("--selector is required", result.stderr)
                explicit = json.loads(self.cli("session", "show", "--selector", "example", cwd=directory).stdout)
                self.assertEqual(explicit, self.owner())

    def test_optional_selector_rejects_switched_detached_and_nested_repository(self) -> None:
        checkout = self.start_project()
        self.claim()
        self.git(checkout, "switch", "-c", "unregistered")
        result = self.cli("session", "show", cwd=checkout, ok=False)
        self.assertIn("--selector is required", result.stderr)
        self.git(checkout, "checkout", "--detach")
        result = self.cli("session", "show", cwd=checkout, ok=False)
        self.assertIn("--selector is required", result.stderr)
        self.git(checkout, "switch", "baton/example")
        nested = checkout / "nested"
        self.git(self.root, "init", "-b", "baton/example", str(nested))
        (nested / "README.md").write_text("Independent nested repository\n")
        self.git(nested, "add", "README.md")
        self.git(nested, "commit", "-m", "Initialize nested repository")
        result = self.cli("session", "show", cwd=nested, ok=False)
        self.assertIn("--selector is required", result.stderr)

    def test_explicit_name_slot_and_alias_selectors_work_after_rename_and_cleanup_preserves_owner(self) -> None:
        self.start_project()
        self.claim(provider="claude")
        owner = self.owner()
        project_uuid = self.project()["uuid"]
        self.cli("rename", "renamed", "example")
        for selector in ("example", "renamed", "p3"):
            self.assertEqual(self.owner(selector), owner)
        self.assertEqual(self.project()["uuid"], project_uuid)
        committed = json.loads(self.git(self.slots / "p3", "show", "HEAD:project.json"))
        self.assertEqual(committed["owner"], owner)
        self.cli("cleanup", "renamed")
        archive = fixtures.fixtures.read_state(self.projects)["archive"]
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["owner"], owner)
        self.assertEqual(archive[0]["uuid"], project_uuid)

    def test_concurrent_claims_are_serialized_and_only_one_owner_wins(self) -> None:
        self.start_project()
        with self.held(self.manager_dir() / "locks" / "p3.lock"):
            processes = []
            for name, session_id in (("first", SESSION_ID), ("second", OTHER_SESSION_ID)):
                process, log = self.spawn("session", "claim", "--provider", "codex", "--session-id", session_id,
                                          "--selector", "example", cwd=self.root, name=name)
                self.wait_for(log, "waiting for the slot p3 lock")
                self.assertIsNone(process.poll())
                processes.append((name, session_id, process))
        outcomes = [(name, session_id, self.finish(process, name)) for name, session_id, process in processes]
        winners = [session_id for _, session_id, (code, _, _) in outcomes if code == 0]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(self.owner()["session_id"], winners[0])
        self.assertEqual(self.project()["state"], "active")

    def test_interrupted_claim_reports_exact_recovery_and_preserves_identity(self) -> None:
        self.start_project()
        coordinator = self.slots / "p3"
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.cli("session", "claim", "--provider", "codex", "--session-id", SESSION_ID,
                 "--selector", "example", ok=False)
        state = self.project()
        self.assertEqual(state["state"], "claiming")
        self.assertEqual(state["owner"]["session_id"], SESSION_ID)
        status = json.loads(self.cli("status", "example", "--json").stdout)
        self.assertEqual(status["pending_operation"]["operation"], "claiming")
        command = shlex.split(status["pending_operation"]["command"])
        self.assertEqual(command[:3], ["baton", "session", "claim"])
        self.assertEqual(command[command.index("--session-id") + 1], SESSION_ID)
        self.assertEqual(command[command.index("--provider") + 1], "codex")
        self.assertIn("--selector", command)
        self.cli("session", "claim", "--provider", "codex", "--session-id", OTHER_SESSION_ID,
                 "--selector", "example", ok=False)
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.cli(*command[1:])
        self.assertEqual(self.owner(), state["owner"])
        self.assertEqual(self.project()["state"], "active")
        self.assertIsNone(json.loads(self.cli("status", "example", "--json").stdout)["pending_operation"])
        self.assertEqual(self.git(coordinator, "status", "--porcelain"), "")

    def test_resume_and_fork_require_owner_and_no_pending_operation(self) -> None:
        self.start_project()
        log = self.fake_agents()
        for action in ("resume", "fork"):
            self.cli("session", action, "--selector", "example", ok=False)
        self.claim()
        state_path = fixtures.fixtures.state_file(self.projects)
        state = self.project()
        state.update(state="adding", pending_add=["beta"])
        state_path.write_text(json.dumps(state))
        for action in ("resume", "fork"):
            result = self.cli("session", action, "--selector", "example", ok=False)
            self.assertIn("outstanding project operation", result.stderr)
        self.assertFalse(log.exists())

    def set_session_variables(self) -> None:
        for variable in session_variables() | {provider_fixtures.EXTRA.session_env}:
            self.env[variable] = OTHER_SESSION_ID

    def check_native_launch(self, provider: str) -> None:
        checkout = self.start_project()
        log = self.fake_agents()
        self.claim(provider=provider)
        owner = self.owner()
        self.set_session_variables()
        for action in ("resume", "fork"):
            with self.subTest(action=action):
                self.cli("session", action, cwd=checkout)
                invocation = json.loads(log.read_text().splitlines()[-1])
                self.assertEqual(invocation["provider"], provider)
                self.assertEqual(invocation["session_env"], [provider_fixtures.EXTRA.session_env])
                self.assertEqual(Path(invocation["cwd"]).resolve(), (self.slots / "p3").resolve())
                if provider == "codex":
                    expected = [action, SESSION_ID, "--cd", str(self.slots / "p3")]
                else:
                    expected = ["--resume", SESSION_ID] + (["--fork-session"] if action == "fork" else [])
                args = invocation["args"]
                if action == "resume":
                    self.assertEqual(args, expected)
                else:
                    self.assertEqual(args[:-1], expected)
                    prompt = args[-1]
                    for value in ("example", self.project()["uuid"], SESSION_ID, "explor", "not the owner", "claim"):
                        self.assertIn(value, prompt)
                    self.assertTrue("read-only" in prompt.lower() or "unchanged" in prompt.lower())
                    self.assertIn("explicit", prompt.lower())
                self.assertEqual(self.owner(), owner)

    def test_codex_resume_and_fork_arguments_context_and_unlocked_launch(self) -> None:
        self.check_native_launch("codex")

    def test_claude_resume_and_fork_arguments_context_and_unlocked_launch(self) -> None:
        self.check_native_launch("claude")

    def fresh(self) -> None:
        self.temp.cleanup()
        self.setUp()

    def test_claim_reads_only_the_selected_providers_session_variable(self) -> None:
        for name, entry in (*PROVIDERS.items(), (provider_fixtures.EXTRA.name, provider_fixtures.EXTRA)):
            with self.subTest(provider=name):
                self.fresh()
                self.start_project()
                for variable in session_variables() | {provider_fixtures.EXTRA.session_env}:
                    self.env[variable] = OTHER_SESSION_ID
                if entry.session_env is not None:
                    self.env[entry.session_env] = SESSION_ID.upper()
                claim = ("session", "claim", "--provider", name, "--selector", "example")
                if entry.session_env is None:
                    result = self.extra_cli(*claim, ok=False)
                    self.assertIn("--session-id", result.stderr)
                    self.assertIsNone(self.owner())
                    claim += ("--session-id", SESSION_ID)
                self.extra_cli(*claim)
                self.assertEqual(self.owner()["provider"], name)
                self.assertEqual(self.owner()["session_id"], SESSION_ID)

    def test_unregistered_provider_cannot_claim(self) -> None:
        self.start_project()
        result = self.cli("session", "claim", "--provider", provider_fixtures.EXTRA.name,
                          "--session-id", SESSION_ID, "--selector", "example", ok=False)
        self.assertIn("invalid choice", result.stderr)
        self.assertIsNone(self.owner())

    def check_fresh_launch(self, provider: str) -> None:
        entry = PROVIDERS.get(provider) or provider_fixtures.EXTRA
        checkout = self.start_project()
        log = self.fake_agents()
        self.extra_cli("session", "claim", "--provider", provider, "--session-id", SESSION_ID, "--selector", "example")
        owner = self.owner()
        self.set_session_variables()
        coordinator = self.slots / "p3"
        for action in ("resume", "fork"):
            with self.subTest(action=action):
                self.extra_cli("session", action, cwd=checkout)
                invocation = json.loads(log.read_text().splitlines()[-1])
                self.assertEqual(invocation["provider"], entry.binary)
                self.assertEqual(Path(invocation["cwd"]).resolve(), coordinator.resolve())
                self.assertEqual(invocation["session_env"], [])
                if action == "resume":
                    self.assertEqual(invocation["args"], entry.resume(SESSION_ID, coordinator)[1:])
                    continue
                self.assertEqual(invocation["args"][:-1], entry.start(coordinator, "PROMPT")[1:-1])
                self.assertNotIn(SESSION_ID, invocation["args"][:-1])
                prompt = invocation["args"][-1]
                for value in ("example", self.project()["uuid"], SESSION_ID, "fresh session", "no owner history",
                              "not the owner", "claim", "explicitly authorizes"):
                    self.assertIn(value, prompt)
                self.assertNotIn("inherited its conversation", prompt)
        self.assertEqual(self.owner(), owner)

    def test_agy_resume_and_fresh_fork_fallback(self) -> None:
        self.check_fresh_launch("agy")

    def test_cursor_resume_and_fresh_fork_fallback(self) -> None:
        self.check_fresh_launch("cursor")

    def test_extra_provider_resume_and_fresh_fork_fallback(self) -> None:
        self.check_fresh_launch(provider_fixtures.EXTRA.name)

    def test_binary_override_applies_to_interactive_launches(self) -> None:
        self.start_project()
        log = self.fake_agents()
        self.claim(provider="claude")
        self.env["CLAUDE_BIN"] = str(self.root / "bin" / "agent")
        self.cli("session", "resume", "--selector", "example")
        self.assertEqual(json.loads(log.read_text())["provider"], "agent")

    def archive_project(self, provider: str = "codex") -> tuple[dict[str, Any], Path]:
        self.start_project()
        self.claim(provider=provider)
        project = self.project()
        self.cli("cleanup", "example")
        archive = next((self.manager_dir() / "v2" / "archive").glob("*.json"))
        return project, archive

    def test_archived_forks_use_source_and_frozen_identity_for_every_provider(self) -> None:
        for name, provider in PROVIDERS.items():
            with self.subTest(provider=name):
                self.fresh()
                log = self.fake_agents()
                project, archive = self.archive_project(name)
                frozen = archive.read_bytes()
                self.set_session_variables()
                self.create("alpha", name="replacement")
                replacement = fixtures.fixtures.state_file(self.projects).read_bytes()
                source = Path(project["repos"][0]["source"])
                for selector in ("example", project["uuid"]):
                    self.assertEqual(self.owner(selector), project["owner"])
                    self.cli("session", "fork", "--selector", selector)
                    call = json.loads(log.read_text().splitlines()[-1])
                    self.assertEqual(Path(call["cwd"]).resolve(), source.resolve())
                    self.assertEqual(call["session_env"], [provider_fixtures.EXTRA.session_env])
                    expected = (provider.fork(SESSION_ID, source, "PROMPT") if provider.supports_fork
                                else provider.start(source, "PROMPT"))
                    self.assertEqual(call["args"][:-1], expected[1:-1])
                    prompt = call["args"][-1]
                    for value in (project["uuid"], SESSION_ID, "archived", "not the owner", "Frozen manifest",
                                  "historical", "explicitly", str(Path(project["artifacts_path"]) / "plan.md")):
                        self.assertIn(value, prompt)
                    if not provider.supports_fork:
                        self.assertIn("no owner history", prompt)
                    self.assertEqual(archive.read_bytes(), frozen)
                    self.assertEqual(fixtures.fixtures.state_file(self.projects).read_bytes(), replacement)
                for action in ("resume", "claim"):
                    arguments = ("--provider", "codex", "--session-id", SESSION_ID) if action == "claim" else ()
                    self.cli("session", action, "--selector", project["uuid"], *arguments, ok=False)

    def test_reused_active_name_wins_while_uuid_forks_archive(self) -> None:
        log = self.fake_agents()
        project, archive = self.archive_project()
        self.create("alpha")
        self.claim(provider="claude", session_id=OTHER_SESSION_ID)
        self.assertEqual(self.owner()["session_id"], OTHER_SESSION_ID)
        self.assertEqual(self.owner(project["uuid"]), project["owner"])
        self.cli("session", "fork", "--selector", "example")
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])["provider"], "claude")
        before = archive.read_bytes()
        self.cli("session", "fork", "--selector", project["uuid"])
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])["provider"], "codex")
        self.assertEqual(archive.read_bytes(), before)

    def test_archived_fork_validates_first_source_without_fallback(self) -> None:
        log = self.fake_agents()
        project, archive = self.archive_project()
        original = json.loads(archive.read_text())
        source = Path(project["repos"][0]["source"])
        nested = source / "nested"
        nested.mkdir()
        other = self.make_repo("other")
        cases = [(None, "no valid first repository"), ([], "no valid first repository"),
                 ([None], "no valid first repository"), ([{}], "lacks"),
                 ([project["repos"][0], None], "lacks"),
                 ([dict(project["repos"][0], source=str(self.root / "missing"))], "unavailable"),
                 ([dict(project["repos"][0], source=str(nested))], "exact Git checkout root"),
                 ([dict(project["repos"][0], source=str(other))], "identity changed")]
        for repos, message in cases:
            with self.subTest(repos=repos):
                changed = dict(original, repos=repos)
                archive.write_text(json.dumps(changed))
                before = archive.read_bytes()
                result = self.cli("session", "fork", "--selector", project["uuid"], ok=False)
                self.assertIn(message, result.stderr)
                self.assertEqual(archive.read_bytes(), before)
        self.assertFalse(log.exists())

    def test_archived_missing_owner_binary_session_and_provider_exit(self) -> None:
        self.fake_agents()
        project, archive = self.archive_project()
        original = archive.read_bytes()
        changed = json.loads(original)
        changed["owner"] = None
        archive.write_text(json.dumps(changed))
        result = self.cli("session", "fork", "--selector", project["uuid"], ok=False)
        self.assertIn("cannot start its exploratory session", result.stderr)
        archive.write_bytes(original)
        self.env["CODEX_BIN"] = str(self.root / "missing-agent")
        result = self.cli("session", "fork", "--selector", project["uuid"], ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.env.pop("CODEX_BIN")
        # Missing native history is reported by the provider through its nonzero exit.
        self.env["AGENT_EXIT_CODE"] = "23"
        result = self.cli("session", "fork", "--selector", project["uuid"], ok=False)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(archive.read_bytes(), original)

    def test_native_launch_propagates_exit_status_and_explicit_selector_works_outside(self) -> None:
        self.start_project()
        self.claim()
        self.fake_agents()
        self.env["AGENT_EXIT_CODE"] = "23"
        result = self.cli("session", "resume", "--selector", "example", ok=False)
        self.assertEqual(result.returncode, 23)


if __name__ == "__main__":
    unittest.main()
