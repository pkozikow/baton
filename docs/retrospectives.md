# Retrospectives

Interactive `baton retro` forks the project owner session to write a retrospective
with the bundled `baton-retro` skill. When the owner's provider has no native fork
(agy, Cursor), it starts a fresh session instead. The fresh prompt says that no owner
history is available and points only at saved evidence: plans, notes, the
blackboard Markdown export and the identity snapshot. It infers the active project from a registered
branch; otherwise pass `--selector`. It can also select an archived project.

Retrospectives assess saved validation evidence, operational anomalies and reusable
native-tool follow-ups as detailed in [baton-retro](../agent-skills/baton-retro/SKILL.md).

## Unattended retrospectives

```sh
baton retro pending --since 3d --dry-run --json
baton retro pending --since 3d --timeout 1800
baton retro --selector FULL_PROJECT_UUID --non-interactive
baton retro show --selector FULL_PROJECT_UUID --json
```

The daily cron job described in [Installation](installation.md#retrospective-cron-job)
runs `baton retro pending` on a schedule.

### Discovery

Pending discovery is one local JSON scan of archived manifests. It uses the UTC
`completed_at` recorded on successful cleanup, then checks only candidate retro
artifacts and UUID-specific run state. It does not run per-project status, Git
history, native-stack, network, transcript, or model operations. Active projects are
excluded. Historical archives without a reliable completion timestamp are reported
as unknown-date and skipped; file modification times are never substituted. They
can still be selected explicitly for a retro.

Full UUID selectors identify historical projects even when a name or slot has been
reused. Active name/slot selectors retain their normal meaning; an archived name is
accepted only when no active project has it and the historical match is
unambiguous. `retro show` returns the selected frozen manifest, owner and artifact
paths without recreating its coordinator. Artifact commits also accept full UUID
selectors.

### Execution

Unattended runs use a native noninteractive fork of the recorded owner session when
its provider supports one (Codex, Claude), and otherwise a fresh headless session
(agy, Cursor). The session receives the complete `baton-retro` skill and full project
identity. The launcher supplies `identity.json` with the resolved project identity,
owner, artifact paths, and frozen manifest. Unattended sessions read that snapshot
instead of running `retro show`, `status`, or other framework metadata commands.
They must not inspect reused code worktrees, alter the owner, or modify another
active project.

Private workspaces use unique directories in the system temporary directory,
honoring `TMPDIR`, and must be outside `.git`. Baton copies the project's artifact
directory into the workspace, minus SQLite files, locks, temporary files, symlinks,
`.git` and an existing `retro.md`: plans, archived notes, durable validation
summaries/harnesses and the blackboard Markdown export, plus the identity snapshot.
The session runs with the workspace as its working directory, receives no other
directory, and the prompt, frozen manifest and `identity.json` name only the copies
(`artifacts_path`, `retro_path` and the manifest's artifact paths point into the
workspace's `evidence/` directory). Fork-capable providers still see
their inherited history; the copy replaces only file access.

The session prints its full draft once between the standalone lines
`BATON_RETRO_DRAFT_BEGIN` and `BATON_RETRO_DRAFT_END`, so no provider needs write
access. Only Baton publishes into the artifact checkout. The trusted launcher
recovers the draft from standard output only (some provider CLIs echo the prompt or
repeat the final message on standard error), verifies its project UUID, rechecks the
owner and artifact identity, and publishes `retro.md` without overwriting an existing
artifact, then uses the normal artifact commit workflow. Missing or deleted
pre-cleanup evidence is reported as a limitation.

Active retros consume an on-demand Markdown snapshot of the blackboard and report
its sequence; the final closure snapshot may be later. Archived retros read saved
Markdown without requiring the database or inspecting reused slots.

#### Safety floor

Every unattended invocation must block writes outside the workspace in the CLI
itself (sandbox or permission mode, not just the prompt). It never uses blanket
approval or sandbox bypass (`--force`, `--yolo`, `--dangerously-*`,
`bypassPermissions`, `danger-full-access`); workspace trust such as Cursor `--trust`
is not blanket approval. Anything that would need approval is denied, never
prompted. Reads are confined where the CLI enforces it and best effort elsewhere;
the run result records the provider's `read_confinement`.

| Provider | Unattended invocation (cwd is the workspace) | Writes | Reads |
|----------|----------------------------------------------|--------|-------|
| codex | `codex exec --config approval_policy="never" -C WS -s read-only fork --skip-git-repo-check ID -` | none: read-only sandbox, approvals never | best effort: the sandbox does not restrict reads |
| claude | `claude --print --resume ID --fork-session --restricted --strict-mcp-config --permission-mode dontAsk --permission-prompts none --tools Read,Glob,Grep` | none: `--restricted` ignores user, project and local settings and their allow rules and removes command tools; `dontAsk` denies everything outside `--tools` | enforced: `--restricted` confines file tools to the working directory |
| agy | `agy --print PROMPT --sandbox` | workspace only; outside is denied | enforced |
| cursor | `agent -p --mode ask --sandbox enabled --trust --workspace WS PROMPT` | none: ask mode is read-only | best effort: Cursor's default read boundary is the system |

agy aborts the whole turn, with no output, when any tool call is denied. Its prompt
therefore tells it to use only file reading tools and never run shell commands; a
denied call fails the retro closed. `<NAME>_BIN` overrides each executable.

Incomplete or ambiguous marker blocks and conflicting project UUIDs fail rather
than being published; existing invalid files or symlinks are never replaced by the
recovered draft. Failures include a bounded excerpt of the session output for
diagnosis.

#### Logins under cron

Cron runs use each CLI's stored login; Baton never writes API keys into the crontab.
The forwarded environment is listed in
[Installation](installation.md#retrospective-cron-job); Claude reports "Not logged
in" without `USER` because its login lives in the keychain. Keychain- or keyring-backed logins (Claude, agy) may still fail to
unlock in a cron session; if so, set that vendor's token environment variable for
the cron job manually, following the vendor's documentation.

#### Run state and recovery

Locks, attempt logs (`attempt-N.log` for standard output and publication,
`attempt-N.stderr.log` for standard error), and running/completed/failed state remain
under `<coordinator git common dir>/baton/v2/retros/<full-uuid>/`. Runs for the
same UUID cannot overlap, including when the calling process dies while its
supervisor continues. Model execution is bounded
by `--timeout` (default 1800 seconds); leftover provider process-group members are
terminated. Successful or skipped runs remove their temporary workspace. Failed
workspaces remain available for recovery, with best-effort cleanup after seven days,
and retries use fresh workspaces. A verified published draft whose artifact commit
failed retries the commit without another model run. Normal artifact publication
rules still apply: a network failure may leave a valid commit local, and actual
checkout divergence needs user reconciliation.

### Completion rules

A nonempty existing retro recording the project's full UUID is complete. Empty
files, symlinks, missing or conflicting UUIDs, and incomplete authoritative outputs
require inspection; automation
never overwrites them. Unowned projects are skipped with an explicit reason.
Scheduled runs do not claim ownership or resurrect archived projects.


## Weekly cross-project reports

The weekly job synthesizes common themes and recurring issues across project
retros. It uses a fresh unattended session of the explicitly selected provider,
without any project owner's conversation history. The same unattended write
restrictions, stored logins and bounded process cleanup apply.

```sh
baton retro summarize --dry-run --json
baton retro summarize --provider codex --timeout 1800 --json
# Equivalent cron wrapper:
baton-retro-cron run-weekly --provider codex
```

Preview needs no provider, performs no model calls, and writes no state or artifacts.
Run it before manually generating a report: the first successful manual or cron run
owns that local ISO week, and later runs reuse its result. Weekly installation is
an independent opt-in described in [Installation](installation.md#weekly-report-cron).

### Selection and output

Inputs are the direct project files `<current artifacts_root>/*/retro.md`,
including active and archived project directories. No live project registry
entry is required. Discovery uses one local Git `HEAD` snapshot and the committer
date of each retro's most recent content change, including creation and updates.
Pure file/directory renames do not make old evidence new. Project completion dates,
prose dates, author dates and filesystem modification times do not control selection.
Merge resolutions count when they introduce content different from every parent;
merely adopting an unchanged parent blob preserves its earlier date. Discovery
follows the parent that supplies the surviving version, so discarded branch edits
cannot refresh it.
Rewritten/imported Git history can change these timestamps.

The first attempt freezes its UTC end time and preceding 168-hour window
`[end - 168 hours, end)`, snapshot and sources. Retries keep that selection. Sources
come from committed blobs; dirty working copies are flagged and untracked, empty,
invalid or symlinked inputs are reported as exclusions. Discovery does not fetch:
coverage describes the local snapshot, and remote freshness is unknown. It does not
read historical artifact roots, project worktrees, or prior weekly reports. A
project colliding with the reserved `retros` directory causes an explicit error.

Reports are stored at `<current artifacts_root>/retros/<ISO-year-Wweek>/report.md`
(for example `retros/2026-W44/report.md`), alongside `sources.json` with the window,
snapshot, relative source paths, hashes and limitations. Absolute host checkout
paths stay in local operational state. The ISO year/week uses the
host's local timezone; window comparisons use UTC. These are rolling seven-day
reports, with no catch-up for missed weeks. DST and late starts can cause small
overlaps or gaps between adjacent windows, visible in the recorded timestamps.

A typical report is 600–1,000 words (about 1–2 pages) covering the shared themes,
recurring issues, useful practices and prioritized recommendations. Findings link
to the supporting retros; the source manifest identifies the analyzed versions if
those files later change. Recurrence across projects requires at least two distinct
retros. A single retro produces appropriately limited findings, and an empty week
produces a brief deterministic report without launching a model.

Each model invocation has a conservative 48 KiB UTF-8 input budget, including its
instructions, source manifest, complete evidence and framing. Requests contain all
of this input directly in the prompt, so providers need no file-reading or shell
tools. Weeks that exceed this budget are analyzed in batches
of complete committed retros, followed by synthesis of the derived notes. Notes
retain every batch source's citation and are limited to 8 KiB each; further bounded
passes combine notes when needed. All selected sources remain in the final
provenance, and recurrence still counts distinct projects, never batches.

A single retrospective that cannot fit with its prompt and manifest, excessive
source metadata, or a provider without an established safe budget fails explicitly
rather than dropping evidence. Source-size checks run before copying oversized
blobs; an individual oversized source can also make preview fail. `--timeout`
is one total model-execution budget across all analysis and synthesis passes.
No recommendations are implemented automatically.

### Persistence and recovery

Only the trusted launcher writes report artifacts and commits their exact paths,
preserving unrelated staged or unstaged changes. Report/provenance files remain
under the effective current artifacts root, including environment overrides. Local
locks, attempt logs, frozen selection and verified retry output are operational
state outside that versioned area, in
`${XDG_STATE_HOME:-~/.local/state}/baton/retro-summaries/<sha256(root)>/<ISO-week>/`,
where the hash is of the resolved artifacts root. Supervisor
diagnostics go to `supervisor.log` there, independently of the caller’s pipes.

Concurrent runs are serialized locally; publication rechecks the destination after
artifact synchronization so a report already published by another host is preserved.
Complete existing reports are reused. Partial publication resumes only when existing
file hashes match retained verified output; conflicting contents or symlinked
outputs require manual inspection and are never overwritten. Commit/push retries
reuse verified output without another model call. Results distinguish saved files,
local commits and remote publication; network failure may leave a local commit.
Until the final draft is verified, retries may rerun intermediate analyses against
the same frozen sources; intermediate notes are not published as source evidence.
Normal artifact reconciliation rules apply: never reset or rebase the shared checkout.
