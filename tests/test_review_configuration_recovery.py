"""Real Git failures must leave project configuration safely resumable."""

import json
import unittest

from baton.gitutil import write_json

import test_project_manager as fixtures


class ReviewConfigurationRecoveryTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create

    def fail_next_commit(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text('#!/bin/sh\nrm -- "$0"\nexit 1\n')
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)

    def test_failed_commit_reconciles_before_next_update(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.fail_next_commit()
        self.cli("config", "set", "--selector", "example", "--review-focus", "security", ok=False)
        record = json.loads(fixtures.state_file(self.projects).read_text())
        self.assertIn("pending_manifest", record)
        self.assertEqual(record["review_loop"], {"focused_reviews": ["security"]})
        self.cli("config", "set", "--selector", "example", "--general-review-rounds", "0")
        record = json.loads(fixtures.state_file(self.projects).read_text())
        self.assertNotIn("pending_manifest", record)
        manifest = json.loads((self.slots / "p3/project.json").read_text())
        self.assertEqual(manifest["review_loop"], record["review_loop"])
        self.assertEqual(manifest["review_loop"], {"focused_reviews": ["security"], "general_rounds": 0})
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_pending_retry_preserves_unrelated_edits(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.fail_next_commit()
        self.cli("config", "set", "--selector", "example", "--no-focused-reviews", ok=False)
        manifest = self.slots / "p3/project.json"
        original = manifest.read_text()
        manifest.write_text(original + "user edit\n")
        state = fixtures.state_file(self.projects).read_bytes()
        self.cli("config", "set", "--selector", "example", "--general-review-rounds", "0", ok=False)
        self.assertEqual(manifest.read_text(), original + "user edit\n")
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), state)
        manifest.write_text(original)
        extra = self.slots / "p3/unrelated.txt"
        extra.write_text("preserve me\n")
        self.cli("config", "set", "--selector", "example", "--general-review-rounds", "0", ok=False)
        self.assertEqual(extra.read_text(), "preserve me\n")
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), state)
        extra.unlink()
        self.cli("config", "set", "--selector", "example", "--general-review-rounds", "0")

    def test_preprojection_failure_leaves_registry_unchanged(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        state = fixtures.state_file(self.projects).read_bytes()
        unexpected = self.slots / "p3/repos/unexpected"
        unexpected.write_text("preserve me\n")
        self.cli("config", "set", "--selector", "example", "--review-focus", "security", ok=False)
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), state)
        self.assertEqual(unexpected.read_text(), "preserve me\n")
        unexpected.unlink()
        self.cli("config", "set", "--selector", "example", "--review-focus", "security")

    def test_journal_replays_when_projection_was_not_replaced(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        coordinator = self.slots / "p3"
        previous = (coordinator / "project.json").read_text()
        self.fail_next_commit()
        self.cli("config", "set", "--selector", "example", "--review-focus", "complexity", ok=False)
        # Recreate interruption after journaling but before the atomic projection replacement.
        (coordinator / "project.json").write_text(previous)
        self.git(coordinator, "reset", "HEAD", "--", "project.json")
        self.cli("config", "set", "--selector", "example", "--general-review-rounds", "0")
        record = json.loads(fixtures.state_file(self.projects).read_text())
        projection = json.loads((coordinator / "project.json").read_text())
        self.assertEqual(record["review_loop"], projection["review_loop"])
        self.assertEqual(projection["review_loop"]["focused_reviews"], ["complexity"])
        self.assertNotIn("pending_manifest", record)

    def test_failed_atomic_serialization_preserves_target_and_removes_temporary(self) -> None:
        target = self.root / "project.json"
        original = '{"review_loop": {"focused_reviews": ["security"]}}\n'
        target.write_text(original)
        with self.assertRaises(TypeError):
            write_json(target, {"review_loop": {"focused_reviews": ["complexity"]}, "bad": object()})
        self.assertEqual(target.read_text(), original)
        self.assertEqual(list(self.root.glob(".project.json.*.tmp")), [])
        write_json(target, {"review_loop": {"focused_reviews": ["complexity"]}})
        self.assertEqual(json.loads(target.read_text())["review_loop"]["focused_reviews"], ["complexity"])
