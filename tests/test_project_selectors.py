import json
import unittest

import test_project_manager as fixtures


class ProjectSelectorsTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot
    create = fixtures.ProjectManagerTest.create
    checkout = fixtures.ProjectManagerTest.checkout

    def two_projects(self) -> tuple[dict, dict]:
        self.make_repo("alpha")
        self.make_repo("beta")
        self.create("alpha", name="first")
        self.create("beta", slot="p4", name="second")
        records = fixtures.read_state(self.projects)["projects"]
        return records["p3"], records["p4"]

    def test_status_flag_and_legacy_resolve_name_slot_uuid_independently_of_cwd(self) -> None:
        first, second = self.two_projects()
        for cwd in (self.root, self.slots / "p4", self.checkout("beta", "second")):
            for selector in (first["name"], first["slot"], first["uuid"]):
                for arguments in (("--selector", selector), (selector,)):
                    with self.subTest(cwd=cwd, arguments=arguments):
                        status = json.loads(self.cli("status", *arguments, "--json", cwd=cwd).stdout)
                        self.assertEqual(status["uuid"], first["uuid"])
        inferred = json.loads(self.cli("status", "--json", cwd=self.slots / "p4").stdout)
        self.assertEqual(inferred["uuid"], second["uuid"])

    def test_unknown_and_blank_explicit_selectors_never_fall_back_to_cwd(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        before = fixtures.state_file(self.projects).read_bytes()
        for selector in ("missing-project", "alpha", "", "   "):
            for arguments in (("status", "--json"), ("path",), ("add", "alpha"),
                              ("rename", "renamed"), ("cleanup", "--dry-run"), ("abandon",)):
                with self.subTest(selector=selector, command=arguments):
                    failed = self.cli(*arguments, "--selector", selector, cwd=self.slots / "p3", ok=False)
                    self.assertNotIn("Traceback", failed.stderr)
                    self.assertEqual(fixtures.state_file(self.projects).read_bytes(), before)
                    self.assertTrue(self.checkout("alpha").is_dir())

    def test_legacy_and_flag_selectors_conflict_even_when_equal(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        before = fixtures.state_file(self.projects).read_bytes()
        for arguments in (("status", "example"), ("rename", "renamed", "example"),
                          ("cleanup", "example", "--dry-run"), ("abandon", "example")):
            with self.subTest(arguments=arguments):
                failed = self.cli(*arguments, "--selector", "example", ok=False)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(fixtures.state_file(self.projects).read_bytes(), before)

    def test_path_flag_repository_member_and_legacy_forms(self) -> None:
        first, _ = self.two_projects()
        for selector in (first["name"], first["slot"], first["uuid"]):
            for arguments, expected in ((("path", selector), self.slots / "p3"),
                                        (("path", selector, "alpha"), self.checkout("alpha", "first")),
                                        (("path", "--selector", selector), self.slots / "p3"),
                                        (("path", "--selector", selector, "alpha"), self.checkout("alpha", "first"))):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.cli(*arguments, cwd=self.slots / "p4").stdout.strip(), str(expected))
        self.cli("branch", "init", "alpha", "initial", "--selector", "first")
        expected = self.cli("path", "first", "alpha", "--member", "initial").stdout.strip()
        self.assertEqual(self.cli("path", "alpha", "--selector", "first", "--member", "initial").stdout.strip(), expected)
        self.assertEqual(self.cli("path", "first", "alpha", "--selector", "first", ok=False).returncode, 2)

    def test_add_and_rename_target_selected_project_instead_of_cwd(self) -> None:
        first, _ = self.two_projects()
        self.make_repo("gamma")
        other = fixtures.state_file(self.projects, "p4").read_bytes()
        self.cli("add", "gamma", "--selector", first["uuid"], cwd=self.slots / "p4")
        self.assertTrue(self.checkout("gamma", "first").is_dir())
        self.assertFalse(self.checkout("gamma", "second").exists())
        self.cli("rename", "renamed", "--selector", "p3", cwd=self.slots / "p4")
        self.assertTrue(self.checkout("alpha", "renamed").is_dir())
        self.assertFalse(self.checkout("alpha", "first").exists())
        renamed = json.loads(self.cli("status", "--selector", first["uuid"], "--json").stdout)
        self.assertEqual(renamed["name"], "renamed")
        self.assertEqual(fixtures.state_file(self.projects, "p4").read_bytes(), other)

    def test_cleanup_flag_dry_run_and_execution_preserve_other_project(self) -> None:
        first, _ = self.two_projects()
        before = fixtures.state_file(self.projects).read_bytes()
        other = fixtures.state_file(self.projects, "p4").read_bytes()
        self.cli("cleanup", "--selector", first["uuid"], "--dry-run", cwd=self.slots / "p4")
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout("alpha", "first").is_dir())
        self.cli("cleanup", "--selector", "first", cwd=self.slots / "p4")
        self.assertFalse(self.checkout("alpha", "first").exists())
        self.assertEqual(fixtures.state_file(self.projects, "p4").read_bytes(), other)
        self.assertTrue(self.checkout("beta", "second").is_dir())

    def test_abandon_flag_reaches_noninteractive_guard_without_mutation(self) -> None:
        first, _ = self.two_projects()
        before = fixtures.state_file(self.projects).read_bytes()
        result = self.cli("abandon", "--selector", first["uuid"], cwd=self.slots / "p4", ok=False)
        self.assertIn("Interactive confirmation", result.stderr)
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout("alpha", "first").is_dir())

    def test_create_flag_selects_slot_outside_and_inside_other_project_and_follow_up(self) -> None:
        self.make_repo("alpha")
        self.make_repo("beta")
        self.ensure_slot("p4")
        self.cli("create", "first", "--selector", "p3", "--repos", "alpha")
        first_before = fixtures.state_file(self.projects).read_bytes()
        self.cli("create", "second", "--selector", "p4", "--repos", "beta", cwd=self.slots / "p3")
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), first_before)
        self.assertEqual(json.loads(fixtures.state_file(self.projects, "p4").read_text())["name"], "second")
        for selector in ("first", "p0", "p11", ""):
            with self.subTest(selector=selector):
                self.assertEqual(self.cli("create", "bad", "--selector", selector, "-r", "alpha", ok=False).returncode, 2)
        first = json.loads(first_before)
        self.cli("cleanup", "--selector", "first")
        self.cli("create", "follow-up", "--selector", "p3", "--from", first["uuid"], cwd=self.slots / "p4")
        follow_up = json.loads(fixtures.state_file(self.projects).read_text())
        self.assertEqual(follow_up["follow_up_from"], first["uuid"])
        self.assertEqual(follow_up["slot"], "p3")
