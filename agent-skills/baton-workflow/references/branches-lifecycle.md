# Branches, publication, and lifecycle

This reference owns branch operations and publication. For multi-branch planning,
read the [plan reference](../../baton-plan/references/multi-branch.md), which owns
plan layout, branch assignments and separate review gates. Read unchanged required
references once per task; links back here do not require repeated reads.

`branch prune`, `abandon`, `create` into an occupied slot, and any retirement or
cleanup that reports warnings require confirmation in an interactive terminal.
Agents run the command with `--dry-run` where available, then give the user the
exact command to run.


## Member retirement

Whenever a member's PR is merged, the parent agent must run
`baton branch cleanup <repo> <member>` as the normal follow-up, without waiting
for a separate cleanup request or project
closure. The readiness, integration and safety requirements below still apply.
If checks block cleanup, report the blocker and keep it pending until resolved;
do not automatically override checks or discard work just because the PR merged.

Use `baton branch cleanup <repo> <member>` to retire one integrated or abandoned member,
or `baton branch prune [<repo>]` to preview and confirm all eligible members;
both accept `--dry-run` and `--selector`. Only the parent performs retirement,
after affected workers/reviews have returned and services have stopped. Archive
needed ignored notes before removing their checkout, following
[Archive Repository Notes](../../baton-retro/SKILL.md#archive-repository-notes)
and its [artifact commit](../../baton-retro/SKILL.md#verify-commit-and-report) step. Retirement leaves the slot,
plans and blackboard active; status JSON always includes `retired_entries`, and
`status --all` adds them to text output. Do not hand work to retired paths or reuse their member names. When a
native predecessor is retired, use normal `gh stack rebase`, not `--no-trunk`.
If a group's native top is retired, start a new independent member/group instead
of attempting to extend it from an earlier member. Resume pending retirement
using the exact command printed by `status`; whole-project cleanup stays separate.

A closed, unmerged PR is abandonment evidence only when its head matches the exact
local tip. Any open PR for the branch blocks retirement. Preserve and report new
local commits rather than treating an older closed PR as abandonment of that work.
Abandoned native stack members retire only as a whole group through `branch prune`
once every live member is eligible; do not remove one member from native tracking
or mark it merged to bypass that requirement. The project remains active.

## Stack readiness and operations

Use native `gh stack` for dependent PRs; never stack branches manually.
Project membership is separate from PR topology: a single repository may have
standalone PRs and several native stacks in one project. Use
`baton branch init <repo> <first-member>` to name the first checkout without
native tracking. `baton branch add <repo> <name>` always starts from the
freshly fetched default branch, even if invoked from a stack checkout; its PR
must target that default. Add `--after <member>` only to explicitly extend that
member through native stacking; it must be the stack top, or an independent
branch with real changes that becomes a new native stack root.
The manifest's `stack_root` per entry and `stacks` inventory identify actual
groups; do not infer dependencies from branch prefixes.
Use `baton status` to inspect expected PR bases and group membership.
Use `baton stack status <repo> --member <name>` to inspect a specific group. Implement and commit the first real change in the first worktree
before adding a child; never leave an empty project branch beneath the real
PRs. The first PR must target the repo default (`main`/`master`), and later PRs
target their native predecessor. Empty commits do not satisfy the layer guard.
Run native `gh stack rebase`, `sync`, `push`, and `submit` from a member
checkout of the same native stack group, never from the coordinator or another
group's worktree; keep affected worktrees idle during history rewrites. If you
add a branch directly through `gh stack`, restore the initiating worktree's
registered branch and use `baton branch register <repo> <member> --after <predecessor>` before
cleanup. Do not use native `sync --prune`: project cleanup owns branch deletion.

Only the project's parent (never a branch worker) runs `baton add|branch|rename|cleanup|abandon` and
native `gh stack rebase|sync|push|submit`; workers may run `baton stack status`. A worker that needs a topology
change or a rebase (including a review stopped by `needs_rebase`) posts the
request on the blackboard, sends the parent a direct runtime message when
supported, then stops and returns it in its final report. The board wakes no
one, and the parent waits for the worker to return before rewriting history.
The parent rewrites a stack's history only after the workers in its checkouts
have returned, including all reviews and other launched processes. A first
real commit makes a predecessor eligible for a child; by default, wait until
the interfaces and code the child consumes are stable, tested, and committed.
The predecessor's worker hands off that readiness checkpoint, the parent runs
`branch add --after`, then the child's implementation worker starts in the new
worktree. Child planning can proceed earlier using planned names and paths,
even if the predecessor needs a rebase:
commit the planned subplan from the coordinator with the planned handoff's
command, not from the predecessor's inspection worktree, and refresh the handoff
once the parent creates the child.
Name known first members with `branch init` before writing the master
worktree table, subplans, or spawning workers; never while agents work in that
checkout, because it moves the worktree. When a single-branch project grows,
wait for or stop the active worker first and give any replacement the new path.
## Publishing PRs

Publish draft PRs only within authorized implementation work. Drafts may expose
pending execution validation when clearly labeled; merge readiness requires the
applicable independent validation evidence, with material failures and required
coverage gaps resolved. Independent branches
use a PR subagent following repository conventions against the entry's `pr_base`.
For native stacks, the parent publishes the group: run `gh stack submit --auto`
(it pushes every branch and opens draft PRs on the right bases) from a member
checkout of the group, after rebasing any child with `needs_rebase` and running
the repo's pre-push checks in every member checkout, diffing against that
member's `pr_base`, including repository-required note/archive guards. A PR
subagent may then only set titles/descriptions with `gh pr edit`. Give it the
entry's `pr_base` and prohibit repo ship/PR workflows, rebases, merges, pushes
and `gh pr create`. Never run a repo's own PR/ship workflow on a stack child.

## Merging PRs

Never merge a PR without explicit user approval of the merge action for that
PR or a clearly identified set of PRs. The same requirement applies to enabling
auto-merge or any workflow that will merge a PR later.

Approval to implement or execute a plan is not merge approval, even when the
approved plan explicitly calls for merging. Passing checks, successful reviews,
and implementation readiness do not grant merge approval either.

Prepare the PR and complete the required checks and validation first. If the
user has not already explicitly approved the merge action, present the PR links,
readiness evidence and any remaining issues, then ask for merge approval. Proceed
only within the scope explicitly approved by the user.


## Whole-project cleanup

Before `baton cleanup`, run `baton retro [--selector <project>]` and await the owner-session fork completely. It reads the complete sibling [baton-retro skill](../../baton-retro/SKILL.md), writes `retro.md`, and archives relevant repository notes. The retro agent owns the final artifact and decides whether to delegate sections; it never claims ownership. For an unowned project or work outside the framework, read that skill completely and invoke it directly rather than inventing an owner session.

Cleanup seals a verified archive (`board.sqlite`, `overview.md`, `history.md`,
`manifest.json`) under project `coordination/` before deleting worktrees. Archive
failure blocks cleanup; repair and resume without editing or thawing sealed state.
Sealing does not complete unfinished tasks.

`baton cleanup [PROJECT] --force` is last-resort local slot release. It reports
blockers and always asks for interactive confirmation; `--force --dry-run` only
previews. Preservation/removal failures become warnings when the coordinator can
still be safely reset and the active slot record released. Unsafe or unremovable
resources can remain; report every leftover and preservation gap. Keep primary
checkouts, other projects and symlink targets protected. Force cleanup never
closes PRs, including pending abandonment PRs. Use it only within the user's
explicitly authorized cleanup scope, and preserve needed evidence first when
possible.

Cleanup deletes ignored files in code worktrees and flags configured note files (default `notes/` and `thoughts/`) not archived under the project. `BATON_NOTE_DIRS` explicitly overrides that list; include every participating repository's note directories. Collect final handoffs and stop/join all workers, reviews, and services before cleanup. Never return with a launched review pending. Resume pending cleanup with the exact status command. Retirement of merged members remains a separate, immediate follow-up.


For authorized abandonment, run `baton abandon [<project>]` after the same retro
and worker/service joining duties. Inspect its state/PR summary and explicitly
confirm in a terminal. It closes all supported open project PRs (including
retired branches), then invokes ordinary cleanup; cleanup warnings may need a
second confirmation. It never deletes remote branches. Lookup or close failures
leave resumable intent and existing worktrees; cleanup failures can leave partial
local progress after PRs have closed. Resume with status's exact `baton abandon`
command, including after coordinator parking. Changed local scope and newly
opened/reopened PRs require renewed confirmation. Cleanup without `--force` must resume pending intent; do not use another
topology command to bypass it. Explicit force cleanup instead reports incomplete
PR closures and releases only the local slot. Completed abandonment is ordinary
archival and can be the source of an explicitly authorized follow-up project.
