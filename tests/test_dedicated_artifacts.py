"""Projects whose artifacts live on their own branch of the artifacts repository (dedicated_artifacts_branch)."""

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import uuid

from baton.configuration import configured_dedicated_artifacts_branch
from baton.errors import ProjectError
from baton.manager import Manager
from baton.manifest import artifacts_branch_name, artifacts_final_path, project_claims
from baton.retro_summary import run_summary

import test_project_manager as fixtures
from test_project_manager import read_state, state_file


class DedicatedArtifactsConfigurationTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli

    def test_value_must_be_a_boolean(self) -> None:
        self.assertFalse(configured_dedicated_artifacts_branch({}))
        self.assertTrue(configured_dedicated_artifacts_branch({"dedicated_artifacts_branch": True}))
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "dedicated_artifacts_branch"):
                configured_dedicated_artifacts_branch({"dedicated_artifacts_branch": value})

    def test_set_show_and_unset(self) -> None:
        self.assertFalse(json.loads(self.cli("config", "show").stdout)["dedicated_artifacts_branch"])
        self.assertTrue(json.loads(self.cli("config", "set", "--dedicated-artifacts-branch").stdout)
                        ["dedicated_artifacts_branch"])
        self.assertTrue(json.loads((self.root / "config.json").read_text())["dedicated_artifacts_branch"])
        self.assertTrue(json.loads(self.cli("config", "show").stdout)["dedicated_artifacts_branch"])
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.assertFalse(json.loads(self.cli("config", "show").stdout)["dedicated_artifacts_branch"])
        self.cli("config", "set", "--dedicated-artifacts-branch", "--no-dedicated-artifacts-branch", ok=False)
        refused = self.cli("config", "set", "--selector", "p3", "--dedicated-artifacts-branch", ok=False)
        self.assertIn("accepts only review-loop flags", refused.stderr)

    def test_invalid_saved_value_is_rejected(self) -> None:
        (self.root / "config.json").write_text(json.dumps({"dedicated_artifacts_branch": "yes"}))
        failed = self.cli("config", "show", ok=False)
        self.assertIn("dedicated_artifacts_branch must be true or false", failed.stderr)


