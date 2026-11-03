import json
import os
import pty
import shutil
from pathlib import Path
import subprocess
import sys
import unittest

import test_project_manager as fixtures
import test_project_retirement as retirement_fixtures


class AbandonFollowUpTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    interactive = retirement_fixtures.ProjectRetirementTest.interactive
    spawn = fixtures.ProjectManagerTest.spawn
    wait_for = fixtures.ProjectManagerTest.wait_for
    finish = fixtures.ProjectManagerTest.finish
    manager_dir = fixtures.ProjectManagerTest.manager_dir

    def project(self) -> dict:
        return json.loads(fixtures.state_file(self.projects).read_text())

    def github(self, source: Path, prs: list[dict]) -> Path:
        original = self.git(source, "remote", "get-url", "origin")
        self.git(source, "config", f"url.{original}.insteadOf", f"https://github.com/test/{source.name}.git")
        self.git(source, "config", "remote.origin.url", f"https://github.com/test/{source.name}.git")
        state = self.root / "prs.json"
        state.write_text(json.dumps(prs))
        executable = self.root / "bin" / "gh"
        executable.parent.mkdir(exist_ok=True)
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
from urllib.parse import urlparse, parse_qs
root=pathlib.Path({str(self.root)!r})
path=root/'prs.json'
prs=json.loads(path.read_text())
args=sys.argv[1:]
with (root/'gh.log').open('a') as out: out.write(json.dumps(args)+'\\n')
if args[0]=='stack':
    os.execv({actual_gh!r}, [{actual_gh!r}, *args])
elif args[0]=='api':
    assert args[-2:]==['--hostname', 'github.com']
    if (root/'discovery-failure').exists(): sys.exit(1)
    if (root/'raw-api-response.json').exists():
        print((root/'raw-api-response.json').read_text())
        sys.exit(0)
    query=parse_qs(urlparse(args[1]).query)
    repository='/'.join(urlparse(args[1]).path.split('/')[1:3])
    page=int(query['page'][0])
    found=[p for p in prs if p['state']=='open' and p['base']['repo']['full_name']==repository]
    print(json.dumps(found[(page-1)*100:page*100]))
elif args[:2]==['pr','close']:
    number=int(args[2])
    if (root/('fail-close-'+str(number))).exists(): sys.exit(1)
    for pr in prs:
        if pr['number']==number and pr['base']['repo']['full_name']==args[-1].removeprefix('github.com/'): pr['state']='closed'
    path.write_text(json.dumps(prs))
else:
    print('[]')
