"""Projects whose artifacts live on their own branch of the artifacts repository (dedicated_artifacts_branch)."""

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock
import uuid

from baton.configuration import configured_dedicated_artifacts_branch
from baton.errors import ProjectError
from baton.manager import Manager
from baton.manifest import artifacts_branch_name, artifacts_final_path, project_claims
from baton.retro_summary import run_summary

import test_project_manager as fixtures
import test_project_retirement as retirement_fixtures
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


class DedicatedFixture(unittest.TestCase):
    """An artifacts checkout with a GitHub origin and the fake gh, the setting on, and a code repository alpha."""
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot
    github_origin = fixtures.ProjectManagerTest.github_origin
    add_artifacts_origin = fixtures.ProjectManagerTest.add_artifacts_origin
    fake_pull_requests = fixtures.ProjectManagerTest.fake_pull_requests
    interactive = retirement_fixtures.ProjectRetirementTest.interactive

    # With a nested root, the artifacts checkout also holds files outside the configured artifacts root.
    nested = False

    def setUp(self) -> None:
        if type(self) is DedicatedFixture:
            self.skipTest("fixture base")
        fixtures.ProjectManagerTest.setUp(self)
        self.artifacts_root = self.artifacts
        if self.nested:
            self.artifacts_root = self.artifacts / "projects"
            self.artifacts_root.mkdir()
            (self.artifacts_root / ".gitignore").write_text((self.artifacts / ".gitignore").read_text())
            self.git(self.artifacts, "add", "projects")
            self.git(self.artifacts, "commit", "-qm", "Add the artifacts root")
            self.env["BATON_ARTIFACTS_ROOT"] = str(self.artifacts_root)
        self.origin = self.add_artifacts_origin()
        # Every gh call in these tests reaches the fake, never GitHub.
        self.pull_requests = self.fake_pull_requests()
        self.other = self.root / "artifacts-other"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.other))
        self.cli("config", "set", "--dedicated-artifacts-branch")
        self.make_repo("alpha")

    def prs(self) -> list[dict]:
        return json.loads(self.pull_requests.read_text()) if self.pull_requests.exists() else []

    def gh_calls(self, action: str) -> list[list[str]]:
        log = self.root / "gh.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call for call in calls if call[:2] == ["pr", action]]

    def archived(self) -> dict:
        return read_state(self.projects)["archive"][-1]

    def post(self, text: str, name: str = "example") -> None:
        generation = self.project(name)["blackboard"]["generation"]
        self.cli("blackboard", "post", "--selector", name, "--expected-generation", generation,
                 "--agent", "worker", "--text", text)

    def write_plan(self, text: str = "Plan\n", name: str = "example") -> Path:
        plan = Path(self.project(name)["artifacts_path"]) / "plan.md"
        plan.write_text(text)
        self.cli("artifacts", "commit", "--selector", name, "-m", "Update plan", str(plan))
        return plan

    def level(self) -> None:
        """The shared checkout is on main, clean and level with origin/main."""
        self.git(self.artifacts, "fetch", "-q", "origin")
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertEqual(self.git(self.artifacts, "branch", "--show-current"), "main")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def merged_into_origin(self, commit: str) -> None:
        self.git(self.origin, "merge-base", "--is-ancestor", commit, "main")

    def after_merge(self, script: str) -> None:
        (self.root / "gh-after-merge.sh").write_text(script)

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


class DedicatedArtifactsTest(DedicatedFixture):

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
        # With the setting on, the commit reaches origin/main through a merged pull request.
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--no-merges", "--format=%s"), "Add plan")

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



