# Projects

## Lifecycle

`baton init` creates the coordinator, its ten slots, the code-worktrees root and the
default artifact repository. A project is created in a free slot with `baton create`, which adds a
worktree per participating repository; repository names and relative paths resolve
under the configured [source-repository root](configuration.md#storage-roots).
Agents plan, implement and review in those worktrees, coordinating through the
blackboard. When the work is merged, `baton cleanup` seals the coordination history
and releases the slot; `baton abandon` closes the project's PRs first. A
[follow-up project](#follow-up-projects) can then continue from the archive. The
[Quick start](../README.md#quick-start) shows the commands. Paths from
`baton status --json` are authoritative, especially `master_path`, `current_entry`,
and per-entry `subplan_path` and PR bases. Never reconstruct registered paths from
branch names.

## Project discovery

See [Find and revisit projects](../README.md#find-and-revisit-projects) for
examples. Run search from any directory, including outside Git and without an active
project. It scans the configured active registry and saved archives. `--state`
is `all` by default; `active` includes current projects with pending operations,
and `archived` selects closed projects. Results retain their actual lifecycle state.

The query is one string of literal, case-insensitive keywords and double-quoted
phrases. All terms must occur within the same project, possibly in different
sources. A phrase stays within one source, with whitespace normalized across
wrapped lines. There is no regex, stemming, or semantic search. Empty queries and
unmatched quotes are errors. No matches returns exit 0 with an empty JSON `results` array.

Search covers project names/previous names, UUIDs, repository IDs, branch/member
names (including retired members), and selected saved `.md`/`.txt` artifacts such
as plans, subplans, retrospectives and archived notes. Saved `coordination/overview.md` and `coordination/history.md` are included.
Coverage and exclusions are reported in output.
Provider conversations, source code, Git history, unsaved worktree notes, live
SQLite boards and hidden export duplicates are excluded. Search never exports or
repairs a board, follows symlinks, or visits historical worktree/slot paths.
Missing or unsafe files and ambiguous artifact directories produce coverage
warnings. Use the returned artifact directory for direct inspection of excluded
saved evidence. Search neither claims ownership nor launches a provider.

One result is returned per UUID. Rank favors terms found in metadata, then distinct
matching artifacts, with UUID as a stable tie-breaker; repeated text in one file
does not increase that file's rank. Text and versioned JSON output provide bounded
excerpts and source references, identity, owner information and artifact/plan
paths. `--limit` defaults to 20 and must be positive. The entire corpus is searched
before limiting results; totals and truncation are reported. JSON stays on stdout,
with diagnostics on stderr. Review warnings before treating no matches as complete
coverage.

Search scans saved text directly without an index or service. For performance
measurement, see the [developer benchmark](../README.md#development).

An agent can refine queries and read matching artifacts to identify the intended
project. If several candidates remain plausible, ask the user to clarify. Follow-up
commands should use the selected UUID: an active name or previous name takes
precedence over an archived name, slots select current occupants, and ambiguous archived names
require a full UUID. Finding a project does not reopen it or authorize new work.

## Coordinator slots

Coordinator slots and their idle branches are `p1` through `p10`; these names are
also reserved as project names. `baton list` always shows those ten slots. Historical
archives keep their original metadata and remain selectable by project UUID or
unambiguous project name.

Registered coordinator slots are metadata-only: code changes belong in the
participating worktrees. `baton handoff` supplies authoritative paths and worker
rules.

## Artifact directories

New projects store artifacts in
`<artifacts_root>/<creation-name>-<first-eight-UUID-hex-characters>/`. The exact
absolute `artifacts_path` is persisted in the project manifest and remains stable
through project renames. `master_path` and each entry's `subplan_path` from status
remain authoritative.

Reusing a completed project's name creates a separate artifact directory; an actual
UUID-prefix/path collision is refused, including a directory whose tracked files were
deleted without a commit. The full UUID remains the identity.

Artifact commits use
`baton artifacts commit --selector NAME -m MESSAGE /absolute/path...`. Full UUID
selectors are accepted. When the artifact checkout has an `origin` remote, the
command syncs with it and pushes the commit; `--no-push` commits locally only. Shared blackboard files are ignored coordination state; use
`baton blackboard` commands and never commit them.

## Branches and coordinator links

New projects use `<branch-prefix>/<project>` branches (see
[Branch prefix](configuration.md#branch-prefix)); existing source checkouts stay on
their current branches. Only the project parent changes branch topology or publishes native
stacks; use the bundled planning skill's multi-branch reference for coordination
details.

Independent `baton branch add` operations allow uncommitted changes in existing
members because they create a separate worktree from the default branch.
Stacked additions (`--after`) still require clean registered worktrees. Member
name, branch, and destination-path conflicts remain errors.

| Command | Purpose |
| --- | --- |
| `baton add REPO...` | Add repositories to the current project |
| `baton rename NAME [PROJECT]` | Rename a project; the previous name stays selectable |
| `baton path [PROJECT] [REPO] [--member NAME]` | Print a registered checkout path |
| `baton list` | List the coordinator slots and their projects |
| `baton branch init REPO MEMBER` | Name the first checkout of a repository |
| `baton branch add REPO MEMBER [--after MEMBER]` | Add an independent branch, or extend a native stack |
| `baton branch register REPO MEMBER [--after MEMBER]` | Register a branch created directly with `gh stack` |
| `baton branch cleanup REPO MEMBER` / `branch prune [REPO]` | Retire one member, or all eligible members |
| `baton stack status REPO [--member NAME]` | Inspect a native stack group |
| `baton status --all` | Also show retired members in text output |

Coordinator links use `repos/<repo>--<member>` for every named checkout, including a
named first member. An unnamed first checkout uses `repos/<repo>`. Retiring the
first member does not create a plain link to another member. Status reports each
checkout's canonical link. Unexpected targets and regular files block reconciliation;
in-progress operations track the exact links they are authorized to change.

## Member retirement

Member retirement accepts an integrated tip (verified through Git ancestry or
merged-PR evidence), or an abandoned tip whose closed, unmerged PR matches the exact
local commit. Any open PR for the branch blocks retirement, including when the tip
is already integrated. GitHub PR lookup failures block retirement; pending operations
recheck PR state before removing worktrees or changing native tracking.

Additional local commits after a merged or closed PR need their own integration or
abandonment evidence.

Retirement preserves the tip in an archive ref and records `integrated` or `abandoned`
in its history. Dirty files and unarchived notes retain their existing safeguards.
Abandoned native stack members must retire as a whole group: partial removal would
leave native tracking referencing an unmerged missing branch. Use `branch prune` once
all live group members are eligible; unsafe groups are skipped without affecting
unrelated eligible members. Native tracking is removed locally only for the completed
group; no PR is closed and no remote branch is deleted by retirement.

## Sessions and ownership

A project's first top-level agent claims its real runtime session with
`baton session claim --provider NAME`. Every registered provider can own a project;
session variables and per-provider claim guidance are described in
[Providers](configuration.md#providers). Ownership is permanent. Exploratory and
retrospective forks inherit history but never ownership.

`baton session show|resume|fork` and `baton retro` infer the active project from a
registered branch; otherwise pass `--selector`. Resume and fork launch the owner's
CLI. For providers without a native fork, `session fork` starts a fresh session
whose prompt says no owner history is available; Baton never resumes the real owner
session to emulate a fork. Use `baton open-plan` to open the
current project's existing branch subplan or master plan in `$VISUAL`, then
`$EDITOR`, falling back to `vim`; the value is split like a shell command, so
`EDITOR="code -w"` works.

`session show` and `session fork` also accept a closed project's unambiguous name
or full UUID. `claim`, `resume` and implicit branch inference remain active-only.
A closed-project fork uses the first repository's recorded source checkout. A missing/replaced
source or absent owner fails clearly; Baton does not create a worktree, switch
branches, reopen the project, or fall back to its old coordinator slot. The source
checkout may have changed since closure: use saved artifacts and inherited owner
history as historical evidence. The fork prompt includes the frozen identity and
artifact paths, and grants no ownership or authority to direct workers. Native
provider/session failures propagate without resuming the owner as a substitute.


## Notes and cleanup

Cleanup fetches each repository's default branch before checking whether project
branches are merged, including squash merges verified through GitHub PRs.
`baton cleanup --dry-run` also refreshes these remote-tracking refs; it preserves
project state, local branches, and worktrees. If fetching fails, cleanup stops
without using stale merge evidence. `--allow-unmerged REPO[:MEMBER]` (repeatable)
skips the merged-PR check for a whole repository or one member (`root` names an
unnamed first checkout), preserving unmerged tips under archive refs before
removing the worktrees.

Archive relevant ignored repository notes before cleanup. `BATON_NOTE_DIRS` is a
path-separated list of relative note directories; the default includes both `notes`
and `thoughts`. An explicit override replaces that list.

Cleanup seals the coordination history before removing worktrees; see
[Blackboard and tasks](blackboard-and-tasks.md#cleanup-and-sealed-archives).


### Force cleanup

Use `baton cleanup [PROJECT] --force` for last-resort local cleanup when ordinary
cleanup cannot finish. It reports discovered blockers and the removal scope,
then always requires interactive confirmation, even when no warnings were found.
Declining or running without an interactive terminal leaves the project in place.
`--force --dry-run` reports the plan and warnings without confirmation or cleanup.

Force cleanup attempts tip preservation, coordination archival and resource
removal independently. Missing source repositories, broken worktree metadata,
dirty or unmerged code, unarchived notes, failed exports and native-stack problems
become warnings. Dirty/untracked/ignored worktree contents may be discarded after
confirmation. Resources that cannot be safely identified or removed can remain
on disk or in Git; warnings identify those leftovers and preservation failures.
Busy source repositories are skipped instead of waiting for another project's lock.
Shared artifact directories are retained even if coordination archival fails.

The required outcome is a reusable slot: its coordinator must still belong to the
framework, be clean on the idle slot branch, and have no project links or manifest
conflicts; its active registry record must be released. Failure to accomplish
that safely remains an error. Force cleanup does not delete primary checkouts,
other projects' resources or symlink targets. It does not grant permission to
modify a coordinator belonging to another repository.

Force cleanup is local only and does not close PRs. With pending abandonment it
warns that PR closures may be incomplete and releases local resources instead of
resuming `baton abandon`. Ordinary cleanup continues to resume pending abandonment.


## Abandoning a project

Run `baton abandon [PROJECT]` after joining all workers, reviews and services.
Run the retrospective first to preserve relevant notes, as for regular cleanup.
Baton inspects local work and discovers every open GitHub PR for recorded branches,
including retired members and exact pre-rename branch names recorded in this
project UUID's coordinator manifests. Naming the first member can also rename its
original root branch; that earlier recorded branch remains in PR discovery. Repository identity is matched for each
historical record: a repository added after a rename does not inherit guessed
old-name branches. Missing required rename history blocks abandonment with restore
guidance. Unsupported remotes are explicitly reported as local cleanup only, and
lookup failures stop the operation. Supported PRs have both their head and base
in the GitHub repository recorded by `origin`; fork-origin PRs targeting a separate
upstream repository are outside this integration.

The summary identifies the project, worktrees, local branches, PR numbers/URLs,
unmerged work and cleanup warnings. `Abandon this project? [y/N]` requires an
interactive terminal and explicit `y` or `yes`; no, empty input and EOF cancel.
After approval Baton closes the listed PRs without comments or remote branch deletion,
then runs ordinary cleanup. Dirty files, ignored notes and unmerged work can require
a second cleanup confirmation; archive refs and coordination safeguards still apply.

PR closures can succeed even if later closure or cleanup fails. Resume with the
exact `baton abandon FULL_UUID` command printed by status. Intent survives partial
closure, worktree removal and coordinator parking. The confirmed historical branch
inventory stays in pending intent, so recovery still checks old PRs after the
coordinator project branch has been deleted. Retries recheck PRs; changed
local scope or new/reopened PRs require a fresh summary and approval. Cleanup without `--force`
cannot bypass pending abandonment. If a recorded `origin` URL changes, restore the
original URL printed by Baton before resuming. Both successful cleanup and abandonment produce ordinary archived projects. GitHub changes and local deletion are separate systems;
Baton rechecks before cleanup but cannot lock remote PR state throughout deletion.

## Follow-up projects

From a coordinator slot, use a closed project's full UUID:

```sh
baton create follow-up --from 0123456789abcdef0123456789abcdef
```

Only archived projects qualify, whether cleaned or abandoned; source ownership is
not required. Ordinary `create NAME --repos ...` remains unchanged, including a
project literally named `follow-up`. Repository inputs and `--from` are exclusive.

Names are automatic: `example` produces `example-followup`; creating from that
closed follow-up produces `example-followup2`, then `example-followup3`. The
lineage survives renaming a follow-up.
A regular project naturally named `example-followup2` starts a separate lineage as
`example-followup2-followup`. Repeating from the same predecessor derives the same
name. Active names and previous names block creation actionably; Baton never skips
a sequence number. Reusing an archived name follows ordinary creation rules and
gets a fresh UUID and artifact directory.

The source repository set includes added repositories and those with all members
retired. Missing or replaced recorded checkouts block creation; current alias
mappings cannot substitute another repository. New worktrees use freshly fetched current defaults and
current configuration, with a new identity, unclaimed owner and fresh blackboard.
Old members, stacks, code branches, archive refs, owners and tasks are not copied.
Occupied-slot replacement uses normal confirmation and cleanup safeguards.

Before activation, Baton generates and locally commits `follow-up.md`. No artifact
remote fetch or push is needed. Applicable local artifact checkout, conflict,
identity and ignore checks run before replacing an occupied slot. Interrupted
creation resumes with the exact `create follow-up --from UUID` command in status,
retaining the reserved identity and refusing unexpected context files or symlinks.

Status JSON exposes `follow_up_from`, `follow_up_path`, `follow_up_root_name` and
`follow_up_number`; human status and ownership claims point to the context file.
Claim stdout remains JSON, with reading guidance on stderr. Read this context when
taking ownership. It links the immediate predecessor's saved plan, optional retro
and coordination overview/history, reporting missing evidence as unavailable.
A predecessor's earlier context is linked for manual backward discovery: C points
to B, and B still points to A. Historical plans, tasks, instructions, paths and
ownership are evidence to reconsider against the new task and current code,
never live authority. Old slots, code worktrees and live boards are not context links.
