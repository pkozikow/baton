"""Server-rendered pages of the read-only web UI.

Every page escapes the values it shows with html.escape. Markdown travels escaped inside
<pre data-markdown> and is rendered in the browser with raw HTML disabled.
"""

from dataclasses import dataclass
import html
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
from typing import Any, Iterable
from urllib.parse import quote

from blackboard import BlackboardError
from ..artifacts import open_artifact, read_artifact, unsafe_artifact_path
from ..blackboard_sqlite import entry_text
from ..errors import ProjectError
from ..manifest import entries, entry_stack_root, member_name, subplan_directory
from ..review_records import RECORD, run_rows, saved_runs_safely, unsaved_runs
from ..slots import SLOT_NAMES
from . import prs

UUID = re.compile(r"[0-9a-f]{32}")
TEXT_LIMIT = 2 * 1024 * 1024
BOARD_TAIL = 50
SEARCH_LIMIT = 50
# Broader than markdown-it on purpose (any indentation, list markers, blockquotes, CRLF): over-matching only
# relaxes style-src on that page, while missing a fence would leave its diagram as code.
MERMAID_FENCE = re.compile(r"^(?:[ \t>]|[-*+][ \t]|\d+[.)][ \t])*(?:`{3,}|~{3,})[ \t]*mermaid(?:[ \t\r]|$)", re.MULTILINE)
SQLITE_MAGIC = b"SQLite format 3\x00"
# Hidden entries, live board files and SQLite databases with their sidecars are never listed or served.
HIDDEN_NAMES = re.compile(r"^(?:\..*|blackboard-lock\.md|blackboard\.sqlite.*|.+\.(?:sqlite3?|db)(?:-(?:wal|shm|journal))?)$",
                          re.IGNORECASE)
# Board reads take a shared flock; serializing them keeps request threads from stacking shared
# locks ahead of an agent waiting for the exclusive lock.
BOARD_LOCK = threading.Lock()


@dataclass
class Page:
    title: str
    body: str
    status: int = 200
    mermaid: bool = False
    fragment: bool = False
    location: str | None = None


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def link(href: str, text: Any) -> str:
    return f'<a href="{e(href)}">{e(text)}</a>'


def project_url(uuid: str, suffix: str = "") -> str:
    return f"/project/{uuid}{suffix}"


def file_url(uuid: str, relative: PurePosixPath | str) -> str:
    return project_url(uuid, "/files/" + quote(str(relative)))


def table(headers: Iterable[str], rows: Iterable[Iterable[str]], empty: str = "None.") -> str:
    """A table of already escaped cells."""
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        return f'<p class="muted">{e(empty)}</p>'
    return ("<table><thead><tr>" + "".join(f"<th>{e(header)}</th>" for header in headers)
            + f"</tr></thead><tbody>{body}</tbody></table>")


def markdown(text: str) -> str:
    return f'<pre class="markdown-source" data-markdown>{e(text)}</pre>'


def has_mermaid(text: str) -> bool:
    return bool(MERMAID_FENCE.search(text))


def error_page(status: int, title: str, message: str) -> Page:
    return Page(title, f"<h1>{e(title)}</h1><p class=\"error\">{e(message)}</p>", status=status)


def not_found(message: str = "Nothing is available at this address.") -> Page:
    return error_page(404, "Not found", message)


def not_initialized() -> Page:
    return error_page(503, "Baton is not initialized",
                      "No project registry is available. Run baton init with a trusted installation; "
                      "the web UI never creates or repairs the registry.")


def registry_ready(manager: Any) -> bool:
    """Whether the registry layout exists, checked before any call that could repair it."""
    try:
        return bool(manager.layout.is_dir())
    except (ProjectError, OSError):
        return False


def owner_label(project: dict[str, Any]) -> str:
    owner = project.get("owner")
    return str(owner.get("provider")) if isinstance(owner, dict) and owner.get("provider") else "unclaimed"


def repositories(project: dict[str, Any]) -> str:
    repos = project.get("repos")
    return ", ".join(str(repo.get("id")) for repo in repos if isinstance(repo, dict)) if isinstance(repos, list) else ""


def pending_command(manager: Any, project: dict[str, Any]) -> str | None:
    try:
        return manager.resume_command(project)
    except (KeyError, TypeError, AttributeError):
        return None


