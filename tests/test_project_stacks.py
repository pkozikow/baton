import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import unittest
from typing import Any

import test_project_manager as fixtures


HEAD_FILTER = (
    "import json, sys\n"
    "data, args = json.load(open(sys.argv[1])), sys.argv[2:]\n"
    "head = args[args.index('--head') + 1] if '--head' in args else None\n"
    "print(json.dumps([pr for pr in data if head is None or pr.get('headRefName') == head]))\n"
)


class ProjectStacksTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    replace_interactively = fixtures.ProjectManagerTest.replace_interactively
    update_state = fixtures.ProjectManagerTest.update_state

    def native(self, checkout: Path, *args: str, ok: bool = True) -> Any:
        result = self.run_command(["gh", "stack", *args], checkout, ok=ok)
        return json.loads(result.stdout) if args == ("view", "--json") and ok else result

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.slots / "p3" / "project.json").read_text())

    def layer(self, name: str, repo: str = "alpha") -> Path:
        return self.checkout(repo, f"example--{name}")

    def init_stack(self, *members: str, repo: str = "alpha") -> None:
        root = Path(self.cli("path", "p3", repo).stdout.strip())
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        entry = next(item for item in project["repos"] if item["id"] == repo)
        if not self.git(root, "diff", entry["base_ref"], "HEAD", "--stat"):
            self.commit_file(root, "fixture-root.txt", "First PR work\n")
        self.native(root, "init", "--base", entry["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/"), entry["branch"])
        entry["stack_root"] = entry["branch"]
        entry["stacks"] = [{"root": entry["branch"]}]
        state_path = fixtures.state_file(self.projects)
        state_path.write_text(json.dumps(project))
        for name in members:
            self.add_stack_member(repo, name, cwd=self.slots / "p3")
            self.commit_file(self.layer(name, entry["id"]), f"fixture-{name}.txt", f"{name} PR work\n")

    def add_stack_member(self, repo: str, name: str, *, cwd: Path | None = None, ok: bool = True) -> Any:
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        pending = project.get("pending_member")
        registered = next(entry for entry in project["repos"] if entry["id"] == repo)
        parent = pending["after"] if pending else [registered, *registered.get("members", [])][-1]["branch"]
        return self.cli("branch", "add", repo, name, "--after", parent, cwd=cwd or self.slots / "p3", ok=ok)

    def integrate_stack(self, source: Path, top: str) -> None:
        self.git(source, "merge", f"baton/example--{top}")
        self.git(source, "push")

    def commit_file(self, checkout: Path, filename: str, content: str) -> str:
        (checkout / filename).write_text(content)
        self.git(checkout, "add", filename)
        self.git(checkout, "commit", "-m", f"Add {filename}")
        return self.git(checkout, "rev-parse", "HEAD")

    def fake_pull_requests(self, responses: list[dict[str, Any]], filter_head: bool = True) -> Path:
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        self.assertIsNotNone(actual_gh)
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        payload = self.root / "pull-requests.json"
        payload.write_text(json.dumps(responses))
        marker = self.root / "github-invocations"
        executable = bindir / "gh"
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "stack" ]; then\n'
            f"  exec {shlex.quote(str(actual_gh))} \"$@\"\n"
            "fi\n"
            f"printf '%s\\n' \"$@\" >> {shlex.quote(str(marker))}\n"
            # Like GitHub, --head narrows results to that branch name unless a test needs raw payloads.
            + (f"exec python3 -c {shlex.quote(HEAD_FILTER)} {shlex.quote(str(payload))} \"$@\"\n" if filter_head
               else f"cat {shlex.quote(str(payload))}\n")
        )
        executable.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        return marker

    def squash_stack(self) -> tuple[Path, list[dict[str, Any]]]:
        source = self.make_repo("alpha")
        url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", url)
        self.git(source, "remote", "set-url", "origin", url)
        self.create("alpha")
        self.commit_file(self.checkout("alpha"), "root.txt", "Root change\n")
        self.init_stack("api")
        api_tip = self.commit_file(self.layer("api"), "api.txt", "API change\n")
        self.git(self.checkout("alpha"), "merge", "--squash", "baton/example--api")
        self.git(self.checkout("alpha"), "commit", "-m", "Squash API into root")
        root_tip = self.git(self.checkout("alpha"), "rev-parse", "HEAD")
        self.git(source, "merge", "--squash", "baton/example")
        self.git(source, "commit", "-m", "Squash root into main")
        self.git(source, "push")
        main_tip = self.git(source, "rev-parse", "HEAD")
        return source, [
            {"state": "MERGED", "headRefName": "baton/example--api", "headRefOid": api_tip,
             "baseRefName": "baton/example", "mergeCommit": {"oid": root_tip}},
            {"state": "MERGED", "headRefName": "baton/example", "headRefOid": root_tip,
             "baseRefName": "main", "mergeCommit": {"oid": main_tip}},
        ]

    def test_stack_is_opt_in_and_each_layer_has_a_worktree(self) -> None:
        self.make_repo("alpha")
        self.make_repo("Beta", default="master")
        self.create("alpha", "Beta")
        self.init_stack("api", "ui")
        alpha, beta = self.manifest()["repos"]
        self.assertTrue(alpha["stacks"])
        self.assertFalse(beta.get("stacks"))
        self.assertEqual([item["member"] for item in alpha["members"]], ["api", "ui"])
        for name, checkout in (("example", self.checkout("alpha")),
                               ("example--api", self.layer("api")),
                               ("example--ui", self.layer("ui"))):
            self.assertEqual(self.git(checkout, "branch", "--show-current"), f"baton/{name}")
            self.assertIn("example", self.cli("status", cwd=checkout).stdout)
            stack = self.native(checkout, "view", "--json")
            self.assertEqual([item["name"] for item in stack["branches"]],
                             ["baton/example", "baton/example--api", "baton/example--ui"])
        self.assertEqual(self.cli("path", "p3", "alpha", "--member", "api").stdout.strip(), str(self.layer("api")))
        self.assertEqual(self.cli("path", "alpha", "--member", "ui", cwd=self.layer("api")).stdout.strip(), str(self.layer("ui")))
        self.assertEqual(self.cli("path", "p3", "Beta").stdout.strip(), str(self.checkout("Beta")))
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_native_cascading_rebase_uses_owning_worktrees(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", "ui")
        self.commit_file(self.layer("api"), "api.txt", "API\n")
        self.commit_file(self.layer("ui"), "ui.txt", "UI\n")
        root_tip = self.commit_file(self.checkout("alpha"), "root.txt", "Root\n")
        self.native(self.layer("ui"), "rebase", "--no-trunk")
        for name in ("api", "ui"):
            self.git(self.layer(name), "merge-base", "--is-ancestor", root_tip, "HEAD")
            self.assertEqual(self.git(self.layer(name), "branch", "--show-current"), f"baton/example--{name}")
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")

    def test_status_reports_children_behind_their_predecessor(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", "ui")
        data = json.loads(self.cli("status", "--json", "p3").stdout)
        self.assertEqual([entry["needs_rebase"] for entry in data["entries"]], [False, False, False])
        self.commit_file(self.layer("api"), "api-v2.txt", "Newer API\n")
        data = json.loads(self.cli("status", "--json", cwd=self.layer("ui")).stdout)
        self.assertEqual([(entry["member"], entry["needs_rebase"]) for entry in data["entries"]],
                         [(None, False), ("api", False), ("ui", True)])
        self.assertTrue(data["current_entry"]["needs_rebase"])
        self.assertIn("needs rebase onto baton/example--api", self.cli("status", "p3").stdout)
        self.native(self.layer("ui"), "rebase", "--no-trunk")
        data = json.loads(self.cli("status", "--json", "p3").stdout)
        self.assertEqual([entry["needs_rebase"] for entry in data["entries"]], [False, False, False])
        self.git(self.layer("api"), "update-ref", "-d", "refs/heads/baton/example--api")
        data = json.loads(self.cli("status", "--json", "p3").stdout)
        ui = data["entries"][2]
        self.assertEqual((ui["needs_rebase"], ui["base_reliable"]), (None, False))
        self.assertIn("cannot compare baton/example--ui", ui["base_error"])

    def test_new_layer_starts_at_latest_top_layer_tip(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        tip = self.commit_file(self.layer("api"), "latest.txt", "Current top-layer work\n")
        self.add_stack_member( "alpha", "ui", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.layer("ui"), "rev-parse", "HEAD"), tip)
        self.assertEqual((self.layer("ui") / "latest.txt").read_text(), "Current top-layer work\n")
        self.assertEqual(self.git(self.layer("api"), "branch", "--show-current"), "baton/example--api")

    def test_native_add_resumes_after_gh_fails_after_creating_branch(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        bindir = self.root / "interrupt-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(str(actual_gh))} \"$@\" || exit $?\n"
            'if [ "$1" = "stack" ] && [ "$2" = "add" ]; then exit 1; fi\n'
        )
        wrapper.chmod(0o755)
        original_path = self.env["PATH"]
        self.env["PATH"] = str(bindir) + os.pathsep + original_path
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example--api")
        tip = self.git(source, "rev-parse", "baton/example--api")
        self.env["PATH"] = original_path
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")
        self.assertEqual(self.git(self.layer("api"), "rev-parse", "HEAD"), tip)
        self.assertEqual(len(self.manifest()["repos"][0]["members"]), 1)
        state = fixtures.read_state(self.projects)
        self.assertEqual(state["projects"]["p3"]["state"], "active")
        self.assertNotIn("pending_member", state["projects"]["p3"])

    def test_stack_add_resumes_after_manifest_commit_failure(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3", ok=False)
        self.assertTrue(self.layer("api").exists())
        tip = self.git(self.layer("api"), "rev-parse", "HEAD")
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.layer("api"), "rev-parse", "HEAD"), tip)
        self.assertEqual(len(self.manifest()["repos"][0]["members"]), 1)
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_stack_add_resumes_after_checkout_before_catalog_save(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            'if [ "$(git branch --show-current)" = "baton/example--api" ]; then exit 1; fi\n'
        )
        hook.chmod(0o755)
        self.env["GIT_CONFIG_VALUE_3"] = str(hooks)
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3", ok=False)
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example--api")
        self.env["GIT_CONFIG_VALUE_3"] = "/dev/null"
        self.add_stack_member( "alpha", "api", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.checkout("alpha"), "branch", "--show-current"), "baton/example")
        self.assertEqual(self.git(self.layer("api"), "branch", "--show-current"), "baton/example--api")

    def test_cleanup_refuses_owned_branch_used_as_another_native_trunk(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        self.native(source, "init", "--base", "baton/example--api", "foreign-feature")
        catalog = source / ".git" / "gh-stack"
        before = catalog.read_bytes()
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("another native stack", result.stderr.lower())
        self.assertEqual(catalog.read_bytes(), before)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertTrue(self.layer("api").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/foreign-feature")

    def test_cleanup_rejects_malformed_catalog_schema_and_trunk(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        catalog = source / ".git" / "gh-stack"
        original = json.loads(catalog.read_text())
        # The parser's full rejection matrix lives in NativeCatalogTest; retain the cleanup safety boundary.
        catalog.write_text(json.dumps({**original, "stacks": [{**original["stacks"][0], "trunk": None}]}))
        before_catalog = catalog.read_bytes()
        state = fixtures.state_file(self.projects)
        before_state = state.read_bytes()
        branches = ("baton/example", "baton/example--api")
        tips = {branch: self.git(source, "rev-parse", branch) for branch in branches}
        worktrees = self.git(source, "worktree", "list", "--porcelain")
        coordinator_tip = self.git(self.slots / "p3", "rev-parse", "HEAD")
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("Invalid native stack branches", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(state.read_bytes(), before_state)
        self.assertEqual({branch: self.git(source, "rev-parse", branch) for branch in branches}, tips)
        self.assertEqual(self.git(source, "worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.git(self.slots / "p3", "rev-parse", "HEAD"), coordinator_tip)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertTrue(self.layer("api").exists())

    def test_cleanup_dry_run_does_not_write_native_catalog_or_project_state(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        self.integrate_stack(source, "api")
        catalog = source / ".git" / "gh-stack"
        state = fixtures.state_file(self.projects)
        before_catalog, before_state = catalog.read_bytes(), state.read_bytes()
        registry = state.parents[2]
        before_registry = {str(path.relative_to(registry)): path.read_bytes()
                           for path in registry.rglob("*") if path.is_file()}
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES", None)
        self.cli("cleanup", "p3", "--dry-run")
        self.assertEqual({str(path.relative_to(registry)): path.read_bytes()
                          for path in registry.rglob("*") if path.is_file()}, before_registry)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(state.read_bytes(), before_state)
        self.assertTrue(self.layer("api").exists())
        self.assertTrue(self.checkout("alpha").exists())

    def test_native_member_can_be_registered_without_creating_another_branch(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        self.native(self.checkout("alpha"), "add", "baton/example--api")
        tip = self.commit_file(self.checkout("alpha"), "api.txt", "Existing native branch\n")
        self.git(self.checkout("alpha"), "switch", "baton/example")
        self.cli("branch", "register", "alpha", "api", "--after", "baton/example", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.layer("api"), "rev-parse", "HEAD"), tip)
        self.assertEqual(self.manifest()["repos"][0]["members"][0]["branch"], "baton/example--api")

    def test_status_json_reports_stack_bases_and_membership_drift(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        data = json.loads(self.cli("status", "--json", cwd=self.layer("api")).stdout)
        root, api = data["entries"]
        self.assertEqual((root["workflow"], root["stack_root"], root["pr_base"], root["base_ref"]),
                         ("native_stack", "baton/example", "main", "refs/remotes/origin/main"))
        self.assertEqual((api["member"], api["pr_base"], api["base_ref"], api["base_reliable"]),
                         ("api", "baton/example", "refs/heads/baton/example", True))
        self.assertEqual(data["current_entry"], api)
        self.native(self.layer("api"), "add", "baton/example--drift")
        result = self.cli("status", "--json", "p3")
        data = json.loads(result.stdout)
        self.assertTrue(all(not entry["base_reliable"] for entry in data["entries"]))
        self.assertIn("Unregistered: baton/example--drift", data["errors"][0])
        self.assertIn("baton branch register", data["errors"][0])
        self.update_state(lambda project: project.update(state="adding", pending_add=["Beta"]))
        data = json.loads(self.cli("status", "--json", "p3").stdout)
        self.assertIn(f"Repeat the outstanding command to finish it: cd {shlex.quote(str(self.slots / 'p3'))} "
                      "&& baton add Beta", data["errors"][0])
        self.assertNotIn("branch register", data["errors"][0])

    def test_cleanup_blocks_native_stack_created_without_project_registration(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.native(self.checkout("alpha"), "init", "--base", "main", "baton/example")
        catalog = source / ".git" / "gh-stack"
        before = catalog.read_bytes()
        result = self.replace_interactively(b"y\n", cleanup=True, ok=False)
        self.assertNotIn("[y/N]", result.stderr)
        self.cli("rename", "renamed", "p3", ok=False)
        self.assertEqual(catalog.read_bytes(), before)
        self.assertTrue(self.checkout("alpha").exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example")
        self.assertFalse(self.manifest()["repos"][0].get("stacks"))


    def test_registration_rejects_branch_not_in_native_stack(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        self.git(source, "branch", "baton/example--unmanaged")
        self.cli("branch", "register", "alpha", "unmanaged", "--after", "baton/example", cwd=self.slots / "p3", ok=False)
        self.assertFalse(self.layer("unmanaged").exists())
        self.assertEqual(self.manifest()["repos"][0].get("members", []), [])

    def test_register_adopts_only_an_existing_worktree_at_exact_managed_path(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        self.native(self.checkout("alpha"), "add", "baton/example--api")
        self.git(self.checkout("alpha"), "switch", "baton/example")
        unrelated = self.root / "foreign-api"
        self.git(source, "worktree", "add", str(unrelated), "baton/example--api")
        self.cli("branch", "register", "alpha", "api", "--after", "baton/example", cwd=self.slots / "p3", ok=False)
        self.assertTrue(unrelated.exists())
        self.assertFalse(self.layer("api").exists())
        self.git(source, "worktree", "move", str(unrelated), str(self.layer("api")))
        self.cli("branch", "register", "alpha", "api", "--after", "baton/example", cwd=self.slots / "p3")
        self.assertEqual(self.git(self.layer("api"), "branch", "--show-current"), "baton/example--api")
        self.assertEqual(len(self.manifest()["repos"][0]["members"]), 1)

    def test_branch_and_path_collisions_do_not_mutate_stack(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        self.git(source, "branch", "baton/example--branch-conflict")
        path = self.layer("path-conflict")
        path.mkdir()
        (path / "keep.txt").write_text("Unrelated data\n")
        before = (self.slots / "p3" / "project.json").read_bytes()
        for name in ("branch-conflict", "path-conflict", "../outside", "api/ui"):
            self.add_stack_member( "alpha", name, cwd=self.slots / "p3", ok=False)
            self.assertEqual((self.slots / "p3" / "project.json").read_bytes(), before)
            self.assertEqual([item["name"] for item in self.native(self.checkout("alpha"), "view", "--json")["branches"]], ["baton/example"])
        self.assertEqual((path / "keep.txt").read_text(), "Unrelated data\n")

    def test_stack_commands_resolve_repository_name(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", repo="alpha")
        self.assertEqual(self.cli("path", "p3", "alpha", "--member", "api").stdout.strip(), str(self.layer("api", "alpha")))
        result = self.cli("stack", "status", "alpha", cwd=self.slots / "p3")
        self.assertIn("baton/example--api", result.stdout)

    def test_cleanup_removes_registered_layers_only_and_fetches_once(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", "ui")
        self.integrate_stack(source, "ui")
        self.git(source, "branch", "baton/example--unmanaged")
        unrelated = self.layer("unmanaged")
        self.git(source, "worktree", "add", str(unrelated), "baton/example--unmanaged")
        trace = self.root / "git-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.cli("cleanup", "p3")
        self.assertEqual(trace.read_text().count("git fetch origin refs/heads/main:refs/remotes/origin/main"), 1)
        for name in ("example", "example--api", "example--ui"):
            self.assertFalse(self.checkout("alpha", name).exists())
            self.git(source, "show-ref", "--verify", f"refs/heads/baton/{name}", ok=False)
        self.assertTrue(unrelated.exists())
        self.git(source, "show-ref", "--verify", "refs/heads/baton/example--unmanaged")

    def test_cleanup_refuses_unregistered_native_members(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack()
        self.native(self.checkout("alpha"), "add", "baton/example--api")
        self.git(self.checkout("alpha"), "switch", "baton/example")
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("register", result.stderr.lower())
        self.assertTrue(self.checkout("alpha").exists())

    def test_layer_dirtiness_requires_confirmation_and_preserves_all_tips(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", "ui")
        tips = [self.commit_file(self.layer(name), f"{name}.txt", f"{name}\n") for name in ("api", "ui")]
        notes = self.layer("ui") / "notes.txt"
        notes.write_text("Untracked work\n")
        self.replace_interactively(b"n\n", cleanup=True, ok=False)
        self.assertEqual(notes.read_text(), "Untracked work\n")
        self.assertTrue(self.layer("api").exists())
        result = self.replace_interactively(b"y\n", cleanup=True)
        self.assertIn("notes.txt", result.stderr)
        archived = self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/")
        for tip in tips:
            self.assertIn(tip, archived)
        self.assertFalse(self.checkout("alpha").exists())
        self.assertFalse(self.layer("api").exists())
        self.assertFalse(self.layer("ui").exists())

    def test_cleanup_recognizes_squash_merge_chain_to_default(self) -> None:
        source, responses = self.squash_stack()
        marker = self.fake_pull_requests(responses)
        self.cli("cleanup", "p3")
        self.assertFalse(self.layer("api").exists())
        self.assertFalse(self.checkout("alpha").exists())
        calls = marker.read_text().splitlines()
        self.assertEqual(calls.count("list"), 2)
        self.assertEqual([calls[i + 1] for i, arg in enumerate(calls) if arg == "--head"],
                         ["baton/example", "baton/example--api"])
        self.assertNotIn("1000", calls)
        archived = self.git(source, "for-each-ref", "--format=%(objectname)", "refs/project-archive/")
        for response in responses:
            self.assertIn(response["headRefOid"], archived)

    def test_merge_chain_does_not_cover_new_local_layer_commits(self) -> None:
        _, responses = self.squash_stack()
        self.fake_pull_requests(responses)
        tip = self.commit_file(self.layer("api"), "follow-up.txt", "Not merged\n")
        self.cli("cleanup", "p3", ok=False)
        self.assertEqual(self.git(self.layer("api"), "rev-parse", "HEAD"), tip)
        self.assertTrue(self.checkout("alpha").exists())

    def test_child_pr_requires_merged_state_and_integrated_merge_commit(self) -> None:
        source, responses = self.squash_stack()
        self.fake_pull_requests([{**responses[0], "mergeCommit": {"oid": responses[0]["headRefOid"]}},
                                 responses[1]])
        state = fixtures.state_file(self.projects)
        manifest = self.slots / "p3" / "project.json"
        catalog = source / ".git" / "gh-stack"
        before = (state.read_bytes(), manifest.read_bytes(), catalog.read_bytes(),
                  self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                  self.git(source, "worktree", "list", "--porcelain"))
        result = self.cli("cleanup", "p3", ok=False)
        self.assertIn("baton/example--api", result.stderr)
        self.assertTrue(self.layer("api").is_dir())
        self.assertTrue(self.checkout("alpha").is_dir())
        self.assertEqual((state.read_bytes(), manifest.read_bytes(), catalog.read_bytes(),
                          self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
                          self.git(source, "worktree", "list", "--porcelain")), before)

    def test_cleanup_resumes_after_locked_layer_without_losing_other_tips(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api", "ui")
        self.integrate_stack(source, "ui")
        self.git(source, "worktree", "lock", str(self.layer("api")))
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.layer("api").exists())
        self.git(source, "worktree", "unlock", str(self.layer("api")))
        self.cli("cleanup", "p3")
        for name in ("example", "example--api", "example--ui"):
            self.assertFalse(self.checkout("alpha", name).exists())

    def test_cleanup_resumes_after_native_unstack_before_journal_save(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        self.integrate_stack(source, "api")
        actual_gh = shutil.which("gh", path=self.env["PATH"])
        bindir = self.root / "interrupt-bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(str(actual_gh))} \"$@\" || exit $?\n"
            'if [ "$1" = "stack" ] && [ "$2" = "unstack" ]; then exit 1; fi\n'
        )
        wrapper.chmod(0o755)
        original_path = self.env["PATH"]
        self.env["PATH"] = str(bindir) + os.pathsep + original_path
        self.cli("cleanup", "p3", ok=False)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertTrue(self.layer("api").exists())
        self.env["PATH"] = original_path
        self.cli("cleanup", "p3")
        self.assertFalse(self.checkout("alpha").exists())
        self.assertFalse(self.layer("api").exists())

    def test_paused_native_rebase_blocks_cleanup_until_aborted(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.commit_file(self.checkout("alpha"), "shared.txt", "Initial\n")
        self.init_stack("api")
        self.commit_file(self.layer("api"), "shared.txt", "API edit\n")
        self.commit_file(self.checkout("alpha"), "shared.txt", "Root edit\n")
        self.native(self.layer("api"), "rebase", "--no-trunk", ok=False)
        result = self.cli("cleanup", "p3", "--allow-unmerged", "alpha", ok=False)
        self.assertIn("rebase", result.stderr.lower())
        self.assertTrue(self.checkout("alpha").exists())
        self.assertTrue(self.layer("api").exists())
        self.native(self.layer("api"), "rebase", "--abort")
        self.cli("cleanup", "p3", "--allow-unmerged", "alpha")

    def test_live_native_operation_lock_blocks_cleanup_without_override(self) -> None:
        source = self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        self.integrate_stack(source, "api")
        catalog = source / ".git" / "gh-stack"
        before = catalog.read_bytes()
        with (source / ".git" / "gh-stack-operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.replace_interactively(b"y\n", cleanup=True, ok=False)
            self.assertNotIn("[y/N]", result.stderr)
            self.assertEqual(catalog.read_bytes(), before)
            self.assertTrue(self.checkout("alpha").exists())
            self.assertTrue(self.layer("api").exists())
        self.cli("cleanup", "p3")

    def test_layer_branch_identity_cannot_be_overridden(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        self.git(self.layer("api"), "switch", "-c", "unrelated")
        result = self.replace_interactively(b"y\n", cleanup=True, ok=False)
        self.assertNotIn("[y/N]", result.stderr)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertEqual(self.git(self.layer("api"), "branch", "--show-current"), "unrelated")

    def test_stack_rename_is_rejected_without_mutating_native_metadata(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        self.init_stack("api")
        before = self.native(self.layer("api"), "view", "--json")
        self.cli("rename", "renamed", "p3", ok=False)
        self.assertEqual(self.native(self.layer("api"), "view", "--json"), before)
        self.assertTrue(self.checkout("alpha").exists())
        self.assertTrue(self.layer("api").exists())


if __name__ == "__main__":
    unittest.main()