class DedicatedCleanupTest(DedicatedFixture):
    nested = True

    def started(self) -> tuple[dict, Path, str, Path]:
        """An active project with a committed plan and a board post; returns it with its worktree, branch and final path."""
        self.create("alpha")
        self.write_plan()
        self.post("preserve this")
        project = self.project()
        record = project["artifacts_branch"]
        return project, Path(record["path"]), record["branch"], Path(record["final_path"])

    def assert_archived(self, project: dict, worktree: Path, name: str, final: Path) -> dict:
        archived = self.archived()
        self.assertEqual((archived["uuid"], archived["state"]), (project["uuid"], "archived"))
        self.assertEqual(archived["artifacts_path"], str(final))
        self.assertEqual(archived["blackboard"]["directory"], str(final))
        self.assertEqual(archived["coordination_archive"]["directory"], str(final / "coordination"))
        self.assertEqual((final / "plan.md").read_text(), "Plan\n")
        self.assertIn("preserve this", (final / "coordination" / "history.md").read_text())
        self.assertFalse(worktree.exists())
        self.assertFalse(self.checkout("alpha").exists())
        self.git(self.artifacts, "rev-parse", "--verify", f"refs/heads/{name}", ok=False)
        self.git(self.origin, "rev-parse", "--verify", f"refs/heads/{name}", ok=False)
        self.merged_into_origin(archived["coordination_archive"]["commit"])
        self.level()
        self.assertEqual(len(self.gh_calls("create")), 1)
        self.assertEqual([pr["state"] for pr in self.prs()], ["MERGED"])
        return archived

    def test_cleanup_merges_one_pull_request_and_archives_the_project_at_its_final_path(self) -> None:
        project, worktree, name, final = self.started()
        upstream = self.push_upstream("upstream.md")
        reflog = self.git(self.artifacts, "reflog", "show", "--format=%gs", "refs/heads/main").splitlines()
        result = self.cli("cleanup", "p3")
        self.assertIn(f"Artifacts branch {name}: open and merge its pull request", result.stdout)
        archived = self.assert_archived(project, worktree, name, final)
        sealed = archived["coordination_archive"]["commit"]
        [pr] = self.prs()
        self.assertEqual((pr["headRefName"], pr["baseRefName"], pr["headRefOid"]), (name, "main", sealed))
        self.assertEqual(self.gh_calls("merge"), [["pr", "merge", "1", "--repo", "github.com/example/artifacts",
                                                   "--merge", "--match-head-commit", sealed]])
        self.merged_into_origin(upstream)
        # Baton made no commit in the shared checkout: it only fast-forwarded it.
        entries = self.git(self.artifacts, "reflog", "show", "--format=%gs", "refs/heads/main").splitlines()
        self.assertEqual(entries[len(entries) - len(reflog):], reflog)
        self.assertTrue(all(entry.startswith("merge ") for entry in entries[:len(entries) - len(reflog)]), entries)
        manifest = json.loads((final / "coordination" / "manifest.json").read_text())
        self.assertEqual(manifest["context"]["master_path"], str(final / "plan.md"))
        self.assertEqual(self.cli("path", "--selector", project["uuid"], "--artifacts").stdout.strip(), str(final))
        self.assertIn(str(final), self.cli("search", "example", "--json").stdout)
        shown = json.loads(self.cli("retro", "show", "--selector", project["uuid"], "--json").stdout)
        self.assertEqual(shown["artifacts_path"], str(final))
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.cli("create", "follow-up", "--from", project["uuid"], cwd=self.slots / "p3")
        context = Path(self.project("example-followup")["follow_up_path"]).read_text()
        self.assertIn(str(final), context)

    def test_inspect_refusals_leave_the_project_active(self) -> None:
        project, worktree, name, final = self.started()
        directory = Path(project["artifacts_path"])

        def refused(text: str) -> None:
            result = self.cli("cleanup", "p3", ok=False)
            self.assertIn(text, result.stderr)
            self.assertEqual(self.project()["state"], "active")
            self.assertTrue(worktree.is_dir())
            self.assertTrue(self.checkout("alpha").is_dir())
            self.assertEqual(self.gh_calls("merge"), [])

        (self.root / "gh-offline").touch()
        refused("check that gh is installed and authenticated")
        (self.root / "gh-offline").unlink()

        self.pull_requests.write_text(json.dumps([{
            "repository": "github.com/example/artifacts", "number": 1, "url": "https://github.com/example/artifacts/pull/1",
            "state": "CLOSED", "headRefName": name, "headRefOid": "0" * 40, "baseRefName": "main"}]))
        refused("was closed without merging")
        self.pull_requests.write_text("[]")

        (worktree / "outside.md").write_text("Outside the project directory\n")
        self.git(worktree, "add", "outside.md")
        self.git(worktree, "commit", "-qm", "Change outside the project")
        refused("changes files outside")
        self.git(worktree, "reset", "-q", "--hard", "HEAD~1")

        (directory / "draft.md").write_text("Uncommitted\n")
        refused("Uncommitted files in the artifacts worktree")
        (directory / "draft.md").unlink()

        final.mkdir()
        (final / "plan.md").write_text("Local\n")
        refused("already exists in the shared artifacts checkout")
        (final / "plan.md").unlink()
        final.rmdir()

        self.push_upstream("collide.md")
        (self.artifacts / "collide.md").write_text("Local copy\n")
        refused("collide with changes in origin/main")
        (self.artifacts / "collide.md").unlink()

        (self.artifacts / "code.py").write_text("print('local')\n")
        self.git(self.artifacts, "add", "code.py")
        self.git(self.artifacts, "commit", "-qm", "Local code change")
        refused("unpublished commits outside")
        self.git(self.artifacts, "reset", "-q", "--hard", "HEAD~1")

        self.git(worktree, "switch", "-qc", "elsewhere")
        refused("not on its branch")
        self.git(worktree, "switch", "-q", name)

        dry = self.cli("cleanup", "p3", "--dry-run")
        self.assertIn("Would clean", dry.stdout)
        self.assertEqual(self.prs(), [])

    def test_ignored_files_other_than_the_board_need_confirmation(self) -> None:
        project, worktree, name, final = self.started()
        (self.artifacts / ".git" / "info" / "exclude").write_text("*.log\n")
        (Path(project["artifacts_path"]) / "debug.log").write_text("Ignored\n")
        dry = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("1 ignored files in the artifacts worktree", dry.stderr)
        self.assertIn("debug.log", dry.stderr)
        self.assertNotIn("blackboard.sqlite", dry.stderr)
        refused = self.cli("cleanup", "p3", ok=False)
        self.assertIn("Interactive confirmation is required", refused.stderr)
        self.assertEqual(self.project()["state"], "active")
        self.interactive("cleanup", "p3", reply=b"y\n")
        self.assertEqual(self.archived()["artifacts_path"], str(final))
        self.assertFalse(worktree.exists())

    def test_unpublished_artifact_commits_in_the_shared_checkout_are_published_by_cleanup(self) -> None:
        project, worktree, name, final = self.started()
        other = self.artifacts_root / "other-project"
        other.mkdir()
        (other / "notes.md").write_text("Notes\n")
        self.git(self.artifacts, "add", "projects/other-project")
        self.git(self.artifacts, "commit", "-qm", "Unpublished artifact commit")
        local = self.git(self.artifacts, "rev-parse", "HEAD")
        self.cli("cleanup", "p3")
        self.merged_into_origin(local)
        self.assertEqual([(pr["headRefName"], pr["state"]) for pr in self.prs()],
                         [(name, "MERGED"), (f"baton/artifacts-{local}", "MERGED")])
        self.assertEqual(self.archived()["artifacts_path"], str(final))
        self.level()

    def test_interrupted_seal_is_retried_with_the_lock_in_the_worktree_git_directory(self) -> None:
        project, worktree, name, final = self.started()
        lock = Path(self.git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
        self.assertNotEqual(lock.parent, self.artifacts / ".git")
        lock.write_text("test commit contention")
        failed = self.cli("cleanup", "p3", ok=False)
        self.assertIn("index.lock", failed.stderr)
        pending = self.project()
        self.assertEqual(pending["state"], "cleaning")
        self.assertNotIn("commit", pending["coordination_archive"])
        self.assertTrue(self.checkout("alpha").exists())
        coordination = Path(project["artifacts_path"]) / "coordination"
        history = (coordination / "history.md").read_text()
        # The evidence files the interrupted seal left are not a dirty worktree while cleaning.
        self.assertIn("coordination/", self.git(worktree, "status", "--porcelain"))
        (coordination / "history.md").unlink()
        lock.unlink()
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)
        self.assertEqual((final / "coordination" / "history.md").read_text(), history)

    def test_failed_merge_is_resumed_with_the_same_pull_request(self) -> None:
        project, worktree, name, final = self.started()
        (self.root / "gh-fail-merge").touch()
        failed = self.cli("cleanup", "p3", ok=False)
        self.assertIn("every worktree is intact", failed.stderr)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertTrue(worktree.is_dir() and self.checkout("alpha").is_dir())
        self.assertEqual([pr["state"] for pr in self.prs()], ["OPEN"])
        (self.root / "gh-fail-merge").unlink()
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)
        self.assertEqual(len(self.gh_calls("merge")), 2)

    def test_death_after_the_merge_is_resumed_without_merging_again(self) -> None:
        project, worktree, name, final = self.started()
        self.after_merge("kill -9 $BATON_PID\n")
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertEqual([pr["state"] for pr in self.prs()], ["MERGED"])
        self.assertTrue(worktree.is_dir())
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)
        self.assertEqual(len(self.gh_calls("merge")), 1)

    def test_a_file_that_blocks_the_fast_forward_stops_cleanup_with_every_worktree_intact(self) -> None:
        project, worktree, name, final = self.started()
        self.after_merge(f"mkdir -p {final} && echo local > {final / 'plan.md'}\n")
        failed = self.cli("cleanup", "p3", ok=False)
        self.assertIn("cannot take the merged artifacts branch", failed.stderr)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertTrue(worktree.is_dir() and self.checkout("alpha").is_dir())
        # Resuming is refused at inspect until the file is gone.
        self.assertIn("collide with changes in origin/main", self.cli("cleanup", "p3", ok=False).stderr)
        (final / "plan.md").unlink()
        final.rmdir()
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)
        self.assertEqual(len(self.gh_calls("merge")), 1)

    def test_a_failure_after_the_shared_checkout_step_is_resumed_with_the_final_path_present(self) -> None:
        project, worktree, name, final = self.started()
        self.git(self.repos / "alpha", "worktree", "lock", str(self.checkout("alpha")))
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertTrue((final / "plan.md").is_file())
        self.assertTrue(worktree.is_dir())
        self.git(self.repos / "alpha", "worktree", "unlock", str(self.checkout("alpha")))
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)
        self.assertEqual(len(self.gh_calls("merge")), 1)

    def test_a_worktree_left_after_archiving_is_reported(self) -> None:
        project, worktree, name, final = self.started()
        self.git(self.artifacts, "worktree", "lock", str(worktree))
        result = self.cli("cleanup", "p3")
        self.assertIn(f"Leftover (its content is merged; remove it by hand): artifacts worktree {worktree}", result.stderr)
        archived = self.archived()
        self.assertEqual(archived["artifacts_path"], str(final))
        self.assertTrue(worktree.is_dir())
        self.git(self.origin, "rev-parse", "--verify", f"refs/heads/{name}", ok=False)

    def test_a_branch_moved_after_sealing_is_refused_at_verify(self) -> None:
        project, worktree, name, final = self.started()
        directory = Path(project["artifacts_path"])
        self.after_merge(f"echo late > {directory / 'late.md'} && git -C {directory} add late.md "
                         f"&& git -C {worktree} commit -qm Late\n")
        failed = self.cli("cleanup", "p3", ok=False)
        self.assertIn("moved after it was sealed", failed.stderr)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertTrue(worktree.is_dir() and self.checkout("alpha").is_dir())
        self.assertIn("moved after it was sealed", self.cli("cleanup", "p3", ok=False).stderr)
        self.assertEqual(len(self.gh_calls("merge")), 1)

    def test_turning_the_setting_off_mid_project_still_merges_through_the_pull_request(self) -> None:
        project, worktree, name, final = self.started()
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.cli("cleanup", "p3")
        self.assert_archived(project, worktree, name, final)

    def test_abandon_merges_the_artifacts_pull_request(self) -> None:
        project, worktree, name, final = self.started()
        result = self.interactive("abandon", "example", reply=b"y\n")
        self.assertIn(f"Artifacts branch {name} will be published through a pull request", result.stdout)
        self.assert_archived(project, worktree, name, final)

    def test_replacing_the_slot_project_merges_its_artifacts_pull_request(self) -> None:
        project, worktree, name, final = self.started()
        master, slave = os.openpty()
        try:
            process = subprocess.Popen(["bash", str(fixtures.SCRIPT), "create", "next", "-r", "alpha"],
                                       cwd=self.slots / "p3", env=self.env, stdin=slave, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.write(master, b"y\n")
            stdout, stderr = process.communicate(timeout=60)
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn(f"publish artifacts branch {name} through a pull request, merge", stderr)
        self.assertEqual(self.archived()["artifacts_path"], str(final))
        self.assertEqual(self.project("next")["state"], "active")

    def force(self, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return self.interactive("cleanup", "p3", "--force", reply=b"y\n", ok=ok)

    def assert_force_retained(self, result: subprocess.CompletedProcess[str], project: dict, worktree: Path,
                              name: str) -> None:
        self.assertIn(f"Leftover artifacts worktree {worktree} and branch {name}", result.stderr)
        archived = self.archived()
        self.assertEqual((archived["uuid"], archived["state"]), (project["uuid"], "archived"))
        self.assertEqual(archived["artifacts_path"], project["artifacts_path"])
        self.assertEqual(self.git(worktree, "branch", "--show-current"), name)
        self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{name}"), self.git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.gh_calls("create") + self.gh_calls("merge"), [])
        manifest = json.loads((Path(project["artifacts_path"]) / "coordination" / "manifest.json").read_text())
        self.assertEqual(manifest["context"]["master_path"], str(Path(project["artifacts_branch"]["final_path"]) / "plan.md"))
        # Sealed: no new artifacts on the retained branch.
        self.assertIn("sealed", self.cli("artifacts", "commit", "--selector", project["uuid"], "-m", "Late",
                                         project["artifacts_path"] + "/plan.md", ok=False).stderr)
        self.create("alpha", name="next")
        self.assertEqual(self.project("next")["state"], "active")

    def test_force_cleanup_from_active_retains_the_artifacts_worktree(self) -> None:
        project, worktree, name, final = self.started()
        self.assert_force_retained(self.force(), project, worktree, name)

    def test_force_cleanup_from_cleaning_retains_the_artifacts_worktree(self) -> None:
        project, worktree, name, final = self.started()
        lock = Path(self.git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
        lock.write_text("test commit contention")
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        lock.unlink()
        self.assert_force_retained(self.force(), project, worktree, name)

    def test_force_cleanup_after_a_normal_seal_retains_the_artifacts_worktree(self) -> None:
        project, worktree, name, final = self.started()
        (self.root / "gh-fail-merge").touch()
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.project()["coordination_archive"]["commit"])
        (self.root / "gh.log").unlink()
        self.assert_force_retained(self.force(), project, worktree, name)


class SharedCheckoutPublicationTest(DedicatedFixture):
    """With the setting on, commits in the shared checkout reach origin/main only through merged pull requests."""
    nested = True

    def archived_project(self) -> dict:
        self.create("alpha")
        self.write_plan()
        self.cli("cleanup", "p3")
        self.pull_requests.write_text("[]")
        (self.root / "gh.log").unlink()
        return self.archived()

    def legacy_project(self) -> Path:
        """An active project created before the setting was enabled, which commits in the shared checkout."""
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.create("alpha")
        self.cli("config", "set", "--dedicated-artifacts-branch")
        return Path(self.project()["artifacts_path"])

    def local_commit(self, relative: str, text: str = "Local\n") -> str:
        path = self.artifacts / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git(self.artifacts, "add", relative)
        self.git(self.artifacts, "commit", "-qm", f"Add {relative}")
        return self.git(self.artifacts, "rev-parse", "HEAD")

    def publish(self, directory: Path) -> dict[str, bool]:
        """Publish the shared checkout as an artifact commit with nothing new to commit does, in this process."""
        root = self.artifacts.resolve()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            manager = Manager()
            manager.load()
            return manager.commit_artifact_paths(root, "Nothing new", [str(directory.resolve().relative_to(root))],
                                                 artifacts_root=self.artifacts_root.resolve())

    def assert_one_merged_publication(self, commit: str, count: int = 1) -> dict:
        prs = self.prs()
        self.assertEqual(len(prs), count)
        pr = prs[-1]
        self.assertEqual((pr["headRefName"], pr["baseRefName"], pr["state"]), (f"baton/artifacts-{commit}", "main", "MERGED"))
        self.assertEqual(self.gh_calls("merge")[-1][-2:], ["--match-head-commit", commit])
        self.merged_into_origin(commit)
        self.git(self.origin, "rev-parse", "--verify", f"refs/heads/baton/artifacts-{commit}", ok=False)
        self.level()
        return pr

    def test_archived_project_commit_is_published_through_one_merged_pull_request(self) -> None:
        archived = self.archived_project()
        upstream = self.push_upstream("projects/upstream.md")
        retro = Path(archived["artifacts_path"]) / "retro.md"
        retro.write_text("Retro\n")
        result = self.cli("artifacts", "commit", "--selector", archived["uuid"], "-m", "Add retro", str(retro))
        self.assertNotIn("Pushed", result.stdout)
        head = self.git(self.artifacts, "rev-parse", "HEAD^2")
        self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s", head), "Add retro")
        self.assert_one_merged_publication(head)
        self.merged_into_origin(upstream)
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")

    def test_project_created_before_the_setting_publishes_through_a_pull_request(self) -> None:
        directory = self.legacy_project()
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(plan))
        self.assert_one_merged_publication(self.git(self.artifacts, "rev-parse", "HEAD^2"))

    def test_ahead_and_behind_checkout_is_reconciled_without_a_rebase(self) -> None:
        directory = self.legacy_project()
        (self.root / "gh-fail-merge").touch()
        first = self.local_commit(f"projects/{directory.name}/notes.md")
        self.assertFalse(self.publish(directory)["published"])
        self.push_upstream("projects/upstream.md")
        (self.root / "gh-fail-merge").unlink()
        self.assertTrue(self.publish(directory)["published"])
        # The same HEAD reuses its open pull request, now merged into a moved main.
        self.assertEqual([pr["state"] for pr in self.prs()], ["MERGED"])
        self.assertEqual(len(self.gh_calls("create")), 1)
        self.merged_into_origin(first)
        self.level()

    def test_merge_failure_keeps_the_commit_local_and_the_next_commit_publishes_both(self) -> None:
        directory = self.legacy_project()
        (self.root / "gh-fail-merge").touch()
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        failed = self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(plan))
        self.assertIn("Pull request publication of", failed.stderr)
        first = self.git(self.artifacts, "rev-parse", "HEAD")
        self.git(self.origin, "merge-base", "--is-ancestor", first, "main", ok=False)
        (self.root / "gh-fail-merge").unlink()
        (directory / "notes.md").write_text("Notes\n")
        self.cli("artifacts", "commit", "--selector", "example", "-m", "Add notes", str(directory / "notes.md"))
        second = self.git(self.artifacts, "rev-parse", "HEAD^2")
        self.assertEqual(self.git(self.artifacts, "rev-parse", f"{second}~1"), first)
        self.assert_one_merged_publication(second, count=2)
        self.merged_into_origin(first)

    def test_local_commit_outside_the_artifacts_root_blocks_publication_but_upstream_code_does_not(self) -> None:
        directory = self.legacy_project()
        code = self.push_upstream("code.py")
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(plan))
        # Upstream code outside the artifacts root is fast-forwarded with the setting on.
        self.merged_into_origin(code)
        self.git(self.artifacts, "merge-base", "--is-ancestor", code, "HEAD")
        self.assert_one_merged_publication(self.git(self.artifacts, "rev-parse", "HEAD^2"))
        local = self.local_commit("local.py")
        (directory / "notes.md").write_text("Notes\n")
        blocked = self.cli("artifacts", "commit", "--selector", "example", "-m", "Add notes", str(directory / "notes.md"))
        self.assertIn("change files outside", blocked.stderr)
        self.assertEqual(len(self.prs()), 1)
        self.git(self.origin, "merge-base", "--is-ancestor", local, "main", ok=False)

    def test_upstream_code_is_not_fast_forwarded_with_the_setting_off(self) -> None:
        directory = self.legacy_project()
        self.cli("config", "set", "--no-dedicated-artifacts-branch")
        self.push_upstream("code.py")
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        result = self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(plan))
        self.assertIn("would deploy them", result.stderr)
        self.assertIn("dedicated_artifacts_branch is off", result.stderr)
        self.assertEqual(self.prs(), [])

    def test_rerun_after_an_unobserved_merge_opens_no_second_pull_request(self) -> None:
        directory = self.legacy_project()
        self.after_merge("kill -9 $BATON_PID\n")
        plan = directory / "plan.md"
        plan.write_text("Plan\n")
        self.cli("artifacts", "commit", "--selector", "example", "-m", "Add plan", str(plan), ok=False)
        head = self.git(self.artifacts, "rev-parse", "HEAD")
        self.merged_into_origin(head)
        self.assertTrue(self.publish(directory)["published"])
        self.assertEqual(len(self.gh_calls("create")), 1)
        self.assertEqual(len(self.gh_calls("merge")), 1)
        self.level()

    def test_publishers_of_the_same_head_both_report_published(self) -> None:
        directory = self.legacy_project()
        head = self.local_commit(f"projects/{directory.name}/notes.md")
        # The merge lands but this publisher sees a failure, as when a concurrent publisher merged first.
        (self.root / "gh-fail-after-merge").touch()
        self.assertTrue(self.publish(directory)["published"])
        (self.root / "gh-fail-after-merge").unlink()
        self.assertTrue(self.publish(directory)["published"])
        self.assertEqual(len(self.gh_calls("create")), 1)
        self.merged_into_origin(head)
        self.level()

    def test_a_commit_landing_before_the_fast_forward_is_published_on_its_own_turn(self) -> None:
        directory = self.legacy_project()
        first = self.local_commit(f"projects/{directory.name}/notes.md")
        later = self.artifacts / "projects" / directory.name / "later.md"
        self.after_merge(f"echo later > {later} && git -C {self.artifacts} add {later} "
                         f"&& git -C {self.artifacts} commit -qm Later\n")
        self.assertTrue(self.publish(directory)["published"])
        second = self.git(self.artifacts, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD~1"), first)
        self.git(self.origin, "merge-base", "--is-ancestor", second, "main", ok=False)
        self.assertTrue(self.publish(directory)["published"])
        self.assert_one_merged_publication(second, count=2)

    def test_published_even_when_a_local_file_blocks_the_fast_forward(self) -> None:
        directory = self.legacy_project()
        head = self.local_commit(f"projects/{directory.name}/notes.md")
        self.push_upstream("projects/upstream.md")
        (self.artifacts_root / "upstream.md").write_text("Local copy\n")
        self.assertTrue(self.publish(directory)["published"])
        self.merged_into_origin(head)
        self.assertEqual(self.git(self.artifacts, "rev-parse", "HEAD"), head)

    def test_an_open_pull_request_with_another_base_is_not_reused(self) -> None:
        directory = self.legacy_project()
        head = self.local_commit(f"projects/{directory.name}/notes.md")
        name = f"baton/artifacts-{head}"
        self.git(self.artifacts, "push", "-q", "origin", f"{head}:refs/heads/{name}")
        self.pull_requests.write_text(json.dumps([{
            "repository": "github.com/example/artifacts", "number": 1, "url": "https://github.com/example/artifacts/pull/1",
            "state": "OPEN", "headRefName": name, "headRefOid": head, "baseRefName": "release"}]))
        self.assertTrue(self.publish(directory)["published"])
        self.assertEqual([(pr["baseRefName"], pr["state"]) for pr in self.prs()], [("release", "OPEN"), ("main", "MERGED")])
        self.assertEqual(self.gh_calls("merge")[0][2], "2")

    def test_retro_summary_reports_published_after_its_pull_request_merges(self) -> None:
        with mock.patch.dict(os.environ, self.env | {"XDG_STATE_HOME": str(self.root / "state")}, clear=True):
            result = run_summary(Manager(), "claude", 2, now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "published")
        self.assert_one_merged_publication(self.git(self.artifacts, "rev-parse", "HEAD^2"))

    def test_a_dedicated_worktree_is_never_fast_forwarded_to_the_default_branch(self) -> None:
        self.create("alpha")
        record = self.project()["artifacts_branch"]
        worktree = Path(record["path"])
        self.push_upstream("projects/upstream.md")
        self.git(worktree, "fetch", "-q", "origin")
        with mock.patch.dict(os.environ, self.env, clear=True):
            manager = Manager()
            manager.load()
            manager.fast_forward_artifacts(worktree, "main", any_change=True)
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), record["base_sha"])
        self.assertEqual(self.git(worktree, "branch", "--show-current"), record["branch"])


if __name__ == "__main__":
    unittest.main()
