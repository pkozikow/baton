import json
from pathlib import Path
import shlex
import unittest

from test_project_manager import artifact_path

import test_project_branches as fixtures


class ProjectHandoffTest(unittest.TestCase):
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

    def handoff(self, *args: str, ok: bool = True) -> str:
        return self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                        *args, ok=ok).stdout

    def test_handoff_supplies_readable_workflow_for_workers_outside_slots(self) -> None:
        first = self.init_members()
        output = self.handoff("--member", "api")
        from baton.skills_install import bundled_skills
        workflow = bundled_skills() / "baton-workflow/SKILL.md"
        self.assertTrue(workflow.is_file())
        self.assertIn(str(workflow), output)
        for boundary in ("branch/mutating-worker required reading", "Never claim or replace",
                         "Never switch or detach", "Await every launched process",
                         "Only the parent changes topology"):
            self.assertIn(boundary, output)
        output_from_checkout = self.cli("handoff", "--agent", "worker", "--repo", "alpha",
                                        "--member", "api", cwd=first).stdout
        self.assertIn(str(workflow), output_from_checkout)

    def test_handoff_without_board_reports_initialization_guidance(self) -> None:
        self.init_members()
        state_path = fixtures.fixtures.state_file(self.projects)
        state = json.loads(state_path.read_text())
        state.pop("blackboard")
        state_path.write_text(json.dumps(state))
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha", "--member", "api", ok=False)
        self.assertIn("Initialize the baton blackboard", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_handoff_with_uninitialized_board_does_not_create_state(self) -> None:
        self.init_members()
        state_path = fixtures.fixtures.state_file(self.projects)
        state = json.loads(state_path.read_text())
        directory = Path(state["blackboard"]["directory"])
        state["blackboard"]["initialized"] = False
        state_path.write_text(json.dumps(state))
        for path in directory.iterdir():
            if path.name.startswith("blackboard"):
                path.unlink()
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        self.handoff("--member", "api")
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()})

    def test_task_guidance_is_read_only_and_uses_actual_schema(self) -> None:
        self.init_members()
        data = json.loads(self.cli("status", "example", "--json").stdout)
        directory = Path(data["blackboard"]["path"]).parent
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        output = self.handoff("--member", "api")
        for value in ("task list", "task show", "Only done", "never ownership", data["uuid"]):
            self.assertIn(value, output)
        after = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        self.assertEqual(before, after)

    def test_task_handoff_commands_drive_claim_ask_resume_and_settle(self) -> None:
        self.init_members()
        generation = json.loads(self.cli("status", "example", "--json").stdout)["blackboard"]["generation"]
        board = ("--selector", "example", "--expected-generation", generation)
        task = json.loads(self.cli("task", "create", "--title", "Build", *board, "--agent", "pm").stdout)
        self.assertNotIn("task settle", self.handoff("--member", "api"))
        output = self.handoff("--member", "api", "--task", task["id"])
        commands = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in output.splitlines()
                    if line.startswith(("Claim first", "Ask blocking", "Resume after", "Post task progress", "Settle once"))}
        self.assertIn("parent: pm", output)
        self.assertIn("never re-claim", output)
        claimed = json.loads(self.run_command(shlex.split(commands["Claim first"]), self.root).stdout)
        self.assertEqual((claimed["state"], claimed["assignee"]), ("in_progress", "worker"))
        ask = [value if value != "QUESTION" else "Which API?" for value in shlex.split(commands["Ask blocking questions"])]
        asked = json.loads(self.run_command([*ask, "--timeout", "0"], self.root).stdout)
        self.assertEqual(asked["entries"], [])
        self.cli("blackboard", "post", *board, "--agent", "pm", "--to", "worker", "--kind", "answer",
                 "--reply-to", asked["question"]["id"], "--text", "REST")
        resume = [asked["question"]["id"] if value == "QUESTION_ID" else value for value in shlex.split(commands["Resume after an ask timeout"])]
        self.assertEqual(json.loads(self.run_command([*resume, "--timeout", "0"], self.root).stdout)["entries"][0]["body"], "REST")
        progress = [value if value != "NOTE" else "Parser done" for value in shlex.split(commands["Post task progress"])]
        posted = json.loads(self.run_command(progress, self.root).stdout)
        linked = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)["entries"]
        self.assertEqual([(entry["task_id"], entry["author"]) for entry in linked if entry["id"] == posted["id"]], [(task["id"], "worker")])
        replacements = {"CLAIMED_VERSION": str(claimed["version"]), "succeeded|failed": "succeeded", "SUMMARY": "Built"}
        settle = [replacements.get(value, value) for value in shlex.split(commands["Settle once before returning"].removesuffix(" [--files PATH ...] [--report PATH]"))]
        settled = json.loads(self.run_command(settle, self.root).stdout)
        self.assertEqual((settled["state"], settled["settlement"]["agent"]), ("done", "worker"))
        self.assertEqual(json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)["entries"][-1]["recipient"], "pm")
        for args in (("--task", task["id"]), ("--task", "missing")):
            with self.subTest(args=args):
                self.assertIn("baton:", self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                                                 "--member", "api", *args, ok=False).stderr)
        other = json.loads(self.cli("task", "create", "--title", "Other", "--assignee", "other", *board, "--agent", "pm").stdout)
        blocked = json.loads(self.cli("task", "create", "--title", "Blocked", *board, "--agent", "pm").stdout)
        pending = json.loads(self.cli("task", "create", "--title", "Pending", *board, "--agent", "pm").stdout)
        self.cli("task", "dependency", "add", "--id", blocked["id"], "--prerequisite", pending["id"],
                 "--expected-version", "1", *board, "--agent", "pm")
        for unclaimable in (other, blocked):
            with self.subTest(task=unclaimable["title"]):
                self.assertIn("neither claimable", self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                                                            "--member", "api", "--task", unclaimable["id"], ok=False).stderr)

    def line(self, output: str, label: str) -> str:
        return next(line.removeprefix(f"{label}: ") for line in output.splitlines() if line.startswith(f"{label}: "))

    def refresh_command(self, output: str) -> list[str]:
        return shlex.split(self.line(output, "Refresh instructions"))

    def board(self) -> tuple[str, ...]:
        generation = json.loads(self.cli("status", "example", "--json").stdout)["blackboard"]["generation"]
        return ("--selector", "example", "--expected-generation", generation)

    def test_refresh_command_is_exact_and_reproduces_the_handoff(self) -> None:
        self.init_members()
        data = json.loads(self.cli("status", "example", "--json").stdout)
        task = json.loads(self.cli("task", "create", "--title", "Build", *self.board(), "--agent", "pm").stdout)
        for args, selection in ((("--member", "baton/example--api"), ["--member", "api"]),
                                (("--member", "api", "--task", task["id"]), ["--member", "api", "--task", task["id"]]),
                                (("--planned-member", "tests", "--after", "baton/example--api"),
                                 ["--planned-member", "tests", "--after", "api"])):
            with self.subTest(args=args):
                output = self.handoff(*args)
                refresh = self.refresh_command(output)
                self.assertEqual(refresh, [data["helper_path"], "handoff", "--selector", data["uuid"], "--agent", "worker",
                                           "--repo", "alpha", *selection])
                self.assertIn("never reconstruct commands from memory", output)
                self.assertIn("wait for or ask the parent for a fresh handoff", output)
                self.assertEqual(self.run_command(refresh, self.root).stdout, output)
        claimable = self.handoff("--member", "api", "--task", task["id"])
        self.assertIn(f"Claim first: {data['helper_path']} task claim --id {task['id']} --expected-version 1", claimable)
        self.assertIn("CLAIMED_VERSION", self.line(claimable, "Settle once before returning"))

    def test_unnamed_member_refresh_names_its_branch(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        output = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha").stdout
        refresh = self.refresh_command(output)
        self.assertEqual(refresh[-2:], ["--member", "baton/example"])
        self.assertEqual(self.run_command(refresh, self.root).stdout, output)

    def test_own_in_progress_task_handoff_recovers_history_and_settles_with_current_version(self) -> None:
        self.init_members()
        board = self.board()
        task = json.loads(self.cli("task", "create", "--title", "Build", *board, "--agent", "pm").stdout)
        output = self.handoff("--member", "api", "--task", task["id"])
        claimed = json.loads(self.run_command(shlex.split(self.line(output, "Claim first")), self.root).stdout)
        note = [value if value != "NOTE" else "Parser done" for value in shlex.split(self.line(output, "Post task progress"))]
        self.run_command(note, self.root)
        ask = [value if value != "QUESTION" else "Which API?" for value in shlex.split(self.line(output, "Ask blocking questions"))]
        question = json.loads(self.run_command([*ask, "--timeout", "0"], self.root).stdout)["question"]
        # A parent update after the claim makes the claim's version stale.
        updated = json.loads(self.cli("task", "update", "--id", task["id"], "--expected-version", str(claimed["version"]),
                                      "--description", "Use REST", *board, "--agent", "pm").stdout)
        self.assertGreater(updated["version"], claimed["version"])
        refreshed = self.run_command(self.refresh_command(output), self.root).stdout
        self.assertNotIn("Claim first", refreshed)
        self.assertNotIn("CLAIMED_VERSION", refreshed)
        self.assertIn(f"version: {updated['version']} | parent: pm\nAlready claimed by this agent: do not claim again.", refreshed)
        history = json.loads(self.run_command(shlex.split(self.line(refreshed, "Recover task history")), self.root).stdout)
        self.assertFalse(history["timed_out"])
        self.assertEqual([(entry["kind"], entry["body"]) for entry in history["entries"]],
                         [("note", "Parser done"), ("question", "Which API?")])
        self.assertEqual(history["entries"][1]["id"], question["id"])
        self.cli("blackboard", "post", *board, "--agent", "pm", "--to", "worker", "--kind", "answer",
                 "--reply-to", question["id"], "--text", "REST")
        resume = [question["id"] if value == "QUESTION_ID" else value
                  for value in shlex.split(self.line(refreshed, "Resume after an ask timeout"))]
        self.assertEqual(json.loads(self.run_command([*resume, "--timeout", "0"], self.root).stdout)["entries"][0]["body"], "REST")
        replacements = {"succeeded|failed": "succeeded", "SUMMARY": "Built"}
        settle = self.line(refreshed, "Settle once before returning").removesuffix(" [--files PATH ...] [--report PATH]")
        settled = json.loads(self.run_command([replacements.get(value, value) for value in shlex.split(settle)], self.root).stdout)
        self.assertEqual((settled["state"], settled["settlement"]["agent"]), ("done", "worker"))
        result = self.run_command(self.refresh_command(output), self.root, ok=False)
        self.assertIn("neither claimable by this agent nor in progress and assigned to it", result.stderr)

    def test_released_task_handoff_is_claimable_again(self) -> None:
        self.init_members()
        board = self.board()
        task = json.loads(self.cli("task", "create", "--title", "Build", *board, "--agent", "pm").stdout)
        self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *board, "--agent", "worker")
        self.cli("task", "release", "--id", task["id"], "--expected-version", "2", *board, "--agent", "worker")
        output = self.handoff("--member", "api", "--task", task["id"])
        self.assertIn(f"--id {task['id']} --expected-version 3", self.line(output, "Claim first"))
        self.assertNotIn("Already claimed", output)

    def test_handoff_refuses_others_in_progress_held_and_settled_tasks(self) -> None:
        self.init_members()
        board = self.board()
        tasks = {title: json.loads(self.cli("task", "create", "--title", title, *board, "--agent", "pm").stdout)
                 for title in ("Other", "Held", "Done")}
        self.cli("task", "claim", "--id", tasks["Other"]["id"], "--expected-version", "1", *board, "--agent", "other")
        for title, outcome in (("Held", "failed"), ("Done", "succeeded")):
            self.cli("task", "claim", "--id", tasks[title]["id"], "--expected-version", "1", *board, "--agent", "worker")
            self.cli("task", "settle", "--id", tasks[title]["id"], "--expected-version", "2", *board, "--agent", "worker",
                     "--outcome", outcome, "--summary", title)
        for title, task in tasks.items():
            with self.subTest(task=title):
                result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                                  "--member", "api", "--task", task["id"], ok=False)
                self.assertIn("neither claimable by this agent nor in progress and assigned to it", result.stderr)

    def test_stale_stack_child_warns_only_for_own_in_progress_task(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Stable dependency\n")
        self.add_member("tests", after="api")
        board = self.board()
        own, bound, elsewhere, other = (
            json.loads(self.cli("task", "create", "--title", title, *board, "--agent", "pm", *scope).stdout)
            for title, scope in (("Own", ()), ("Bound", ("--repo", "alpha", "--member", "tests")),
                                 ("Elsewhere", ("--repo", "alpha", "--member", "api")), ("New", ())))
        for task in (own, bound, elsewhere):
            self.cli("task", "claim", "--id", task["id"], "--expected-version", "1", *board, "--agent", "worker")
        self.commit_file(first, "api.txt", "Dependency changed\n")
        for task in (own, bound):
            with self.subTest(task=task["title"]):
                output = self.handoff("--member", "tests", "--task", task["id"])
                self.assertIn("PR base: baton/example--api\nWarning: baton/example--tests needs a rebase onto "
                              "baton/example--api; the parent rebases it. Do not rebase, merge or sync it yourself", output)
                self.assertIn("Already claimed by this agent", output)
        for args in ((), ("--task", other["id"]), ("--task", elsewhere["id"])):
            with self.subTest(args=args):
                result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                                  "--member", "tests", *args, ok=False)
                self.assertIn("Parent must rebase", result.stderr)

    def test_handoff_and_status_derive_current_paths_without_existing_plans(self) -> None:
        first = self.init_members()
        data = json.loads(self.cli("status", "--json", cwd=first).stdout)
        entry = data["current_entry"]
        self.assertEqual(data["master_path"], str(artifact_path(self.projects, self.artifacts, "example") / "plan.md"))
        self.assertEqual(entry["subplan_path"], str(artifact_path(self.projects, self.artifacts, "example") / "branches" / "alpha" /
                                                  "example--api" / "plan.md"))
        self.assertTrue(Path(data["helper_path"]).is_file())
        self.assertEqual(Path(data["helper_path"]), fixtures.fixtures.SCRIPT.with_name("baton"))
        helper_status = self.run_command([data["helper_path"], "status", "example", "--json"], first)
        self.assertEqual(json.loads(helper_status.stdout)["uuid"], data["uuid"])
        output = self.handoff("--member", "api")
        for value in (data["master_path"], entry["subplan_path"], str(first), data["blackboard"]["generation"],
                      data["helper_path"], "PR base: main", "Workflow: independent", "Parent must supply"):
            self.assertIn(value, output)
        self.assertFalse(Path(entry["subplan_path"]).exists())

    def test_handoff_requires_exact_member_in_repository_with_multiple_entries(self) -> None:
        self.init_members()
        docs = self.add_member("docs")
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha", ok=False)
        self.assertIn("ambiguous", result.stderr)
        output = self.handoff("--member", "baton/example--docs")
        self.assertIn(f"Worktree: {docs}", output)
        self.assertIn("Member: docs", output)
        self.handoff("--member", "missing", ok=False)
        self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "missing", ok=False)

    def test_stack_child_handoff_uses_predecessor_and_refuses_stale_or_unreliable_base(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Stable dependency\n")
        self.add_member("tests", after="api")
        output = self.handoff("--member", "tests")
        self.assertIn("PR base: baton/example--api", output)
        self.assertIn("Base ref: refs/heads/baton/example--api", output)
        self.assertIn("Workflow: native_stack", output)
        self.commit_file(first, "api.txt", "Dependency changed\n")
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--member", "tests", ok=False)
        self.assertIn("Parent must rebase", result.stderr)
        (self.repos / "alpha" / ".git" / "gh-stack-rebase-state").write_text("{}")
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--member", "tests", ok=False)
        self.assertIn("Finish or abort", result.stderr)

    def test_renamed_project_paths_follow_status(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        first = self.checkout("alpha")
        self.cli("rename", "renamed", "example")
        data = json.loads(self.cli("status", "renamed", "--json").stdout)
        self.assertEqual(data["master_path"], str(artifact_path(self.projects, self.artifacts, "renamed") / "plan.md"))
        entry = data["entries"][0]
        self.assertEqual(entry["subplan_path"], str(artifact_path(self.projects, self.artifacts, "renamed") / "branches" / "alpha" /
                                                  "renamed" / "plan.md"))
        self.assertNotEqual(entry["path"], str(first))
        output = self.cli("handoff", "--selector", "renamed", "--agent", "worker", "--repo", "alpha").stdout
        self.assertIn(entry["subplan_path"], output)
        self.assertIn(entry["path"], output)
        self.assertIn("Member: (unnamed; select with --member baton/renamed)", output)
        selected = self.cli("handoff", "--selector", "renamed", "--agent", "worker", "--repo", "alpha",
                            "--member", "baton/renamed").stdout
        self.assertEqual(selected, output)

    def test_planned_child_before_first_commit_is_read_only_and_plan_only(self) -> None:
        first = self.init_members()
        source = self.repos / "alpha"
        self.assertEqual(self.git(first, "rev-parse", "HEAD"), self.git(source, "rev-parse", "main"))
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = self.handoff("--planned-member", "tests", "--after", "api")
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        planned = self.worktrees / "alpha" / "example--tests"
        subplan = artifact_path(self.projects, self.artifacts, "example") / "branches" / "alpha" / "example--tests" / "plan.md"
        for value in ("PLAN ONLY", "plan/refine only", "PLANNED, not registered", "DOES NOT EXIST",
                      "does not establish child-creation readiness or tested dependencies",
                      "refresh the handoff", f"Read-only inspection worktree: {first}",
                      "Member: tests (planned)", "Branch: baton/example--tests (planned)",
                      f"Worktree: {planned} (planned)", f"Subplan: {subplan} (planned)",
                      "PR base: baton/example--api (planned)", "Workflow: native_stack (planned)", f"Read {first}/AGENTS.md"):
            self.assertIn(value, output)
        self.assertNotIn("plan/refine/implement/publish", output)
        self.assertFalse(planned.exists())
        self.assertFalse(subplan.exists())
        self.assertEqual(self.git(source, "show-ref", "--verify", "refs/heads/baton/example--tests", ok=False), "")

    def test_planned_subplan_commit_uses_coordinator_and_preserves_inspection_ownership(self) -> None:
        first = self.init_members()
        self.add_member("docs")
        output = self.handoff("--planned-member", "tests", "--after", "api")
        child_plan = artifact_path(self.projects, self.artifacts, "example") / "branches" / "alpha" / "example--tests" / "plan.md"
        sibling_plan = child_plan.parents[1] / "example--docs" / "plan.md"
        child_plan.parent.mkdir(parents=True)
        child_plan.write_text("Child planning\n")
        sibling_plan.parent.mkdir(parents=True)
        sibling_plan.write_text("Another worker's staged plan\n")
        self.git(self.artifacts, "add", str(sibling_plan))
        refused = self.cli("artifacts", "commit", "--no-push", "--selector", "example", "-m", "Child planning",
                           str(child_plan), cwd=first, ok=False)
        self.assertIn("another branch's subplan", refused.stderr)
        self.assertIn(f"Plan commit working directory: {self.slots / 'p3'}", output)
        self.assertIn("not the inspection worktree", output)
        command = next(line.removeprefix("Plan commit command: ") for line in output.splitlines()
                       if line.startswith("Plan commit command: "))
        self.run_command(["bash", "-c", command], first)
        self.assertEqual(self.git(self.artifacts, "show", f"HEAD:{artifact_path(self.projects, self.artifacts).name}/branches/alpha/example--tests/plan.md"),
                         "Child planning")
        self.assertEqual(self.git(self.artifacts, "diff", "--cached", "--name-only"),
                         f"{artifact_path(self.projects, self.artifacts).name}/branches/alpha/example--docs/plan.md")
        self.assertEqual(self.git(self.slots / "p3", "status", "--porcelain"), "")

    def test_planned_handoff_allows_stale_predecessor_but_registered_handoff_requires_rebase(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Stable dependency\n")
        self.add_member("checks", after="api")
        self.commit_file(first, "api.txt", "Dependency changed\n")
        output = self.handoff("--planned-member", "tests", "--after", "checks")
        self.assertIn("Predecessor needs rebase; read-only planning may proceed", output)
        self.assertIn("before creating the child", output)
        self.assertIn("PR base: baton/example--checks (planned)", output)
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--member", "checks", ok=False)
        self.assertIn("Parent must rebase", result.stderr)

    def test_planned_destination_matches_created_child(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Stable dependency\n")
        output = self.handoff("--planned-member", "tests_2", "--after", "api")
        child = self.add_member("tests_2", after="api")
        data = json.loads(self.cli("status", "--json", cwd=child).stdout)["current_entry"]
        for label, key in (("Branch", "branch"), ("Worktree", "path"), ("Subplan", "subplan_path")):
            self.assertIn(f"{label}: {data[key]} (planned)", output)

    def test_planned_handoff_requires_compatible_flags_and_simple_slug(self) -> None:
        self.init_members()
        for args in (("--planned-member", "tests"), ("--after", "api"),
                     ("--member", "api", "--planned-member", "tests", "--after", "api"),
                     ("--planned-member", "../tests", "--after", "api")):
            with self.subTest(args=args):
                self.handoff(*args, ok=False)

    def test_planned_handoff_requires_registered_unambiguous_reliable_predecessor(self) -> None:
        first = self.init_members()
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--planned-member", "tests", "--after", "missing", ok=False)
        self.assertIn("not registered or is ambiguous", result.stderr)
        self.assertIn("--after", result.stderr)
        self.add_member("docs")
        state_path = fixtures.fixtures.state_file(self.projects)
        state = json.loads(state_path.read_text())
        state["repos"][0]["members"][0]["member"] = "api"
        state_path.write_text(json.dumps(state))
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--planned-member", "tests", "--after", "api", ok=False)
        self.assertIn("not registered or is ambiguous", result.stderr)
        state["repos"][0]["members"][0]["member"] = "docs"
        state_path.write_text(json.dumps(state))
        self.commit_file(first, "api.txt", "Stable dependency\n")
        self.add_member("checks", after="api")
        (self.repos / "alpha" / ".git" / "gh-stack-rebase-state").write_text("{}")
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--planned-member", "tests", "--after", "api", ok=False)
        self.assertIn("Finish or abort", result.stderr)

    def test_planned_handoff_requires_stack_top_without_requiring_its_first_commit(self) -> None:
        first = self.init_members()
        self.commit_file(first, "api.txt", "Stable dependency\n")
        checks = self.add_member("checks", after="api")
        result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                          "--planned-member", "tests", "--after", "api", ok=False)
        self.assertIn("--after must name the native stack top", result.stderr)
        self.assertEqual(self.git(checks, "rev-parse", "HEAD"), self.git(first, "rev-parse", "HEAD"))
        output = self.handoff("--planned-member", "tests", "--after", "checks")
        self.assertIn("PLAN ONLY", output)
        self.assertIn("PR base: baton/example--checks", output)

    def test_planned_handoff_refuses_registered_branch_and_path_collisions(self) -> None:
        self.init_members()
        for name, expected in (("api", "already registered"), ("branch", "branch or path already exists"),
                               ("directory", "branch or path already exists"),
                               ("symlink", "branch or path already exists")):
            if name == "branch":
                self.git(self.repos / "alpha", "branch", "baton/example--branch")
            elif name == "directory":
                (self.worktrees / "alpha" / "example--directory").mkdir()
            elif name == "symlink":
                (self.worktrees / "alpha" / "example--symlink").symlink_to(self.root / "missing")
            result = self.cli("handoff", "--selector", "example", "--agent", "worker", "--repo", "alpha",
                              "--planned-member", name, "--after", "api", ok=False)
            self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