def archived_records(manager: Any) -> list[dict[str, Any]]:
    """Archived projects, newest first."""
    return list(reversed(list(manager.archived_projects())))


def overview(manager: Any) -> Page:
    rows = []
    for slot in SLOT_NAMES:
        project = manager.state["projects"].get(slot)
        if project is None:
            rows.append([e(slot), '<span class="muted">idle</span>', "", "", "", ""])
            continue
        command = pending_command(manager, project)
        rows.append([e(slot), link(project_url(project["uuid"]), project["name"]), e(project.get("state")),
                     e(repositories(project)), e(owner_label(project)),
                     f"<code>{e(command)}</code>" if command else ""])
    archived = archived_records(manager)
    archived_rows = [[link(project_url(project["uuid"]), project["name"]), e(str(project.get("completed_at") or "")[:10]),
                      e(repositories(project))] for project in archived]
    body = ("<h1>Projects</h1>" + search_form("", "all")
            + "<h2>Coordinator slots</h2>"
            + table(("Slot", "Project", "State", "Repositories", "Owner", "Pending operation"), rows)
            + f"<h2>Archived projects ({len(archived)})</h2>"
            + table(("Project", "Completed", "Repositories"), archived_rows, "No archived projects."))
    return Page("Projects", body)


def find_project(manager: Any, uuid: str) -> dict[str, Any] | None:
    if not UUID.fullmatch(uuid):
        return None
    try:
        project = manager.artifact_project(uuid)
    except ProjectError:
        return None
    return project if project.get("uuid") == uuid else None


def project_nav(project: dict[str, Any], current: str) -> str:
    uuid = project["uuid"]
    items = [("Status", project_url(uuid)), ("Board", project_url(uuid, "/board")),
             ("Reviews", project_url(uuid, "/reviews")), ("Files", project_url(uuid, "/files/"))]
    links = [f'<span class="current">{e(label)}</span>' if label == current else link(href, label)
             for label, href in items]
    state = "archived" if project.get("_archived") else project.get("state")
    return (f'<p class="crumbs">{link("/", "Projects")} / {link(project_url(uuid), project["name"])} '
            f'<span class="badge">{e(state)}</span></p><nav class="tabs">{" ".join(links)}</nav>')


def artifact_root(manager: Any, project: dict[str, Any]) -> Path | None:
    try:
        directory = manager.artifact_directory(project)
    except (KeyError, TypeError):
        return None
    return None if unsafe_artifact_path(directory) or not directory.is_dir() else directory


