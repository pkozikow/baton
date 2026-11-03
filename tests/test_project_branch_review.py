import fcntl
import os
import shlex
import shutil
import unittest

import test_project_branches as fixtures


class ProjectBranchReviewTest(unittest.TestCase):
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

    def test_register_multiple_unregistered_native_members_one_at_a_time(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API implementation\n")
        top = self.add_member("tests", after="api")
        self.commit_file(top, "tests.txt", "API tests\n")
        self.native(top, "add", "baton/example--extra-one")
        self.commit_file(top, "one.txt", "First native-only member\n")
        self.native(top, "add", "baton/example--extra-two")
        self.commit_file(top, "two.txt", "Second native-only member\n")
        self.git(top, "switch", "baton/example--tests")
        for name in ("extra-one", "extra-two"):
            self.cli("branch", "register", "alpha", name, cwd=self.slots / "p3")
            state = fixtures.fixtures.read_state(self.projects)
            self.assertEqual(state["projects"]["p3"]["state"], "active")
            self.assertTrue(self.member(name).exists())
            if name == "extra-one":
                result = self.cli("cleanup", "p3", "--dry-run", ok=False)
                self.assertIn("baton/example--extra-two", result.stderr)

    def test_first_init_rejects_foreign_reference_before_renaming(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        root = self.checkout("alpha")
        self.add_member("docs")
        self.git(source, "branch", "foreign-feature")
        self.native(source, "init", "--base", "baton/example--docs", "foreign-feature")
        state_path = fixtures.fixtures.state_file(self.projects)
        before = state_path.read_bytes()
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3", ok=False)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertTrue(root.exists())
        self.assertEqual(self.git(root, "branch", "--show-current"), "baton/example")

    def test_native_operation_lock_blocks_add_before_project_journaling(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API implementation\n")
        source = self.repos / "alpha"
        state_path = fixtures.fixtures.state_file(self.projects)
        before = state_path.read_bytes()
        with (source / ".git" / "gh-stack-operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli("branch", "add", "alpha", "tests", "--after", "api",
                              cwd=self.slots / "p3", ok=False)
        self.assertIn("operation", result.stderr.lower())
        self.assertEqual(state_path.read_bytes(), before)
        self.assertFalse(self.checkout("alpha", "example--tests").exists())

    def test_register_rejects_foreign_trunk_before_adopting_branch(self) -> None:
        self.init_members()
        source = self.repos / "alpha"
        self.git(source, "branch", "baton/example--tooling")
        self.git(source, "branch", "foreign-feature")
        self.native(source, "init", "--base", "baton/example--tooling", "foreign-feature")
        state_path = fixtures.fixtures.state_file(self.projects)
        before = state_path.read_bytes()
        catalog = (source / ".git" / "gh-stack").read_bytes()
        self.cli("branch", "register", "alpha", "tooling", cwd=self.slots / "p3", ok=False)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual((source / ".git" / "gh-stack").read_bytes(), catalog)
        self.assertFalse(self.checkout("alpha", "example--tooling").exists())

    def test_registration_relaxation_does_not_allow_missing_managed_members(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API implementation\n")
        self.add_member("tests", after="api")
        source = self.repos / "alpha"
        self.native(first, "unstack", "--local")
        self.git(source, "branch", "baton/example--extra")
        self.native(first, "init", "--base", "main", "baton/example--api", "baton/example--extra")
        self.git(first, "switch", "baton/example--api")
        state_path = fixtures.fixtures.state_file(self.projects)
        before = state_path.read_bytes()
        result = self.cli("branch", "register", "alpha", "extra", cwd=self.slots / "p3", ok=False)
        self.assertIn("baton/example--tests", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertFalse(self.checkout("alpha", "example--extra").exists())

    def test_native_lock_after_add_blocks_checkout_restoration_until_resume(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API implementation\n")
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        self.assertIsNotNone(actual_gh)
        lock_path = self.repos / "alpha" / ".git" / "gh-stack-operation.lock"
        ready = self.root / "native-lock-ready"
        release = self.root / "native-lock-release"
        lock_program = (
            "import fcntl, pathlib, time\n"
            f"handle = open({str(lock_path)!r}, 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            f"pathlib.Path({str(ready)!r}).touch()\n"
            "deadline = time.monotonic() + 10\n"
            f"while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
        )
        bindir = self.root / "lock-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(
            "#!/bin/sh\n" + shlex.quote(str(actual_gh)) + ' "$@" || exit $?\n'
            'if [ "$1" = "stack" ] && [ "$2" = "add" ]; then\n'
            "  python3 -c " + shlex.quote(lock_program) + " >/dev/null 2>&1 &\n"
            "  attempts=0\n"
            "  until [ -f " + shlex.quote(str(ready)) + " ]; do\n"
            "    attempts=$((attempts + 1))\n"
            "    [ $attempts -lt 100 ] || exit 1\n"
            "    sleep 0.01\n"
            "  done\nfi\n"
        )
        wrapper.chmod(0o755)
        original = self.env["PATH"]
        self.env["PATH"] = str(bindir) + os.pathsep + original
        try:
            result = self.cli("branch", "add", "alpha", "tests", "--after", "api",
                              cwd=self.slots / "p3", ok=False)
            self.assertIn("operation is running", result.stderr)
            self.assertEqual(self.git(first, "branch", "--show-current"), "baton/example--tests")
            self.assertFalse(self.checkout("alpha", "example--tests").exists())
        finally:
            release.touch()
            self.env["PATH"] = original
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.cli("branch", "add", "alpha", "tests", "--after", "api", cwd=self.slots / "p3")
        self.assertEqual(self.git(first, "branch", "--show-current"), "baton/example--api")
        self.assertTrue(self.member("tests").exists())


if __name__ == "__main__":
    unittest.main()
