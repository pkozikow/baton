"""Command-line interface for Baton."""

import argparse
import contextlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import sqlite3
from typing import Any, Iterator

from blackboard import BlackboardError
from .errors import ProjectError, NoProject
from .manager import Manager
from .providers import PROVIDERS
from .slots import SLOT_NAMES
from .doctor_freshness import require_trusted_runtime


@contextlib.contextmanager
def live_project_guard(manager: Any, project: dict[str, Any], board: Any, expected_generation: str) -> Iterator[dict[str, Any]]:
    # Acquire the registry lock only after the board lock, never while
    # collecting input or waiting on another board writer.
    with manager.global_lock() as registry:
        record = registry / "v2/projects" / f"{project['slot']}.json"
        if not record.is_file():
            raise NoProject("No active project; the blackboard is closed.")
        current = json.loads(record.read_text())
        if (current.get("uuid") != project["uuid"] or
                current.get("blackboard", {}).get("generation") != expected_generation):
            raise ProjectError("Blackboard generation changed; this agent session is stale.")
        if current["state"] == "cleaning":
            raise ProjectError("Project is being cleaned up; the blackboard no longer accepts updates.")
        latest = manager.board_for(current)
        if latest.directory != board.directory or latest.project != board.project:
            raise ProjectError("Blackboard changed during project rename; retry after the rename finishes.")
        yield current


