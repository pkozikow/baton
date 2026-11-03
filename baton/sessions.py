"""Immutable project ownership and native owner-session launchers."""

from datetime import datetime, timezone
import contextlib
import json
from importlib.resources import files
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import uuid

from .core import ManagerCore
from .errors import ProjectError
from .gitutil import branch, common_dir, git, require_agent_write_path
from .manifest import project_entries, coordination_context, render_coordination_context
from .providers import PROVIDERS, Provider, UnsupportedProviderOperation, get_provider, scrubbed_environment


# Live databases, their sidecars and locks stay out of unattended evidence copies; the Markdown export
# and plans are the evidence.
LIVE_EVIDENCE_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".lock", ".tmp")
LIVE_EVIDENCE_NAMES = ("blackboard-lock.md", "retro.md", ".git")


def copy_retro_evidence(source: Path, destination: Path) -> None:
    """Copy saved project evidence into a private workspace without following symlinks."""
    def ignored(directory: str, names: list[str]) -> set[str]:
        return {name for name in names
                if (Path(directory) / name).is_symlink() or name in LIVE_EVIDENCE_NAMES
                or name.startswith(".snapshot-") or name.endswith(LIVE_EVIDENCE_SUFFIXES)}
    shutil.copytree(source, destination, ignore=ignored)


def relocate(value: Any, source: Path, destination: Path) -> Any:
    """Rewrite absolute paths under source to the same relative path under destination."""
    if isinstance(value, dict):
        return {key: relocate(item, source, destination) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, source, destination) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        try:
            return str(destination / Path(value).relative_to(source))
        except ValueError:
            return value
    return value


