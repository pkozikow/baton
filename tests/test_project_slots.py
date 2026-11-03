import json
from pathlib import Path
import unittest

import test_project_manager as fixtures


class ProjectSlotsTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot
    create = fixtures.ProjectManagerTest.create
    checkout = fixtures.ProjectManagerTest.checkout

    def status(self, selector: str | None = None, *, cwd: Path | None = None) -> dict:
        args = ["status", "--json"] + ([selector] if selector else [])
        return json.loads(self.cli(*args, cwd=cwd).stdout)

    def test_b_slot_directory_cannot_create_a_project(self) -> None:
        self.make_repo("alpha")
        self.ensure_slot("b3")
        failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "b3", ok=False)
        self.assertIn("Enter a coordinator slot", failed.stderr)
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_unsupported_active_slot_is_rejected_without_mutation(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        source = fixtures.state_file(self.projects)
        project = json.loads(source.read_text())
        project["slot"] = "b3"
        unsupported = fixtures.state_file(self.projects, "b3")
        unsupported.write_text(json.dumps(project))
        source.unlink()
        before = unsupported.read_bytes()
        for args in (("list",), ("status", "example"), ("init",)):
            with self.subTest(args=args):
                failed = self.cli(*args, ok=False)
                self.assertIn("Unsupported active coordinator slot b3", failed.stderr)
                self.assertEqual(unsupported.read_bytes(), before)

    def test_invalid_active_record_is_rejected_without_mutation(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        path = fixtures.state_file(self.projects)
        original = json.loads(path.read_text())
        records = ((original | {"slot": "b3"}, "does not match"),
                   (original | {"slot": "p4"}, "does not match"),
                   ([], "must be a JSON object"))
        for record, message in records:
            with self.subTest(record=record):
                path.write_text(json.dumps(record))
                before = path.read_bytes()
                failed = self.cli("list", ok=False)
                self.assertIn(message, failed.stderr)
                self.assertNotIn("Traceback", failed.stderr)
                self.assertEqual(path.read_bytes(), before)

    def test_b_slot_names_are_ordinary_project_names(self) -> None:
        self.make_repo("alpha")
        self.create("alpha", name="b3")
        self.assertEqual(self.status("b3")["slot"], "p3")
        self.cli("rename", "b10", "b3")
        self.assertEqual(self.status("b10")["slot"], "p3")

    def test_fresh_initialization_creates_only_ten_p_slots_and_is_idempotent(self) -> None:
        self.projects = self.root / "fresh-projects"
        self.slots = self.root / "fresh-slots"
        self.env.update(BATON_REPO=str(self.projects), BATON_SLOTS_ROOT=str(self.slots))
        self.cli("init")
        expected = [f"p{number}" for number in range(1, 11)]
        self.assertEqual(set(path.name for path in self.slots.iterdir()), set(expected))
        for slot in expected:
            self.assertEqual(self.git(self.slots / slot, "branch", "--show-current"), slot)
        before = self.git(self.projects, "worktree", "list", "--porcelain")
        self.cli("init")
        self.assertEqual(self.git(self.projects, "worktree", "list", "--porcelain"), before)
        self.assertEqual(self.cli("list").stdout.splitlines(), [f"{slot}: idle" for slot in expected])
        self.assertIn("p1", (self.projects / "README.md").read_text())

    def test_p_slots_remain_independent_across_init_and_cleanup(self) -> None:
        self.make_repo("alpha")
        self.make_repo("beta")
        self.create("alpha", slot="p3", name="first")
        first_file = fixtures.state_file(self.projects, "p3")
        before = first_file.read_bytes()
        tip = self.git(self.slots / "p3", "rev-parse", "HEAD")
        self.cli("init")
        self.assertEqual(first_file.read_bytes(), before)
        self.assertEqual(self.git(self.slots / "p3", "rev-parse", "HEAD"), tip)
        self.create("beta", slot="p4", name="second")
        for selector, name, slot in (("p3", "first", "p3"), ("first", "first", "p3"),
                                     ("p4", "second", "p4"), ("second", "second", "p4")):
            with self.subTest(selector=selector):
                data = self.status(selector)
                self.assertEqual((data["name"], data["slot"]), (name, slot))
                self.assertEqual(self.cli("path", selector).stdout.strip(), str(self.slots / slot))
        self.assertEqual(self.status(cwd=self.slots / "p4")["slot"], "p4")
        self.assertEqual(self.status(cwd=self.checkout("beta", "second"))["slot"], "p4")
        lines = self.cli("list").stdout.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertIn("p3: first [active] alpha", lines)
        self.assertIn("p4: second [active] beta", lines)
        second_file = fixtures.state_file(self.projects, "p4")
        second_before = second_file.read_bytes()
        self.cli("cleanup", "p3")
        self.assertEqual(second_file.read_bytes(), second_before)
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")
        self.assertIn("p3: idle", self.cli("list").stdout.splitlines())
        self.cli("cleanup", "p4")
        archived = fixtures.read_state(self.projects)["archive"]
        self.assertEqual({entry["slot"] for entry in archived}, {"p3", "p4"})

    def test_p_slot_rename_owner_blackboard_and_handoff(self) -> None:
        self.make_repo("alpha")
        self.create("alpha", slot="p10")
        session_id = "2fac417b-93f7-4d24-b236-61b37672c72f"
        self.cli("session", "claim", "--provider", "codex", "--session-id", session_id, "--selector", "p10")
        self.assertEqual(json.loads(self.cli("session", "show", cwd=self.slots / "p10").stdout)["session_id"], session_id)
        self.cli("rename", "renamed", "p10")
        data = self.status("p10")
        self.assertEqual(data["name"], "renamed")
        self.assertEqual(data["slot"], "p10")
        self.assertEqual(data["owner"]["session_id"], session_id)
        handoff = self.cli("handoff", "--selector", "p10", "--repo", "alpha", "--agent", "worker").stdout
        self.assertIn(str(self.checkout("alpha", "renamed")), handoff)
        board = json.loads(self.cli("blackboard", "read", "--selector", "p10", "--json").stdout)
        self.assertTrue(board["generation"])
        self.cli("cleanup", "p10")
        self.assertEqual(self.git(self.slots / "p10", "branch", "--show-current"), "p10")

    def test_p_slots_are_reserved_for_create_and_rename(self) -> None:
        self.make_repo("alpha")
        self.ensure_slot("p3")
        for number in (1, 3, 10):
            name = f"p{number}"
            with self.subTest(name=name):
                failed = self.cli("create", name, "--repos", "alpha", cwd=self.slots / "p3", ok=False)
                self.assertIn("reserved", failed.stderr)
        self.create("alpha", slot="p3")
        before = fixtures.state_file(self.projects, "p3").read_bytes()
        for name in ("p1", "p3", "p10"):
            failed = self.cli("rename", name, "p3", ok=False)
            self.assertIn("reserved", failed.stderr)
            self.assertEqual(fixtures.state_file(self.projects, "p3").read_bytes(), before)

    def test_pending_operations_remain_listed(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        path = fixtures.state_file(self.projects)
        data = json.loads(path.read_text())
        data["state"] = "cleaning"
        path.write_text(json.dumps(data))
        self.assertIn("p3: example [cleaning] alpha", self.cli("list").stdout)