''')
        executable.chmod(0o755)
        self.env["PATH"] = str(executable.parent) + os.pathsep + self.env["PATH"]
        return state

    @staticmethod
    def pr(number: int, branch: str = "baton/example", head_repo: str = "test/alpha") -> dict:
        return {"number": number, "html_url": f"https://github.com/test/alpha/pull/{number}", "state": "open",
                "head": {"ref": branch, "sha": "a" * 40, "repo": {"full_name": head_repo}},
                "base": {"repo": {"full_name": "test/alpha"}}}

    def test_abandon_cancel_and_non_tty_leave_project_unchanged(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        before = fixtures.state_file(self.projects).read_bytes(), prs.read_bytes()
        for reply in (b"n\n", b"\n", b"\x04"):
            self.interactive("abandon", reply=reply, ok=False)
            self.assertEqual(before, (fixtures.state_file(self.projects).read_bytes(), prs.read_bytes()))
        self.cli("abandon", "example", ok=False)
        self.assertEqual(before, (fixtures.state_file(self.projects).read_bytes(), prs.read_bytes()))

    def test_paginated_exact_identity_and_no_remote_branch_deletion(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.git(source, "push", "origin", "baton/example")
        prs = self.github(source, [self.pr(i, head_repo="fork/alpha") for i in range(1, 101)] + [self.pr(101)])
        result = self.interactive("abandon", reply=b"yes\n")
        self.assertIn("#101", result.stdout)
        records = json.loads(prs.read_text())
        self.assertEqual([pr["number"] for pr in records if pr["state"] == "closed"], [101])
        self.assertIn("refs/heads/baton/example", self.git(source, "ls-remote", "origin"))
        archived = fixtures.read_state(self.projects)["archive"][-1]
        self.assertNotIn("pending_abandon", archived)
        self.assertEqual(archived["state"], "archived")

    def test_partial_close_resume_and_cleanup_cannot_bypass_intent(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        failure = self.root / "fail-close-2"
        failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual(self.project()["state"], "abandoning")
        self.assertTrue(self.checkout("alpha").exists())
        data = json.loads(self.cli("status", "example", "--json").stdout)
        self.assertEqual(data["pending_operation"]["command"], "baton abandon " + self.project()["uuid"])
        self.cli("cleanup", "example", ok=False)
        self.cli("add", "alpha", cwd=self.slots / "p3", ok=False)
        failure.unlink()
        self.interactive("cleanup", reply=b"y\n")
        self.assertTrue(all(pr["state"] == "closed" for pr in json.loads(prs.read_text())))

    def test_cleanup_second_confirmation_and_reopened_pr(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        self.git(self.checkout("alpha"), "commit", "--allow-empty", "-m", "Unmerged change")
        result = self.interactive("abandon", reply=b"y\nn\n", ok=False)
        self.assertIn("Continue cleanup anyway?", result.stderr)
        self.assertEqual(self.project()["state"], "abandoning")
        records = json.loads(prs.read_text())
        records.append(self.pr(2))
        prs.write_text(json.dumps(records))
        self.interactive("abandon", reply=b"n\n", ok=False)
        self.assertEqual(json.loads(prs.read_text())[1]["state"], "open")
        self.interactive("abandon", reply=b"y\ny\n")
        self.assertIn("refs/project-archive", self.git(source, "show-ref"))

    def test_discovery_failure_never_starts_abandonment(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        (self.root / "discovery-failure").touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual(self.project()["state"], "active")
        self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")

    def close(self) -> dict:
        project = self.project()
        self.cli("cleanup", "example" if project["name"] == "example" else project["uuid"])
        return project

    def follow_up(self, predecessor: dict, ok: bool = True) -> subprocess.CompletedProcess:
        return self.cli("create", "follow-up", "--from", predecessor["uuid"], cwd=self.slots / "p3", ok=ok)

    def test_follow_up_chain_fresh_defaults_context_and_claim_json(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        first = self.close()
        original_archive = fixtures.read_state(self.projects)["archive"]
        self.git(source, "commit", "--allow-empty", "-m", "Advanced default")
        self.git(source, "push")
        self.follow_up(first)
        second = self.project()
        self.assertEqual(second["name"], "example-followup")
        self.assertEqual(second["follow_up_from"], first["uuid"])
        self.assertIsNone(second["owner"])
        self.assertNotEqual(second["blackboard"]["generation"], first["blackboard"]["generation"])
        self.assertEqual(self.git(self.checkout("alpha", second["name"]), "rev-parse", "HEAD"), self.git(source, "rev-parse", "HEAD"))
        context = Path(second["follow_up_path"])
        self.assertIn(first["uuid"], context.read_text())
        self.assertIn("historical evidence", context.read_text())
        self.assertIn("unavailable", context.read_text())
        self.assertEqual(fixtures.read_state(self.projects)["archive"], original_archive)
        self.git(self.artifacts, "show", "HEAD:" + str(context.relative_to(self.artifacts)))
        claim = self.cli("session", "claim", "--selector", second["uuid"], "--provider", "codex", "--session-id", "12345678-1234-1234-1234-123456789012")
        self.assertEqual(json.loads(claim.stdout)["provider"], "codex")
        self.assertIn(str(context), claim.stderr)
        self.cli("cleanup", second["uuid"])
        self.follow_up(second)
        third = self.project()
        self.assertEqual(third["name"], "example-followup2")
        self.assertEqual(third["follow_up_from"], second["uuid"])
        self.assertIn(str(context), Path(third["follow_up_path"]).read_text())
        self.cli("cleanup", third["uuid"])
        self.follow_up(third)
        self.assertEqual(self.project()["name"], "example-followup3")

    def test_follow_up_requires_closed_full_uuid_and_exclusive_inputs(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        active = self.project()
        self.follow_up(active, ok=False)
        for identifier in ("example", "p3", "bad", "0" * 32):
            self.cli("create", "follow-up", "--from", identifier, cwd=self.slots / "p3", ok=False)
        self.cli("create", "anything", "--from", active["uuid"], cwd=self.slots / "p3", ok=False)
        self.cli("create", "follow-up", "--from", active["uuid"], "--repos", "alpha", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.project(), active)

    def test_follow_up_preflight_preserves_occupied_slot(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        first = self.close()
        self.create("alpha", name="occupied")
        before = self.project()
        with (self.artifacts / ".gitignore").open("a") as output:
            output.write("follow-up.md\n")
        result = self.follow_up(first, ok=False)
        self.assertIn("must not be ignored", result.stderr)
        self.assertEqual(self.project(), before)
        self.assertTrue(self.checkout("alpha", "occupied").exists())

    def test_follow_up_uses_recorded_source_and_rejects_missing_source(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        first = self.close()
        self.env["BATON_REPOS_ROOT"] = str(self.root / "different-repos")
        self.follow_up(first)
        self.assertEqual(self.project()["repos"][0]["source"], str(source))
        second = self.project()
        self.cli("cleanup", second["uuid"])
        source.rename(source.with_name("moved"))
        self.follow_up(second, ok=False)
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def git_failure(self, condition: str) -> Path:
        executable = self.root / "bin" / "git"
        executable.parent.mkdir(exist_ok=True)
        real_git = shutil.which("git", path=self.env["PATH"])
        marker = self.root / "git-failure"
        marker.touch()
        executable.write_text(f'''#!{sys.executable}
