# Blackboard and tasks

## SQLite coordination

Projects use an ignored `blackboard.sqlite` with SQLite schema 2. The live
database, sidecars, locks and temporary files must stay ignored in the artifact
checkout; the built-in checkout configures this, and a custom one needs the rules in
[Custom artifact repository](configuration.md#custom-artifact-repository).
`baton status --json` reports the board's authoritative paths.

`blackboard init` creates a missing board, and plain `blackboard read` prints it as
Markdown. `post`, `update` and `edit` take their body from `--text`, `--file` or
stdin.

`blackboard post` returns an entry ID and version. `--kind` is free-form
(default `note`), for example `decision` or `finding`; use `--reply-to ID` for
threads.
`blackboard update --id ID --expected-version N --agent NAME --text BODY
--expected-generation GEN` updates body/kind with optimistic version checking.
Routing and original authorship are immutable. `blackboard edit
--expected-revision REVISION --expected-generation GEN --agent NAME --file FILE`
updates only the shared overview using its `revision`, which every `read --json`
returns (`read --tail 0 --json` returns just the overview); entries and prior
versions remain in history. New boards start with a current-state overview template (goal,
phase/status, owners and assignments, blockers and open questions, next steps,
pointers) that the parent replaces at major handoffs. `read --json` includes structured entries and
audit events; `--since CURSOR --limit N` advances through ordered changes. `--to
NAME` includes broadcasts. `--tail` is a snapshot selector and cannot be combined with
`--since` or `--limit`.

The `settlement` kind is reserved: only `task settle` creates settlement entries,
`post` rejects the kind, and `update` cannot change an entry to or from a
settlement. `read` and exports render settlements readably.

### Waiting, questions, and liveness

`blackboard wait --since CURSOR [--to NAME] [--kind K,...] [--task ID]` returns
entries created after the cursor, polling until one matches or `--timeout` (default
90 seconds, below common agent command limits; pass a longer one for background
waits) elapses. It accepts cursors from `read --json` or an earlier `wait`.
Filtered waits require `--since`, so nothing posted before the first wait is skipped.
The returned cursor is the highest sequence scanned, even on timeout; continue from
it. Only creations are reported, never later updates. `wait --reply-to ID` returns
existing replies to an entry immediately, otherwise it waits for one. Timeouts exit
0 with `"timed_out": true`. Stale generations and frozen or sealed boards are errors.

`blackboard ask --to NAME --agent NAME [--task ID] --text QUESTION` posts a
`question`, prints its ID and resume command to stderr, and waits for its reply
(default 90 seconds). On timeout it returns the question ID; resume with `wait --reply-to ID` rather than asking again. Reply with
`post --reply-to ID --kind answer`.

`blackboard agents [--quiet-after SECONDS]` reports, per agent that wrote to the
board, its last write, in-progress tasks, settlements of current task versions, and
unanswered questions. Its advisory verdict is `waiting` (unanswered question),
`settled` (settled with no task in progress), `active` (wrote within
`--quiet-after`, default 900 seconds), or `quiet`. Baton cannot observe processes:
verdicts are advisory, and none proves an agent exited.

`wait`, `ask` and `agents` require `--expected-generation`; `wait` and `agents` are
reads and return JSON.

### Export

Use `baton blackboard export --selector PROJECT --expected-generation GEN` for
Markdown analysis. The default ignored `.blackboard-export/` contains
`overview.md`, complete `history.md`, a standalone `board.sqlite`, and checksummed
`manifest.json`. Optional `--output DIRECTORY` requires a safe destination
belonging to the same board; sealed evidence cannot be overwritten. Existing SQLite
sidecars must be recovered or moved before replacing a snapshot. Retros receive the
Markdown paths; analysis does not require SQLite tooling.

Exports include complete task descriptions, states, references, dependencies and
audit versions, with task/dependency/state counts in the manifest. They also
include readable recorded owner, repository/member/branch/ref and plan context
beside task state and complete history. Fields absent from historical metadata are
reported as not recorded. The `overview.md` task section marks held tasks and lists
unanswered questions (questions with no reply), derived only from the snapshot.

## Cleanup and sealed archives

Cleanup checks required artifact ignore rules before changing lifecycle state.
Invalid ignore rules must be repaired while coordination remains writable. Cleanup freezes writes, validates a complete sealed bundle in tracked
`coordination/`, commits it locally, and verifies the committed files before
removing any worktree or native stack metadata. The archive records sealing, without
claiming unfinished tasks or cleanup completed.

Archive/commit failures leave normal cleanup blocked and resumable: repair the artifact
checkout or restore the sealed source, then repeat cleanup. Last-resort
`cleanup --force` can release the slot with a warning that archival failed;
the shared artifact directory and surviving board files are retained. Do not thaw or edit
sealed state. A failed remote push retains the verified local commit; retry artifact
publication when access is restored. Archived retros use the saved Markdown and
frozen UUID, never reused slots or worktree paths.

Final sealing freezes the recorded owner, repository and plan context; cleanup
retries reuse it and committed evidence bytes, including when derived Markdown needs
recovery. Closure preserves unresolved work without completing tasks.

## Tasks and blocking prerequisites

Project-local tasks live in the same database and immutable audit stream as
notes. Other database schemas are rejected; Baton does not migrate or
upgrade them.

All task commands return JSON. Mutations require `--agent` and
`--expected-generation`; existing-task mutations also require `--expected-version`.
The CLI can infer the project from context, but workers should pass an explicit
`--selector` to avoid targeting a reused slot. Use returned versions for subsequent
changes. Parent creates; worker claims and settles:

```sh
baton task create --title 'Implement interface' --description 'Acceptance evidence' \
  --priority 2 --assignee worker --repo REPO --selector PROJECT \
  --agent parent --expected-generation GENERATION
baton task claim --id TASK --expected-version 1 --selector PROJECT \
  --agent worker --expected-generation GENERATION
baton task settle --id TASK --expected-version 2 --outcome succeeded \
  --summary 'Result and evidence' --files src/a.py --report report.md --to parent \
  --selector PROJECT --agent worker --expected-generation GENERATION
```

Other coordination commands:

```sh
baton task dependency add --id CHILD --prerequisite PARENT --expected-version 1 \
  --selector PROJECT --agent parent --expected-generation GENERATION
baton task list --selector PROJECT --ready --agent worker
baton task list --selector PROJECT --blocked
baton task list --selector PROJECT --held
baton blackboard post --task TASK --text 'Evidence for this task' \
  --selector PROJECT --agent worker --expected-generation GENERATION
```

### States

Tasks start `open`; claim atomically checks unfinished prerequisites and assignment
then enters `in_progress`. Only its assignee can release or finish that claim.
Release returns `open` and clears assignment. `assign --assignee OTHER` explicitly
reassigns, releasing an active claim to `open`; `assign --unassign` recovers an
abandoned claim. Cancellation preserves evidence and assignment. `done` and
`cancelled` are terminal; only `done` satisfies prerequisites.

Workers end a claimed task with `settle` exactly once: in one transaction it checks
the claim and version, records a settlement entry, and moves `succeeded` to `done`
or `failed` back to `open` with the assignee kept. A failed task is held: `claim`,
`update` and dependency changes are refused until the parent runs `assign` to retry
or reassign it, or `cancel`.
Every claim and settle bumps the version, so a late settle from an earlier claim
fails. Repeating the settle that produced the current version returns the existing
settlement with `"replayed": true`. `done` completes a task you claimed yourself
without recording a settlement entry.
Show/list report `settlement` only when it produced the current version, and
`held`; held tasks are not ready, and `task list --held` lists them.

Model a decision as a task that dependents list as a prerequisite; settling it
`succeeded` with the choice in the summary makes them ready. Metadata can still be
updated on terminal tasks. Assignment is not ownership: assignment labels provide
cooperative coordination, not identity authentication, session ownership or
project, worktree or stack authority. No task transition
authorizes branch, stack, cleanup or publication operations.

### Metadata and dependencies

Use `task update` for single-line title, description, nonnegative integer
priority (0 highest, default 2) and optional repository/member/plan
references. Repository/member must exist when supplied or changed; historical
references survive unrelated edits and retirement. Use `--clear-repo`,
`--clear-member` and `--clear-plan` to clear nullable references (combine clearing
the member with moving to another repository). Member requires repository; plan is
descriptive text, never executed.

Dependencies are project-local, reject cycles and can only change on open tasks;
release before editing an active task's prerequisites. Duplicate edge changes are
no-ops after generation, state and version checks. List filters include state,
assignee, repository and member; ready queries optionally check the requesting
agent's assignment eligibility. Blockers identify cancelled prerequisites
explicitly. Show/list return `tasks`, each with prerequisites, blockers, readiness
and assignment eligibility. Priority orders results; it does not schedule work.

### Handoffs

Every handoff prints read-only task list/show commands scoped to the selected
project and worker. Inspect assignment eligibility and prerequisites before an
explicit claim. Handoffs read the current board without initializing or changing it. For example:

```sh
baton handoff --selector PROJECT --agent worker --repo REPO --task TASK
```

`handoff --task ID` also prints the worker's claim command with the current
version, `ask` and `wait --reply-to` commands addressed to the task's creator, a
`post --task` progress command, a `settle` template, and the worker obligations.

Every handoff prints a `Refresh instructions` command: the same handoff with the
project UUID, resolved repository and member (or planned member and predecessor),
and task. Workers rerun it after a restart or context loss instead of
reconstructing commands; coordinators dispatch the handoff output verbatim with
that command. For a task already `in_progress` and assigned to the requesting
agent, the handoff omits the claim, prints the task's current version in the
settle command (a `task update` after the claim makes the claim's version stale),
and prints `Recover task history`, a non-blocking `wait --task` read from the
start of the board that returns the task-linked notes, questions and settlements.
There, if the selected member needs a rebase and the task names no other member,
the handoff warns instead of refusing, so the worker can still ask and settle while
the parent rebases. Other tasks the worker cannot claim are refused:
held, settled, blocked by prerequisites, or assigned to another agent.
