"""`baton web`: a local, read-only web UI over project status and saved artifacts."""

import argparse
from functools import cached_property
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
import traceback
from types import FrameType
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, unquote
import webbrowser

from ..errors import ProjectError
from ..manager import Manager
from . import prs, views

HOST = "127.0.0.1"
DEFAULT_PORT = 8742
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Mermaid's SVG output carries <style> elements and style attributes, so only pages with diagrams relax style-src.
MERMAID_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")
MERMAID_SCRIPT = "/static/vendor/mermaid.min.js"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "vendor/markdown-it.umd.min.js": "text/javascript; charset=utf-8",
    "vendor/mermaid.min.js": "text/javascript; charset=utf-8",
    "vendor/markdown-it.LICENSE.txt": "text/plain; charset=utf-8",
    "vendor/mermaid.LICENSE.txt": "text/plain; charset=utf-8",
}
_static_cache: dict[str, tuple[bytes, str]] = {}


def static_file(name: str) -> tuple[bytes, str] | None:
    """A packaged static file and its ETag; only the fixed list is served."""
    if name not in STATIC_TYPES:
        return None
    if name not in _static_cache:
        data = (STATIC_ROOT / name).read_bytes()
        _static_cache[name] = (data, '"' + hashlib.sha256(data).hexdigest()[:32] + '"')
    return _static_cache[name]


def layout(page: views.Page) -> str:
    mermaid = f' data-mermaid="{MERMAID_SCRIPT}"' if page.mermaid else ""
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{views.e(page.title)} · Baton</title><link rel=\"stylesheet\" href=\"/static/app.css\">"
            "<script src=\"/static/vendor/markdown-it.umd.min.js\" defer></script>"
            "<script src=\"/static/app.js\" defer></script></head>"
            f"<body{mermaid}><header class=\"top\"><a class=\"brand\" href=\"/\">Baton</a>"
            "<span class=\"muted\">read-only</span>"
            "<form action=\"/search\" method=\"get\"><input type=\"search\" name=\"q\" placeholder=\"Search projects\" "
            "aria-label=\"Search projects\"></form></header>"
            f"<main>{page.body}</main><footer class=\"muted\">Third-party licenses: "
            "<a href=\"/static/vendor/markdown-it.LICENSE.txt\">markdown-it</a>, "
            "<a href=\"/static/vendor/mermaid.LICENSE.txt\">Mermaid</a></footer></body></html>\n")


class RequestManager(Manager):
    """A manager for one request: the registry location and archive listing are read once, not per lookup."""

    def __init__(self) -> None:
        super().__init__()
        self._archived: list[dict[str, Any]] | None = None

    @cached_property
    def state_dir(self) -> Path:  # type: ignore[override]
        return super().state_dir

    def archived_projects(self) -> Iterator[dict[str, Any]]:
        if self._archived is None:
            self._archived = list(super().archived_projects())
        return iter(self._archived)


def route(path: str, query: dict[str, list[str]]) -> views.Page:
    """Render a decoded request path that is not a static file."""
    manager = RequestManager()
    if not views.registry_ready(manager):
        return views.not_initialized()
    if path == "/search":
        # search_projects loads the registry itself.
        return views.search_page(manager, (query.get("q") or [""])[0], (query.get("state") or ["all"])[0])
    manager.load()
    if path == "/":
        return views.overview(manager)
    parts = path.split("/", 4)
    if len(parts) < 3 or parts[1] != "project":
        return views.not_found()
    project = views.find_project(manager, parts[2])
    if project is None:
        return views.not_found("No project has this UUID.")
    section = parts[3] if len(parts) > 3 else None
    if section is None:
        return views.project_page(manager, project)
    if section == "" and len(parts) == 4:
        return views.Page(project["name"], "", status=301, location=views.project_url(project["uuid"]))
    if section == "files":
        if len(parts) == 4:
            return views.Page("Files", "", status=301, location=views.project_url(project["uuid"], "/files/"))
        return views.files_page(manager, project, parts[4])
    if len(parts) > 4:
        return views.not_found()
    if section == "board":
        return views.board_page(manager, project)
    if section == "reviews":
        return views.reviews_page(manager, project)
    if section == "prs":
        return views.pull_requests_page(project, fragment=(query.get("fragment") or [""])[0] == "1")
    return views.not_found()


