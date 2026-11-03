# Multi-branch project plans

Use this workflow when the project has more than one branch in total, in one
repository or across several, whether the PRs are independent, stacked, or a
mixture. Project membership does not imply a PR dependency. Single-branch
projects do not need this reference.
Run only the requested phases and assigned scope: a validation request does
not restart project planning, and a branch worker does not take over the master.

## Plan layout and scope

Use `master_path`, per-entry `subplan_path`, registered names, worktrees and
PR bases from `baton status --json`. Keep all plans beneath the master's directory;
never reconstruct registered paths. For planned children, use
`baton handoff --agent AGENT --repo REPO --planned-member NAME --after PREDECESSOR`
for labeled future paths.
Outside a project, use the base skill's artifact directory and agreed branch
names. Unless the user supplies another layout, keep the master at `plan.md` and
subplans at `branches/<repo>/<branch-suffix>/plan.md` beneath that directory;
remove a branch prefix only when it is known. Registered paths always win.

The master stays high-level: shared goal, requirements and non-goals, cross-repo
contracts, branch boundaries, dependency/merge order, integration acceptance
criteria, and links to every branch subplan. Record each branch's owner,
repository, intended PR base/predecessor, worktree, and readiness/status. Do not
duplicate branch implementation steps in the master or infer dependencies from
similar names. Use the project framework's independent/native-stack workflow
and registered topology when arranging branches.

Each subplan covers one branch's bounded changes, files/components, interface
obligations, dependencies, focused tests, acceptance criteria, and PR base. Link
back to the master and to any prerequisite subplans. Include an issue link when available
using the base skill's conventions; reuse the master's issue unless a distinct
branch issue is already specified. Keep the plans brief but implementable.

If a single-branch project grows into this workflow, retain its project
`plan.md` as the master and move branch-specific detail into the corresponding
subplan, preserving completed work and decisions. If branch names change,
rename their subplan directories and update the master's links/mapping.

## Parent and branch-worker ownership

The parent owns the master, partitioning, shared contracts, scheduling and
integration. Read the workflow's [project worker handoff](../../baton-workflow/references/coordination.md#project-worker-handoff)
and [tasks, questions, and settlement](../../baton-workflow/references/coordination.md#tasks-questions-and-settlement)
sections completely. They own assignment fields, board/messaging and task duties.
Before topology/history changes or publication, read
[branches and lifecycle](../../baton-workflow/references/branches-lifecycle.md)
completely; only the parent operates topology/native stacks. Keep affected
workers and processes stopped during history rewrites.

Always delegate each branch subplan and its implementation to a branch-worker
subagent, preferably the same worker for both. If unavailable, report the
limitation and ask for direction rather than taking over. For replacements,
hand off the reviewed plan, decisions, review results and exact worktree.
Apply [Worker dispatch](../../baton-workflow/references/coordination.md#worker-dispatch)
before launching. Add the complete base planning skill and this reference to the
generated handoff, with explicit phase/scope and repository instructions.

Name known first members with `branch init` before writing worktree tables or
subplans or spawning workers. Child timing and early child planning follow
[stack readiness](../../baton-workflow/references/branches-lifecycle.md#stack-readiness-and-operations).
Task readiness never grants topology or publication authority.

Workers own only their assigned branch/subplan and report proposed shared-scope
changes to the parent. They do not split their branch into further branches
without agreement. A worker assigned one subplan must not recursively spawn
workers to own all the project's other subplans; it may use the base skill's
specialist research, review, and PR workers for its own assignment.

Plan generation can run concurrently after the parent sets clear boundaries.
Implementation can run concurrently only where dependencies and ownership
allow it; dependent stack members wait for the prerequisite contract/change.
Use each registered checkout rather than switching managed worktrees between
branches, and avoid concurrent edits to the configured shared artifact checkout.

### Stack children and repo workflows

Before following any repo-provided workflow for a native stack child
(repository skills, contributing docs, `AGENTS.md`/`CLAUDE.md`, helper
scripts), the worker scans it for steps that sync with or compare against the
default branch: pulling, merging or rebasing onto `main`/`master`/`origin/HEAD`,
`merge-base` with it, drift checks, or review diffs against it. Then:

- Comparisons and review baselines use the child's expected PR base from
  `status --json`. If that base is marked unreliable, stop and tell the parent.
- Anything that rewrites or integrates history is replaced by
  parent-coordinated native `gh stack sync`/`rebase`, after affected workers
  have returned and all their launched processes have finished. Never rebase
  or merge the default branch into a child.
- If a repository workflow conflicts with these stack rules, report the
  conflict to the parent before proceeding with that step.


## Separate planning and implementation reviews

1. The parent drafts the master and dispatches the base skill's dedicated plan
   review worker with `Reviewers: configured`. Review project scope,
   decomposition, contracts, topology, sequencing, and end-to-end verification,
   not detailed branch mechanics. Refine the master before workers finalize
   their subplans; provisional research/drafts may proceed in parallel.
2. Each branch worker runs Phase 1 and Phase 2 for its own subplan, with a
   separate plan review worker/report and readiness verdict. Apply the same
   reviewer/model policy and triage rules as the base skill: author and review
   the assigned subplan path directly with `Reviewers: configured`. The
   master remains the parent's high-level document.
3. The parent reconciles branch findings and checks consistency with the
   master. Material changes to shared contracts, scope, or dependencies require
   a master-plan review; material changes within a subplan require its own plan
   review. Communicate affected decisions to peers through the blackboard.
   Neither review substitutes for the other. Commit meaningful updates following
   [artifact safety](../../baton-workflow/references/startup.md#worktree-and-artifact-safety).
4. When implementation is requested, dispatch the branch workers with reviewed
   subplans. Each runs Phase 3 with its own focused checks and implementation
   review cycle. Follow the base skill's implementation steps and review loop,
   including the repository's required checks.
   Review against the branch's expected PR base from `status --json` (default
   branch for independent PRs, predecessor for stack children), not always
   main/master. A stale child is rebased first
   ([`needs_rebase`](../../baton-workflow/references/startup.md#resolve-the-project)).
   Supply the master and relevant sibling contracts as
   review context, without widening the branch's diff to the whole project. A master
   review never clears an individual branch's gates.
5. After branch gates pass, the parent checks combined work against the master,
   runs authorized integration tests and dispatches an independent read-only
   integration reviewer. Apply [Worker dispatch](../../baton-workflow/references/coordination.md#worker-dispatch)
   and [Review-only specialists](../../baton-workflow/references/coordination.md#review-only-specialists)
   completely. Supply master/subplans, all worktrees, explicit branch/base pairs,
   relevant contracts and repo instructions. This is not a single-branch runner
   invocation; inspect participating diffs/interactions without switching branches
   or creating a synthetic merge. Require coverage, severity and accept/reject/defer
   triage. The dispatcher records durable outcomes; send fixes to branch owners
   and rerun affected checks/reviews. Repeat integration review after fixes until
   no accepted P0/P1/P2 findings remain, with a maximum of three passes. Report
   unresolved findings, missing coverage or failed reviewers; separate branch
   passes do not establish project readiness.

6. Run [Phase 4 validation](validation.md) on the committed candidates; the parent
   owns combined criteria, participating revisions and fix routing.

Keep master/subplan readiness, implementation/integration code review and execution
validation outcomes distinct in the handoff.