@contextlib.contextmanager
def task_project_guard(manager: Any, project: dict[str, Any], board: Any,
                       expected_generation: str, tasks: Any) -> Iterator[None]:
    with live_project_guard(manager, project, board, expected_generation) as current:
        tasks.project = current
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton", description="Local project coordination across independent Git repositories.")
    commands = parser.add_subparsers(dest="command", required=True)
    selector_help = "Project UUID, name, or coordinator slot (default: current project)"
    commands.add_parser("config", help="Show or configure project storage roots")
    commands.add_parser("skills", help="Install or update bundled agent skills")
    commands.add_parser("doctor", help="Check installation freshness, paths, and configured review models")
    commands.add_parser("review", help="Run plan or implementation reviewers (used by the review skills)")
    commands.add_parser("init")
    commands.add_parser("list")
    search = commands.add_parser("search", help="Find current and past projects from metadata and saved artifacts")
    search.add_argument("query", help='Literal keywords and double-quoted phrases (all must match)')
    search.add_argument("--state", choices=("all", "active", "archived"), default="all")
    search.add_argument("--limit", type=int, default=20, help="Maximum projects to return (default: 20)")
    search.add_argument("--json", action="store_true", help="Machine-readable matches and coverage warnings")
    open_plan = commands.add_parser("open-plan", help="Open the current project's plan or existing branch subplan in $VISUAL, $EDITOR, or vim")
    open_plan.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
    retro = commands.add_parser("retro", help="Fork the project owner (or start a fresh session when its provider "
                                              "cannot fork) to write a retrospective with baton-retro")
    retro.add_argument("retro_action", nargs="?", choices=("pending", "show", "summarize"))
    retro.add_argument("--selector", help="Full project UUID, unambiguous name, or active slot (default: current branch)")
    retro.add_argument("--non-interactive", action="store_true", help="Run a bounded headless owner fork or fresh session")
    retro.add_argument("--timeout", type=int, default=1800, help="Headless run time limit in seconds")
    retro.add_argument("--json", action="store_true")
    session = commands.add_parser("session", help="Claim immutable project ownership or resume/fork its owner")
    session_commands = session.add_subparsers(dest="session_action", required=True)
    for action in ("claim", "show", "resume", "fork"):
        command = session_commands.add_parser(action)
        command.add_argument("--selector", help="Full UUID, project name, or active slot (default: current project's branch)")
        if action == "claim":
            command.add_argument("--provider", choices=tuple(PROVIDERS), required=True)
            command.add_argument("--session-id", help="Current session UUID (default: the provider's session "
                                                      "variable, when it has one; see provider_guidance)")
    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--selector", choices=SLOT_NAMES, help="Target coordinator slot (default: current slot)")
    inputs = create.add_mutually_exclusive_group(required=True)
    inputs.add_argument("-r", "--repos", nargs="+")
    inputs.add_argument("--from", dest="from_uuid", metavar="FULL_UUID")
    add = commands.add_parser("add")
    add.add_argument("repos", nargs="+")
    add.add_argument("--selector", help=selector_help)
    rename = commands.add_parser("rename")
    rename.add_argument("name")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true",
                        help="Machine-readable project view; exits 3 with empty stdout outside a project")
    status.add_argument("--all", action="store_true", help="Also display retired members")
    handoff = commands.add_parser("handoff", help="Render registered or plan-only worker metadata and handoff rules")
    handoff.add_argument("--agent", required=True)
    handoff.add_argument("--repo", required=True)
    handoff_member = handoff.add_mutually_exclusive_group()
    handoff_member.add_argument("--member")
    handoff_member.add_argument("--planned-member", help="Render a plan-only child assignment without registering it")
    handoff.add_argument("--after", help="Registered predecessor for --planned-member (required)")
    handoff.add_argument("--selector")
    handoff.add_argument("--task", help="Add claim, ask, resume and settle commands for this task; for the "
                         "agent's own in-progress task, omit the claim and print its history read and current version")
    path = commands.add_parser("path")
    path.add_argument("selector", nargs="?")
    path.add_argument("repo", nargs="?")
    path.add_argument("--selector", dest="explicit_selector", metavar="PROJECT", help=selector_help)
    path.add_argument("--member", help="Registered member (first checkout is the default)")
    stack = commands.add_parser("stack", help="Inspect native stack groups")
    stack_commands = stack.add_subparsers(dest="stack_action", required=True)
    stack_status = stack_commands.add_parser("status")
    stack_status.add_argument("repo")
    stack_status.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
    stack_status.add_argument("--member", help="Select a native stack by one of its registered members")
    branch_command = commands.add_parser("branch", help="Manage independent branches or explicitly extend a native stack")
    branch_commands = branch_command.add_subparsers(dest="branch_action", required=True)
    for action in ("init", "add", "register"):
        command = branch_commands.add_parser(action)
        command.add_argument("repo")
        command.add_argument("member")
        command.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
        if action != "init":
            command.add_argument("--after", help="Explicit predecessor in a native stack (default: independent)")
    for action in ("cleanup", "prune"):
        command = branch_commands.add_parser(action)
        command.add_argument("repo", nargs="?" if action == "prune" else None)
        if action == "cleanup":
            command.add_argument("member")
        command.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
        command.add_argument("--dry-run", action="store_true")
    blackboard = commands.add_parser("blackboard", help="Cooperative agent notes with a separate permanent file lock")
    board_commands = blackboard.add_subparsers(dest="board_action", required=True)
    for action in ("init", "read", "post", "edit", "update", "export"):
        command = board_commands.add_parser(action)
        command.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
        if action in ("read", "post", "edit", "update", "export"):
            command.add_argument("--expected-generation", required=action != "read",
                                 help="Fence this agent session to the generation supplied by its parent "
                                      "(blackboard.generation in baton status --json)")
        if action == "export":
            command.add_argument("--output", type=Path, help="Snapshot directory (default: ignored project export directory)")
        elif action == "read":
            command.add_argument("--json", action="store_true", help="Return text and its SHA256 revision as one snapshot")
            command.add_argument("--to", help="Read posts addressed to this agent, including all broadcasts")
            command.add_argument("--tail", type=int, help="Return the last N matching posts")
            command.add_argument("--limit", type=int, help="Maximum forward matching events")
            command.add_argument("--since", help="Incremental cursor from an earlier read --json (not an edit revision)")
        elif action in ("post", "edit", "update"):
            content = command.add_mutually_exclusive_group()
            content.add_argument("--text", help="Prepared Markdown content (default: read stdin before locking)")
            content.add_argument("--file", help="Read prepared Markdown from this file before locking")
            command.add_argument("--agent", required=True, help="Stable parent/subagent name")
            if action in ("post", "update"):
                command.add_argument("--kind", default=None)
            if action == "post":
                command.add_argument("--reply-to", help="Parent entry ID")
                command.add_argument("--task", help="Linked task ID")
                command.add_argument("--to", default="all", help="Recipient label (default: all agents)")
            elif action == "update":
                command.add_argument("--id", required=True)
                command.add_argument("--expected-version", type=int, required=True)
            else:
                command.add_argument("--expected-revision", required=True, help="Overview SHA256 from any blackboard read --json (e.g. --tail 0); stale edits fail")
    for action in ("wait", "ask"):
        command = board_commands.add_parser(action, help="Wait for new matching entries or a reply" if action == "wait"
                                            else "Post a question and wait for its reply")
        command.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
        command.add_argument("--expected-generation", required=True)
        command.add_argument("--timeout", type=float, default=90.0,
                             help="Seconds before returning timed_out with a resumable cursor (default: 90, "
                                  "below common agent command limits)")
        command.add_argument("--poll-interval", type=float, default=1.0)
        command.add_argument("--json", action="store_true", help="Return structured JSON (also the default)")
        if action == "wait":
            start = command.add_mutually_exclusive_group(required=True)
            start.add_argument("--since", help="Cursor from read --json or an earlier wait")
            start.add_argument("--reply-to", help="Return replies to this entry, waiting if none exist yet")
            command.add_argument("--to", help="Only entries addressed to this agent, including all broadcasts")
            command.add_argument("--kind", help="Comma-separated entry kinds, e.g. question,settlement")
            command.add_argument("--task", help="Only entries linked to this task ID")
        else:
            content = command.add_mutually_exclusive_group()
            content.add_argument("--text", help="Prepared question (default: read stdin before locking)")
            content.add_argument("--file", help="Read the prepared question from this file before locking")
            command.add_argument("--agent", required=True, help="Stable parent/subagent name")
            command.add_argument("--to", required=True, help="Agent expected to answer")
            command.add_argument("--task", help="Linked task ID")
    agents = board_commands.add_parser("agents", help="Advisory per-agent activity, tasks, settlements and open questions")
    agents.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
    agents.add_argument("--expected-generation", required=True)
    agents.add_argument("--quiet-after", type=float, default=900.0,
                        help="Seconds without a write before an agent is reported quiet (default: 900)")
    agents.add_argument("--json", action="store_true", help="Return structured JSON (also the default)")
    task = commands.add_parser("task", help="Project-local atomic tasks and blocking dependencies")
    task_commands = task.add_subparsers(dest="task_action", required=True)
    for action in ("create", "show", "list", "update", "claim", "release", "assign", "done", "settle", "cancel", "dependency"):
        command = task_commands.add_parser(action)
        if action == "dependency":
            command.add_argument("dependency_action", choices=("add", "remove"))
            command.add_argument("--prerequisite", required=True)
        command.add_argument("--selector")
        command.add_argument("--expected-generation", required=action not in ("show", "list"))
        command.add_argument("--agent", required=action not in ("show", "list"))
        if action not in ("create", "list"):
            command.add_argument("--id", required=True)
        if action not in ("create", "show", "list"):
            command.add_argument("--expected-version", type=int, required=True)
        command.add_argument("--json", action="store_true", help="Return structured JSON (also the default)")
        if action in ("create", "update"):
            command.add_argument("--title", required=action == "create")
            command.add_argument("--description")
            command.add_argument("--priority", type=int)
            command.add_argument("--plan")
            if action == "update":
                for field in ("repo", "member", "plan"):
                    command.add_argument(f"--clear-{field}", action="store_true")
        if action in ("create", "update", "list"):
            command.add_argument("--repo")
            command.add_argument("--member")
        if action in ("create", "assign", "list"):
            command.add_argument("--assignee")
        if action == "assign":
            command.add_argument("--unassign", action="store_true")
        if action == "settle":
            command.add_argument("--outcome", choices=("succeeded", "failed"), required=True,
                                 help="succeeded completes the task; failed reopens it held for the parent")
            command.add_argument("--summary", required=True)
            command.add_argument("--files", nargs="+", default=[], metavar="PATH")
            command.add_argument("--report", metavar="PATH")
            command.add_argument("--to", default="all", help="Recipient label (default: all agents)")
        if action == "list":
            command.add_argument("--state", choices=("open", "in_progress", "done", "cancelled"))
            query = command.add_mutually_exclusive_group()
            query.add_argument("--ready", action="store_true")
            query.add_argument("--blocked", action="store_true")
            query.add_argument("--held", action="store_true",
                               help="Tasks held after a failed settlement until the parent assigns or cancels them")
    artifacts = commands.add_parser("artifacts", help="Commit project plans, subplans and retros in the shared checkout")
    artifact_commands = artifacts.add_subparsers(dest="artifacts_action", required=True)
    artifact_commit = artifact_commands.add_parser("commit")
    artifact_commit.add_argument("--selector", help="Project name or coordinator slot (default: current project)")
    artifact_commit.add_argument("-m", "--message", required=True)
    artifact_commit.add_argument("--no-push", action="store_true",
                                 help="Commit locally without syncing with or pushing to origin")
    artifact_commit.add_argument("paths", nargs="+", metavar="PATH", help="Paths inside the project's artifact directory")
    abandon = commands.add_parser("abandon")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--force", action="store_true",
                         help="Warn and confirm best-effort local cleanup; fail only if the slot cannot be safely released")
    cleanup.add_argument("--allow-unmerged", action="append", default=[], metavar="REPO[:MEMBER]",
                         help="Preserve unmerged tips under archive refs and remove the worktrees without a merged-PR "
                              "check, for a whole repository or one member (root names an unnamed first checkout)")
    for command in (status, rename, abandon, cleanup):
        selection = command.add_mutually_exclusive_group()
        # An absent positional must not overwrite the value supplied by --selector.
        selection.add_argument("selector", nargs="?", default=argparse.SUPPRESS, metavar="PROJECT")
        selection.add_argument("--selector", help=selector_help)
    return parser


