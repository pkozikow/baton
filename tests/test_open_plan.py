import json
import os
from pathlib import Path
import shutil
import signal
import unittest

import test_project_manager as fixtures


class OpenPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectManagerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.capture = self.fixture.root / "editor-arguments.json"
        viewer = self.fixture.root / "bin" / "vim"
        viewer.parent.mkdir()
        viewer.write_text(
            '#!/usr/bin/env python3\n'
            'import fcntl, json, os, pathlib, sys\n'
            'root = pathlib.Path(os.environ["BATON_REPO"]) / ".git" / "baton"\n'
            'for path in (root / "locks").glob("*.lock"):\n'
            '    with path.open("a") as lock:\n'
            '        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
            'pathlib.Path(os.environ["PLAN_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n'
            'if "PLAN_SIGNAL" in os.environ:\n'
            '    os.kill(os.getpid(), int(os.environ["PLAN_SIGNAL"]))\n'
            'sys.exit(int(os.environ.get("PLAN_EXIT_CODE", "0")))\n'
        )
        viewer.chmod(0o755)
        self.fixture.env.update(
            PATH=f"{viewer.parent}{os.pathsep}{self.fixture.env['PATH']}",
            PLAN_CAPTURE=str(self.capture),
        )
        self.viewer = viewer
        for variable in ("VISUAL", "EDITOR"):
            self.fixture.env.pop(variable, None)

    def open_plan(self, cwd: Path, name: str | None, *args: str, exit_code: int = 0,
                  editor_args: tuple[str, ...] = ()) -> None:
        self.capture.unlink(missing_ok=True)
        result = self.fixture.cli("open-plan", *args, cwd=cwd, ok=name is not None and exit_code == 0)
        if name is None:
            self.assertFalse(self.capture.exists())
        else:
            project, _, relative = name.partition("/")
            expected = fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts, project) / relative / "plan.md"
            self.assertEqual(result.returncode, exit_code, result.stderr)
            self.assertEqual(json.loads(self.capture.read_text()), [*editor_args, str(expected)])
            self.assertTrue(expected.parent.is_dir())

    def test_slot_repo_and_subdirectory_resolve_same_plan(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        slot = self.fixture.slots / "p3"
        checkout = self.fixture.checkout("alpha")
        (slot / "subdir").mkdir()
        (checkout / "subdir").mkdir()
        for path in (slot, slot / "subdir", checkout, checkout / "subdir", slot / "repos" / "alpha"):
            with self.subTest(path=path):
                self.open_plan(path, "example")

    def test_missing_master_parent_is_created(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        shutil.rmtree(fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts))
        self.open_plan(self.fixture.checkout("alpha"), "example")

    def test_manifest_name_wins_over_changed_repo_branch(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        checkout = self.fixture.checkout("alpha")
        self.fixture.git(checkout, "switch", "-c", "baton/different")
        self.open_plan(checkout, "example")

    def test_named_first_member_resolves_project_plan(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        slot = self.fixture.slots / "p3"
        self.fixture.cli("branch", "init", "alpha", "first", cwd=slot)
        for path in (self.fixture.checkout("alpha", "example--first"), slot / "repos" / "alpha--first"):
            self.open_plan(path, "example")

    def test_independent_member_uses_existing_subplan_else_master(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        slot = self.fixture.slots / "p3"
        self.fixture.cli("branch", "init", "alpha", "api", cwd=slot)
        self.fixture.cli("branch", "add", "alpha", "docs", cwd=slot)
        docs = self.fixture.checkout("alpha", "example--docs")
        self.open_plan(docs, "example")
        subplan_name = "example/branches/alpha/example--docs"
        subplan = fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts) / subplan_name.partition("/")[2] / "plan.md"
        subplan.parent.mkdir(parents=True)
        subplan.write_text("Docs subplan\n")
        (docs / "subdir").mkdir()
        self.open_plan(docs / "subdir", subplan_name)
        self.open_plan(slot, "example")
        self.open_plan(self.fixture.checkout("alpha", "example--api"), "example")

    def test_multiple_repositories_use_their_subplans(self) -> None:
        for name in ("alpha", "beta"):
            self.fixture.make_repo(name)
        self.fixture.create("alpha", "beta")
        for repo in ("alpha", "beta"):
            subplan_name = f"example/branches/{repo}/example"
            subplan = fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts) / subplan_name.partition("/")[2] / "plan.md"
            subplan.parent.mkdir(parents=True)
            subplan.write_text(f"{repo} plan\n")
            self.open_plan(self.fixture.checkout(repo), subplan_name)
        self.open_plan(self.fixture.slots / "p3", "example")

    def test_single_entry_ignores_existing_subplan(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        subplan = fixtures.artifact_path(self.fixture.projects, self.fixture.artifacts) / "branches" / "alpha" / "example" / "plan.md"
        subplan.parent.mkdir(parents=True)
        subplan.write_text("Not a multi-entry project\n")
        self.open_plan(self.fixture.checkout("alpha"), "example")

    def test_explicit_selector_from_outside_project(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        for selector in ("example", "p3"):
            self.open_plan(self.fixture.root, "example", "--selector", selector)

    def test_previous_name_selector_resolves_renamed_project(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        self.fixture.cli("rename", "renamed", cwd=self.fixture.slots / "p3")
        self.open_plan(self.fixture.root, "renamed", "--selector", "example")


    def test_idle_slot_and_unregistered_repositories_fail(self) -> None:
        self.fixture.ensure_slot("p1")
        self.open_plan(self.fixture.slots / "p1", None)
        self.open_plan(self.fixture.root, None)
        for name in ("alpha", "unmanaged"):
            source = self.fixture.make_repo(name)
            (source / "backend").mkdir()
            (source / "frontend").mkdir()
            self.fixture.git(source, "switch", "-c", "baton/unregistered")
            self.open_plan(source, None)

    def test_name_comes_from_project_state_not_the_slot_manifest(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        manifest_path = self.fixture.slots / "p3" / "project.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["name"] = "../escape"
        manifest_path.write_text(json.dumps(manifest))
        self.open_plan(self.fixture.slots / "p3", "example")

    def test_visual_then_editor_override_vim_and_split_like_a_shell(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        editor = self.fixture.root / "editor dir" / "my-editor"
        editor.parent.mkdir()
        shutil.copy(self.viewer, editor)
        self.viewer.unlink()
        self.fixture.env["EDITOR"] = f"'{editor}' -w"
        self.open_plan(self.fixture.checkout("alpha"), "example", editor_args=("-w",))
        self.fixture.env["VISUAL"] = f"'{editor}' --visual"
        self.open_plan(self.fixture.checkout("alpha"), "example", editor_args=("--visual",))
        self.fixture.env["VISUAL"] = "   "
        self.open_plan(self.fixture.checkout("alpha"), "example", editor_args=("-w",))

    def test_editor_exit_code_is_propagated_without_held_project_lock(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        self.fixture.env["PLAN_EXIT_CODE"] = "23"
        self.open_plan(self.fixture.checkout("alpha"), "example", exit_code=23)

    def test_editor_signal_exit_code_uses_shell_convention(self) -> None:
        self.fixture.make_repo("alpha")
        self.fixture.create("alpha")
        self.fixture.env["PLAN_SIGNAL"] = str(signal.SIGTERM)
        self.open_plan(self.fixture.checkout("alpha"), "example", exit_code=128 + signal.SIGTERM)



if __name__ == "__main__":
    unittest.main()
