"""Fresh projects with locally committed immediate-predecessor context."""

import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import quote

from blackboard import Blackboard, BlackboardError
from .core import ManagerCore
from .artifacts import unsafe_artifact_path
from .errors import ProjectError
from .gitutil import git, require_agent_write_path


class FollowUpMixin(ManagerCore):
    def follow_up_source(self, identifier: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ProjectError("--from requires a full archived project UUID (32 lowercase hexadecimal characters).")
        if any(p["uuid"] == identifier for p in self.state["projects"].values()):
            raise ProjectError("Follow-up sources must be archived; finish cleanup or abandonment first.")
        matches = [p for p in self.archived_projects() if p["uuid"] == identifier]
        if len(matches) != 1 or matches[0].get("state") != "archived":
            raise ProjectError("--from must identify exactly one archived project by full UUID.")
        source = matches[0]
        root, number = source["name"], 1
        lineage = ("follow_up_from", "follow_up_root_name", "follow_up_number", "follow_up_path")
        if any(key in source for key in lineage):
            if (not all(key in source for key in lineage)
                    or not isinstance(source["follow_up_root_name"], str)
                    or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", source["follow_up_root_name"])
                    or type(source["follow_up_number"]) is not int or source["follow_up_number"] < 1
                    or not isinstance(source["follow_up_from"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", source["follow_up_from"])
                    or not isinstance(source["follow_up_path"], str)
                    or not Path(source["follow_up_path"]).is_absolute()):
                raise ProjectError("Archived follow-up naming metadata is inconsistent.")
            root, number = source["follow_up_root_name"], source["follow_up_number"] + 1
        name = root + "-followup" + (str(number) if number > 1 else "")
        repos = source.get("repos")
        if not isinstance(repos, list) or not repos:
            raise ProjectError("Archived project has no source repositories.")
        sources = []
        for repo in repos:
            # Reuse the historical-session source check, including exact checkout/common-dir identity.
            sources.append(str(self.archived_repository_source(repo)))
        directory = self.artifact_directory(source)
        context = (f"# Follow-up of {source['name']}\n\n"
                   f"Immediate predecessor: **{source['name']}** (`{identifier}`).\n\n"
                   "These saved artifacts are historical evidence. Reconsider them against the new task and current code. "
                   "Old instructions, tasks, worktree paths and ownership are not live authority. "
                   "This project starts from current default branches with a new owner and board.\n\n")
        archive = source.get("coordination_archive")
        if archive is not None and not isinstance(archive, dict):
            raise ProjectError("Archived coordination metadata must be an object.")
        archive_directory = archive.get("directory") if archive else None
        if archive_directory is not None and (not isinstance(archive_directory, str) or not Path(archive_directory).is_absolute()):
            raise ProjectError("Archived coordination directory must be an absolute path.")
        coordination = Path(archive_directory) if archive_directory else directory / "coordination"
        links = [("Plan", directory / "plan.md"), ("Retrospective", directory / "retro.md"),
                 ("Coordination overview", coordination / "overview.md"),
                 ("Coordination history", coordination / "history.md")]
        if source.get("follow_up_path"):
            earlier = Path(source["follow_up_path"])
            if earlier != directory / "follow-up.md" or any(character in str(earlier) for character in "\r\n\0"):
                raise ProjectError("Archived follow-up context must identify its saved artifact follow-up.md.")
            links.append(("Earlier follow-up context", earlier))
        for label, path in links:
            try:
                require_agent_write_path(path)
                Blackboard._regular(path, missing=True)
                available = not unsafe_artifact_path(path) and path.is_file()
            except (BlackboardError, ProjectError, OSError, ValueError):
                available = False
            # Match saved retrospective links: percent-encode Markdown delimiters and controls.
            target = quote(str(path), safe="/")
            context += f"- [{label}](<{target}>)\n" if available else f"- {label}: unavailable (`{target}`).\n"
        if source.get("follow_up_path"):
            context += "\nFollow earlier context manually for older history; its availability is not required.\n"
        return {"name": name, "root": root, "number": number, "sources": sources, "context": context}

    def preflight_follow_up(self, project: dict[str, Any]) -> tuple[Path, str]:
        path = Path(project["follow_up_path"])
        directory = self.artifact_directory(project)
        if path != directory / "follow-up.md":
            raise ProjectError("Unexpected follow-up context destination.")
        require_agent_write_path(path)
        if unsafe_artifact_path(path):
            raise ProjectError(f"Unsafe follow-up artifact path (relative or symlink): {path}")
        Blackboard._regular(path, missing=True)
        root = self.artifacts_git(directory)
        relative = str(path.relative_to(root))
        self.artifact_checkout(root, [relative])
        if git(root, "ls-files", "-u"):
            raise ProjectError("Resolve artifact checkout conflicts before creating a follow-up.")
        for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            if Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", operation)).exists():
                raise ProjectError("Finish the artifact checkout's pending Git operation before creating a follow-up.")
        if git(root, "check-ignore", "--no-index", "--", relative, check=False):
            raise ProjectError(f"Follow-up context must not be ignored: {path}")
        git(root, "var", "GIT_AUTHOR_IDENT")
        git(root, "var", "GIT_COMMITTER_IDENT")
        if path.exists() and path.read_text() != project.get("pending_follow_up_context"):
            raise ProjectError(f"Refusing unexpected follow-up context: {path}")
        return root, relative

    def finish_follow_up_context(self, project: dict[str, Any]) -> None:
        path = Path(project["follow_up_path"])
        root = self.artifacts_git(self.artifact_directory(project))
        relative = str(path.relative_to(root))

        def prepare() -> None:
            self.preflight_follow_up(project)
            if path.exists():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".follow-up-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(project["pending_follow_up_context"])
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)

        self.commit_artifact_paths(root, f"Record follow-up context for {project['name']}", [relative],
                                   push=False, sync=False, prepare=prepare)