class SessionsMixin(ManagerCore):
    def session_project(self, selector: str | None) -> dict[str, Any]:
        if selector is not None:
            return self.project(selector)
        cwd = Path.cwd()
        root = git(cwd, "rev-parse", "--show-toplevel", check=False)
        if root:
            identity, current = common_dir(Path(root)), branch(Path(root))
            if current:
                for project in self.state["projects"].values():
                    if (current == project["branch"] and identity == common_dir(self.repo)) or any(
                        current == entry["branch"] and identity == Path(entry["common_dir"])
                        for entry in project_entries(project)
                    ):
                        return project
        raise ProjectError("--selector is required when run outside a project's branch.")

    def claim_owner(self, selector: str, provider: str, session_id: str | None) -> None:
        project = self.project(selector)
        variable = get_provider(provider).session_env
        if variable is not None and session_id is None:
            session_id = os.environ.get(variable)
        if not session_id:
            raise ProjectError("Supply the current session's UUID with --session-id"
                               + (f" ({provider} also accepts {variable})." if variable else "."))
        try:
            session_id = str(uuid.UUID(session_id))
        except ValueError:
            raise ProjectError("--session-id must be the current agent session's valid UUID.") from None
        owner = project.get("owner")
        if owner is not None and (owner["provider"], owner["session_id"]) != (provider, session_id):
            raise ProjectError(f"Project {project['name']} is already owned by "
                               f"{owner['provider']} session {owner['session_id']}; ownership cannot change.")
        if project["state"] not in ("active", "claiming"):
            raise ProjectError("Finish the outstanding project operation before claiming ownership.")
        if owner is None:
            self.check_coordinator(project)
            project["owner"] = {"provider": provider, "session_id": session_id,
                                "claimed_at": datetime.now(timezone.utc).isoformat()}
            # Save the immutable claim before materialization so a failed commit cannot let another session win.
            project["state"] = "claiming"
            self.save()
        if project["state"] == "claiming":
            with contextlib.redirect_stdout(sys.stderr):
                self.materialize(project)
        if project.get("follow_up_path"):
            print(f"Read follow-up context: {project['follow_up_path']}. Predecessor artifacts are historical evidence; reconsider them against the new task and current code.", file=sys.stderr)
        print(json.dumps(project["owner"], indent=2))

    def retro_snapshot(self, selector: str) -> dict[str, Any]:
        project = self.artifact_project(selector)
        directory = self.artifact_directory(project)
        return {"uuid": project["uuid"], "owner": project.get("owner"),
                "artifacts_path": str(directory), "retro_path": str(directory / "retro.md"),
                "archived": project.get("state") == "archived", "manifest": project}

    def validate_retro_directory(self, project: dict[str, Any]) -> Path:
        directory = self.artifact_directory(project)
        require_agent_write_path(directory)
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
            raise ProjectError(f"Artifact directory is unavailable or unsafe: {directory}")
        active = [json.loads(path.read_text()) for path in (self.layout / "projects").glob("*.json")]
        for other in (*active, *self.archived_projects()):
            if other.get("uuid") != project["uuid"] and self.artifact_directory(other).resolve() == directory.resolve():
                raise ProjectError("Artifact directory is shared by multiple project UUIDs; resolve it before running a retro.")
        return directory

    def retro_coordination(self, project: dict[str, Any], directory: Path) -> tuple[dict[str, Any], Path | None, int | None]:
        """Return the coordination context plus the readable board snapshot directory and sequence, if any."""
        context = coordination_context(project, directory)
        if not project.get("blackboard"):
            return context, None, None
        archived = project.get("state") == "archived"
        if archived and project.get("coordination_archive"):
            snapshot = project["coordination_archive"]
        elif not archived:
            info = project["blackboard"]
            names = ("blackboard.sqlite", "blackboard-lock.md")
            absent = not info.get("initialized") and not any((Path(info["directory"]) / name).exists() or (Path(info["directory"]) / name).is_symlink() for name in names)
            snapshot = None if absent else self.export_blackboard(project)
        else:
            snapshot = None
        if snapshot is None or snapshot.get("uninitialized"):
            return context, None, None
        evidence = Path(snapshot.get("directory") or directory / ".blackboard-export")
        return snapshot.get("context") or context, evidence, snapshot["sequence"]

    def retro_prompt(self, project: dict[str, Any], *, forked: bool = True, evidence: Path | None = None) -> str:
        """Build the retro launch prompt.

        forked: the session inherits the owner's history; otherwise it is a fresh session.
        evidence: unattended run; the prompt names only paths under this copy of the artifact
        directory. The caller copies the evidence after this call, since building the prompt
        refreshes the board export.
        """
        skill = files("baton").joinpath("skills", "baton-retro", "SKILL.md")
        if not skill.is_file():
            skill = Path(__file__).resolve().parents[1] / "agent-skills" / "baton-retro" / "SKILL.md"
        instructions = skill.read_text()
        if not instructions.strip():
            raise ProjectError(f"Retro skill is empty: {skill}")
        directory = self.artifact_directory(project)
        archived = project.get("state") == "archived"
        unattended = evidence is not None
        mode = (
            "This is an unattended retrospective. Your working directory is a private workspace holding "
            "identity.json and copies of the saved project evidence; read only those. Print the complete draft "
            "exactly once between the standalone lines BATON_RETRO_DRAFT_BEGIN and BATON_RETRO_DRAFT_END. "
            "That is your only output channel: do not create or modify any file. The trusted framework will "
            "verify its UUID, publish it without overwriting an existing artifact, and commit it. Do not run "
            "artifact commit yourself. Do not ask questions or wait for input; document evidence gaps. Do not "
            "write or modify the artifact checkout, project registry, code repositories, or coordinator. Read "
            "the supplied identity.json instead of running framework metadata commands. "
            if unattended else
            "This invocation authorizes creating/updating retro.md, archiving relevant project notes "
            "and archive-skipped.txt, and committing only those artifacts. "
        )
        location = "copied evidence directory" if unattended else "artifact directory"
        historical = (
            "The project is archived. The manifest below is its frozen identity/evidence snapshot. "
            "Its slot and code worktree paths may now belong to another project: do not run baton "
            "status, read current worktree notes, or use those paths as current project evidence. "
            f"Use {'inherited owner history and ' if forked else ''}already saved plans, notes, and board in the "
            f"{location}. Missing pre-cleanup evidence is a limitation, not permission to inspect another "
            "project. Never resurrect the project or claim ownership. "
            if archived else (
                f"Verify identity with baton retro show --selector {project['uuid']} --json before writing. "
                if not unattended else ""
            )
        )
        context, snapshot, sequence = self.retro_coordination(project, directory)
        manifest = project
        if evidence is not None:
            context = relocate(context, directory, evidence)
            snapshot = Path(relocate(str(snapshot), directory, evidence)) if snapshot else None
            manifest = relocate(project, directory, evidence)
        coordination = ""
        if snapshot is not None:
            coordination = (f"Coordination Markdown evidence: {snapshot / 'overview.md'} and {snapshot / 'history.md'}. "
                            f"Snapshot sequence: {sequence}. Read Markdown for analysis; the final closure snapshot may be later. "
                            "Read task assignments, priorities, prerequisites, cancelled blockers and unresolved work in that same Markdown snapshot; "
                            "only done satisfies dependencies, and sealing does not complete tasks. "
                            "Do not edit or require access to the SQLite database.\n")
        session = (
            "You are a retrospective fork of the project's owner session, not the owner. "
            if forked else
            "You are a fresh retrospective session for this project, not the owner. The owner's provider cannot "
            "fork its session, so you have no owner history: rely only on the saved plans, notes, blackboard "
            "export, and identity named below. "
        )
        return (
            "Use $baton-retro to conduct the retrospective now. " + session +
            "Do not claim or replace ownership, change code "
            "or project topology, or direct implementation workers. " + mode + historical +
            "Decide whether to analyze alone or delegate sections; integrate and own the final document.\n\n"
            f"Selected project: {project['name']}\nProject UUID: {project['uuid']}\n"
            f"Project selector: {project['uuid']}\nImmutable owner: {json.dumps(project.get('owner'))}\n"
            f"Project artifacts directory (evidence): {evidence or directory}\n"
            + render_coordination_context(context) + coordination
            + ("" if unattended else f"Final retrospective: {directory / 'retro.md'}\n") +
            f"Frozen manifest: {json.dumps(manifest, sort_keys=True)}\n\n"
            "Use this selected identity even if "
            + ("inherited history" if forked else "saved evidence") +
            " names an old path or another project. "
            "Include the full project UUID in the Outcome section. "
            f"Read and follow the complete skill supplied here ({skill}):\n\n{instructions}"
        )

    def retro(self, selector: str, *, non_interactive: bool = False, timeout: int = 1800) -> int:
        if non_interactive:
            result = self.retro_noninteractive(selector, timeout=timeout)
            print(json.dumps(result, indent=2))
            return result.get("returncode", 0)
        project = self.artifact_project(selector)
        self.validate_retro_directory(project)
        owner = self.retro_owner(project)
        provider = PROVIDERS[owner["provider"]]
        prompt = self.retro_prompt(project, forked=provider.supports_fork)
        if project.get("state") != "archived":
            return self.launch_owner(self.selector_for(project), "fork", prompt=prompt)
        directory = self.artifact_directory(project)
        if not directory.is_dir() or directory.is_symlink():
            raise ProjectError(f"Archived artifact directory is unavailable or unsafe: {directory}")
        command = (provider.fork(owner["session_id"], directory, prompt) if provider.supports_fork
                   else provider.start(directory, prompt))
        return subprocess.run(command, cwd=directory, env=scrubbed_environment()).returncode

    @staticmethod
    def retro_owner(project: dict[str, Any], *, context: str = "retrospective session") -> dict[str, str]:
        owner = project.get("owner")
        if not owner:
            # An active project can still be claimed; an archived one cannot.
            remedy = ("the first agent must run baton session claim" if project.get("state") == "active"
                      else f"cannot start its {context}")
            raise ProjectError(f"Project {project['name']} has no owner; {remedy}.")
        if not isinstance(owner, dict) or owner.get("provider") not in PROVIDERS:
            raise ProjectError(f"The recorded owner has no supported provider ({', '.join(PROVIDERS)}).")
        if not isinstance(owner.get("session_id"), str):
            raise ProjectError("The recorded owner has no valid session UUID.")
        try:
            uuid.UUID(owner["session_id"])
        except (KeyError, ValueError, TypeError):
            raise ProjectError("The recorded owner has no valid session UUID.") from None
        return owner

    def retro_noninteractive(self, selector: str, *, timeout: int = 1800) -> dict[str, Any]:
        from .retro_automation import create_draft_workspace, execute_retro, PublicationError
        project = self.artifact_project(selector)
        if project.get("state") not in ("active", "archived"):
            raise ProjectError("Finish the outstanding project operation before launching the retro.")
        owner = self.retro_owner(project)
        provider: Provider = PROVIDERS[owner["provider"]]
        session_id = owner["session_id"] if provider.supports_fork else None
        directory = self.validate_retro_directory(project)
        workspace = create_draft_workspace()
        try:
            draft = workspace / "retro.md"
            source_cli = Path(__file__).resolve().parents[1] / "bin" / "baton"
            launcher = source_cli if source_cli.is_file() else Path(sys.executable).parent / "baton"
            environment = scrubbed_environment(dict(os.environ, BATON_CLI=str(launcher)))

            def publish(source: Path) -> None:
                # Resolve again by immutable UUID. Names and slots may have been reused while the fork ran.
                current = self.artifact_project(project["uuid"])
                if self.validate_retro_directory(current) != directory or current.get("owner") != project.get("owner"):
                    raise ProjectError("Project artifact identity or owner changed during the retrospective.")
                data = source.read_bytes()
                with (directory / "retro.md").open("xb") as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    self.commit_artifacts(current, "Write project retrospective", [str(directory / "retro.md")])
                except ProjectError as error:
                    raise PublicationError(str(error)) from error

            def retry_publish() -> None:
                current = self.artifact_project(project["uuid"])
                if self.validate_retro_directory(current) != directory or current.get("owner") != project.get("owner"):
                    raise ProjectError("Project artifact identity or owner changed during the retrospective.")
                self.commit_artifacts(current, "Write project retrospective", [str(directory / "retro.md")])

            try:
                # Resolve support first so a blocked provider exports and copies no evidence.
                provider.exec_unattended(session_id, workspace, "")
            except UnsupportedProviderOperation as error:
                result: dict[str, Any] = {
                    "uuid": project["uuid"], "status": "blocked", "returncode": 1, "provider": provider.name,
                    "error": f"Unattended retrospective unsupported for provider {provider.name}: {error}"}
            else:
                # Only the copies are named: identity.json and the prompt never point at live artifacts.
                evidence = workspace / "evidence"
                identity_path = workspace / "identity.json"
                snapshot = relocate(self.retro_snapshot(project["uuid"]), directory, evidence)
                identity_path.write_text(json.dumps(snapshot, indent=2) + "\n")
                prompt = (f"Identity snapshot: {identity_path}\n\n"
                          + self.retro_prompt(project, forked=provider.supports_fork, evidence=evidence))
                copy_retro_evidence(directory, evidence)
                command, delivery = provider.exec_unattended(session_id, workspace, prompt)
                result = execute_retro(self.layout, project, directory / "retro.md", command, workspace, prompt,
                                       timeout=timeout, draft=draft, publish=publish, retry_publish=retry_publish,
                                       environment=environment, managed_workspace=True, prompt_delivery=delivery)
                result.update(provider=provider.name, read_confinement=provider.read_confinement)
        except BaseException:
            with contextlib.suppress(OSError):
                (workspace / ".failed").touch()
            raise
        if result.get("status") in ("completed", "complete", "running", "blocked"):
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                (workspace / ".failed").touch()
        return result

    def archived_fork_source(self, project: dict[str, Any]) -> Path:
        repos = project.get("repos")
        if not isinstance(repos, list) or not repos or not isinstance(repos[0], dict):
            raise ProjectError("Archived project has no valid first repository; cannot select its source checkout.")
        for repo in repos[1:]:
            self.archived_repository_source(repo, verify_checkout=False)
        return self.archived_repository_source(repos[0])

    @staticmethod
    def archived_repository_source(repo: dict[str, Any], *, verify_checkout: bool = True) -> Path:
        if not isinstance(repo, dict) or any(
            not isinstance(repo.get(key), str) or not repo[key] for key in ("id", "source", "common_dir")
        ):
            raise ProjectError("Archived repository lacks its ID, source checkout or common_dir identity.")
        source, identity = Path(repo["source"]), Path(repo["common_dir"])
        if not verify_checkout:
            return source
        if not source.is_absolute() or not identity.is_absolute() or not source.is_dir():
            raise ProjectError(f"Archived source checkout is unavailable: {source}; restore the recorded checkout.")
        root = git(source, "rev-parse", "--show-toplevel", check=False)
        if not root or Path(root).resolve() != source.resolve():
            raise ProjectError(f"Archived source is not an exact Git checkout root: {source}; restore the recorded checkout.")
        if common_dir(source) != identity.resolve():
            raise ProjectError(f"Archived source repository identity changed: {source}; restore the recorded repository.")
        return source

    def archived_fork_prompt(self, project: dict[str, Any], *, forked: bool) -> str:
        directory = self.artifact_directory(project)
        session = (
            "You are an exploratory fork with inherited owner history, not the owner. " if forked else
            "You are a fresh exploratory session, not the owner. The owner's provider cannot fork, "
            "so you have no owner history; rely on saved artifacts. "
        )
        return (
            session + "The project is archived and must remain closed. Answer questions using "
            "inherited history when available and the saved project artifacts. Do not claim ownership, "
            "reopen the project, direct workers, or change project or code state unless the user explicitly "
            "authorizes changes. The current source checkout may have evolved and is not the historical "
            "project snapshot. Old slot and worktree paths in history or the frozen manifest are historical: "
            "do not use them as current evidence or read a live coordinator's project.json. "
            "Missing saved evidence is a limitation, not permission to inspect another project. "
            "Wait for the user's questions.\n\n"
            f"Selected project: {project['name']}\nProject UUID: {project['uuid']}\nState: archived\n"
            f"Immutable owner: {json.dumps(project.get('owner'))}\n"
            f"Project artifacts directory: {directory}\nMaster plan: {directory / 'plan.md'}\n"
            + f"Frozen coordination archive: {json.dumps(project.get('coordination_archive'))}\n"
            + f"Frozen manifest: {json.dumps(project, sort_keys=True)}\n"
        )

    def launch_owner(self, selector: str, action: str, *, prompt: str | None = None) -> int:
        project = self.artifact_project(selector) if action == "fork" else self.project(selector)
        if project.get("state") == "archived":
            owner = self.retro_owner(project, context="exploratory session")
            source = self.archived_fork_source(project)
            provider = get_provider(owner["provider"])
            prompt = prompt or self.archived_fork_prompt(project, forked=provider.supports_fork)
            command = (provider.fork(owner["session_id"], source, prompt) if provider.supports_fork
                       else provider.start(source, prompt))
            return subprocess.run(command, cwd=source, env=scrubbed_environment()).returncode
        if project["state"] != "active":
            raise ProjectError("Finish the outstanding project operation before launching the owner session.")
        owner = project.get("owner")
        if owner is None:
            raise ProjectError(f"Project {project['name']} has no owner; the first agent must run baton session claim.")
        coordinator = self.check_coordinator(project)
        provider, session_id = get_provider(owner["provider"]), owner["session_id"]
        if action == "resume":
            command = provider.resume(session_id, coordinator)
        else:
            if prompt is None:
                prompt = (
                    f"You are an exploratory fork of project {project['name']} (UUID {project['uuid']}). "
                    f"The immutable owner is {provider.name} session {session_id}. You are not the owner, "
                    "even though you inherited its conversation history. "
                    if provider.supports_fork else
                    f"You are an exploratory session for project {project['name']} (UUID {project['uuid']}). "
                    f"The immutable owner is {provider.name} session {session_id}. You are a fresh session, not "
                    "the owner: the owner's provider cannot fork its session, so no owner history is available. "
                    "Use the project's saved plans, notes, and blackboard for context. "
                ) + (
                    "Do not claim ownership or act as the owner. "
                    "Answer questions and explore scenarios. Keep project files, metadata, and workers unchanged "
                    "unless the user explicitly authorizes changes. Read the project's project.json and agent "
                    "instructions before any authorized work. Wait for the user's questions."
                )
            command = (provider.fork(session_id, coordinator, prompt) if provider.supports_fork
                       else provider.start(coordinator, prompt))
        return subprocess.run(command, cwd=coordinator, env=scrubbed_environment()).returncode
