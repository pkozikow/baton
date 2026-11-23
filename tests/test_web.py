import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from baton.artifacts import NonRegularArtifact, read_artifact
from baton.web import prs, server
import test_project_manager as fixtures

VENDOR = Path(server.__file__).resolve().parent / "static" / "vendor"
STRICT_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class WebTest(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot
    fake_github_cli = fixtures.ProjectManagerTest.fake_github_cli

    def setUp(self) -> None:
        fixtures.ProjectManagerTest.setUp(self)
        # Unsaved review runs are discovered in TMPDIR; never let the developer's real runs appear.
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.env["TMPDIR"] = str(self.tmp)
        source = self.make_repo("alpha")
        github_url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", github_url)
        self.git(source, "remote", "set-url", "origin", github_url)
        self.create("alpha")
        self.project = fixtures.read_state(self.projects)["projects"]["p3"]
        self.uuid = self.project["uuid"]
        self.directory = Path(self.project["artifacts_path"])
        self.port = 0

    def serve(self, *, override: bool = False) -> None:
        """Run the real server in this process with the fixture environment, by default without the write override."""
        env = dict(self.env)
        if not override:
            env.pop("BATON_ALLOW_UNTRUSTED_WRITES")
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        httpd = server.WebServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_port

    def request(self, path: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                timeout: float = 60) -> tuple[int, http.client.HTTPMessage, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read().decode()
        finally:
            connection.close()

    def page(self, path: str, status: int = 200) -> str:
        code, _headers, body = self.request(path)
        self.assertEqual(code, status, body)
        return body

    def archive(self, name: str, uuid: str, completed: str, index: int, **extra: Any) -> dict[str, Any]:
        directory = self.artifacts / f"{name}-{uuid[:8]}"
        directory.mkdir(exist_ok=True)
        record = {**self.project, "name": name, "uuid": uuid, "artifacts_path": str(directory), "slot": "p5",
                  "completed_at": completed, "blackboard": None, **extra}
        path = self.projects / ".git" / "baton" / "v2" / "archive" / f"{index:06d}-{uuid}.json"
        path.write_text(json.dumps(record))
        return record

    def board(self) -> tuple[str, ...]:
        generation = fixtures.read_state(self.projects)["projects"]["p3"]["blackboard"]["generation"]
        return ("--selector", "p3", "--expected-generation", generation)

    def board_state(self) -> tuple[int, str]:
        snapshot = json.loads(self.cli("blackboard", "read", "--selector", "p3", "--json", "--tail", "0").stdout)
        return snapshot["sequence"], snapshot["revision"]

    def test_overview_lists_active_slot_and_archived_newest_first(self) -> None:
        self.archive("older", "a" * 32, "2026-01-01T00:00:00+00:00", 1)
        self.archive("newer", "b" * 32, "2026-02-01T00:00:00+00:00", 2)
        self.serve()
        body = self.page("/")
        self.assertIn(f'href="/project/{self.uuid}">example</a>', body)
        self.assertIn("p3", body)
        self.assertIn("Archived projects (2)", body)
        self.assertLess(body.index(">newer</a>"), body.index(">older</a>"))
        self.assertIn("2026-02-01", body)

    def test_project_pages_for_active_and_archived_projects(self) -> None:
        state = fixtures.read_state(self.projects)["projects"]["p3"]
        state["repos"][0]["members"] = [{"branch": "baton/example--old", "member": "old", "retirement_state": "retired",
                                         "path": str(self.worktrees / "alpha" / "example--old"), "step": "done",
                                         "backup_ref": "refs/project-archive/old"}]
        state["state"] = "adding"
        state["pending_add"] = ["beta"]
        fixtures.state_file(self.projects).write_text(json.dumps(state))
        (self.directory / "plan.md").write_text("# Plan\n")
        self.archive("finished", "c" * 32, "2026-03-01T00:00:00+00:00", 1, previous_names=["earlier"],
                     follow_up_from=self.uuid)
        self.serve()
        body = self.page(f"/project/{self.uuid}")
        self.assertIn("baton/example", body)
        self.assertIn("Retired members", body)
        self.assertIn("baton/example--old", body)
        self.assertIn("baton add beta", body)
        self.assertIn(f'href="/project/{self.uuid}/files/plan.md"', body)
        self.assertIn(f'href="/project/{"c" * 32}">finished</a>', body)
        self.assertIn(f'data-pull-requests="/project/{self.uuid}/prs?fragment=1"', body)
        archived = self.page(f"/project/{'c' * 32}")
        self.assertIn("archived", archived)
        self.assertIn("earlier", archived)
        self.assertIn(f'href="/project/{self.uuid}">example</a>', archived)
        self.assertIn("2026-03-01", archived)
        code, headers, _body = self.request(f"/project/{self.uuid}/")
        self.assertEqual((code, headers["Location"]), (301, f"/project/{self.uuid}"))
        for missing in ("/project/" + "d" * 32, "/project/example", "/project/" + self.uuid + "/unknown"):
            self.page(missing, 404)
        # A status failure before per-repository handling still shows the registry record.
        os.environ["BATON_NOTE_DIRS"] = "/absolute"
        failed = self.page(f"/project/{self.uuid}")
        self.assertIn("Status unavailable", failed)
        self.assertIn("BATON_NOTE_DIRS", failed)
        self.assertIn("baton/example--old", failed)

    def test_board_shows_posts_tasks_and_held_tasks_and_archived_coordination(self) -> None:
        board = self.board()
        self.cli("blackboard", "post", *board, "--agent", "owner", "--text", "Status <b>update</b> posted")
        worker = (*board, "--agent", "worker")
        self.cli("task", "create", "--title", "Open task", *worker)
        held = json.loads(self.cli("task", "create", "--title", "Held task", *worker).stdout)
        self.cli("task", "claim", "--id", held["id"], "--expected-version", "1", *worker)
        self.cli("task", "settle", "--id", held["id"], "--expected-version", "2", "--outcome", "failed",
                 "--summary", "Blocked on review", *worker)
        self.make_repo("beta")
        self.create("beta", slot="p4", name="old")
        old = fixtures.read_state(self.projects)["projects"]["p4"]
        self.cli("cleanup", "p4")
        self.serve()
        body = self.page(f"/project/{self.uuid}/board")
        self.assertIn("Status &lt;b&gt;update&lt;/b&gt; posted", body)
        self.assertIn("Open task", body)
        self.assertIn("Held task", body)
        self.assertIn("held", body)
        self.assertIn("failed: Blocked on review", body)
        self.assertIn("worker", body)
        archived = self.page(f"/project/{old['uuid']}/board")
        self.assertIn(f'href="/project/{old["uuid"]}/files/coordination/overview.md"', archived)
        self.assertIn(f'href="/project/{old["uuid"]}/files/coordination/history.md"', archived)
        self.assertIn("Coordination: old", self.page(f"/project/{old['uuid']}/files/coordination/overview.md"))

    def test_reviews_link_saved_outputs_and_list_unsaved_runs_without_links(self) -> None:
        saved_id, unsaved_id = "20261123T205223Z-plan-saved1", "20261123T210000Z-implementation-unsaved1"
        run = self.directory / "reviews" / saved_id
        run.mkdir(parents=True)
        (run / "example-plan-review-claude.md").write_text("# Review\nLooks good.\n")
        (run / "run.json").write_text(json.dumps({
            "id": saved_id, "kind": "plan", "status": "succeeded", "started": "2026-11-23T20:52:23+00:00",
            "project": {"uuid": self.uuid, "name": "example"},
            "reviewers": [{"provider": "claude", "status": "succeeded",
                           "review": "/private/tmp/plan-review.x/example-plan-review-claude.md"},
                          {"provider": "codex", "status": "failed", "failed_output": "/tmp/gone/codex.md.failed"}]}))
        unsaved = self.tmp / "peer-review.abc123"
        unsaved.mkdir()
        (unsaved / "example-review-claude.md").write_text("unsaved output\n")
        (unsaved / "run.json").write_text(json.dumps({
            "id": unsaved_id, "kind": "implementation", "focus": "security", "status": "succeeded",
            "started": "2026-11-23T21:00:00+00:00", "project": {"uuid": self.uuid, "name": "example"},
            "reviewers": [{"provider": "claude", "status": "succeeded", "review": str(unsaved / "example-review-claude.md")}]}))
        self.serve()
        body = self.page(f"/project/{self.uuid}/reviews")
        review = f"/project/{self.uuid}/files/reviews/{saved_id}/example-plan-review-claude.md"
        self.assertIn(f'href="{review}"', body)
        self.assertIn(f'href="/project/{self.uuid}/files/reviews/{saved_id}/run.json"', body)
        self.assertIn("1 of 2", body)
        self.assertIn("implementation/security", body)
        self.assertIn(str(unsaved), body)
        self.assertNotIn("example-review-claude.md", body)
        self.assertNotIn("codex.md.failed", body)
        self.assertIn("Looks good.", self.page(review))

    def test_reviews_skip_unsafe_or_malformed_saved_runs(self) -> None:
        reviews = self.directory / "reviews"
        reviews.mkdir()
        outside = self.root / "outside-run"
        outside.mkdir()

        def record(run_id: str, **extra: Any) -> str:
            return json.dumps({"id": run_id, "kind": "plan", "status": "succeeded", "started": "2026-11-23T20:00:00+00:00",
                               "reviewers": [{"provider": "claude", "status": "succeeded"}], **extra})

        good = "20261123T200000Z-plan-good"
        (reviews / good).mkdir()
        (reviews / good / "run.json").write_text(record(good))
        linked_dir = "20261123T200001Z-plan-linkeddir"
        (outside / "run.json").write_text(record(linked_dir))
        (reviews / linked_dir).symlink_to(outside)
        linked_record = "20261123T200002Z-plan-linkedrecord"
        (reviews / linked_record).mkdir()
        (outside / "record.json").write_text(record(linked_record))
        (reviews / linked_record / "run.json").symlink_to(outside / "record.json")
        fifo = "20261123T200003Z-plan-fifo"
        (reviews / fifo).mkdir()
        os.mkfifo(reviews / fifo / "run.json")
        malformed = {"20261123T200004Z-plan-notjson": "{", "20261123T200005Z-plan-list": "[]",
                     "20261123T200006Z-plan-reviewer": record("20261123T200006Z-plan-reviewer", reviewers=["claude"]),
                     "20261123T200007Z-plan-renamed": record("20261123T200000Z-plan-other")}
        for name, content in malformed.items():
            (reviews / name).mkdir()
            (reviews / name / "run.json").write_text(content)
        directory_record = "20261123T200008Z-plan-directory"
        (reviews / directory_record / "run.json").mkdir(parents=True)
        self.serve()
        code, _headers, body = self.request(f"/project/{self.uuid}/reviews", timeout=10)
        self.assertEqual(code, 200, body)
        self.assertIn(good, body)
        for skipped in (linked_dir, linked_record, fifo, *malformed, directory_record, "20261123T200000Z-plan-other"):
            self.assertNotIn(skipped, body)

    def test_read_artifact_closes_the_descriptor_of_a_non_regular_target(self) -> None:
        (self.directory / "nested").mkdir()
        before = len(os.listdir("/dev/fd"))
        for _ in range(20):
            with self.assertRaises(NonRegularArtifact):
                read_artifact(self.directory, PurePosixPath("nested"))
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_file_views_and_containment(self) -> None:
        (self.directory / "plan.md").write_text("# Plan\n\n<script>alert(1)</script> [x](javascript:alert(1))\n")
        (self.directory / "notes.txt").write_text("plain <text>\n")
        nested = self.directory / "branches" / "alpha"
        nested.mkdir(parents=True)
        (nested / "plan.md").write_text("# Subplan\n")
        (self.directory / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret evidence\n")
        (self.directory / "linked.md").symlink_to(outside / "secret.md")
        (self.directory / "linked-dir").symlink_to(outside)
        hidden = self.directory / ".hidden"
        hidden.mkdir()
        (hidden / "note.md").write_text("hidden evidence\n")
        with sqlite3.connect(self.directory / "data.db") as connection:
            connection.execute("CREATE TABLE t (x)")
        shutil.copyfile(self.directory / "data.db", self.directory / "innocent.txt")
        self.serve()
        base = f"/project/{self.uuid}/files"
        plan = self.page(f"{base}/plan.md")
        self.assertIn("<pre class=\"markdown-source\" data-markdown>", plan)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", plan)
        self.assertNotIn("<script>alert", plan)
        self.assertIn("plain &lt;text&gt;", self.page(f"{base}/notes.txt"))
        listing = self.page(f"{base}/")
        self.assertIn('href="branches/"', listing)
        self.assertIn('href="plan.md"', listing)
        for absent in ("linked", ".hidden", "blackboard", "data.db"):
            self.assertNotIn(absent, listing)
        code, headers, _body = self.request(f"{base}/branches")
        self.assertEqual((code, headers["Location"]), (301, f"{base}/branches/"))
        self.assertIn('href="alpha/"', self.page(f"{base}/branches/"))
        self.assertIn("Subplan", self.page(f"{base}/branches/alpha/plan.md"))
        self.assertIn("binary or non-UTF-8", self.page(f"{base}/image.bin"))
        for probe in ("../outside/secret.md", "%2e%2e/%2e%2e/outside/secret.md", "branches/../../outside/secret.md",
                      "%252e%252e/outside/secret.md", "/etc/passwd", "%2Fetc%2Fpasswd", ".hidden/note.md",
                      "%2ehidden/note.md", "linked.md", "linked-dir/secret.md", "blackboard.sqlite",
                      "blackboard.sqlite-wal", "blackboard-lock.md", "data.db", "innocent.txt", "missing.md",
                      "plan.md%00.txt"):
            with self.subTest(probe=probe):
                code, _headers, body = self.request(f"{base}/{probe}")
                self.assertIn(code, (400, 404))
                self.assertNotIn("secret evidence", body)
                self.assertNotIn("hidden evidence", body)

    def test_search_links_matching_artifact_and_reports_bad_queries(self) -> None:
        (self.directory / "plan.md").write_text("Rotation of refresh tokens.\n")
        self.serve()
        body = self.page("/search?q=%22refresh+tokens%22")
        self.assertIn(f'href="/project/{self.uuid}/files/plan.md"', body)
        self.assertIn(f'href="/project/{self.uuid}">example</a>', body)
        self.assertIn("1 matching projects", body)
        self.assertIn("0 matching projects", self.page("/search?q=absent-term&state=archived"))
        for query in ("/search", "/search?q=", "/search?q=%22unclosed", "/search?q=x&state=bogus"):
            with self.subTest(query=query):
                self.assertIn('class="error"', self.page(query, 400))

    def test_pull_requests_fragment_and_unavailable_lookups(self) -> None:
        marker = self.fake_github_cli([{"number": 7, "title": "Add <feature>", "state": "OPEN",
                                        "url": "https://github.com/test/alpha/pull/7"}])
        self.serve()
        os.environ["PATH"] = self.env["PATH"]
        body = self.page(f"/project/{self.uuid}/prs?fragment=1")
        self.assertNotIn("<html", body)
        self.assertIn('href="https://github.com/test/alpha/pull/7"', body)
        self.assertIn("Add &lt;feature&gt;", body)
        self.assertEqual(marker.read_text().splitlines(), ["pr", "list", "--repo", "github.com/test/alpha", "--head",
                                                           "baton/example", "--state", "all", "--limit", "20",
                                                           "--json", "number,title,state,url"])
        self.assertIn("<html", self.page(f"/project/{self.uuid}/prs"))
        self.fake_github_cli({"message": "failure"}, exit_code=1)
        self.assertIn("unavailable (lookup failed)", self.page(f"/project/{self.uuid}/prs?fragment=1"))
        tools = self.root / "only-git"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
        os.environ["PATH"] = str(tools)
        self.assertIn("unavailable (gh is not installed)", self.page(f"/project/{self.uuid}/prs?fragment=1"))

    def test_methods_hosts_cross_site_and_security_headers(self) -> None:
        self.serve()
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "BREW"):
            with self.subTest(method=method):
                code, headers, _body = self.request("/", method=method)
                self.assertEqual((code, headers["Allow"]), (405, "GET, HEAD"))
        for host in ("evil.example", f"evil.example:{self.port}", f"localhost.evil.example:{self.port}", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/", headers={"Host": host})[0], 403)
        for site, expected in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)):
            with self.subTest(site=site):
                self.assertEqual(self.request("/", headers={"Sec-Fetch-Site": site})[0], expected)
        code, headers, body = self.request("/", method="HEAD")
        self.assertEqual((code, body), (200, ""))
        for path in ("/", "/static/app.js", "/missing", f"/project/{self.uuid}"):
            with self.subTest(path=path):
                _code, headers, _body = self.request(path)
                self.assertEqual(headers["Content-Security-Policy"], STRICT_CSP)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIsNone(headers["Access-Control-Allow-Origin"])
        _code, headers, _body = self.request("/static/vendor/mermaid.min.js")
        etag = headers["ETag"]
        self.assertEqual(self.request("/static/vendor/mermaid.min.js", headers={"If-None-Match": etag})[0], 304)
        self.assertEqual(self.request("/static/vendor/../app.js")[0], 404)

    def test_mermaid_is_loaded_only_for_pages_with_diagrams(self) -> None:
        (self.directory / "plan.md").write_text("# Plan\n\n```mermaid\ngraph TD; A-->B\n```\n")
        (self.directory / "retro.md").write_text("# Retro\n\n```python\nmermaid = 1\n```\n")
        self.serve()
        code, headers, body = self.request(f"/project/{self.uuid}/files/plan.md")
        self.assertEqual(code, 200)
        self.assertIn('data-mermaid="/static/vendor/mermaid.min.js"', body)
        self.assertEqual(headers["Content-Security-Policy"], STRICT_CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'"))
        code, headers, body = self.request(f"/project/{self.uuid}/files/retro.md")
        self.assertNotIn("mermaid.min.js", body)
        self.assertEqual(headers["Content-Security-Policy"], STRICT_CSP)
        fences = {"crlf.md": "# CRLF\r\n\r\n```mermaid\r\ngraph TD; A-->B\r\n```\r\n",
                  "list.md": "- Step\n\n    ```mermaid\n    graph TD; A-->B\n    ```\n",
                  "quote.md": "> Quoted\n>\n> ~~~ mermaid\n> graph TD; A-->B\n> ~~~\n"}
        for name, text in fences.items():
            with self.subTest(name=name):
                (self.directory / name).write_bytes(text.encode())
                self.assertIn('data-mermaid="/static/vendor/mermaid.min.js"', self.page(f"/project/{self.uuid}/files/{name}"))

    def test_request_manager_reads_the_archive_once(self) -> None:
        self.archive("finished", "e" * 32, "2026-03-01T00:00:00+00:00", 1)
        self.serve()
        manager = server.RequestManager()
        manager.load()
        self.assertEqual(manager.artifact_project("e" * 32)["name"], "finished")
        for path in (self.projects / ".git" / "baton" / "v2" / "archive").glob("*.json"):
            path.unlink()
        self.assertEqual([project["name"] for project in manager.archived_projects()], ["finished"])
        self.assertEqual(manager.state_dir, manager.state_dir)
        self.assertEqual(server.Manager().layout, manager.layout)

    def test_pull_request_lookup_respects_the_total_budget(self) -> None:
        tools = self.root / "slow-git"
        tools.mkdir()
        real_git = shutil.which("git") or "/usr/bin/git"
        (tools / "git").write_text(f'#!/bin/sh\ncase "$*" in *remote.origin.url*) sleep 30;; esac\nexec {real_git} "$@"\n')
        (tools / "git").chmod(0o755)
        project = {"repos": [{"id": "first", "source": str(self.repos / "alpha"), "branch": "baton/example"},
                             {"id": "second", "source": str(self.repos / "alpha"), "branch": "baton/example"}]}
        with patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}):
            started = time.monotonic()
            result = prs.lookup(project, call_timeout=0.5, total_budget=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([(pulls.repo, pulls.unavailable) for pulls in result],
                         [("first", "lookup failed"), ("second", "lookup time budget exhausted")])

    def test_uninitialized_registry_is_never_created_or_repaired(self) -> None:
        registry = self.projects / ".git" / "baton"
        shutil.rmtree(registry / "v2")
        before = sorted(path.relative_to(registry) for path in registry.rglob("*"))
        # The override makes this a trusted runtime, where a registry load would repair a lone v2.tmp.
        self.serve(override=True)
        for staging in (False, True):
            if staging:
                (registry / "v2.tmp").mkdir()
                before.append(Path("v2.tmp"))
            for path in ("/", f"/project/{self.uuid}", "/search?q=example", f"/project/{self.uuid}/prs"):
                with self.subTest(staging=staging, path=path):
                    self.assertIn("not initialized", self.page(path, 503))
            self.assertEqual(sorted(path.relative_to(registry) for path in registry.rglob("*")), sorted(before))

    def test_every_route_leaves_registry_artifacts_and_board_unchanged(self) -> None:
        (self.directory / "plan.md").write_text("# Plan\n")
        self.cli("blackboard", "post", *self.board(), "--agent", "owner", "--text", "note")
        self.fake_github_cli([])

        def snapshot() -> dict[str, bytes | None]:
            files: dict[str, bytes | None] = {}
            for root in (self.projects / ".git" / "baton", self.artifacts):
                for path in root.rglob("*"):
                    if path.name.startswith("blackboard.sqlite"):
                        continue
                    files[str(path)] = path.read_bytes() if path.is_file() else None
            return files

        before, board = snapshot(), self.board_state()
        self.serve()
        os.environ["PATH"] = self.env["PATH"]
        for path in ("/", f"/project/{self.uuid}", f"/project/{self.uuid}/board", f"/project/{self.uuid}/reviews",
                     f"/project/{self.uuid}/files/", f"/project/{self.uuid}/files/plan.md", f"/project/{self.uuid}/prs",
                     "/search?q=plan", "/static/app.css"):
            with self.subTest(path=path):
                self.page(path)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.board_state(), board)

    def test_vendored_files_match_their_record(self) -> None:
        record = json.loads((VENDOR / "VENDORED.json").read_text())
        self.assertEqual(set(record), {"markdown-it", "mermaid"})
        for library in record.values():
            data = (VENDOR / library["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), library["sha256"])
            self.assertEqual(len(data), library["bytes"])
            self.assertIn(library["version"], (VENDOR / library["license_file"]).read_text())
        self.assertIn("Apache License", (VENDOR / "mermaid.LICENSE.txt").read_text())


class WebProcessTest(unittest.TestCase):
    """The installed command: startup errors, Ctrl-C and child cleanup."""

    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create
    ensure_slot = fixtures.ProjectManagerTest.ensure_slot

    setUp = fixtures.ProjectManagerTest.setUp

    def start(self, *args: str) -> tuple[subprocess.Popen[str], int]:
        env = {key: value for key, value in self.env.items() if key != "BATON_ALLOW_UNTRUSTED_WRITES"}
        process = subprocess.Popen(["bash", str(fixtures.SCRIPT), "web", *args], cwd=self.root, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        assert process.stdout is not None
        line = process.stdout.readline()
        self.assertIn("http://127.0.0.1:", line)
        return process, int(line.rsplit(":", 1)[1].strip("/\n"))

    def stop(self, process: subprocess.Popen[str]) -> str:
        process.send_signal(signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        return stderr

    def test_busy_port_fails_cleanly(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES")
            result = self.cli("web", "--port", str(port), ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"cannot listen on 127.0.0.1:{port}", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_sigint_exits_without_traceback(self) -> None:
        process, port = self.start("--port", "0")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request("GET", "/")
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        self.assertIn("Stopped.", self.stop(process))

    def test_sigint_during_stalled_gh_leaves_no_child_process(self) -> None:
        source = self.make_repo("alpha")
        github_url = "https://github.com/test/alpha.git"
        self.git(source, "config", f"url.{self.root / 'alpha.git'}.insteadOf", github_url)
        self.git(source, "remote", "set-url", "origin", github_url)
        self.create("alpha")
        uuid = fixtures.read_state(self.projects)["projects"]["p3"]["uuid"]
        bindir = self.root / "stalled-bin"
        bindir.mkdir()
        pid_file = self.root / "gh.pid"
        (bindir / "gh").write_text(f"#!/bin/sh\necho $$ > {pid_file}\nsleep 60\n")
        (bindir / "gh").chmod(0o755)
        self.env["PATH"] = f"{bindir}{os.pathsep}{self.env['PATH']}"
        process, port = self.start("--port", "0")

        def fetch() -> None:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request("GET", f"/project/{uuid}/prs?fragment=1")
                connection.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()

        client = threading.Thread(target=fetch)
        client.start()
        deadline = time.monotonic() + 20
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        group = int(pid_file.read_text())
        self.stop(process)
        client.join(30)
        deadline = time.monotonic() + 10
        while subprocess.run(["pgrep", "-g", str(group)], capture_output=True).returncode == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotEqual(subprocess.run(["pgrep", "-g", str(group)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