import os, pathlib, sys
args=sys.argv[1:]
marker=pathlib.Path({str(marker)!r})
if marker.exists() and ({condition}):
    print('injected Git failure', file=sys.stderr)
    sys.exit(1)
os.execv({real_git!r}, [{real_git!r}, *args])
''')
        executable.chmod(0o755)
        self.env["PATH"] = str(executable.parent) + os.pathsep + self.env["PATH"]
        return marker

    def test_retry_changed_tip_requires_new_confirmation_before_remaining_close(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        failure = self.root / "fail-close-2"
        failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        failure.unlink()
        self.git(self.checkout("alpha"), "commit", "--allow-empty", "-m", "Changed after approval")
        result = self.interactive("abandon", reply=b"n\n", ok=False)
        self.assertIn("Changed after approval", result.stdout)
        self.assertEqual(json.loads(prs.read_text())[1]["state"], "open")
        self.interactive("abandon", reply=b"y\ny\n")

    def test_retry_after_partial_worktree_removal(self) -> None:
        self.make_repo("alpha")
        self.make_repo("beta")
        self.create("alpha", "beta")
        marker = self.git_failure("'worktree' in args and 'remove' in args and args[-1].endswith('/alpha/example')")
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertFalse(self.checkout("beta").exists())
        self.assertTrue(self.checkout("alpha").exists())
        marker.unlink()
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_retry_after_coordinator_parking(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        marker = self.git_failure(f"args[:2] == ['-C', {str(self.projects)!r}] and args[2:4] == ['update-ref', '-d'] and args[4] == 'refs/heads/baton/example'")
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")
        marker.unlink()
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_follow_up_creation_retry_keeps_identity_and_context_without_artifact_remote(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        marker = self.git_failure(f"args[:2] == ['-C', {str(self.artifacts)!r}] and 'commit' in args")
        self.follow_up(predecessor, ok=False)
        pending = self.project()
        self.assertEqual(pending["state"], "creating")
        self.assertTrue(Path(pending["follow_up_path"]).is_file())
        data = json.loads(self.cli("status", "p3", "--json").stdout)
        self.assertIn("create follow-up --from " + predecessor["uuid"], data["pending_operation"]["command"])
        self.git(self.artifacts, "remote", "add", "origin", str(self.root / "unavailable-artifact-remote"))
        # Cache the default locally; creation must not contact this unavailable remote.
        self.git(self.artifacts, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(self.artifacts, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        marker.unlink()
        self.follow_up(predecessor)
        self.assertEqual(self.project()["uuid"], pending["uuid"])
        self.assertEqual(self.project()["state"], "active")
        self.assertNotIn("pending_follow_up_context", json.loads((self.slots / "p3" / "project.json").read_text()))

    def test_follow_up_renamed_lineage_natural_suffix_and_name_collisions(self) -> None:
        self.make_repo("alpha")
        self.create("alpha", name="natural-followup2")
        first = self.close()
        self.follow_up(first)
        second = self.project()
        self.assertEqual(second["name"], "natural-followup2-followup")
        self.cli("rename", "renamed", second["uuid"])
        self.cli("cleanup", second["uuid"])
        self.follow_up(second)
        self.assertEqual(self.project()["name"], "natural-followup2-followup2")
        self.cli("cleanup", self.project()["uuid"])
        self.create("alpha", name="natural-followup2-followup", slot="p4")
        self.follow_up(first, ok=False)
        self.cli("rename", "collision-alias", "p4")
        result = self.follow_up(first, ok=False)
        self.assertIn("former name", result.stderr)

    def test_retired_branches_are_discovered_and_all_repository_sources_inherited(self) -> None:
        alpha = self.make_repo("alpha")
        beta = self.make_repo("beta")
        self.create("alpha")
        self.cli("add", "beta", cwd=self.slots / "p3")
        self.cli("branch", "init", "alpha", "api", cwd=self.slots / "p3")
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3")
        self.cli("branch", "cleanup", "alpha", "api", "--selector", "example")
        self.cli("branch", "cleanup", "alpha", "docs", "--selector", "example")
        self.assertTrue(all(entry["retirement_state"] == "retired"
                            for entry in [self.project()["repos"][0], *self.project()["repos"][0]["members"]]))
        prs = self.github(alpha, [self.pr(1, "baton/example--api"), self.pr(2, "baton/example--docs")])
        predecessor = self.project()
        self.interactive("abandon", reply=b"y\n")
        self.assertTrue(all(pr["state"] == "closed" for pr in json.loads(prs.read_text())))
        self.follow_up(predecessor)
        repos = self.project()["repos"]
        self.assertEqual([repo["source"] for repo in repos], [str(alpha), str(beta)])
        for repo in repos:
            self.assertNotIn("members", repo)
            self.assertNotIn("stacks", repo)
            self.assertNotIn("retirement_state", repo)
            self.assertTrue(Path(repo["path"]).exists())

    def test_unsafe_historical_leaf_and_ancestor_are_unavailable(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        directory = Path(predecessor["artifacts_path"])
        (directory / "plan.md").symlink_to(self.root / "missing-plan")
        self.follow_up(predecessor)
        self.assertIn("Plan: unavailable", Path(self.project()["follow_up_path"]).read_text())
        self.cli("cleanup", self.project()["uuid"])
        (directory / "plan.md").unlink()
        directory.rename(directory.with_name("relocated-evidence"))
        directory.symlink_to(directory.with_name("relocated-evidence"), target_is_directory=True)
        self.follow_up(predecessor)
        self.assertIn("Coordination history: unavailable", Path(self.project()["follow_up_path"]).read_text())

    def test_context_retry_refuses_unexpected_file_and_symlink(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        marker = self.git_failure(f"args[:2] == ['-C', {str(self.artifacts)!r}] and 'commit' in args")
        self.follow_up(predecessor, ok=False)
        context = Path(self.project()["follow_up_path"])
        original = context.read_text()
        marker.unlink()
        context.write_text("user content")
        self.follow_up(predecessor, ok=False)
        self.assertEqual(context.read_text(), "user content")
        context.unlink()
        context.symlink_to(self.root / "unrelated-context")
        self.follow_up(predecessor, ok=False)
        self.assertTrue(context.is_symlink())
        context.unlink()
        context.write_text(original)
        self.follow_up(predecessor)
        self.assertEqual(self.project()["state"], "active")

    def test_native_stack_abandonment_preserves_tips_and_untracks_locally(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.git(self.checkout("alpha"), "commit", "--allow-empty", "-m", "Root change")
        (self.checkout("alpha") / "root.txt").write_text("root implementation")
        self.git(self.checkout("alpha"), "add", "root.txt")
        self.git(self.checkout("alpha"), "commit", "-m", "Implement root")
        self.cli("branch", "add", "alpha", "child", "--after", "baton/example", cwd=self.slots / "p3")
        child = Path(self.cli("path", "p3", "alpha", "--member", "child").stdout.strip())
        self.git(child, "commit", "--allow-empty", "-m", "Child change")
        source = self.repos / "alpha"
        tips = [self.git(path, "rev-parse", "HEAD") for path in (self.checkout("alpha"), child)]
        prs = self.github(source, [self.pr(1), self.pr(2, "baton/example--child")])
        self.env["GH_HOST"] = "unrelated.example.invalid"
        self.interactive("abandon", reply=b"y\ny\n")
        self.assertTrue(all(pr["state"] == "closed" for pr in json.loads(prs.read_text())))
        archived = fixtures.read_state(self.projects)["archive"][-1]
        self.assertTrue(all(record["untracked"] for record in archived["repos"][0]["stacks"]))
        refs = self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive")
        self.assertTrue(all(tip in refs for tip in tips))

    def test_coordination_commit_failure_retains_intent_and_worktrees(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        marker = self.git_failure(f"args[:2] == ['-C', {str(self.artifacts)!r}] and 'commit' in args")
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertIn("pending_abandon", self.project())
        records = json.loads(prs.read_text())
        records[0]["state"] = "open"
        prs.write_text(json.dumps(records))
        self.interactive("abandon", reply=b"n\n", ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")
        marker.unlink()
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_concurrent_cleanup_cannot_skip_pending_pr_closures(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        (self.root / "fail-close-2").touch()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        abandon, abandon_log = self.spawn("abandon", "example", cwd=self.slots / "p3", name="abandon", stdin=slave)
        self.wait_for(abandon_log, "Abandon this project?")
        cleanup, cleanup_log = self.spawn("cleanup", "example", cwd=self.slots / "p3", name="cleanup")
        self.wait_for(cleanup_log, "waiting for the slot p3 lock")
        os.write(master, b"y\n")
        self.assertNotEqual(self.finish(abandon, "abandon")[0], 0)
        code, _, error = self.finish(cleanup, "cleanup")
        self.assertNotEqual(code, 0)
        self.assertIn("Interactive confirmation is required for abandonment", error)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(json.loads(prs.read_text())[1]["state"], "open")

    def test_concurrent_follow_up_names_are_reserved_once(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        fixtures.ProjectManagerTest.ensure_slot(self, "p4")
        racers = [self.spawn("create", "follow-up", "--from", predecessor["uuid"], cwd=self.slots / slot, name=slot)[0]
                  for slot in ("p3", "p4")]
        outcomes = [self.finish(process, slot)[0] for process, slot in zip(racers, ("p3", "p4"))]
        self.assertEqual(sorted(code == 0 for code in outcomes), [False, True])
        projects = fixtures.read_state(self.projects)["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(next(iter(projects.values()))["name"], "example-followup")

    def test_repository_discovery_request_count_does_not_scale_with_recorded_branches(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        for number in range(8):
            self.cli("branch", "add", "alpha", f"member{number}", cwd=self.slots / "p3")
        for number in range(4):
            self.cli("branch", "cleanup", "alpha", f"member{number}", "--selector", "example")
        unrelated = [self.pr(number, f"unrelated-{number}") for number in range(10, 130)]
        prs = self.github(source, [self.pr(1), self.pr(2, "baton/example--member0"), *unrelated])
        self.interactive("abandon", reply=b"y\n")
        calls = [json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()]
        api = [call for call in calls if call[0] == "api"]
        self.assertEqual(len(api), 6)  # Three independently verified snapshots, two complete pages each.
        self.assertEqual([call[1].split("page=")[-1] for call in api], ["1", "2", "1", "2", "1", "2"])
        self.assertTrue(all("head=" not in call[1] for call in api))
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "closed"], [1, 2])

    def test_pending_abandonment_blocks_changed_origin_before_discovery_or_cleanup(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        failure = self.root / "fail-close-2"
        failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        original = self.git(source, "config", "--get", "remote.origin.url")
        self.assertEqual(self.project()["pending_abandon"]["repositories"]["alpha"]["origin"], original)
        failure.unlink()
        before = fixtures.state_file(self.projects).read_bytes(), (self.root / "gh.log").read_bytes()
        for changed in (str(self.root / "alpha.git"), "https://github.com/other/repository.git"):
            self.git(source, "config", "remote.origin.url", changed)
            result = self.interactive("abandon", reply=b"y\n", ok=False)
            self.assertIn("Restore the recorded origin URLs", result.stderr)
            self.assertEqual(before, (fixtures.state_file(self.projects).read_bytes(), (self.root / "gh.log").read_bytes()))
            self.assertEqual(json.loads(prs.read_text())[1]["state"], "open")
            self.assertTrue(self.checkout("alpha").exists())
        self.git(source, "config", "remote.origin.url", original)
        self.interactive("abandon", reply=b"y\n")

    def test_changed_origin_during_abandonment_confirmation_requires_rerun(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        original = self.git(source, "config", "--get", "remote.origin.url")
        other = "https://github.com/other/repository.git"
        self.git(source, "config", "--add", f"url.{self.root / 'alpha.git'}.insteadOf", other)
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        for index, changed in enumerate((str(self.root / "alpha.git"), other)):
            process, log = self.spawn("abandon", "example", cwd=self.slots / "p3", name=f"origin-race-{index}", stdin=slave)
            self.wait_for(log, "Abandon this project?")
            self.git(source, "config", "remote.origin.url", changed)
            os.write(master, b"y\n")
            code, _, error = self.finish(process, f"origin-race-{index}")
            self.assertNotEqual(code, 0)
            self.assertIn("scope changed during confirmation", error)
            self.assertNotIn("pending_abandon", self.project())
            self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")
            self.assertTrue(self.checkout("alpha").exists())
            self.git(source, "config", "remote.origin.url", original)

    def test_changed_origin_during_cleanup_confirmation_cannot_remove_worktrees(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.github(source, [self.pr(1)])
        original = self.git(source, "config", "--get", "remote.origin.url")
        (self.checkout("alpha") / "dirty.txt").write_text("preserve until cleanup confirmation")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process, log = self.spawn("abandon", "example", cwd=self.slots / "p3", name="cleanup-origin-race", stdin=slave)
        self.wait_for(log, "Abandon this project?")
        os.write(master, b"y\n")
        self.wait_for(log, "Continue cleanup anyway?")
        self.git(source, "config", "remote.origin.url", str(self.root / "alpha.git"))
        os.write(master, b"y\n")
        code, _, error = self.finish(process, "cleanup-origin-race")
        self.assertNotEqual(code, 0)
        self.assertIn("Restore the recorded origin URLs", error)
        self.assertTrue(self.checkout("alpha").exists())
        self.git(source, "config", "remote.origin.url", original)
        self.interactive("abandon", reply=b"y\n")

    def test_follow_up_rejects_malformed_later_repository_record(self) -> None:
        self.make_repo("alpha")
        self.make_repo("beta")
        self.create("alpha", "beta")
        predecessor = self.close()
        archive = next((self.manager_dir() / "v2" / "archive").glob("*.json"))
        original = json.loads(archive.read_text())
        for malformed in (None, {}, {"id": "beta", "source": 42, "common_dir": "missing"}):
            changed = dict(original, repos=[original["repos"][0], malformed])
            archive.write_text(json.dumps(changed))
            before = archive.read_bytes()
            result = self.follow_up(predecessor, ok=False)
            self.assertIn("lacks its ID", result.stderr)
            self.assertEqual(archive.read_bytes(), before)
            self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_optional_historical_git_metadata_paths_are_unavailable(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        directory = Path(predecessor["artifacts_path"])
        (directory / "plan.md").symlink_to(source / ".git" / "HEAD")
        self.follow_up(predecessor)
        context = Path(self.project()["follow_up_path"]).read_text()
        self.assertIn("Plan: unavailable", context)
        self.assertNotIn("[Plan]", context)

    def test_prior_context_metadata_and_links_are_safe(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        first = self.close()
        self.follow_up(first)
        predecessor = self.close()
        archive = next((self.manager_dir() / "v2" / "archive").glob(f"*-{predecessor['uuid']}.json"))
        original = json.loads(archive.read_text())
        for invalid in ("/tmp/unrelated-context.md", "/tmp/context>\nInjected instructions", 42):
            archive.write_text(json.dumps(dict(original, follow_up_path=invalid)))
            result = self.follow_up(predecessor, ok=False)
            self.assertIn("Archived follow-up", result.stderr)
            self.assertEqual(fixtures.read_state(self.projects)["projects"], {})
        archive.write_text(json.dumps(original))
        prior = Path(predecessor["follow_up_path"])
        prior.unlink()
        prior.symlink_to(self.root / "unsafe-context")
        self.follow_up(predecessor)
        context = Path(self.project()["follow_up_path"]).read_text()
        self.assertIn("Earlier follow-up context: unavailable", context)
        self.assertNotIn("[Earlier follow-up context]", context)
        self.cli("cleanup", self.project()["uuid"])
        prior.unlink()
        self.follow_up(predecessor)
        self.assertIn("Earlier follow-up context: unavailable", Path(self.project()["follow_up_path"]).read_text())

    def test_historical_link_targets_escape_markdown_delimiters_and_controls(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        archive = next((self.manager_dir() / "v2" / "archive").glob("*.json"))
        record = json.loads(archive.read_text())
        saved = self.root / "saved>\nInjected [instructions]"
        saved.mkdir()
        (saved / "overview.md").write_text("historical overview")
        record["coordination_archive"]["directory"] = str(saved)
        archive.write_text(json.dumps(record))
        self.follow_up(predecessor)
        context = Path(self.project()["follow_up_path"]).read_text()
        self.assertIn("saved%3E%0AInjected%20%5Binstructions%5D/overview.md", context)
        self.assertNotIn("\nInjected", context)

    def test_malformed_optional_coordination_metadata_fails_clearly(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        predecessor = self.close()
        archive = next((self.manager_dir() / "v2" / "archive").glob("*.json"))
        record = json.loads(archive.read_text())
        for invalid in ([], "not-an-object", {"directory": 42}, {"directory": "relative/path"}):
            archive.write_text(json.dumps(dict(record, coordination_archive=invalid)))
            result = self.follow_up(predecessor, ok=False)
            self.assertIn("Archived coordination", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_unrelated_deleted_fork_heads_do_not_block_discovery(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        deleted = [self.pr(2, "unrelated-branch"), self.pr(3)]
        for pr in deleted:
            pr["head"]["repo"] = None
        prs = self.github(source, [self.pr(1), *deleted, self.pr(4, head_repo="foreign/alpha")])
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "closed"], [1])

    def test_confirmed_project_pr_with_unavailable_head_still_blocks_recovery(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        (self.root / "fail-close-1").touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        records = json.loads(prs.read_text())
        records[0]["head"]["repo"] = None
        prs.write_text(json.dumps(records))
        before = fixtures.state_file(self.projects).read_bytes()
        result = self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertIn("confirmed pull request head repository is unavailable", result.stderr)
        self.assertEqual(fixtures.state_file(self.projects).read_bytes(), before)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")

    def renamed_prs(self) -> tuple[Path, Path, Path]:
        alpha = self.make_repo("alpha")
        beta = self.make_repo("beta")
        self.create("alpha")
        self.cli("rename", "middle", "example")
        self.cli("add", "beta", cwd=self.slots / "p3")
        self.cli("rename", "latest", "middle")
        records = [self.pr(number, "baton/" + name) for number, name in enumerate(("example", "middle", "latest"), 1)]
        for number, name in enumerate(("example", "middle", "latest"), 4):
            pr = self.pr(number, "baton/" + name, "test/beta")
            pr["base"]["repo"]["full_name"] = "test/beta"
            pr["html_url"] = f"https://github.com/test/beta/pull/{number}"
            records.append(pr)
        prs = self.github(alpha, records)
        original = self.git(beta, "config", "--get", "remote.origin.url")
        url = "https://github.com/test/beta.git"
        self.git(beta, "config", f"url.{original}.insteadOf", url)
        self.git(beta, "config", "remote.origin.url", url)
        return alpha, beta, prs

    def test_renamed_project_uses_exact_uuid_and_repository_manifest_history(self) -> None:
        _alpha, _beta, prs = self.renamed_prs()
        coordinator = self.slots / "p3"
        original = (coordinator / "project.json").read_text()
        foreign = json.loads(original)
        foreign["uuid"] = "f" * 32
        foreign["repos"][0]["branch"] = "baton/not-project"
        other_repo = json.loads(original)
        other_repo["repos"][1].update(common_dir="/unrelated/repository/.git", branch="baton/example")
        for manifest in (foreign, other_repo, json.loads(original)):
            (coordinator / "project.json").write_text(json.dumps(manifest, indent=2) + "\n")
            self.git(coordinator, "add", "project.json")
            self.git(coordinator, "commit", "-m", "Record isolated historical identity fixture")
        records = json.loads(prs.read_text())
        records.append(self.pr(7, "baton/not-project"))
        prs.write_text(json.dumps(records))
        result = self.interactive("abandon", reply=b"y\n")
        self.assertIn("#1", result.stdout)
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "closed"], [1, 2, 3, 5, 6])

    def test_renamed_branch_inventory_survives_partial_worktree_cleanup(self) -> None:
        _alpha, _beta, prs = self.renamed_prs()
        marker = self.git_failure("'worktree' in args and 'remove' in args and args[-1].endswith('/alpha/latest')")
        self.interactive("abandon", reply=b"y\n", ok=False)
        pending = self.project()["pending_abandon"]["branches"]
        self.assertEqual(pending["alpha"]["branches"], ["baton/example", "baton/latest", "baton/middle"])
        self.assertEqual(pending["beta"]["branches"], ["baton/latest", "baton/middle"])
        self.assertFalse(self.checkout("beta", "latest").exists())
        records = json.loads(prs.read_text())
        records[0]["state"] = "open"
        prs.write_text(json.dumps(records))
        marker.unlink()
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "open"], [4])

    def test_renamed_inventory_survives_parking_and_deleted_coordinator_ref(self) -> None:
        _alpha, _beta, prs = self.renamed_prs()
        marker = self.git_failure(f"args[:2] == ['-C', {str(self.projects)!r}] and args[2:4] == ['update-ref', '-d'] and args[4] == 'refs/heads/baton/latest'")
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual(self.project()["state"], "cleaning")
        self.assertEqual(self.git(self.slots / "p3", "branch", "--show-current"), "p3")
        marker.unlink()
        self.git(self.projects, "update-ref", "-d", "refs/heads/baton/latest")
        records = json.loads(prs.read_text())
        records[0]["state"] = "open"
        prs.write_text(json.dumps(records))
        trace = self.root / "resume-git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.interactive("abandon", reply=b"y\n")
        self.assertNotIn("git log --full-history", trace.read_text())
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "open"], [4])
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_unavailable_required_rename_history_blocks_before_pr_closure(self) -> None:
        _alpha, _beta, prs = self.renamed_prs()
        self.git_failure("'log' in args and 'project.json' in args")
        result = self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertIn("Cannot establish exact project branch history", result.stderr)
        self.assertNotIn("pending_abandon", self.project())
        self.assertTrue(all(pr["state"] == "open" for pr in json.loads(prs.read_text())))
        self.assertTrue(self.checkout("alpha", "latest").exists())

    def test_truncated_rename_history_cannot_guess_former_branch_names(self) -> None:
        _alpha, _beta, prs = self.renamed_prs()
        tree = self.git(self.projects, "rev-parse", "refs/heads/baton/latest^{tree}")
        anchor = self.git(self.projects, "commit-tree", tree, "-m", "Truncated isolated coordinator history")
        self.git(self.projects, "update-ref", "refs/heads/baton/latest", anchor)
        result = self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertIn("Required former-name/repository manifests are missing", result.stderr)
        self.assertNotIn("pending_abandon", self.project())
        self.assertTrue(all(pr["state"] == "open" for pr in json.loads(prs.read_text())))

    def test_named_first_member_closes_recorded_old_root_pr(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3")
        prs = self.github(source, [self.pr(1), self.pr(2, "baton/example--core"), self.pr(3, "baton/example--other")])
        self.assertNotIn("previous_names", self.project())
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "closed"], [1, 2])

    def test_truncated_first_member_history_cannot_drop_old_root_scope(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.cli("branch", "init", "alpha", "core", cwd=self.slots / "p3")
        prs = self.github(source, [self.pr(1)])
        tree = self.git(self.projects, "rev-parse", "refs/heads/baton/example^{tree}")
        anchor = self.git(self.projects, "commit-tree", tree, "-m", "Truncated isolated first-member history")
        self.git(self.projects, "update-ref", "refs/heads/baton/example", anchor)
        result = self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertIn("Required original first-member manifests are missing", result.stderr)
        self.assertNotIn("pending_abandon", self.project())
        self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")
        self.assertTrue(self.checkout("alpha", "example--core").exists())

    def test_renewed_approval_preserves_closed_pr_identity_on_unavailable_or_changed_head(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        failure = self.root / "fail-close-2"
        failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertEqual({pr["number"] for pr in self.project()["pending_abandon"]["approved"]}, {1, 2})
        original = prs.read_text()
        state = fixtures.state_file(self.projects).read_bytes()
        for head_repo, branch in ((None, "baton/example"), ({"full_name": "foreign/alpha"}, "baton/example"),
                                  ({"full_name": "test/alpha"}, "not-a-project-branch")):
            records = json.loads(original)
            records[0]["state"] = "open"
            records[0]["head"].update(repo=head_repo, ref=branch)
            prs.write_text(json.dumps(records))
            result = self.interactive("abandon", reply=b"y\n", ok=False)
            self.assertIn("confirmed pull request", result.stderr)
            self.assertTrue(self.checkout("alpha").exists())
            self.assertEqual(fixtures.state_file(self.projects).read_bytes(), state)
            self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")
        prs.write_text(original)
        failure.unlink()
        self.interactive("abandon", reply=b"y\n")

    def test_renewed_approval_retains_only_latest_head_and_reopen_needs_fresh_consent(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        failure = self.root / "fail-close-2"
        failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        self.interactive("abandon", reply=b"y\n", ok=False)
        records = json.loads(prs.read_text())
        records[0]["state"] = "open"
        records[0]["head"]["sha"] = "b" * 40
        prs.write_text(json.dumps(records))
        self.interactive("abandon", reply=b"y\n", ok=False)
        approved = [pr for pr in self.project()["pending_abandon"]["approved"] if pr["number"] == 1]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["head_sha"], "b" * 40)
        records = json.loads(prs.read_text())
        records[0]["state"] = "open"
        records[0]["head"]["sha"] = "a" * 40
        prs.write_text(json.dumps(records))
        before = prs.read_bytes(), fixtures.state_file(self.projects).read_bytes()
        result = self.interactive("abandon", reply=b"n\n", ok=False)
        self.assertIn("Abandon this project?", result.stderr)
        self.assertEqual((prs.read_bytes(), fixtures.state_file(self.projects).read_bytes()), before)
        failure.unlink()
        self.interactive("abandon", reply=b"y\n")
        self.assertTrue(all(pr["state"] == "closed" for pr in json.loads(prs.read_text())))

    def test_attempted_then_externally_closed_pr_is_remembered_across_renewal(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1), self.pr(2)])
        first_failure = self.root / "fail-close-1"
        first_failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        first_failure.unlink()
        records = json.loads(prs.read_text())
        records[0]["state"] = "closed"
        prs.write_text(json.dumps(records))
        second_failure = self.root / "fail-close-2"
        second_failure.touch()
        self.interactive("abandon", reply=b"y\n", ok=False)
        records = json.loads(prs.read_text())
        records[0]["state"], records[1]["state"] = "open", "closed"
        prs.write_text(json.dumps(records))
        self.interactive("abandon", reply=b"n\n", ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(json.loads(prs.read_text())[0]["state"], "open")
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_unexpected_base_repository_identity_blocks_before_confirmation(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        prs = self.github(source, [self.pr(1)])
        unexpected = self.pr(1)
        unexpected["base"]["repo"]["full_name"] = "different/canonical-repository"
        (self.root / "raw-api-response.json").write_text(json.dumps([unexpected]))
        before = fixtures.state_file(self.projects).read_bytes(), prs.read_bytes()
        result = self.interactive("abandon", reply=b"y\n", ok=False)
        self.assertIn("base repository different/canonical-repository differs from requested test/alpha", result.stderr)
        self.assertNotIn("Abandon this project?", result.stderr)
        self.assertEqual((fixtures.state_file(self.projects).read_bytes(), prs.read_bytes()), before)
        self.assertTrue(self.checkout("alpha").exists())

    def test_inherited_foreign_manifest_deletion_does_not_block_rename_history(self) -> None:
        source = self.make_repo("alpha")
        coordinator = self.slots / "p3"
        foreign = {"uuid": "f" * 32, "name": "foreign", "repos": [
            {"id": "alpha", "source": str(source), "common_dir": str(source / ".git"), "branch": "baton/foreign"}]}
        (coordinator / "project.json").write_text(json.dumps(foreign) + "\n")
        self.git(coordinator, "add", "project.json")
        self.git(coordinator, "commit", "-m", "Record inherited foreign project manifest")
        self.git(coordinator, "rm", "project.json")
        self.git(coordinator, "commit", "-m", "Remove inherited foreign project manifest")
        self.create("alpha")
        self.cli("rename", "renamed", "example")
        prs = self.github(source, [self.pr(1), self.pr(2, "baton/renamed"), self.pr(3, "baton/foreign")])
        self.interactive("abandon", reply=b"y\n")
        self.assertEqual([pr["number"] for pr in json.loads(prs.read_text()) if pr["state"] == "closed"], [1, 2])
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})
