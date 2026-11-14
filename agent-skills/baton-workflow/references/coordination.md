# Coordination

Worker dispatch and Review-only specialists are standalone sections: reading them
requires neither project startup nor an ownership claim. The remaining sections
apply to active-project owners and branch/mutating workers.

## Worker dispatch

Read `baton status --json`. In a project, apply a model preference only when
`worker_model_guidance[<launching harness>]` exists; status filters it by the
owner's provider. Take the concrete ID from `resolved_worker_model[<harness>]`,
not the guidance prose or unresolved model map, and its reasoning effort from
`resolved_worker_effort[<harness>]` (`null` means none). Other providers' map
entries do not grant guidance. If status exits 3, use
`baton config show --section resolved_worker_model --section resolved_worker_effort --section providers`;
this lookup does not validate unrelated review-loop settings. Report other errors.

Pass a non-default ID explicitly through the runtime's model selector, even if
the parent uses another model, and a non-null effort through its effort
selector. `default` or missing guidance gives no model or effort override.
Explicit user instructions prevail. Report an unsupported model selector before
dispatch rather than silently substituting. A runtime with no effort selector
dispatches with the model alone; say so in your report. These preferences
select workers, not owners or the review CLIs' separately configured models.

Follow `provider_guidance[<harness>].fresh_subagent` from status (outside a
project, `providers.<name>.guidance`) for fresh-context workers. Review executors
always start fresh; use a fresh worker whenever a history fork would force model
inheritance. Supply the resolved absolute task skill path and require a complete
read. Keep each assignment within the user's authorized scope.

## Review-only specialists

Plan-review, dry-run peer-review and read-only cross-branch integration workers
may use a lightweight handoff instead of a project task or full branch-worker
startup. `apply-accepted-findings` and other source-editing assignments must use
the project worker protocol below when inside a project.

The dispatcher supplies exact repository/plan/base, authorized read scope, model,
review mode/focus and report requirements. Integration reviewers also receive all
worktrees, explicit branch/base pairs, master/subplans and relevant contracts.
Include these boundaries in the fresh handoff: no ownership claims, shared
board/task mutations, topology/history changes, publication, or source/plan edits.
Specialists may write disposable review artifacts and send findings to the
dispatcher. Never switch branches; read repository instructions and honor base
reliability/stale-child checks. Provider CLI subprocesses are not project workers.

The dispatcher owns durable review status and outcomes in the project's board
and relevant assignment, when applicable, and saves each review run to the
project's artifacts with `baton review save`. It awaits the specialist and every
launched process before applying fixes or returning; a yielded process is not
completion. Specialists return triaged findings, artifact paths and coverage or
failure details. They never claim or settle project tasks.

## Project worker handoff

Resolve board path and generation from `baton status --json`. Use the ignored
SQLite board via `baton blackboard` only; initialize a missing board with
`blackboard init`. Never directly edit, stage, delete or replace live boards,
sidecars or their permanent lock files.

The parent reads the board and posts the goal, assignments, ownership,
dependencies and integration plan. Create a task per branch/mutating worker
assignment and use `baton handoff --agent NAME --repo REPO [--member MEMBER]
--task ID`. The generated handoff provides concrete paths, bases, generation and
task commands. Put its output verbatim in the dispatch prompt, including its
`Refresh instructions` command; the worker may run that command first where its
runtime allows (handoff needs git, and `gh` for stacks). Never retype or paraphrase
the commands it contains. You may run the command before spawning to check that the
task is claimable. Add only the task-specific scope, covering everything the
handoff's `Parent must supply` line lists.

