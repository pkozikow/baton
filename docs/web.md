# Web UI

`baton web` serves a local, read-only web UI for exploring active and archived
projects: their status, saved artifacts, boards, review runs and pull requests.

```sh
baton web                 # http://127.0.0.1:8742/ until Ctrl-C
baton web --port 0        # pick a free port; the URL is printed
baton web --open          # also open the default browser
```

The server runs in the foreground and stops with Ctrl-C (or SIGTERM). It binds
`127.0.0.1` only; there is no option to listen on other interfaces. A port that
is already in use is reported and the command exits with status 1.

Pages show the state at the time they load; reload to refresh. Every request reads
the registry afresh, so projects created, renamed or archived while the server
runs appear on the next load.

## Views

- **Projects** (`/`): the ten coordinator slots with their active project, state,
  repositories, owner provider and any pending operation, then archived projects,
  newest first. A search box is on every page.
- **Project** (`/project/UUID`): identity (name, previous names, slot, owner,
  follow-up lineage), the branch table that `baton status` reports, retired
  members, status errors, and links to the plan, subplans, retrospective and
  follow-up context. Archived projects show their archived record. Projects are
  addressed by full UUID, so reused names never collide.
- **Board** (`/project/UUID/board`): for an active project, the overview, tasks
  (state, assignee, blockers, held, settlement), per-agent activity and the most
  recent 50 posts. For an archived project, links to the sealed
  `coordination/overview.md` and `coordination/history.md`.
- **Reviews** (`/project/UUID/reviews`): review runs with kind and focus, start
  time, reviewer coverage and status. Saved runs link to each reviewer's saved
  output. Unsaved runs (still in the temporary directory) are listed with their
  path but not linked.
- **Files** (`/project/UUID/files/...`): the artifact directory. Markdown is
  rendered, other UTF-8 text is shown as is, and anything else gets a size notice.
  File URLs mirror the directory layout, so relative links between artifacts work.
- **Search** (`/search?q=...&state=all|active|archived`): the same literal search
  as `baton search`, with excerpts linking to the matching artifact.
- **Pull requests**: the project page looks up each recorded branch's pull
  requests with `gh pr list` after it loads. Each call has 5 seconds, the whole
  lookup 15 seconds, and a repository stops at its first failure. A missing source
  checkout, a non-GitHub remote, a missing `gh` or a failed call shows
  "unavailable"; the rest of the page does not depend on the network.

Markdown and Mermaid diagrams render in the browser. A diagram that fails to parse
stays visible as its source with the error beneath it.

## Read-only guarantees

The UI changes nothing: not the registry, artifacts, boards, Git state or GitHub.
Only GET and HEAD are accepted. If the registry has not been initialized, pages say
so instead of creating or repairing it, even from a trusted installation. Board
reads use the same shared lock and read-only SQLite connection as `baton blackboard
read`, serialized within the server, so SQLite may touch a live board's `-wal` and
`-shm` sidecars as CLI reads already do. Because `baton web` writes nothing, it is
not subject to [runtime write protection](installation.md#runtime-write-protection).

## Safety model

The UI is meant for the local user only and has no authentication.

- Requests whose `Host` header is not `127.0.0.1:PORT` are refused, which stops
  DNS rebinding. When a browser sends `Sec-Fetch-Site`, only `same-origin` and
  `none` (typed or bookmarked URLs) are accepted, so pages on other sites or other
  local ports cannot make the UI run lookups. No CORS headers are sent.
- Every response carries a restrictive Content Security Policy: scripts, styles,
  images and connections only from the server itself, no inline scripts, no
  framing. Pages that contain Mermaid diagrams also allow inline styles, which
  Mermaid's SVG output needs; nothing else is relaxed.
- Artifact text, board posts and saved reviews are untrusted. Markdown is sent
  escaped and rendered with raw HTML disabled; only relative, `http(s)` and
  `mailto` links are kept, and images become links. Mermaid runs at its `strict`
  security level with HTML labels off, so labels are plain text and click
  directives are ignored.
- File views open each path component without following symlinks and refuse
  hidden entries, `..`, live board files (`blackboard.sqlite*`,
  `blackboard-lock.md`) and SQLite databases. Raw artifact bytes are never sent;
  text over 2 MiB, binary and non-UTF-8 files get a notice instead.

## Limits

- No live updates or auto-refresh, no actions (claim, post, cleanup, launch).
- Code worktrees, provider transcripts, unsaved worktree notes, sealed board
  databases and the output files of unsaved review runs are not browsable.

## Vendored browser libraries

The Python runtime stays standard-library only. The UI ships two browser
libraries, vendored unmodified with their license texts under
`baton/web/static/vendor/`: markdown-it 15.0.3 and Mermaid 11.17.2, both MIT,
with permissively licensed bundled packages. `VENDORED.json` records each file's
version, source URL and SHA-256, and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)
summarizes the licenses. Mermaid is pinned to the 11.x line because every 12.x
release bundles elkjs (EPL-2.0 OR GPL-3.0-or-later); any version bump must repeat
the bundled-license check against the release's source map.