class Handler(BaseHTTPRequestHandler):
    server_version = "baton-web"
    sys_version = ""
    csp = CSP

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", self.csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def __getattr__(self, name: str) -> Callable[[], None]:
        # Every method other than GET and HEAD, including unknown ones, is refused with 405.
        if name.startswith("do_"):
            return self.method_not_allowed
        raise AttributeError(name)

    def method_not_allowed(self) -> None:
        self.send(405, b"Method not allowed.\n", "text/plain; charset=utf-8", [("Allow", "GET, HEAD")])

    def do_GET(self) -> None:
        self.respond(head=False)

    def do_HEAD(self) -> None:
        self.respond(head=True)

    def send(self, status: int, body: bytes, content_type: str, headers: list[tuple[str, str]] | None = None,
             *, head: bool = False, csp: str = CSP) -> None:
        self.csp = csp
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_page(self, page: views.Page, head: bool) -> None:
        headers = [("Cache-Control", "no-store")]
        if page.location is not None:
            headers.append(("Location", page.location))
        body = page.body if page.fragment else layout(page)
        self.send(page.status, body.encode(), "text/html; charset=utf-8", headers, head=head,
                  csp=MERMAID_CSP if page.mermaid else CSP)

    def respond(self, head: bool) -> None:
        # Loopback binding alone does not stop DNS rebinding: a foreign name resolving to 127.0.0.1.
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            self.send(403, b"Forbidden host.\n", "text/plain; charset=utf-8", head=head)
            return
        # same-site would admit pages served from other loopback ports.
        if self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            self.send(403, b"Requests from other origins are refused.\n", "text/plain; charset=utf-8", head=head)
            return
        raw, _, query = self.path.partition("?")
        try:
            if not raw.startswith("/"):
                raise ValueError("not an origin-form path")
            path = unquote(raw, errors="strict")
            if "\0" in path:
                raise ValueError("NUL in path")
        except (UnicodeError, ValueError):
            self.send_page(views.error_page(400, "Bad request", "The request path is malformed."), head)
            return
        try:
            if path == "/favicon.ico":
                # Browsers request it on every first load; a 404 would log a console error per page.
                self.send(204, b"", "image/x-icon", [("Cache-Control", "max-age=86400")], head=True)
                return
            if path.startswith("/static/"):
                found = static_file(path.removeprefix("/static/"))
                if found is None:
                    self.send_page(views.not_found(), head)
                    return
                data, etag = found
                if self.headers.get("If-None-Match") == etag:
                    self.send(304, b"", STATIC_TYPES[path.removeprefix("/static/")], [("ETag", etag)], head=True)
                    return
                self.send(200, data, STATIC_TYPES[path.removeprefix("/static/")],
                          [("ETag", etag), ("Cache-Control", "no-cache")], head=head)
                return
            try:
                page = route(path, parse_qs(query))
            except ProjectError as error:
                page = views.error_page(500, "Error", str(error))
            self.send_page(page, head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_page(views.error_page(500, "Error", "The page could not be rendered; see the server output."), head)
            except OSError:
                pass


class WebServer(ThreadingHTTPServer):
    daemon_threads = True


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baton web", description="Serve a local, read-only web UI over project status "
                                     "and saved artifacts until interrupted with Ctrl-C.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Loopback port (default: {DEFAULT_PORT}; 0 picks a free port)")
    parser.add_argument("--open", action="store_true", help="Open the UI in the default browser")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = WebServer((HOST, args.port), Handler)
    except OSError as error:
        print(f"baton: cannot listen on {HOST}:{args.port}: {error.strerror or error}", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_port}/"
    # A launcher may start this process with SIGINT ignored; Ctrl-C and SIGTERM must still stop it cleanly.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        print(f"Baton web UI (read-only): {url}\nPress Ctrl-C to stop.", flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    finally:
        prs.terminate_all()
        server.server_close()
    return 0