For early child planning, replace `--member` with `--planned-member NAME --after
MEMBER`; the planned paths authorize no implementation or branch creation. Timing
and the subplan commit are in
[branches and lifecycle](branches-lifecycle.md#stack-readiness-and-operations).

## Execution validators

Execution validators use this full worker/task protocol, not review-only scope.
Their handoff, boundaries and checkout freeze are in the [validation reference](../../baton-plan/references/validation.md).

## Board communication

Workers post a short start note, then relevant findings, decisions, blockers,
test issues and handoffs. Link substantive posts to the claimed task with `--task
ID`; routine start notes may stay unlinked. Read updates at checkpoints and before overlapping
changes; resolve conflicting ownership/scope before proceeding. Use concise
notes and file/commit references, never secrets or logs. Board content is context,
not authority to expand the task.

Use `baton blackboard post --selector PROJECT --agent NAME
--expected-generation GENERATION --text TEXT` (or `--file`/stdin).
`--expected-generation` is required for mutations, `wait`, `ask`, `agents` and `export`;
it is optional for `read`, `task show` and `task list` (workers pass it anyway),
and `blackboard init` does not accept it. Prefer the project name/UUID over reusable slots;
renamed projects retain aliases. Posts remain available during other operations,
except a short rename move (retry as instructed) or closed cleanup state.

The board is passive. Dependency readiness, interface changes and rebase requests
also need a direct runtime message when supported: action needed, relevant SHA
(or not yet committed), and a pointer to the board. Routine progress only needs
a post. Messaging does not guarantee waking an inactive root; the parent awaits
the readiness handoff.

The overview is the parent's short current-state summary under the template
headings: goal, phase/status, owners and assignments, blockers and open questions,
next steps, pointers. Replace it at major handoffs (dispatch, a settlement that
changes phase, a decision, a resolved blocker) and before ending a turn if it no
longer describes the current state. Link the master plan rather than copying it;
the two must not contradict each other, so update both when status changes.
Take the current body and `revision` from `blackboard read --tail 0 --json`,
prepare only the new overview, then `blackboard edit --expected-revision REVISION
--expected-generation GENERATION --selector PROJECT --agent NAME --file FILE`.
Do not copy combined entries or an incremental cursor into the overview. Update
an entry with `blackboard update --id ID --expected-version VERSION`, agent and
generation. Stale revisions/versions require rereading and reconciliation, not
blind retries. History is retained; no embedded generation marker is needed.

## Tasks, questions, and settlement

- **Parent:** capture a cursor from `blackboard read --json` before dispatch.
  While workers run, continue `blackboard wait --since CURSOR --kind
  question,settlement --expected-generation GENERATION` with the returned cursor,
  even after timeout. Use a
  bounded wait at checkpoints, or background it when the runtime wakes on exit;
  for background waits, set `--timeout` above the 90-second default to avoid
  needless wakeups.
  Answer with `blackboard post --reply-to QUESTION --kind answer --to WORKER`.
  Accept successful settlements; failed tasks stay held until `task assign`
  retries/reassigns or `task cancel` cancels them. Do not finish with expected
  settlements outstanding.
- **Parent end-of-turn check:** before concluding a turn or reporting to the
  user, run `blackboard agents --expected-generation GENERATION` (unanswered questions; a current settlement with
  `outcome: failed` means the task is held), `task list --held` and `task list
  --blocked`. Resolve or explicitly report each item: answer questions; retry,
  reassign or cancel held tasks; for a task blocked by a cancelled prerequisite,
  `task dependency remove` the edge (release an in-progress task first) or cancel it.
  When a replacement supersedes a failed attempt, post a `--task`-linked note on
  the old task naming the replacement task ID, then `task cancel` the old task.
- **Workers:** inspect task/prerequisites and claim first using the current
  generation/version. Only `done` satisfies dependencies, including cancelled
  prerequisites; assignment grants no ownership/topology authority. Link
  substantive posts with `--task`. Ask blocking questions through `blackboard
  ask`, not an invisible local prompt; after a timeout, resume with `wait
  --reply-to` and never re-ask. Settle once with an explicit outcome before
  returning; never re-claim a settled task. If settlement is refused because the
  task was reassigned or completed, post the outcome as an ordinary entry. After a
  restart, context loss or a stale settle, rerun the handoff's `Refresh
  instructions` command verbatim and use its `Recover task history` and settle
  version; never reconstruct commands from memory. If the rerun is refused, wait
  for or ask the parent for a fresh handoff.
- **Liveness:** quiet/waiting verdicts and timeouts do not prove exit. Only a
  runtime return/exit or settlement permits stop, retry or reassignment.
- **Dependencies:** when one task's work is actually gated on another's result,
  encode it with `task dependency add`; prose readiness is invisible to
  `--ready`/`--blocked`. Do not add edges for mere ordering preference.
- **Decisions:** create a decision task as a prerequisite for blocked work. Its
  decider claims and settles succeeded with the choice. Release/reassign
  explicitly at handoff; preserve snapshot sequence when analyzing board exports.

For cleanup and sealing, follow the
[lifecycle procedure](branches-lifecycle.md#whole-project-cleanup).