def main() -> int:
    if sys.argv[1:2] == ["config"]:
        from .configuration import main as config_main
        return config_main(sys.argv[2:])
    if sys.argv[1:2] == ["skills"]:
        from .skills_install import main as skills_main
        return skills_main(sys.argv[2:])
    if sys.argv[1:2] == ["doctor"]:
        from .doctor import main as doctor_main
        return doctor_main(sys.argv[2:])
    if sys.argv[1:2] == ["review"]:
        from .review_runner import main as review_main
        return review_main(sys.argv[2:])
    if sys.argv[1:3] == ["retro", "pending"]:
        from .retro_automation import pending_main
        return pending_main(sys.argv[3:])
    if sys.argv[1:3] == ["retro", "summarize"]:
        from .retro_summary import summary_main
        return summary_main(sys.argv[3:])
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "path" and args.explicit_selector is not None:
        if args.repo is not None:
            parser.error("path --selector accepts at most one positional repository")
        args.repo, args.selector = args.selector, args.explicit_selector
    if getattr(args, "selector", None) is not None and not args.selector.strip():
        parser.error("Project selector must not be empty")
    manager = None
    try:
        if args.command == "retro" and args.retro_action in ("pending", "summarize"):
            raise ProjectError(f"Use baton retro {args.retro_action} [options]; place {args.retro_action} before its options.")
        read_only = (args.command in ("list", "search", "status", "handoff", "path")
                     or (args.command in ("cleanup", "branch") and getattr(args, "dry_run", False))
                     or (args.command == "session" and args.session_action == "show")
                     or (args.command == "retro" and args.retro_action == "show")
                     or (args.command == "blackboard" and args.board_action in ("read", "wait", "agents"))
                     or (args.command == "task" and args.task_action in ("show", "list")))
        if not read_only:
            require_trusted_runtime()
        manager = Manager()
        if args.command in ("branch", "stack", "handoff") and args.repo is not None:
            args.repo = manager.resolve_repo(args.repo)
        if args.command == "init":
            manager.initialize()
            return 0
        mutation = (args.command in ("create", "add", "rename", "abandon")
                    or (args.command == "session" and args.session_action == "claim")
                    or (args.command == "branch" and not getattr(args, "dry_run", False))
                    or (args.command == "cleanup" and not args.dry_run)
                    or (args.command == "blackboard" and args.board_action == "init"))
        if args.command != "search":
            manager.load()
        if args.command in ("session", "retro") and args.selector is None:
            project = manager.session_project(args.selector)
            if args.selector is None:
                args.selector = manager.selector_for(project)
        with (manager.command_lock(getattr(args, "selector", None), create=args.command == "create")
              if mutation else contextlib.nullcontext()):
            if args.command == "create":
                manager.create(args.name, args.repos, from_uuid=args.from_uuid, selector=args.selector)
            elif args.command == "search":
                from .search import render_search
                result = manager.search_projects(args.query, state=args.state, limit=args.limit)
                print(json.dumps(result, indent=2) if args.json else render_search(result))
            elif args.command == "session":
                if args.session_action == "claim":
                    manager.claim_owner(args.selector, args.provider, args.session_id)
                elif args.session_action == "show":
                    print(json.dumps(manager.artifact_project(args.selector).get("owner"), indent=2))
                else:
                    return manager.launch_owner(args.selector, args.session_action)
            elif args.command == "retro":
                if args.retro_action == "show":
                    print(json.dumps(manager.retro_snapshot(args.selector), indent=2))
                else:
                    return manager.retro(args.selector, non_interactive=args.non_interactive, timeout=args.timeout)
            elif args.command == "open-plan":
                return manager.open_plan(args.selector)
            elif args.command == "add":
                manager.add(args.repos, args.selector)
            elif args.command == "rename":
                manager.rename(args.name, args.selector)
            elif args.command == "stack":
                manager.stack(args.stack_action, args.repo, args.selector, args.member)
            elif args.command == "branch":
                if args.branch_action in ("cleanup", "prune"):
                    manager.retire_members(args.repo, getattr(args, "member", None), args.selector,
                                           args.dry_run, prune=args.branch_action == "prune")
                else:
                    manager.manage_member(args.branch_action, args.repo, args.member, args.selector,
                                          after=getattr(args, "after", None))
            elif args.command == "blackboard":
                project = manager.project(args.selector)
                if args.board_action == "init":
                    if project["state"] != "active":
                        raise ProjectError("Finish the outstanding project operation before initializing a blackboard.")
                    manager.check_coordinator(project)
                    manager.initialize_blackboard(project)
                    manager.materialize(project)
                    print(f"Blackboard ready: {project['blackboard']['directory']}")
                else:
                    board = manager.board_for(project)
                    if args.expected_generation is not None and args.expected_generation != project["blackboard"]["generation"]:
                        raise ProjectError("Blackboard generation changed; this agent session is stale.")
                    if args.board_action == "export":
                        print(json.dumps(manager.export_blackboard(project, args.output), indent=2))
                    elif args.board_action == "read":
                        options = {"to": args.to, "tail": args.tail, "since": args.since}
                        if args.limit is not None:
                            options["limit"] = args.limit
                        snapshot = board.read(**options)
                        print(json.dumps(snapshot, indent=2) if args.json else snapshot["text"],
                              end="\n" if args.json else "")
                    elif args.board_action == "agents":
                        print(json.dumps(board.agents(args.quiet_after), indent=2))
                    elif args.board_action == "wait":
                        kinds = None if args.kind is None else [kind.strip() for kind in args.kind.split(",")]
                        print(json.dumps(board.wait(since=args.since, reply_to=args.reply_to, to=args.to, kinds=kinds, task_id=args.task,
                                                    timeout=args.timeout, poll_interval=args.poll_interval), indent=2))
                    else:
                        # Workers must be able to report blockers while add/branch/stack runs or after an interruption.
                        if project["state"] == "cleaning":
                            raise ProjectError("Project is being cleaned up; the blackboard no longer accepts updates.")
                        # Resolve the generation first, then collect input without holding either mutex.
                        content = args.text if args.text is not None else Path(args.file).read_text() if args.file else sys.stdin.read()
                        live_project = lambda: live_project_guard(manager, project, board, args.expected_generation)
                        if args.board_action == "post":
                            options = {}
                            if args.kind is not None or args.reply_to is not None or args.task is not None:
                                options = {"kind": args.kind or "note", "parent_id": args.reply_to, "task_id": args.task}
                            result = board.post(args.agent, content, args.to, guard=live_project, **options)
                        elif args.board_action == "ask":
                            # Announce the question before waiting so a killed command can still resume it.
                            resume = [manager.helper_path(), "blackboard", "wait", "--selector", project["uuid"],
                                      "--expected-generation", args.expected_generation, "--reply-to"]
                            result = board.ask(args.agent, args.to, content, task_id=args.task, guard=live_project,
                                               timeout=args.timeout, poll_interval=args.poll_interval,
                                               posted=lambda question: print(
                                                   f"Question {question['id']} posted; resume with: {shlex.join([*resume, question['id']])}",
                                                   file=sys.stderr, flush=True))
                        elif args.board_action == "update":
                            result = board.update(args.agent, args.id, args.expected_version, content, kind=args.kind, guard=live_project)
                        else:
                            result = board.edit(args.agent, args.expected_revision, content, guard=live_project)
                        print(json.dumps(result, indent=2))
            elif args.command == "task":
                from .tasks import Tasks, FIELDS
                project = manager.project(args.selector)
                board = manager.board_for(project)
                if args.expected_generation is not None and args.expected_generation != board.generation:
                    raise ProjectError("Blackboard generation changed; this agent session is stale.")
                tasks = Tasks(board, project)
                action = args.task_action
                guard = lambda: task_project_guard(manager, project, board, args.expected_generation, tasks)
                if action in ("show", "list"):
                    result = tasks.read(getattr(args, "id", None), actor=args.agent,
                                        ready=getattr(args, "ready", False), blocked=getattr(args, "blocked", False),
                                        held=getattr(args, "held", False),
                                        **{field: getattr(args, field, None) for field in ("state", "assignee", "repo", "member")})
                elif action == "settle":
                    result = tasks.mutate(args.agent, args.id, args.expected_version, action,
                                          {field: getattr(args, field) for field in ("outcome", "summary", "files", "report", "to")}, guard)
                elif action == "dependency":
                    result = tasks.dependency(args.agent, args.id, args.prerequisite, args.expected_version,
                                              remove=args.dependency_action == "remove", guard=guard)
                else:
                    values = {field: getattr(args, field) for field in FIELDS if getattr(args, field, None) is not None}
                    if action == "update":
                        for field in ("repo", "member", "plan"):
                            if getattr(args, "clear_" + field):
                                if field in values:
                                    raise ProjectError(f"Cannot supply and clear {field} together.")
                                values[field] = None
                    if action == "assign":
                        if bool(args.assignee is not None) == args.unassign:
                            raise ProjectError("Assign requires exactly one of --assignee or --unassign.")
                        values["assignee"] = args.assignee
                    result = tasks.create(args.agent, values, guard) if action == "create" else tasks.mutate(args.agent, args.id, args.expected_version, action, values, guard)
                print(json.dumps(result, indent=2))
            elif args.command == "artifacts":
                manager.commit_artifacts(manager.artifact_project(args.selector), args.message, args.paths, push=not args.no_push)
            elif args.command == "abandon":
                manager.abandon(args.selector)
            elif args.command == "cleanup":
                manager.cleanup(args.selector, args.dry_run, args.allow_unmerged, force=args.force)
            elif args.command == "status":
                manager.status(args.selector, args.json, args.all)
            elif args.command == "handoff":
                manager.handoff(args.selector, args.agent, args.repo, args.member,
                                planned_member=args.planned_member, after=args.after, task=args.task)
            elif args.command == "list":
                for slot in SLOT_NAMES:
                    project = manager.state["projects"].get(slot)
                    print(f"{slot}: " + (f"{project['name']} [{project['state']}] " + ", ".join(r["id"] for r in project["repos"]) if project else "idle"))
            elif args.command == "path":
                try:
                    project = manager.project(args.selector)
                    repo_id = args.repo
                except NoProject:
                    if args.explicit_selector is not None or args.repo or not args.selector:
                        raise
                    project, repo_id = manager.project(), args.selector
                if not repo_id:
                    if args.member:
                        raise ProjectError("Specify a repository with --member.")
                    print(manager.slots / project["slot"])
                else:
                    repo_id = manager.resolve_repo(repo_id)
                    matches = [r for r in project["repos"] if r["id"] == repo_id]
                    if not matches:
                        raise ProjectError(f"Repository is not part of project: {repo_id}")
                    entry = manager.member_for(matches[0], args.member)
                    print(entry["path"])
        return 0
    except (ProjectError, BlackboardError, sqlite3.Error, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"baton: {error}", file=sys.stderr)
        # Lets status callers fall back to non-framework defaults outside any project.
        return 3 if args.command == "status" and (isinstance(error, NoProject)
                                                or (manager is not None and not manager.initialized())) else 1
