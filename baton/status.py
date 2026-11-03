"""Status reporting."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any

from .errors import ProjectError, NativeOperationPending
from .gitutil import git, inside, require_agent_write_path
from .manifest import entries, member_name, entry_stack_root, link_name, subplan_directory, note_directories
from .native import registered_stacks
from .core import ManagerCore
from .configuration import read_config
from .providers import effective_worker_models, provider_guidance, resolved_worker_models
from .skills_install import bundled_skills


class StatusMixin(ManagerCore):
    @staticmethod
    def helper_path() -> str:
        source = Path(__file__).resolve().parents[1] / "bin" / "baton"
        if source.is_file():
            return str(source)
        installed = Path(sys.executable).parent / "baton"
        if installed.is_file():
            return str(installed)
        return shutil.which("baton") or str(source)

    def resume_command(self, project: dict[str, Any]) -> str | None:
        slot, state = project["slot"], project["state"]
        selector = self.selector_for(project)
        # create and add take no selector and act on the slot of the current directory.
        here = f"cd {shlex.quote(str(self.slots / slot))} && "
        if project.get("pending_abandon") or state == "abandoning":
            return shlex.join(["baton", "abandon", project["uuid"]])
        if state == "creating":
            if project.get("follow_up_from"):
                return here + shlex.join(["baton", "create", "follow-up", "--from", project["follow_up_from"]])
            return here + shlex.join(["baton", "create", project["name"], "--repos", *project["requested"]])
        if state == "adding":
            return here + shlex.join(["baton", "add", *project["pending_add"]])
        if state == "renaming":
            return shlex.join(["baton", "rename", project["pending_rename"]["name"], selector])
        if state == "cleaning":
            return shlex.join(["baton", "cleanup", selector])
        if state == "claiming":
            owner = project["owner"]
            return shlex.join(["baton", "session", "claim", "--selector", selector,
                               "--provider", owner["provider"], "--session-id", owner["session_id"]])
        if state == "retiring":
            pending = project["pending_retirement"]
            command = ["baton", "branch", "prune" if pending.get("prune") else "cleanup"]
            if pending.get("repo"):
                command.append(pending["repo"])
            if not pending.get("prune"):
                command.append(pending["member"])
            return shlex.join([*command, "--selector", selector])
        if state == "branching":
            pending = project["pending_member"]
            command = ["branch", pending["action"], pending["repo"], pending["name"]]
            if pending.get("after"):
                command += ["--after", pending["after"]]
            return shlex.join(["baton", *command, "--selector", selector])
        return None

    def status_data(self, project: dict[str, Any]) -> dict[str, Any]:
        """Report what status shows without raising for pending operations or native catalog errors."""
        cwd = Path.cwd().resolve()
        directory = self.artifact_directory(project)
        require_agent_write_path(directory)
        notes = note_directories()
        roots = [directory, *(Path(entry["path"]) for repo in project["repos"] for entry in entries(repo, include_retired=True))]
        for root in roots:
            for name in notes:
                require_agent_write_path(root / name)
        resume = self.resume_command(project)
        errors: list[str] = []
        result: list[dict[str, Any]] = []
        retired: list[dict[str, Any]] = []
        for repo in project["repos"]:
            retired_branches: set[str] = set()
            for entry in entries(repo, include_retired=True):
                if entry.get("retirement_state") != "retired":
                    continue
                retired_branches.add(entry["branch"])
                retired.append({
                    "repo": repo["id"], "member": member_name(entry), "link": link_name(entry),
                    "branch": entry["branch"], "path": entry["path"],
                    "subplan_path": str(subplan_directory(self.artifact_directory(project), repo["id"],
                                                          entry["branch"], project["branch_prefix"]) / "plan.md"),
                    "stack_root": entry_stack_root(repo, entry), "retirement_state": "retired",
                    "retirement": entry.get("retirement"), "cleanup_sha": entry.get("cleanup_sha"),
                    "backup_ref": entry.get("backup_ref"), "step": entry["step"],
                })
            default = repo["base_ref"].removeprefix("refs/remotes/origin/").removeprefix("refs/heads/")
            bases: dict[str, str] = {}
            retired_predecessors: set[str] = set()
            error = stack_error = None
            try:
                for _record, _scope, current in registered_stacks(repo, allow_missing=project["state"] == "cleaning",
                                                                  resume=resume):
                    if current is None:
                        continue
                    previous = current["trunk"]["branch"]
                    retired_predecessor = False
                    for member in current["branches"]:
                        if member["branch"] in retired_branches:
                            retired_predecessor = True
                        elif retired_predecessor:
                            retired_predecessors.add(member["branch"])
                        bases[member["branch"]] = previous
                        if not (member.get("pullRequest") or {}).get("merged"):
                            previous = member["branch"]
            except NativeOperationPending as caught:
                # Registration guarantees independent members belong to no native stack, so their base still holds.
                stack_error = f"{repo['id']}: {caught}"
                errors.append(stack_error)
            except (ProjectError, OSError, ValueError) as caught:
                error = f"{repo['id']}: {caught}"
                errors.append(error)
            for entry in entries(repo):
                root = entry_stack_root(repo, entry)
                base = bases.get(entry["branch"], default)
                entry_error = error or (stack_error if root else None)
                needs_rebase = None if entry_error else False
                if entry_error is None and root and (base != default or entry["branch"] in retired_predecessors):
                    # A retired squash-merged predecessor also requires rebasing onto its new default-branch tip.
                    comparison = entry["base_ref"] if base == default else f"refs/heads/{base}"
                    contained = subprocess.run(["git", "-C", entry["source"], "merge-base", "--is-ancestor",
                                                comparison, f"refs/heads/{entry['branch']}"],
                                               capture_output=True).returncode
                    needs_rebase = {0: False, 1: True}.get(contained)
                    if needs_rebase is None:
                        entry_error = f"{repo['id']}: cannot compare {entry['branch']} with its predecessor {base}"
                        errors.append(entry_error)
                result.append({
                    "repo": repo["id"], "member": member_name(entry), "link": link_name(entry),
                    "branch": entry["branch"], "path": entry["path"],
                    "subplan_path": str(subplan_directory(self.artifact_directory(project), repo["id"],
                                                          entry["branch"], project["branch_prefix"]) / "plan.md"),
                    "workflow": "native_stack" if root else "independent", "stack_root": root,
                    "pr_base": base, "base_ref": entry["base_ref"] if base == default else f"refs/heads/{base}",
                    "base_reliable": entry_error is None, "base_error": entry_error, "needs_rebase": needs_rebase,
                    "step": entry["step"],
                })
        here = [e for e in result if inside(cwd, Path(e["path"]))]
        info = project.get("blackboard")
        config = read_config()
        worker_models = effective_worker_models(config)
        resolved_workers = resolved_worker_models(config)
        owner = project.get("owner")
        worker_guidance = {
            provider: (f"When the owner delegates workers through {provider}, explicitly select model {model} "
                       "instead of inheriting the owner's model. This is delegation guidance only; "
                       "it does not change the owner session or review models. "
                       "If the runtime cannot select this model, report the limitation before dispatching "
                       "rather than silently using another model.")
            for provider, model in resolved_workers.items()
            if model != "default" and (owner is None or provider == owner["provider"])
        }
        return {
            "name": project["name"], "slot": project["slot"], "coordinator": str(self.slots / project["slot"]),
            "branch": project["branch"], "state": project["state"],
            "uuid": project["uuid"], "owner": project.get("owner"),
            **{key: project[key] for key in ("follow_up_from", "follow_up_path", "follow_up_root_name", "follow_up_number") if key in project},
            "worker_model": worker_models, "resolved_worker_model": resolved_workers,
            "worker_model_guidance": worker_guidance,
            # Not owner-filtered: a non-owner agent needs its own provider's claim instructions.
            "provider_guidance": provider_guidance(),
            "master_path": str(self.artifact_directory(project) / "plan.md"),
            "helper_path": self.helper_path(),
            "note_directories": notes,
            "blackboard": {"path": str(Path(info["directory"]) / "blackboard.sqlite"), "backend": "sqlite", "generation": info["generation"]} if info else None,
            "pending_operation": {"operation": project["state"], "command": resume} if resume else None,
            "current_entry": max(here, key=lambda e: len(e["path"])) if here else None,
            "entries": result, "retired_entries": retired, "errors": errors,
        }

    def open_plan(self, selector: str | None) -> int:
        data = self.status_data(self.project(selector))
        plan = Path(data["master_path"])
        entry = data["current_entry"]
        if entry is not None and len(data["entries"]) > 1:
            subplan = Path(entry["subplan_path"])
            if subplan.is_file():
                plan = subplan
        plan.parent.mkdir(parents=True, exist_ok=True)
        # Split like a shell so editors that need flags, such as "code -w", work; a blank
        # VISUAL must not hide a usable EDITOR.
        editor = next(filter(None, (shlex.split(os.environ.get(name, "")) for name in ("VISUAL", "EDITOR"))),
                      ["vim"])
        returncode = subprocess.run([*editor, str(plan)]).returncode
        return 128 - returncode if returncode < 0 else returncode

    def task_handoff(self, project: dict[str, Any], helper: str, agent: str,
                     identifier: str) -> tuple[str, dict[str, Any] | None]:
        """Return task instructions and the task when it is already in progress for this agent."""
        from .tasks import Tasks
        tasks = Tasks(self.board_for(project), project)
        task = tasks.read(identifier, actor=agent)["tasks"][0]
        # Only task update changes an in-progress task's version, and settle needs the current one,
        # so a re-handoff of the agent's own claimed task can print the exact settle version.
        claimed = task["state"] == "in_progress" and task["assignee"] == agent
        if not (claimed or task["ready"] and task["assignment_eligible"]):
            raise ProjectError("Task is neither claimable by this agent nor in progress and assigned to it (it is held, "
                               "settled, blocked or assigned elsewhere); create, assign, reroute or unblock it before the handoff.")
        parent = tasks.creator(identifier)
        common = ["--selector", project["uuid"], "--agent", agent, "--expected-generation", tasks.board.generation]
        claim = shlex.join([helper, "task", "claim", "--id", identifier, "--expected-version", str(task["version"]), *common, "--json"])
        ask = shlex.join([helper, "blackboard", "ask", *common, "--to", parent, "--task", identifier, "--text", "QUESTION"])
        resume = shlex.join([helper, "blackboard", "wait", "--selector", project["uuid"], "--expected-generation",
                             tasks.board.generation, "--reply-to", "QUESTION_ID"])
        progress = shlex.join([helper, "blackboard", "post", *common, "--task", identifier, "--text", "NOTE"])
        version = str(task["version"]) if claimed else "CLAIMED_VERSION"
        settle = shlex.join([helper, "task", "settle", "--id", identifier, "--expected-version", version, *common,
                             "--outcome", "succeeded|failed", "--summary", "SUMMARY", "--to", parent])
        if claimed:
            # A start-of-board cursor turns the task-filtered wait into an immediate read of the task's whole history.
            history = shlex.join([helper, "blackboard", "wait", "--selector", project["uuid"], "--expected-generation",
                                  tasks.board.generation, "--since", tasks.board.encode_cursor(0, None), "--task", identifier,
                                  "--timeout", "0", "--json"])
            header = f"\nAlready claimed by this agent: do not claim again.\nRecover task history: {history}\n"
            # Answers are not task-linked, so the history shows question IDs but not whether they were answered.
            versions = ("Recover progress and question IDs from the task history; for each question ID run the resume "
                        "command, which returns an existing reply immediately; never ask it again. Settle with the printed "
                        "current version; if settle is refused as stale, rerun the refresh command instead of editing values. ")
        else:
            header = f"\nClaim first: {claim}\n"
            versions = ("Take CLAIMED_VERSION from the version in the claim output; if settle is refused as stale, rerun "
                        "the refresh command and settle with the version it prints. ")
        return (f"Task: {identifier} ({task['title']}) | version: {task['version']} | parent: {parent}{header}"
                f"Ask blocking questions: {ask}\n"
                f"Resume after an ask timeout: {resume}\n"
                f"Post task progress: {progress}\n"
                f"Settle once before returning: {settle} [--files PATH ...] [--report PATH]\n"
                f"{versions}Use ask for blocking questions, never a local "
                "prompt the parent cannot see; after a timeout resume with wait --reply-to, never ask again. Post "
                "substantive findings, evidence and blockers with --task so they stay attached to this task. Read replies "
                "at checkpoints. Settle exactly once with an explicit outcome before returning and never re-claim a settled task. "
                "If settle is refused because the task was reassigned or completed, post the outcome to the parent as an "
                "ordinary blackboard entry.\n"), task if claimed else None

    def handoff(self, selector: str | None, agent: str, repo: str, member: str | None,
                *, planned_member: str | None = None, after: str | None = None, task: str | None = None) -> None:
        if not agent.strip() or "\n" in agent or "\r" in agent:
            raise ProjectError("Agent name must be a nonempty single line.")
        if (planned_member is None) != (after is None) or (planned_member is not None and member is not None):
            raise ProjectError("Use --planned-member with --after and without --member; --after is only valid for a planned handoff.")
        project = self.project(selector)
        destination = self.member_branch_and_path(project, repo, planned_member) if planned_member is not None else None
        data = self.status_data(project)
        if data["state"] != "active":
            raise ProjectError("Finish the pending project operation before preparing a worker handoff.")
        selected = after if planned_member is not None else member
        candidates = [entry for entry in data["entries"] if entry["repo"] == repo and
                      (selected is None or selected in (entry["member"], entry["branch"]))]
        if len(candidates) != 1:
            flag = "--after" if planned_member is not None else "--member"
            raise ProjectError(f"Repository/member is not registered or is ambiguous; select an exact {flag}.")
        entry = candidates[0]
        if not entry["base_reliable"]:
            raise ProjectError(entry["base_error"])
        board = data["blackboard"]
        if not board:
            raise ProjectError("Initialize the baton blackboard before preparing a worker handoff.")
        # A canonical name that is itself an alias key would resolve elsewhere on rerun; replay an alias instead.
        repo_arg = entry["repo"] if self.resolve_repo(entry["repo"]) == entry["repo"] else min(
            name for name, target in self.repo_aliases.items() if target == entry["repo"])
        planning = ""
        inspection_path = entry["path"]
        if planned_member is not None:
            registered_repo = next(r for r in project["repos"] if r["id"] == repo)
            if entry["stack_root"] is not None:
                current = next(current for record, _, current in registered_stacks(registered_repo)
                               if record["root"] == entry["stack_root"])
                if current is None or current["branches"][-1]["branch"] != entry["branch"]:
                    raise ProjectError("--after must name the native stack top.")
            assert destination is not None
            planned_branch, planned_path = destination
            self.check_member_destination(project, registered_repo, planned_member, planned_branch, planned_path)
            entry = dict(entry, member=planned_member, branch=planned_branch, path=str(planned_path),
                         pr_base=entry["branch"], base_ref=f"refs/heads/{entry['branch']}", workflow="native_stack",
                         subplan_path=str(subplan_directory(self.artifact_directory(project), repo,
                                                            planned_branch, project["branch_prefix"]) / "plan.md"))
            commit = shlex.join([data["helper_path"], "artifacts", "commit", "--selector", self.selector_for(project),
                                 "-m", f"Update planned {planned_member} subplan", entry["subplan_path"]])
            planning = ("Assignment: PLAN ONLY; authorized phases: plan/refine only.\n"
                        "Branch, worktree, subplan, PR base, and workflow below are PLANNED, not registered.\n"
                        "The planned worktree DOES NOT EXIST: do not cd into or create it.\n"
                        "This handoff does not establish child-creation readiness or tested dependencies.\n"
                        "Implementation must wait for the parent to create the child at a stable dependency "
                        "checkpoint and refresh the handoff.\n"
                        f"Read-only inspection worktree: {inspection_path}\n"
                        "Commit only the planned subplan from the coordinator slot, not the inspection worktree; "
                        "when the project has multiple registered members, the inspection worktree permits "
                        "only its own registered subplan.\n"
                        f"Plan commit working directory: {data['coordinator']}\n"
                        f"Plan commit command: (cd {shlex.quote(data['coordinator'])} && {commit})\n")
            if candidates[0]["needs_rebase"]:
                planning += ("Predecessor needs rebase; read-only planning may proceed. The parent must rebase "
                             "before creating the child at the stable dependency checkpoint.\n")
        task_text, owned = self.task_handoff(project, data["helper_path"], agent, task) if task is not None else ("", None)
        warning = ""
        if entry["needs_rebase"] and planned_member is None:
            # Only a task bound to this member, or to none, recovers here; work for another member still waits.
            if owned is None or owned["repo"] not in (None, entry["repo"]) or owned["member"] not in (None, entry["member"]):
                raise ProjectError(f"Parent must rebase {entry['branch']} onto {entry['pr_base']} before handing off work.")
            # A worker recovering its own task still needs to ask and settle; only new work waits for the rebase.
            warning = (f"Warning: {entry['branch']} needs a rebase onto {entry['pr_base']}; the parent rebases it. "
                       "Do not rebase, merge or sync it yourself; ask, progress and settle do not depend on the base.\n")
        command = shlex.join([data["helper_path"], "task", "list", "--selector", data["uuid"],
                              "--repo", repo, "--agent", agent, "--expected-generation", board["generation"], "--json"])
        show = shlex.join([data["helper_path"], "task", "show", "--selector", data["uuid"], "--id", "TASK_ID", "--expected-generation", board["generation"], "--json"])
        task_guidance = (f"Read tasks: {command}\nRead one task: {show}\n"
                         "Inspect prerequisites, cancelled blockers and assignment eligibility before claiming. "
                         "Only done satisfies dependencies. Claim/release/assign/done/cancel require the recorded generation "
                         "and task version; a reassigned task must be claimed again. "
                         "Task assignment is descriptive, never ownership or topology authority. "
                         "This handoff does not claim, assign or complete tasks; parent scope/authorization still applies.\n")
        task_guidance += task_text
        label = " (planned)" if planned_member is not None else ""
        # The UUID survives renames and the resolved member stays unambiguous when members are added.
        # candidates[0] is still the --after predecessor; entry was rewritten to the planned child.
        selection = (["--planned-member", planned_member, "--after", candidates[0]["member"] or candidates[0]["branch"]]
                     if planned_member is not None else ["--member", entry["member"] or entry["branch"]])
        refresh = shlex.join([data["helper_path"], "handoff", "--selector", data["uuid"], "--agent", agent,
                              "--repo", repo_arg, *selection, *(["--task", task] if task is not None else [])])
        print(f"Agent: {agent}\nProject: {data['name']}\n"
              f"Project UUID: {data['uuid']}\nOwner: {json.dumps(data['owner'])}\n"
              "This is a worker assignment, not project ownership. Never claim or replace the project owner.\n"
              f"Blackboard generation: {board['generation']}\nBlackboard: {board['path']}\n"
              f"Helper: {data['helper_path']}\n"
              f"Refresh instructions: {refresh}\n"
              "Rerun it at any time, such as after a restart, context loss or compaction, to regenerate these "
              "instructions with current values; never reconstruct commands from memory. If it is refused, wait "
              "for or ask the parent for a fresh handoff.\n"
              f"Repository: {entry['repo']}\n{planning}"
              f"Member: {entry['member'] or '(unnamed; select with --member ' + entry['branch'] + ')'}{label}\n"
              f"Branch: {entry['branch']}{label}\nWorktree: {entry['path']}{label}\n"
              f"PR base: {entry['pr_base']}{label}\n{warning}Base ref: {entry['base_ref']}{label}\nWorkflow: {entry['workflow']}{label}\n"
              f"Master plan: {data['master_path']}\nSubplan: {entry['subplan_path']}{label}\n\n"
              + task_guidance +
              "Parent must supply before dispatch: owned files/scope, dependencies, acceptance criteria, "
              "integration plan, planning mode, authorized phase "
              f"({'plan/refine only' if planned_member is not None else 'plan/refine/implement/publish'}), "
              "and runtime recipient mappings.\n"
              f"Read {bundled_skills() / 'baton-workflow/SKILL.md'} completely and follow its branch/mutating-worker required reading before starting.\n"
              f"Read {inspection_path}/AGENTS.md and the blackboard before starting; use the helper to post updates.\n"
              f"{'Use the inspection worktree only for read-only inspection.' if planned_member is not None else 'Work only in the assigned worktree.'} "
              "Never switch or detach a primary checkout. "
              f"Inspect another ref with git -C {shlex.quote(inspection_path)} show <ref>:<path>.\n"
              "Post dependency readiness, interface changes and rebase requests on the board, "
              "then directly message affected agents when supported, with the action and commit SHA.\n"
              "Await every launched process; never return with a review pending.\n"
              "Only the parent changes topology or rewrites/publishes native stacks, "
              "after affected workers return. Follow the workflow lifecycle reference for "
              "stable child checkpoints and publication gates.")

    def status(self, selector: str | None, as_json: bool = False, include_retired: bool = False) -> None:
        project = self.project(selector)
        data = self.status_data(project)
        for error in data["errors"]:
            print(f"baton: base unreliable: {error}", file=sys.stderr)
        if as_json:
            print(json.dumps(data, indent=2))
            return
        print(f"{data['slot']}: {data['name']} [{data['state']}]")
        if data["owner"] is not None:
            print(f"  Owner: {data['owner']['provider']} session {data['owner']['session_id']}")
        else:
            print("  Owner: unclaimed")
        if data.get("follow_up_path"):
            print(f"  Follow-up context: {data['follow_up_path']}")
        for guidance in data["worker_model_guidance"].values():
            print(f"  Worker model guidance: {guidance}")
        if data["pending_operation"]:
            print(f"  Pending {data['state']}; resume with: {data['pending_operation']['command']}")
        if data["blackboard"]:
            print(f"  Blackboard: {data['blackboard']['path']}")
        for entry in data["entries"]:
            workflow = f"native stack {entry['stack_root']}" if entry["stack_root"] else "independent"
            base = entry["pr_base"] + ("" if entry["base_reliable"] else " (unreliable)")
            if entry["needs_rebase"]:
                base += f"; needs rebase onto {entry['pr_base']}"
            print(f"  {entry['link']} ({entry['member'] or 'root'}; {workflow}; PR base: {base}): "
                  f"{entry['path']} [{entry['step']}]")
            if Path(entry["path"]).exists():
                # Indented under its entry so the "## branch" header reads as that checkout's git status.
                for line in git(Path(entry["path"]), "status", "--short", "--branch", check=False).splitlines():
                    print(f"    git: {line}")
        if include_retired and data["retired_entries"]:
            print("  Retired members:")
            for entry in data["retired_entries"]:
                print(f"    {entry['repo']} ({entry['member'] or 'root'}): {entry['branch']} "
                      f"[{entry['retirement_state']}; archive: {entry['backup_ref'] or 'missing'}]")
