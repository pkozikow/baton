from dataclasses import replace
from itertools import combinations
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baton.errors import ProjectError
from baton.providers import (DEFAULT_TIER_EFFORT, PROMPT_ARGV, PROMPT_STDIN, PROVIDERS, Provider,
                             UnsupportedProviderOperation, get_provider,
                             provider_guidance, scrubbed_environment, session_variables)
from baton.skills_install import FLOOR, default_targets, discovery_conflicts, unmanaged_copies

import provider_fixtures


SESSION = "2fac417b-93f7-4d24-b236-61b37672c72f"
DIRECTORY = Path("/projects/slot")
WORKSPACE = Path("/tmp/baton-retro-x")
PROMPT = "Retro prompt"
BLANKET = ("--force", "--yolo", "bypassPermissions", "danger-full-access", "--add-dir", "workspace-write")


class ProviderContractTest(unittest.TestCase):
    """Every registered provider, including a test-only extra one, honors the registry contract."""

    def setUp(self) -> None:
        self.enterContext(provider_fixtures.registered())
        self.enterContext(patch.dict(os.environ, {entry.binary_env: "" for entry in PROVIDERS.values()}))

    def assert_argv(self, argv: list[str], entry: object) -> None:
        self.assertIsInstance(argv, list)
        self.assertTrue(argv and all(isinstance(value, str) and value for value in argv), argv)
        self.assertEqual(argv[0], getattr(entry, "binary"))

    def test_every_operation_returns_well_formed_argv(self) -> None:
        for name, entry in PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertEqual(get_provider(name), entry)
                self.assert_argv(entry.resume(SESSION, DIRECTORY), entry)
                self.assertIn(SESSION, entry.resume(SESSION, DIRECTORY))
                start = entry.start(DIRECTORY, PROMPT)
                self.assert_argv(start, entry)
                self.assertEqual(start[-1], PROMPT)
                if entry.supports_fork:
                    fork = entry.fork(SESSION, DIRECTORY, PROMPT)
                    self.assert_argv(fork, entry)
                    self.assertEqual(fork[-1], PROMPT)
                    self.assertIn(SESSION, fork)
                else:
                    with self.assertRaises(UnsupportedProviderOperation):
                        entry.fork(SESSION, DIRECTORY, PROMPT)
                self.assertIn(entry.read_confinement, ("enforced", "best_effort"))
                # A tier without a saved effort resolves to the default, so every effort control must accept it.
                self.assertTrue(not entry.efforts or DEFAULT_TIER_EFFORT in entry.efforts)
                self.assertTrue(entry.effort_flag is None or entry.efforts)
                # The shared review invocation only knows the flag; any other delivery needs its own.
                if entry.efforts and entry.effort_flag is None:
                    self.assertIsNot(type(entry).run_review, Provider.run_review)
                guidance = entry.guidance()
                self.assertEqual(set(guidance), {"claim_command", "session_id_source", "fresh_subagent"})
                self.assertTrue(all(guidance.values()), guidance)
                self.assertIn(f"--provider {name}", guidance["claim_command"])
                self.assertEqual("--session-id" in guidance["claim_command"], entry.session_env is None)
                self.assertTrue(entry.skill_dir and not Path(entry.skill_dir).is_absolute())
                self.assertIn(entry.skill_dir, entry.skill_reads)

    def test_unattended_argv_meets_the_safety_floor_or_is_unsupported(self) -> None:
        for name, entry in PROVIDERS.items():
            for session_id in (SESSION, None):
                with self.subTest(provider=name, session_id=session_id):
                    try:
                        argv, delivery = entry.exec_unattended(session_id, WORKSPACE, PROMPT)
                    except UnsupportedProviderOperation:
                        self.assertTrue(session_id is not None and not entry.supports_fork)
                        continue
                    self.assert_argv(argv, entry)
                    self.assertIn(delivery, (PROMPT_ARGV, PROMPT_STDIN))
                    options = [value for value in argv[1:] if PROMPT not in value]
                    self.assertEqual(len(options), len(argv) - 1 - (delivery == PROMPT_ARGV))
                    for value in options:
                        self.assertFalse(value.startswith("--dangerously"), value)
                        self.assertNotIn(value, BLANKET)
                        if value.startswith("/"):
                            self.assertEqual(value, str(WORKSPACE), "unattended runs receive no other directory")
                    self.assertEqual(SESSION in argv, session_id is not None)
                    if name == "claude":
                        # Without --restricted, allow rules from settings files (including a reviewed
                        # checkout's own) would still apply.
                        self.assertIn("--restricted", argv)
                        self.assertNotIn("--allowedTools", argv)

    def test_unsupported_unattended_mode_raises_the_typed_error(self) -> None:
        with patch.dict(os.environ, {"EXTRA_UNATTENDED": "unsupported"}):
            with self.assertRaises(UnsupportedProviderOperation) as raised:
                provider_fixtures.EXTRA.exec_unattended(None, WORKSPACE, PROMPT)
        self.assertIsInstance(raised.exception, ProjectError)

    def test_binary_overrides_apply_to_every_operation(self) -> None:
        for name, entry in PROVIDERS.items():
            with self.subTest(provider=name), patch.dict(os.environ, {entry.binary_env: "/opt/bin/custom"}):
                self.assertEqual(entry.resume(SESSION, DIRECTORY)[0], "/opt/bin/custom")
                self.assertEqual(entry.start(DIRECTORY, PROMPT)[0], "/opt/bin/custom")
                self.assertEqual(entry.exec_unattended(None, WORKSPACE, PROMPT)[0][0], "/opt/bin/custom")

    def test_environment_names_derive_from_the_provider_name(self) -> None:
        hyphenated = replace(provider_fixtures.EXTRA, name="my-cli")
        self.assertEqual((hyphenated.env_name, hyphenated.binary_env), ("MY_CLI", "MY_CLI_BIN"))
        self.assertEqual(PROVIDERS["cursor"].binary_env, "CURSOR_BIN")

    def test_unknown_provider_is_an_error(self) -> None:
        with self.assertRaisesRegex(ProjectError, "Unsupported provider: missing"):
            get_provider("missing")

    def test_session_variables_are_scrubbed_for_children(self) -> None:
        environment = {"PATH": "/bin", "CODEX_THREAD_ID": SESSION, "CLAUDE_CODE_SESSION_ID": SESSION,
                       "CURSOR_CONVERSATION_ID": SESSION, "EXTRA_SESSION_ID": SESSION}
        self.assertEqual(scrubbed_environment(environment), {"PATH": "/bin"})
        self.assertEqual(session_variables(), {"CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID",
                                               "CURSOR_CONVERSATION_ID", "EXTRA_SESSION_ID"})

    def test_guidance_covers_every_provider(self) -> None:
        self.assertEqual(set(provider_guidance()), set(PROVIDERS))


class GoldenArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {entry.binary_env: "" for entry in PROVIDERS.values()}))

    def test_codex(self) -> None:
        codex = PROVIDERS["codex"]
        self.assertEqual(codex.resume(SESSION, DIRECTORY), ["codex", "resume", SESSION, "--cd", "/projects/slot"])
        self.assertEqual(codex.fork(SESSION, DIRECTORY, PROMPT),
                         ["codex", "fork", SESSION, "--cd", "/projects/slot", PROMPT])
        self.assertEqual(codex.start(DIRECTORY, PROMPT), ["codex", "--cd", "/projects/slot", PROMPT])
        self.assertEqual(codex.exec_unattended(SESSION, WORKSPACE, PROMPT),
                         (["codex", "exec", "--config", 'approval_policy="never"', "-C", str(WORKSPACE),
                           "-s", "read-only", "fork", "--skip-git-repo-check", SESSION, "-"], PROMPT_STDIN))
        self.assertEqual(codex.exec_unattended(None, WORKSPACE, PROMPT),
                         (["codex", "exec", "--config", 'approval_policy="never"', "-C", str(WORKSPACE),
                           "-s", "read-only", "--skip-git-repo-check", "-"], PROMPT_STDIN))
        self.assertEqual((codex.session_env, codex.supports_fork, codex.read_confinement),
                         ("CODEX_THREAD_ID", True, "best_effort"))

    def test_claude(self) -> None:
        claude = PROVIDERS["claude"]
        self.assertEqual(claude.resume(SESSION, DIRECTORY), ["claude", "--resume", SESSION])
        self.assertEqual(claude.fork(SESSION, DIRECTORY, PROMPT), ["claude", "--resume", SESSION, "--fork-session", PROMPT])
        self.assertEqual(claude.start(DIRECTORY, PROMPT), ["claude", PROMPT])
        # --tools is variadic, so the prompt must go on stdin rather than after it.
        restricted = ["--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk",
                      "--permission-prompts", "none", "--tools", "Read,Glob,Grep"]
        self.assertEqual(claude.exec_unattended(SESSION, WORKSPACE, PROMPT),
                         (["claude", "--print", "--resume", SESSION, "--fork-session", *restricted], PROMPT_STDIN))
        self.assertEqual(claude.exec_unattended(None, WORKSPACE, PROMPT),
                         (["claude", "--print", *restricted], PROMPT_STDIN))
        self.assertEqual((claude.session_env, claude.supports_fork, claude.read_confinement),
                         ("CLAUDE_CODE_SESSION_ID", True, "enforced"))

    def test_agy(self) -> None:
        agy = PROVIDERS["agy"]
        self.assertEqual(agy.resume(SESSION, DIRECTORY), ["agy", "--conversation", SESSION])
        self.assertEqual(agy.start(DIRECTORY, PROMPT), ["agy", "-i", PROMPT])
        argv, delivery = agy.exec_unattended(None, WORKSPACE, PROMPT)
        self.assertEqual((argv[:2], argv[3:], delivery), (["agy", "--print"], ["--sandbox"], PROMPT_ARGV))
        # agy aborts the whole turn when any tool is denied, so the prompt rules out shell and writes.
        self.assertTrue(argv[2].endswith(PROMPT))
        self.assertIn("Never run shell commands", argv[2])
        with self.assertRaises(UnsupportedProviderOperation):
            agy.exec_unattended(SESSION, WORKSPACE, PROMPT)
        self.assertEqual((agy.session_env, agy.supports_fork, agy.read_confinement),
                         ("ANTIGRAVITY_CONVERSATION_ID", False, "enforced"))
        # A provider without a session variable must tell agents to pass the ID explicitly.
        claim = replace(agy, session_env=None).guidance()["claim_command"]
        self.assertEqual(claim, "baton session claim --provider agy --session-id <current-session-uuid>")

    def test_cursor(self) -> None:
        cursor = PROVIDERS["cursor"]
        self.assertEqual(cursor.resume(SESSION, DIRECTORY), ["agent", "--resume", SESSION])
        self.assertEqual(cursor.start(DIRECTORY, PROMPT), ["agent", PROMPT])
        self.assertEqual(cursor.exec_unattended(None, WORKSPACE, PROMPT),
                         (["agent", "-p", "--mode", "ask", "--sandbox", "enabled", "--trust", "--workspace",
                           str(WORKSPACE), PROMPT], PROMPT_ARGV))
        with self.assertRaises(UnsupportedProviderOperation):
            cursor.exec_unattended(SESSION, WORKSPACE, PROMPT)
        self.assertEqual((cursor.session_env, cursor.supports_fork, cursor.read_confinement, cursor.default_enabled),
                         ("CURSOR_CONVERSATION_ID", False, "best_effort", False))


class SkillTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(provider_fixtures.registered())
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_every_harness_discovers_each_skill_once_for_every_subset_of_present_providers(self) -> None:
        names = list(PROVIDERS)
        for size in range(len(names) + 1):
            for subset in combinations(names, size):
                with self.subTest(present=subset):
                    targets, notes = default_targets(self.home, lambda entry, home: entry.name in subset)
                    self.assertEqual(targets[:len(FLOOR)], [self.home / directory for directory in FLOOR])
                    self.assertEqual(len(targets), len(set(targets)))
                    for name in subset:
                        self.assertIn(PROVIDERS[name].skill_path(self.home), targets)
                    self.assertEqual(discovery_conflicts(targets, self.home), [])
                    for name in set(names) - set(subset):
                        directory = PROVIDERS[name].skill_path(self.home)
                        if directory not in targets:
                            self.assertTrue(any(str(directory) in note and name in note for note in notes), notes)

    def test_presence_uses_path_or_an_existing_skill_directory(self) -> None:
        (self.home / ".extra/skills").mkdir(parents=True)
        with patch.dict(os.environ, {"PATH": str(self.home / "empty")}):
            targets, notes = default_targets(self.home)
        self.assertIn(self.home / ".extra/skills", targets)
        self.assertNotIn(self.home / ".gemini/antigravity-cli/skills", targets)
        self.assertTrue(any("agy" in note for note in notes))
        binaries = self.home / "bin"
        binaries.mkdir()
        (binaries / "agy").write_text("#!/bin/sh\n")
        (binaries / "agy").chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(binaries)}):
            targets, notes = default_targets(self.home)
        self.assertIn(self.home / ".gemini/antigravity-cli/skills", targets)
        self.assertEqual(notes, [])

    def test_duplicate_reads_without_deduplication_are_reported(self) -> None:
        targets = [self.home / ".gemini/antigravity-cli/skills", self.home / ".gemini/config/skills"]
        conflicts = discovery_conflicts(targets, self.home)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("agy", conflicts[0])
        # Cursor collapses same-named skills, so reading both floor targets is fine.
        self.assertEqual(discovery_conflicts([self.home / ".agents/skills", self.home / ".claude/skills",
                                              self.home / ".codex/skills"], self.home), [])

    def test_unmanaged_baton_copies_in_read_directories_are_reported(self) -> None:
        stale = self.home / ".cursor/skills/baton-plan"
        stale.mkdir(parents=True)
        (self.home / ".cursor/skills/custom").mkdir()
        managed = self.home / ".codex/skills"
        (managed / "baton-retro").mkdir(parents=True)
        (managed / ".baton-skills.json").write_text("{}")
        targets = [self.home / directory for directory in FLOOR]
        warnings = unmanaged_copies(targets, self.home)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(self.home / ".cursor/skills"), warnings[0])
        self.assertIn("baton-plan", warnings[0])
        self.assertIn("cursor", warnings[0])
        self.assertTrue(warnings[0].startswith(str(self.home / ".cursor/skills")), warnings[0])
        self.assertTrue(stale.is_dir())
        self.assertEqual(unmanaged_copies([*targets, self.home / ".cursor/skills"], self.home), [])


if __name__ == "__main__":
    unittest.main()