class DedicatedArtifactsTest(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    github_origin = fixtures.ProjectManagerTest.github_origin
    add_artifacts_origin = fixtures.ProjectManagerTest.add_artifacts_origin

    def setUp(self) -> None:
        fixtures.ProjectManagerTest.setUp(self)
        self.origin = self.add_artifacts_origin()
        self.other = self.root / "artifacts-other"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.other))
        self.cli("config", "set", "--dedicated-artifacts-branch")
        self.make_repo("alpha")

    def project(self, name: str = "example") -> dict:
        return next(p for p in read_state(self.projects)["projects"].values() if p["name"] == name)

    def push_upstream(self, filename: str) -> str:
        self.git(self.other, "pull", "-q", "--ff-only")
        (self.other / filename).write_text("Pushed from another checkout\n")
        self.git(self.other, "add", filename)
        self.git(self.other, "commit", "-qm", f"Add {filename}")
        self.git(self.other, "push", "-q")
        return self.git(self.other, "rev-parse", "HEAD")

    def commit(self, path: Path, message: str, ok: bool = True) -> object:
        return self.cli("artifacts", "commit", "--selector", "example", "-m", message, str(path), ok=ok)

    def shared(self) -> tuple[str, str, str]:
        return (self.git(self.artifacts, "rev-parse", "HEAD"), self.git(self.artifacts, "branch", "--show-current"),
                self.git(self.artifacts, "status", "--porcelain"))

    # creation

    def test_creates_the_branch_and_worktree_from_the_fetched_default(self) -> None:
        upstream = self.push_upstream("upstream.md")
        shared = self.shared()
        self.create("alpha")
        project = self.project()
        record = project["artifacts_branch"]
        suffix = f"example-{project['uuid'][:8]}"
        worktree = self.worktrees / "artifacts" / suffix
        directory = worktree / suffix
        self.assertEqual({key: record[key] for key in ("branch", "path", "final_path", "base_sha", "source")},
                         {"branch": f"baton/{suffix}", "path": str(worktree), "base_sha": upstream,
                          "final_path": str(self.artifacts.resolve() / suffix), "source": str(self.artifacts.resolve())})
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), upstream)
        self.assertEqual(self.git(worktree, "branch", "--show-current"), record["branch"])
        self.assertEqual(project["artifacts_path"], str(directory))
        self.assertEqual(project["blackboard"]["directory"], str(directory))
        self.assertTrue((directory / "blackboard.sqlite").is_file())
        self.assertEqual(artifacts_branch_name(project), record["branch"])
        self.assertEqual(artifacts_final_path(project), Path(record["final_path"]))
        self.assertIsNone(artifacts_branch_name(project, Path(record["final_path"]) / "plan.md"))
        claims = project_claims(project)
        for claim in (("branch", record["common_dir"], record["branch"]), ("path", record["path"]),
                      ("artifacts", record["final_path"]), ("artifacts", str(directory))):
            self.assertIn(claim, claims)

        status = json.loads(self.cli("status", "p3", "--json").stdout)
        self.assertEqual(status["artifacts_branch"], {"branch": record["branch"], "path": str(worktree),
                                                      "base_ref": "refs/remotes/origin/main"})
        self.assertEqual(status["master_path"], str(directory / "plan.md"))
        self.assertEqual(status["entries"][0]["subplan_path"], str(directory / "branches/alpha/example/plan.md"))
        self.assertEqual(status["blackboard"]["path"], str(directory / "blackboard.sqlite"))
        self.assertIn(f"Artifacts branch: {record['branch']} in {worktree}", self.cli("status", "p3").stdout)
        self.assertEqual(self.cli("path", "--selector", "example", "--artifacts").stdout.strip(), str(directory))

        self.assertEqual(self.shared(), shared)
        self.assertFalse(Path(record["final_path"]).exists())
        self.git(self.origin, "rev-parse", "--verify", f"refs/heads/{record['branch']}", ok=False)

    def test_projects_without_the_record_keep_the_shared_checkout(self) -> None:
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.create("alpha")
        project = self.project()
        self.assertNotIn("artifacts_branch", project)
        self.assertNotIn("artifacts_branch", json.loads(self.cli("status", "p3", "--json").stdout))
        self.cli("config", "set", "--dedicated-artifacts-branch")
        directory = Path(project["artifacts_path"])
        (directory / "plan.md").write_text("Plan\n")
        self.commit(directory / "plan.md", "Add plan")
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s"), "Add plan")

    def test_refused_without_a_github_origin(self) -> None:
        self.git(self.artifacts, "remote", "set-url", "origin", str(self.origin))
        failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertIn("requires a GitHub origin", failed.stderr)
        self.assertEqual(read_state(self.projects)["projects"], {})
        self.assertFalse((self.worktrees / "artifacts").exists())
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")

    def test_refused_when_the_final_directory_exists(self) -> None:
        # The final directory name derives from a random UUID, so this one is fixed for the test.
        fixed = uuid.UUID("1234abcd" + "0" * 24)
        (self.artifacts / "example-1234abcd").mkdir()
        with (mock.patch.dict(os.environ, self.env, clear=True), mock.patch("uuid.uuid4", return_value=fixed),
              contextlib.chdir(self.slots / "p3")):
            manager = Manager()
            manager.load()
            with manager.command_lock(None, create=True), self.assertRaisesRegex(ProjectError, "Artifact destination already exists"):
                manager.create("example", ["alpha"])
        self.assertEqual(read_state(self.projects)["projects"], {})
        self.assertFalse((self.worktrees / "artifacts").exists())

    def test_interrupted_creation_resumes_before_the_worktree_add(self) -> None:
        self.push_upstream("upstream.md")
        # A file where the worktree's parent directory belongs stops creation after its record is saved.
        self.worktrees.mkdir(exist_ok=True)
        (self.worktrees / "artifacts").write_text("in the way\n")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        project = self.project()
        record = project["artifacts_branch"]
        self.assertEqual(project["state"], "creating")
        self.git(self.artifacts, "rev-parse", "--verify", f"refs/heads/{record['branch']}", ok=False)
        (self.worktrees / "artifacts").unlink()
        # A branch with other commits is not adopted.
        self.git(self.artifacts, "branch", record["branch"], "HEAD")
        failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertIn("unexpected commits", failed.stderr)
        # An interrupted worktree add -b leaves the branch at its base.
        self.git(self.artifacts, "branch", "-f", record["branch"], record["base_sha"])
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3")
        project = self.project()
        self.assertEqual(project["state"], "active")
        self.assertEqual(self.git(Path(record["path"]), "branch", "--show-current"), record["branch"])
        self.assertTrue((Path(project["artifacts_path"]) / "blackboard.sqlite").is_file())

    def test_interrupted_creation_resumes_after_the_worktree_add(self) -> None:
        rules = (self.artifacts / ".gitignore").read_text()
        # Upstream drops the board ignore rules that the shared checkout still has, so only the worktree check fails.
        self.git(self.other, "rm", "-q", ".gitignore")
        self.git(self.other, "commit", "-qm", "Drop ignore rules")
        self.git(self.other, "push", "-q")
        failed = self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertIn("must be Git-ignored", failed.stderr)
        project = self.project()
        record = project["artifacts_branch"]
        worktree = Path(record["path"])
        self.assertEqual(project["state"], "creating")
        self.assertEqual(self.git(worktree, "branch", "--show-current"), record["branch"])
        (worktree / ".gitignore").write_text(rules)
        self.git(worktree, "add", ".gitignore")
        self.git(worktree, "commit", "-qm", "Restore ignore rules")
        self.cli("create", "example", "--repos", "alpha", cwd=self.slots / "p3")
        project = self.project()
        self.assertEqual(project["state"], "active")
        self.assertTrue((Path(project["artifacts_path"]) / "blackboard.sqlite").is_file())
        self.assertEqual(self.git(worktree, "status", "--porcelain"), "")

    def test_follow_up_commits_its_context_on_the_artifacts_branch(self) -> None:
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.create("alpha")
        source = self.project()["uuid"]
        self.cli("cleanup", "p3")
        self.cli("config", "set", "--dedicated-artifacts-branch")
        shared = self.shared()
        self.cli("create", "follow-up", "--from", source, cwd=self.slots / "p3")
        project = self.project("example-followup")
        record = project["artifacts_branch"]
        worktree = Path(record["path"])
        context = Path(project["follow_up_path"])
        self.assertEqual(context, Path(project["artifacts_path"]) / "follow-up.md")
        self.assertTrue(context.is_file())
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Record follow-up context for example-followup")
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD~1"), record["base_sha"])
        self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
        self.assertEqual(self.shared(), shared)

    def test_artifacts_repository_can_also_be_a_code_repository(self) -> None:
        docs = self.make_repo("docs")
        (docs / "projects").mkdir()
        (docs / "projects" / ".gitignore").write_text((self.artifacts / ".gitignore").read_text())
        self.git(docs, "add", "projects")
        self.git(docs, "commit", "-qm", "Add projects")
        self.github_origin(docs, "docs")
        self.env["BATON_ARTIFACTS_ROOT"] = str(docs / "projects")
        self.create("docs")
        project = self.project()
        record = project["artifacts_branch"]
        suffix = f"example-{project['uuid'][:8]}"
        worktree = self.worktrees / "docs" / suffix
        self.assertEqual((record["path"], record["branch"]), (str(worktree), f"baton/{suffix}"))
        self.assertEqual(project["artifacts_path"], str(worktree / "projects" / suffix))
        self.assertEqual(self.git(self.checkout("docs"), "branch", "--show-current"), "baton/example")
        code_head = self.git(self.checkout("docs"), "rev-parse", "HEAD")
        plan = Path(project["artifacts_path"]) / "plan.md"
        plan.write_text("Plan\n")
        self.commit(plan, "Add plan")
        self.assertEqual(self.git(self.root / "docs.git", "rev-parse", f"refs/heads/{record['branch']}"),
                         self.git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.checkout("docs"), "rev-parse", "HEAD"), code_head)
        self.assertEqual(self.git(docs, "log", "-1", "--format=%s"), "Add projects")

    # commits

    def test_commits_go_on_the_branch_and_push_it_from_the_first_commit(self) -> None:
        self.create("alpha")
        project = self.project()
        record = project["artifacts_branch"]
        name, worktree, directory = record["branch"], Path(record["path"]), Path(project["artifacts_path"])
        upstream = self.push_upstream("upstream.md")
        shared = self.shared()
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        result = self.commit(plan, "Add plan")
        self.assertIn(f"Pushed artifacts branch {name} to origin/{name}", result.stdout)
        self.assertNotIn("fetch origin", result.stderr)
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Add plan")
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD~1"), record["base_sha"])
        self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{name}"), self.git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), upstream)
        self.assertEqual(self.shared(), shared)

        # A failed push keeps the commit local, and the next commit publishes both.
        self.git(self.artifacts, "remote", "set-url", "--push", "origin", str(self.root / "missing.git"))
        plan.write_text("Plan v2\n")
        result = self.commit(plan, "Update plan")
        self.assertIn(f"Push to origin/{name} failed; artifact commits are saved locally", result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{name}"), self.git(worktree, "rev-parse", "HEAD~1"))
        self.git(self.artifacts, "config", "--unset", "remote.origin.pushurl")
        (directory / "notes.md").write_text("Notes\n")
        self.commit(directory / "notes.md", "Add notes")
        self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{name}"), self.git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.git(worktree, "rev-list", "--count", f"{record['base_sha']}..HEAD"), "3")
        self.assertEqual(self.shared(), shared)

        self.git(worktree, "switch", "-qc", "elsewhere")
        plan.write_text("Plan v3\n")
        refused = self.commit(plan, "Wrong branch", ok=False)
        self.assertIn(f"only on {name}", refused.stderr)
        self.git(worktree, "switch", "-q", name)

    def test_sealed_project_refuses_new_artifacts(self) -> None:
        self.create("alpha")
        project = self.project()
        plan = Path(project["artifacts_path"]) / "plan.md"
        plan.write_text("Plan\n")
        path = state_file(self.projects)
        path.write_text(json.dumps(dict(project, state="cleaning")))
        refused = self.commit(plan, "Add plan", ok=False)
        self.assertIn("is cleaning and its artifacts branch", refused.stderr)
        self.assertIn("sealed", refused.stderr)
        self.assertEqual(self.git(Path(project["artifacts_branch"]["path"]), "rev-parse", "HEAD"),
                         project["artifacts_branch"]["base_sha"])

    def test_branch_checkouts_still_commit_only_their_own_subplan(self) -> None:
        self.make_repo("beta")
        self.create("alpha", "beta")
        status = json.loads(self.cli("status", "p3", "--json").stdout)
        own, sibling = (Path(next(e for e in status["entries"] if e["repo"] == repo)["subplan_path"])
                        for repo in ("alpha", "beta"))
        for path in (own, sibling):
            path.parent.mkdir(parents=True)
            path.write_text("Subplan\n")
        self.cli("artifacts", "commit", "--selector", "p3", "-m", "Own subplan", str(own), cwd=self.checkout("alpha"))
        refused = self.cli("artifacts", "commit", "--selector", "p3", "-m", "Sibling", str(sibling),
                           cwd=self.checkout("alpha"), ok=False)
        self.assertIn("another branch's subplan", refused.stderr)

    def test_retro_summary_still_commits_on_the_shared_branch(self) -> None:
        self.create("alpha")
        record = self.project()["artifacts_branch"]
        before = self.git(self.artifacts, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env | {"XDG_STATE_HOME": str(self.root / "state")}, clear=True):
            result = run_summary(Manager(), "claude", 2, now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
        self.assertTrue(result["committed"])
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), before)
        self.assertEqual(self.git(self.artifacts, "branch", "--show-current"), "main")
        self.assertEqual(self.git(Path(record["path"]), "rev-parse", "HEAD"), record["base_sha"])

    # rename and cleanup

    def test_rename_keeps_the_artifacts_branch_worktree_and_directory(self) -> None:
        self.create("alpha")
        before = self.project()
        self.cli("rename", "renamed", "--selector", "example")
        after = self.project("renamed")
        self.assertEqual(after["artifacts_branch"], before["artifacts_branch"])
        self.assertEqual(after["artifacts_path"], before["artifacts_path"])
        self.assertEqual(after["blackboard"]["directory"], before["blackboard"]["directory"])
        worktree = Path(after["artifacts_branch"]["path"])
        self.assertEqual(self.git(worktree, "branch", "--show-current"), after["artifacts_branch"]["branch"])
        plan = Path(after["artifacts_path"]) / "plan.md"
        plan.write_text("Plan\n")
        self.cli("artifacts", "commit", "--selector", "renamed", "-m", "Add plan", str(plan))
        self.assertEqual(self.git(worktree, "log", "-1", "--format=%s"), "Add plan")

    def test_cleanup_and_abandon_refuse_until_branch_merging_exists(self) -> None:
        self.create("alpha")
        for command in (("cleanup", "p3"), ("cleanup", "p3", "--dry-run"), ("abandon", "example")):
            with self.subTest(command=command):
                refused = self.cli(*command, ok=False)
                self.assertIn("not supported yet", refused.stderr)
        self.assertEqual(self.project()["state"], "active")
        self.assertTrue(self.checkout("alpha").exists())


if __name__ == "__main__":
    unittest.main()
