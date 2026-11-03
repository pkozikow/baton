import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest
from typing import Any

import test_project_manager as fixtures
import test_project_stacks as stacks

sys.path.insert(0, str(fixtures.SCRIPT.parent.parent))
from baton.errors import ProjectError
from baton.manager import Manager


class ProjectBranchesTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    replace_interactively = fixtures.ProjectManagerTest.replace_interactively
    native = stacks.ProjectStacksTest.native
    commit_file = stacks.ProjectStacksTest.commit_file
    fake_pull_requests = stacks.ProjectStacksTest.fake_pull_requests

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.slots / "p3" / "project.json").read_text())

    def member(self, name: str, repo: str = "alpha") -> Path:
        return Path(self.cli("path", "p3", repo, "--member", name).stdout.strip())

    def init_members(self, repo: str = "alpha", first: str = "api") -> Path:
        self.make_repo(repo)
        self.create(repo)
        self.cli("branch", "init", repo, first, cwd=self.slots / "p3")
        return self.member(first, repo)

    def add_member(self, name: str, *, after: str | None = None,
                   cwd: Path | None = None, repo: str = "alpha") -> Path:
        args = ["branch", "add", repo, name]
        if after is not None:
            args.extend(["--after", after])
        self.cli(*args, cwd=cwd or self.slots / "p3")
        return self.member(name, repo)

    def catalog(self, source: Path) -> dict[str, Any]:
        path = source / ".git" / "gh-stack"
        return json.loads(path.read_text()) if path.exists() else {"stacks": []}

    def two_groups(self) -> tuple[Path, list[Path]]:
        first = self.init_members()
        source = self.repos / "alpha"
        self.commit_file(first, "api.txt", "API implementation\n")
        api_test = self.add_member("api-tests", after="api")
        self.commit_file(api_test, "api-tests.txt", "API tests\n")
        docs = self.add_member("docs", cwd=api_test)
        self.commit_file(docs, "docs.txt", "Independent documentation\n")
        docs_test = self.add_member("docs-tests", after="docs")
        self.commit_file(docs_test, "docs-tests.txt", "Documentation tests\n")
        tooling = self.add_member("tooling", cwd=docs_test)
        self.commit_file(tooling, "tooling.txt", "Independent tooling\n")
        return source, [first, api_test, docs, docs_test, tooling]

    def test_first_member_preserves_commits_without_native_tracking(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        old = self.checkout("alpha")
        tip = self.commit_file(old, "api.txt", "First real PR\n")
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        first = self.member("api")
        self.assertEqual(first, self.checkout("alpha", "example--api"))
        self.assertEqual(self.git(first, "rev-parse", "HEAD"), tip)
        self.assertFalse(old.exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example", ok=False)
        self.assertEqual(self.catalog(source)["stacks"], [])
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "baton/example")
        self.assertEqual({p.name for p in (self.slots / "p3" / "repos").iterdir()}, {"alpha--api"})
        self.assertEqual((self.slots / "p3" / "repos" / "alpha--api").resolve(), first)
        self.assertEqual(self.cli("path", "p3", "alpha").stdout.strip(), str(first))


    def test_unnamed_checkout_keeps_plain_link(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        links = self.slots / "p3" / "repos"
        self.assertEqual({p.name for p in links.iterdir()}, {"alpha"})
        self.assertEqual((links / "alpha").readlink(), self.checkout("alpha"))

    def test_independent_member_uses_fresh_default_not_invoking_stack(self) -> None:
        for default in ("main", "master"):
            with self.subTest(default=default):
                repo = "repo-" + default
                source = self.make_repo(repo, default=default)
                self.create(repo, slot="p4" if default == "master" else "p3", name="project-" + default)
                slot = self.slots / ("p4" if default == "master" else "p3")
                self.cli("branch", "init", repo, "api", cwd=slot)
                first = Path(self.cli("path", repo, "--member", "api", cwd=slot).stdout.strip())
                self.commit_file(first, "api.txt", "Not a standalone dependency\n")
                self.cli("branch", "add", repo, "api-tests", "--after", "api", cwd=slot)
                child = Path(self.cli("path", repo, "--member", "api-tests", cwd=slot).stdout.strip())
                self.commit_file(child, "api-tests.txt", "Stack tests\n")
                latest = self.commit_file(source, "upstream.txt", "Fresh remote work\n")
                self.git(source, "push")
                self.cli("branch", "add", repo, "docs", cwd=child)
                docs = Path(self.cli("path", repo, "--member", "docs", cwd=slot).stdout.strip())
                self.assertEqual(self.git(docs, "rev-parse", "HEAD"), latest)
                self.assertTrue((docs / "upstream.txt").exists())
                self.assertFalse((docs / "api.txt").exists())
                self.assertFalse((docs / "api-tests.txt").exists())
                self.assertEqual(self.git(child, "branch", "--show-current"),
                                 "baton/project-" + default + "--api-tests")

    def test_one_repo_contains_two_native_stacks_and_standalone(self) -> None:
        source, paths = self.two_groups()
        groups = [{branch["branch"] for branch in stack["branches"]}
                  for stack in self.catalog(source)["stacks"]]
        self.assertCountEqual(groups, [
            {"baton/example--api", "baton/example--api-tests"},
            {"baton/example--docs", "baton/example--docs-tests"},
        ])
        for stack in self.catalog(source)["stacks"]:
            self.assertEqual(stack["trunk"]["branch"], "main")
        for path in paths:
            self.assertEqual(self.git(path, "branch", "--show-current"), "baton/" + path.name)
            self.assertIn("example", self.cli("status", cwd=path).stdout)
        status = self.cli("status", "p3").stdout
        for name in ("api", "api-tests", "docs", "docs-tests", "tooling"):
            self.assertIn("example--" + name, status)
        self.assertIn("main", status)
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_empty_independent_siblings_are_allowed_but_cannot_become_parents(self) -> None:
        first = self.init_members()
        self.add_member("docs")
        self.add_member("tooling")
        for parent in ("api", "docs"):
            result = self.cli("branch", "add", "alpha", "blocked", "--after", parent,
                              cwd=self.slots / "p3", ok=False)
            self.assertIn("changes", result.stderr.lower())
        self.git(first, "commit", "--allow-empty", "-m", "Still no PR change")
        self.cli("branch", "add", "alpha", "blocked", "--after", "api",
                 cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.catalog(self.repos / "alpha")["stacks"], [])
        self.assertFalse(self.checkout("alpha", "example--blocked").exists())

    def test_after_an_unnamed_first_checkout_suggests_branch_init(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        result = self.cli("branch", "add", "alpha", "child", "--after", "example", cwd=self.slots / "p3", ok=False)
        self.assertIn("baton branch init alpha <name>", result.stderr)

    def test_after_requires_top_of_selected_native_stack(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        child = self.add_member("api-tests", after="api")
        self.commit_file(child, "tests.txt", "Tests\n")
        before = (self.slots / "p3" / "project.json").read_bytes()
        result = self.cli("branch", "add", "alpha", "inserted", "--after", "api",
                          cwd=self.slots / "p3", ok=False)
        self.assertIn("top", result.stderr.lower())
        self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before)
        self.assertFalse(self.checkout("alpha", "example--inserted").exists())

    def test_stack_selection_is_ambiguous_in_slot_but_inferred_from_member(self) -> None:
        source, paths = self.two_groups()
        for action in ("status",):
            args = ["stack", action, "alpha"] + (["ambiguous"] if action == "add" else [])
            result = self.cli(*args, cwd=self.slots / "p3", ok=False)
            self.assertIn("member", result.stderr.lower())
        self.cli("branch", "add", "alpha", "api-final", "--after", "api-tests", cwd=paths[1])
        self.cli("branch", "add", "alpha", "docs-final", "--after", "docs-tests", cwd=self.slots / "p3")
        groups = [{branch["branch"] for branch in stack["branches"]}
                  for stack in self.catalog(source)["stacks"]]
        self.assertIn({"baton/example--api", "baton/example--api-tests", "baton/example--api-final"}, groups)
        self.assertIn({"baton/example--docs", "baton/example--docs-tests", "baton/example--docs-final"}, groups)



    def test_member_named_root_is_not_a_reserved_selector(self) -> None:
        first = self.init_members()
        root = self.add_member("root")
        self.assertNotEqual(first, root)
        self.assertEqual(self.cli("path", "p3", "alpha", "--member", "root").stdout.strip(), str(root))
        self.commit_file(root, "root.txt", "Independent member literally named root\n")
        child = self.add_member("root-tests", after="root")
        self.assertTrue((child / "root.txt").exists())
        groups = self.catalog(self.repos / "alpha")["stacks"]
        self.assertEqual([branch["branch"] for branch in groups[0]["branches"]],
                         ["baton/example--root", "baton/example--root-tests"])


    def test_paths_accept_registered_member_names(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        api = self.member("api", "alpha")
        self.commit_file(api, "api.txt", "API\n")
        docs = self.add_member("docs", repo="alpha")
        child = self.add_member("tests", repo="alpha", after="api")
        for name, path in (("api", api), ("docs", docs), ("tests", child)):
            for flag in ("--member",):
                self.assertEqual(self.cli("path", "alpha", flag, name, cwd=docs).stdout.strip(), str(path))
            self.assertEqual((self.slots / "p3" / "repos" / ("alpha--" + name)).resolve(), path)

    def test_native_rebase_changes_only_selected_group(self) -> None:
        _, paths = self.two_groups()
        other_tips = {path: self.git(path, "rev-parse", "HEAD") for path in paths[2:]}
        tip = self.commit_file(paths[0], "api-follow-up.txt", "Parent changed\n")
        self.native(paths[1], "rebase", "--no-trunk")
        self.git(paths[1], "merge-base", "--is-ancestor", tip, "HEAD")
        for path, before in other_tips.items():
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), before)
        for path in paths:
            self.assertEqual(self.git(path, "branch", "--show-current"), "baton/" + path.name)

    def test_branch_and_path_collisions_preserve_inventory(self) -> None:
        self.init_members()
        source = self.repos / "alpha"
        self.git(source, "branch", "baton/example--existing")
        occupied = self.checkout("alpha", "example--occupied")
        occupied.mkdir()
        (occupied / "keep.txt").write_text("Foreign files\n")
        dangling = self.checkout("alpha", "example--dangling")
        dangling.symlink_to(self.root / "missing")
        before = (self.slots / "p3" / "project.json").read_bytes()
        for name in ("api", "existing", "occupied", "dangling", "../outside", "nested/name"):
            self.cli("branch", "add", "alpha", name, cwd=self.slots / "p3", ok=False)
            self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before)
        self.assertEqual((occupied / "keep.txt").read_text(), "Foreign files\n")

    def test_member_destination_checks_pending_branch_and_path_claims_without_mutating(self) -> None:
        self.init_members()
        manager = Manager()
        manager.worktrees = self.worktrees
        manager.state = fixtures.read_state(self.projects)
        project = manager.state["projects"]["p3"]
        repo = project["repos"][0]
        target, path = manager.member_branch_and_path(project, "alpha", "child")
        self.assertEqual(target, "baton/example--child")
        self.assertEqual(path, self.checkout("alpha", "example--child"))
        for invalid in ("../escape", "nested/name", "", "-child"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ProjectError, "simple member slug"):
                manager.member_branch_and_path(project, "alpha", invalid)
        other = {"slot": "p4", "name": "other", "branch": "baton/other", "repos": []}
        manager.state["projects"]["p4"] = other
        for collision in ("branch", "path"):
            with self.subTest(collision=collision):
                pending_entry = dict(repo, branch=target if collision == "branch" else "baton/other--child",
                                     path=str(path) if collision == "path" else str(self.root / "other-child"))
                other["pending_member"] = {"entry": pending_entry}
                before = json.dumps(manager.state, sort_keys=True)
                with self.assertRaisesRegex(ProjectError, "belongs to another project"):
                    manager.check_member_destination(project, repo, "child", target, path)
                self.assertEqual(json.dumps(manager.state, sort_keys=True), before)
                self.assertFalse(path.exists())
                self.assertEqual(manager.reserved, set())
                self.git(self.repos / "alpha", "show-ref", "--verify", f"refs/heads/{target}", ok=False)

    def test_register_adopts_existing_independent_branch_and_worktree(self) -> None:
        self.init_members()
        source = self.repos / "alpha"
        for name, create_worktree in (("branch-only", False), ("existing-worktree", True)):
            with self.subTest(name=name):
                target = "baton/example--" + name
                path = self.checkout("alpha", "example--" + name)
                self.git(source, "branch", target, "main")
                if create_worktree:
                    self.git(source, "worktree", "add", str(path), target)
                self.cli("branch", "register", "alpha", name, cwd=self.slots / "p3")
                self.assertEqual(self.member(name), path)
                self.assertEqual(self.git(path, "branch", "--show-current"), target)
                self.assertEqual(self.catalog(source)["stacks"], [])

    def test_independent_add_resumes_after_coordinator_commit_failure(self) -> None:
        self.init_members()
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3", ok=False)
        checkout = self.checkout("alpha", "example--docs")
        self.assertTrue(checkout.exists())
        tip = self.git(checkout, "rev-parse", "HEAD")
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.member("docs"), "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_dependency_add_recovers_after_native_add_succeeds_then_fails(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "API\n")
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        self.assertIsNotNone(actual_gh)
        bindir = self.root / "interrupt-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text("#!/bin/sh\n" + shlex.quote(str(actual_gh)) + ' "$@" || exit $?\n'
                           'if [ "$1" = "stack" ] && [ "$2" = "add" ]; then exit 1; fi\n')
        wrapper.chmod(0o755)
        original = self.env["PATH"]
        self.env["PATH"] = str(bindir) + os.pathsep + original
        self.cli("branch", "add", "alpha", "tests", "--after", "api",
                 cwd=self.slots / "p3", ok=False)
        self.env["PATH"] = original
        tip = self.git(self.repos / "alpha", "rev-parse", "baton/example--tests")
        self.cli("branch", "add", "alpha", "tests", "--after", "api", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.member("tests"), "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(first, "branch", "--show-current"), "baton/example--api")
        state = fixtures.read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "active")

    def test_register_adopts_native_member_of_explicit_group(self) -> None:
        _, paths = self.two_groups()
        parent = paths[3]
        self.native(parent, "add", "baton/example--docs-final")
        tip = self.commit_file(parent, "final.txt", "Existing native member\n")
        self.git(parent, "switch", "baton/example--docs-tests")
        self.cli("branch", "register", "alpha", "docs-final", "--after", "docs-tests",
                 cwd=self.slots / "p3")
        registered = self.member("docs-final")
        self.assertEqual(self.git(registered, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(parent, "branch", "--show-current"), "baton/example--docs-tests")

    def test_register_rejects_member_of_different_native_group(self) -> None:
        _, paths = self.two_groups()
        parent = paths[3]
        self.native(parent, "add", "baton/example--docs-final")
        self.git(parent, "switch", "baton/example--docs-tests")
        before = (self.slots / "p3" / "project.json").read_bytes()
        self.cli("branch", "register", "alpha", "docs-final", "--after", "api-tests",
                 cwd=self.slots / "p3", ok=False)
        self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before)
        self.assertFalse(self.checkout("alpha", "example--docs-final").exists())

    def test_cleanup_all_groups_once_without_touching_foreign_branches_or_catalog(self) -> None:
        source, paths = self.two_groups()
        for branch in ("api-tests", "docs-tests", "tooling"):
            self.git(source, "merge", "--no-edit", "baton/example--" + branch)
        self.git(source, "push")
        foreign = self.root / "foreign-worktree"
        self.git(source, "worktree", "add", "-b", "foreign-feature", str(foreign), "main")
        self.native(foreign, "init", "--base", "main", "foreign-feature")
        foreign_catalog = next(stack for stack in self.catalog(source)["stacks"]
                               if stack["branches"][0]["branch"] == "foreign-feature")
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        before = (source / ".git" / "gh-stack").read_bytes()
        self.cli("cleanup", "p3", "--dry-run")
        self.assertEqual((source / ".git" / "gh-stack").read_bytes(), before)
        trace.write_text("")
        self.cli("cleanup", "p3")
        self.assertEqual(trace.read_text().count("git fetch origin refs/heads/main:refs/remotes/origin/main"), 1)
        for path in paths:
            self.assertFalse(path.exists())
            self.git(source, "show-ref", "--verify", "refs/heads/baton/" + path.name, ok=False)
        self.assertTrue(foreign.exists())
        self.assertEqual(self.catalog(source)["stacks"], [foreign_catalog])

    def test_independent_pr_merge_into_sibling_is_not_integration_proof(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        api_tip = self.commit_file(first, "api.txt", "API\n")
        docs = self.add_member("docs")
        docs_tip = self.commit_file(docs, "docs.txt", "Documentation\n")
        self.git(first, "merge", "--squash", "baton/example--docs")
        self.git(first, "commit", "-m", "Squash independent docs into sibling")
        parent_merge = self.git(first, "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example--api")
        self.git(source, "commit", "-m", "Squash API into main")
        self.git(source, "push")
        main_tip = self.git(source, "rev-parse", "HEAD")
        self.fake_pull_requests([
            {"state": "MERGED", "headRefName": "baton/example--docs", "headRefOid": docs_tip,
             "baseRefName": "baton/example--api", "mergeCommit": {"oid": parent_merge}},
            {"state": "MERGED", "headRefName": "baton/example--api", "headRefOid": parent_merge,
             "baseRefName": "main", "mergeCommit": {"oid": main_tip}},
        ])
        result = self.cli("cleanup", "p3", "--dry-run", ok=False)
        self.assertIn("baton/example--docs", result.stderr)
        self.assertTrue(docs.exists())
        self.assertTrue(first.exists())
        self.assertNotEqual(api_tip, parent_merge)

    def test_cleanup_independent_squash_merges_directly_to_default(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", "url." + str(self.root / "alpha.git") + ".insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        self.commit_file(first, "api.txt", "API\n")
        docs = self.add_member("docs")
        self.commit_file(docs, "docs.txt", "Documentation\n")
        responses = []
        for path in (first, docs):
            branch = self.git(path, "branch", "--show-current")
            tip = self.git(path, "rev-parse", "HEAD")
            self.git(source, "merge", "--squash", branch)
            self.git(source, "commit", "-m", "Squash " + branch)
            responses.append({"state": "MERGED", "headRefName": branch, "headRefOid": tip,
                              "baseRefName": "main", "mergeCommit": {"oid": self.git(source, "rev-parse", "HEAD")}})
        self.git(source, "push")
        self.fake_pull_requests(responses)
        self.cli("cleanup", "p3")
        self.assertFalse(first.exists())
        self.assertFalse(docs.exists())
        archived = self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/")
        for response in responses:
            self.assertIn(response["headRefOid"], archived)


if __name__ == "__main__":
    unittest.main()