def artifact_links(project: dict[str, Any], directory: Path | None) -> str:
    if directory is None:
        return '<p class="muted">The artifact directory is unavailable.</p>'
    candidates: list[tuple[str, PurePosixPath]] = [("Plan", PurePosixPath("plan.md"))]
    for repo in project.get("repos", []):
        for entry in entries(repo, include_retired=True):
            if isinstance(entry.get("branch"), str):
                subplan = subplan_directory(directory, str(repo.get("id")), entry["branch"], project.get("branch_prefix")) / "plan.md"
                try:
                    candidates.append((f"Subplan {repo.get('id')} {entry['branch']}",
                                       PurePosixPath(subplan.relative_to(directory).as_posix())))
                except ValueError:
                    continue
    candidates += [("Retrospective", PurePosixPath("retro.md")), ("Follow-up context", PurePosixPath("follow-up.md"))]
    items = []
    seen = set()
    for label, relative in candidates:
        path = directory / relative
        if relative not in seen and path.is_file() and not path.is_symlink():
            seen.add(relative)
            items.append(f"<li>{link(file_url(project['uuid'], relative), label)} <code>{e(relative)}</code></li>")
    items.append(f"<li>{link(project_url(project['uuid'], '/files/'), 'All artifacts')}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def lineage(manager: Any, project: dict[str, Any]) -> list[tuple[str, str]]:
    rows = []
    known = {record.get("uuid"): record for record in [*manager.state["projects"].values(), *manager.archived_projects()]}
    predecessor = project.get("follow_up_from")
    if isinstance(predecessor, str):
        name = (known.get(predecessor) or {}).get("name", predecessor)
        rows.append(("Follow-up of", link(project_url(predecessor), name) if UUID.fullmatch(predecessor) else e(predecessor)))
    successors = [record for record in known.values() if record.get("follow_up_from") == project["uuid"]]
    if successors:
        rows.append(("Followed up by", ", ".join(link(project_url(record["uuid"]), record["name"]) for record in successors)))
    return rows


def recorded_entries(project: dict[str, Any]) -> tuple[list[list[str]], list[list[str]]]:
    current, retired = [], []
    for repo in project.get("repos", []):
        for entry in entries(repo, include_retired=True):
            member = member_name(entry) or "root"
            if entry.get("retirement_state") == "retired":
                retired.append([e(repo.get("id")), e(member), f"<code>{e(entry.get('branch'))}</code>",
                                e(entry.get("retirement_state")), f"<code>{e(entry.get('backup_ref') or '')}</code>"])
            else:
                current.append([e(repo.get("id")), e(member), f"<code>{e(entry.get('branch'))}</code>",
                                e("native stack" if entry_stack_root(repo, entry) else "independent"),
                                e(entry.get("step")), e(entry.get("retirement_state") or "")])
    return current, retired


def project_page(manager: Any, project: dict[str, Any]) -> Page:
    uuid = project["uuid"]
    archived = bool(project.get("_archived"))
    directory = artifact_root(manager, project)
    identity = [("Name", e(project["name"])), ("UUID", f"<code>{e(uuid)}</code>"),
                ("State", e("archived" if archived else project.get("state"))),
                ("Slot", e(project.get("slot"))), ("Owner", e(owner_label(project)))]
    owner = project.get("owner")
    if isinstance(owner, dict) and owner.get("session_id"):
        identity.append(("Owner session", f"<code>{e(owner.get('session_id'))}</code>"))
    previous = project.get("previous_names")
    if isinstance(previous, list) and previous:
        identity.append(("Previous names", e(", ".join(map(str, previous)))))
    if archived and project.get("completed_at"):
        identity.append(("Completed", e(project["completed_at"])))
    identity += lineage(manager, project)
    sections = []
    data = error = None
    if not archived:
        try:
            data = manager.status_data(project)
        except (ProjectError, OSError, ValueError, KeyError, TypeError) as caught:
            error = str(caught)
    if data is not None:
        command_text = (data["pending_operation"] or {}).get("command")
    else:
        command_text = None if archived else pending_command(manager, project)
    if command_text:
        identity.append(("Pending operation", f"<code>{e(command_text)}</code>"))
    sections.append("<dl>" + "".join(f"<dt>{e(label)}</dt><dd>{value}</dd>" for label, value in identity) + "</dl>")
    if error is not None:
        sections.append(f'<p class="error">Status unavailable: {e(error)}. Showing the registry record.</p>')
    recorded, retired = recorded_entries(project)
    if data is not None:
        rows = [[e(entry["repo"]), e(entry["member"] or "root"), f"<code>{e(entry['branch'])}</code>",
                 e("native stack" if entry["workflow"] == "native_stack" else "independent"),
                 e(entry["pr_base"]) + ("" if entry["base_reliable"] else ' <span class="error">(unreliable)</span>'),
                 e(entry["step"]), e({True: "needs rebase", False: "", None: "unknown"}[entry["needs_rebase"]])]
                for entry in data["entries"]]
        sections.append("<h2>Branches</h2>" + table(("Repository", "Member", "Branch", "Workflow", "PR base", "Step", "Rebase"), rows))
        if data["errors"]:
            sections.append("<ul class=\"error\">" + "".join(f"<li>{e(item)}</li>" for item in data["errors"]) + "</ul>")
    else:
        sections.append("<h2>Branches</h2>" + table(("Repository", "Member", "Branch", "Workflow", "Step", "Retirement"), recorded))
    if retired:
        sections.append("<h2>Retired members</h2>" + table(("Repository", "Member", "Branch", "Retirement", "Archive ref"), retired))
    sections.append("<h2>Artifacts</h2>" + artifact_links(project, directory))
    prs_url = project_url(uuid, "/prs")
    sections.append(f'<h2>Pull requests</h2><div data-pull-requests="{e(prs_url)}?fragment=1">'
                    f'{link(prs_url, "Look up pull requests")}</div>')
    return Page(project["name"], project_nav(project, "Status") + "".join(sections))


def board_page(manager: Any, project: dict[str, Any]) -> Page:
    nav = project_nav(project, "Board")
    info = project.get("blackboard")
    if project.get("_archived") or not info:
        directory = artifact_root(manager, project)
        items = []
        for name in ("overview.md", "history.md"):
            relative = PurePosixPath("coordination", name)
            if directory is not None and (directory / relative).is_file():
                items.append(f"<li>{link(file_url(project['uuid'], relative), 'Coordination ' + name.removesuffix('.md'))}</li>")
        note = ("The live board was sealed when the project was cleaned up." if project.get("_archived")
                else "This project has no live board.")
        body = f"<p>{e(note)}</p>" + ("<ul>" + "".join(items) + "</ul>" if items else '<p class="muted">No saved coordination files.</p>')
        return Page(f"{project['name']} board", nav + body)
    from ..tasks import Tasks
    try:
        with BOARD_LOCK:
            board = manager.board_for(project)
            snapshot = board.read(tail=BOARD_TAIL)
            tasks = Tasks(board, project).read()["tasks"]
            agents = board.agents()["agents"]
    except (BlackboardError, ProjectError, sqlite3.Error, OSError, ValueError) as error:
        return Page(f"{project['name']} board", nav + f'<p class="error">Board unavailable: {e(error)}</p>')
    titles = {task["id"]: task["title"] for task in tasks}
    task_rows = []
    for task in tasks:
        settlement = task.get("settlement") or {}
        blockers = ", ".join(f"{titles.get(blocker['id'], blocker['id'][:8])} ({blocker['state']})" for blocker in task["blockers"])
        task_rows.append([e(task["title"]), f"<code>{e(task['id'][:8])}</code>", e(task["state"]), e(task["assignee"] or ""),
                          e(task["priority"]), e(blockers), e("held" if task["held"] else ""),
                          e(f"{settlement.get('outcome')}: {settlement.get('summary')}" if settlement else "")])
    agent_rows = [[e(agent["agent"]), e(agent["verdict"]), e(f"{agent['idle_seconds']} s"), e(agent["last_write"]["time"]),
                   e(", ".join(task["title"] for task in agent["in_progress"])),
                   e(len(agent["unanswered_questions"]))] for agent in agents]
    posts = []
    texts = [snapshot["overview"]["body"]]
    for entry in reversed(snapshot["entries"]):
        text = entry_text(entry)
        texts.append(text)
        task = f" · task {titles.get(entry['task_id'], entry['task_id'])}" if entry["task_id"] else ""
        posts.append(f'<article class="post"><header>{e(entry["created_at"])} · <strong>{e(entry["author"])}</strong> → '
                     f'{e(entry["recipient"])} · {e(entry["kind"])}{e(task)} · <code>{e(entry["id"][:8])}</code></header>'
                     f"{markdown(text)}</article>")
    body = (nav + f'<p class="muted">Board sequence {e(snapshot["sequence"])}, status {e(snapshot["status"])}.</p>'
            + "<h2>Overview</h2>" + markdown(snapshot["overview"]["body"])
            + "<h2>Tasks</h2>" + table(("Task", "ID", "State", "Assignee", "Priority", "Blockers", "Held", "Settlement"), task_rows)
            + "<h2>Agents</h2>" + table(("Agent", "Verdict", "Idle", "Last write", "In progress", "Unanswered questions"), agent_rows)
            + f"<h2>Recent posts (last {BOARD_TAIL}, newest first)</h2>" + ("".join(posts) or '<p class="muted">No posts.</p>'))
    return Page(f"{project['name']} board", body, mermaid=any(map(has_mermaid, texts)))


def reviewer_links(directory: Path, project: dict[str, Any], record: dict[str, Any]) -> str:
    """Links to a saved run's record and each reviewer's saved output, by basename within the run's directory."""
    relative = PurePosixPath("reviews", record["id"])
    items = [link(file_url(project["uuid"], relative / RECORD), "record")]
    for reviewer in record["reviewers"]:
        label = f"{reviewer.get('provider')}: {reviewer.get('status')}"
        outputs = []
        for key in ("review", "failed_output"):
            name = PurePosixPath(str(reviewer.get(key) or "")).name
            saved = directory / relative / name
            if name and not name.startswith(".") and saved.is_file() and not saved.is_symlink():
                outputs.append(link(file_url(project["uuid"], relative / name), "review" if key == "review" else "failed output"))
        items.append(e(label) + (" (" + ", ".join(outputs) + ")" if outputs else ""))
    return "<br>".join(items)


def reviews_page(manager: Any, project: dict[str, Any]) -> Page:
    directory = artifact_root(manager, project)
    saved = [] if directory is None else saved_runs_safely(directory)
    records = {record["id"]: record for _path, record in saved}
    rows = []
    for row in reversed(run_rows(saved, unsaved_runs(project))):
        kind = str(row["kind"]) + (f"/{row['focus']}" if row["focus"] else "")
        is_saved = row["saved"]
        rows.append([f"<code>{e(row['id'])}</code>", e(kind), e(row["started"]), e(row["coverage"]), e(row["status"]),
                     "saved" if is_saved else f'unsaved<br><code class="muted">{e(row["path"])}</code>',
                     reviewer_links(directory, project, records[row["id"]]) if is_saved and directory else ""])
    body = (project_nav(project, "Reviews")
            + table(("Run", "Kind", "Started", "Reviewers", "Status", "Saved", "Outputs"), rows, "No review runs found."))
    return Page(f"{project['name']} reviews", body)


def safe_relative(path: str) -> PurePosixPath | None:
    """The artifact-relative path of a decoded URL path, or None when any segment is unsafe or hidden."""
    if path == "":
        return PurePosixPath()
    parts = path.removesuffix("/").split("/")
    if any(part in ("", ".", "..") or "\0" in part or "\\" in part or HIDDEN_NAMES.fullmatch(part) for part in parts):
        return None
    return PurePosixPath(*parts)


def crumbs(project: dict[str, Any], relative: PurePosixPath) -> str:
    items = [link(project_url(project["uuid"], "/files/"), "files")]
    for index, part in enumerate(relative.parts):
        prefix = PurePosixPath(*relative.parts[:index + 1])
        items.append(link(file_url(project["uuid"], prefix) + ("/" if index < len(relative.parts) - 1 else ""), part))
    return '<p class="crumbs">' + " / ".join(items) + "</p>"


def listing(project: dict[str, Any], relative: PurePosixPath, descriptor: int) -> str:
    folders, files = [], []
    with os.scandir(descriptor) as scan:
        for item in scan:
            if HIDDEN_NAMES.fullmatch(item.name) or item.is_symlink():
                continue
            if item.is_dir(follow_symlinks=False):
                folders.append(item.name)
            elif item.is_file(follow_symlinks=False):
                files.append((item.name, item.stat(follow_symlinks=False).st_size))
    rows = [[link(quote(name) + "/", name + "/"), ""] for name in sorted(folders)]
    rows += [[link(quote(name), name), e(f"{size:,} B")] for name, size in sorted(files)]
    return crumbs(project, relative) + table(("Name", "Size"), rows, "Empty directory.")


def files_page(manager: Any, project: dict[str, Any], path: str) -> Page:
    directory = artifact_root(manager, project)
    relative = safe_relative(path)
    if directory is None or relative is None:
        return not_found()
    try:
        descriptor = open_artifact(directory, relative)
    except (OSError, ValueError):
        return not_found()
    nav = project_nav(project, "Files")
    title = f"{project['name']}: {relative}" if relative.parts else f"{project['name']} files"
    try:
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            if path and not path.endswith("/"):
                return Page(title, "", status=301, location=file_url(project["uuid"], relative) + "/")
            return Page(title, nav + listing(project, relative, descriptor))
        if not stat.S_ISREG(mode):
            return not_found()
        if path.endswith("/"):
            # Served under a trailing slash, the file's relative links would resolve beneath it.
            return Page(title, "", status=301, location=file_url(project["uuid"], relative))
        size = os.fstat(descriptor).st_size
    finally:
        os.close(descriptor)
    try:
        data = read_artifact(directory, relative, limit=TEXT_LIMIT)
    except (OSError, ValueError):
        return not_found()
    if data.startswith(SQLITE_MAGIC):
        return not_found()
    head = nav + crumbs(project, relative)
    if len(data) > TEXT_LIMIT:
        return Page(title, head + f'<p class="notice">This file has {size:,} bytes, more than the '
                                  f'{TEXT_LIMIT // (1024 * 1024)} MiB view limit; it is not shown.</p>')
    try:
        text = data.decode("utf-8")
        if "\0" in text:
            raise UnicodeError("NUL byte")
    except UnicodeError:
        return Page(title, head + f'<p class="notice">This file has {size:,} bytes of binary or non-UTF-8 content; it is not shown.</p>')
    if relative.suffix.lower() in (".md", ".markdown"):
        return Page(title, head + markdown(text), mermaid=has_mermaid(text))
    return Page(title, head + f'<pre class="text">{e(text)}</pre>')


def search_form(query: str, state: str) -> str:
    options = "".join(f'<option value="{value}"{" selected" if value == state else ""}>{value}</option>'
                      for value in ("all", "active", "archived"))
    return (f'<form class="search" action="/search" method="get"><input type="search" name="q" value="{e(query)}" '
            f'placeholder="Keywords or &quot;exact phrase&quot;" aria-label="Search projects">'
            f'<select name="state" aria-label="Project state">{options}</select><button type="submit">Search</button></form>')


def search_page(manager: Any, query: str, state: str) -> Page:
    form = "<h1>Search</h1>" + search_form(query, state if state in ("all", "active", "archived") else "all")
    try:
        result = manager.search_projects(query, state=state, limit=SEARCH_LIMIT)
    except ProjectError as error:
        return Page("Search", form + f'<p class="error">{e(error)}</p>', status=400)
    items = []
    for project in result["results"]:
        uuid = project["uuid"]
        root = Path(project["artifacts_path"])
        evidence = []
        for match in project["matches"]:
            source = e(match["source"])
            if match["kind"] == "artifact" and UUID.fullmatch(uuid):
                try:
                    relative = PurePosixPath(Path(match["source"]).relative_to(root).as_posix())
                    source = link(file_url(uuid, relative), relative)
                except ValueError:
                    pass
            evidence.append(f"<li>{source}:{e(match['line'])} — {e(match['excerpt'])}</li>")
        name = link(project_url(uuid), project["name"]) if UUID.fullmatch(uuid) else e(project["name"])
        items.append(f'<li class="result">{name} <span class="badge">{e(project["state"])}</span> '
                     f'<code>{e(uuid)}</code> <span class="muted">{e(", ".join(project["repository_ids"]))}</span>'
                     f'<ul>{"".join(evidence)}</ul></li>')
    summary = f"<p>{e(result['total_matches'])} matching projects" + (f", showing {SEARCH_LIMIT}" if result["truncated"] else "") + ".</p>"
    warnings = "".join(f"<li>{e(warning)}</li>" for warning in result["coverage_warnings"])
    body = (form + summary + ("<ul class=\"results\">" + "".join(items) + "</ul>" if items else "")
            + f'<p class="muted">Not searched: {e(", ".join(result["coverage"]["excluded"]))}.</p>'
            + (f'<details><summary>Coverage warnings</summary><ul>{warnings}</ul></details>' if warnings else ""))
    return Page("Search", body)


def pull_requests_page(project: dict[str, Any], fragment: bool) -> Page:
    rows = []
    for pulls in prs.lookup(project):
        for branch, found in pulls.branches.items():
            if not found:
                rows.append([e(pulls.repo), f"<code>{e(branch)}</code>", '<span class="muted">no pull request</span>', "", ""])
            for pr in found:
                url = pr["url"]
                number = link(url, f"#{pr['number']}") if url.startswith("https://") else e(f"#{pr['number']}")
                rows.append([e(pulls.repo), f"<code>{e(branch)}</code>", number, e(pr["title"]), e(pr["state"].lower())])
        if pulls.unavailable is not None:
            rows.append([e(pulls.repo), "", f'<span class="muted">unavailable ({e(pulls.unavailable)})</span>', "", ""])
    content = table(("Repository", "Branch", "Pull request", "Title", "State"), rows, "No recorded branches.")
    if fragment:
        return Page("Pull requests", content, fragment=True)
    return Page(f"{project['name']} pull requests", project_nav(project, "") + "<h2>Pull requests</h2>" + content)
