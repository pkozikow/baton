---
name: baton-workflow
description: "When started in a Baton coordinator slot under {{BATON_SLOTS_ROOT}} (p1 through p10), read and follow this skill before project work. Also use from anywhere when asked to find, revisit, or ask about a current or past Baton project, or explicitly instructed to work in one."
---

# Baton workflow

Use this skill in configured coordinator slots, for finding or revisiting Baton
projects from anywhere, or when explicitly assigned Baton project work. The
installed slot-root description is discovery metadata; `baton status --json`
owns current project identity and paths. Operate only within the user's scope.

Terms used throughout:

- **Owner:** the session that claimed the project with `baton session claim`.
- **Parent** (dispatcher): the agent that launched a worker or reviewer.
  Topology, publication and lifecycle authority belongs only to the parent of
  the branch workers, never to a branch worker dispatching its own reviews.
- **Coordinator:** a slot directory (`p1` through `p10`) holding project
  metadata; never an agent.

## Required reading by role

Read this entrypoint completely, then the sections for your role. Do not reload
unchanged sections already read during this task.

- **Discovery or historical questions:** read [Discover or revisit a project](references/startup.md#discover-or-revisit-a-project)
  and [Session identity and authority](references/startup.md#session-identity-and-authority).
  No active project or ownership claim is needed. When the user authorizes new
  work from a past project, read [Create a follow-up](references/startup.md#create-a-follow-up-from-historical-work).
- **Project owner or branch/mutating worker (including execution validators):** read startup's
  [Resolve the project](references/startup.md#resolve-the-project),
  [Session identity and authority](references/startup.md#session-identity-and-authority),
  and [Worktree and artifact safety](references/startup.md#worktree-and-artifact-safety),
  then [coordination](references/coordination.md).
- **Read-only review specialist:** read [Review-only specialists](references/coordination.md#review-only-specialists)
  and the assigned executor; full project startup and task settlement do not apply.
- **Before topology/history changes, publication, retirement, or cleanup:** read
  [branches and lifecycle](references/branches-lifecycle.md). A merged member PR
  triggers this read and the owner's required retirement follow-up, without a
  separate cleanup request. Honor safety blockers; this duty does not authorize
  publication or whole-project cleanup.

Execution validators also read the complete [validation reference](../baton-plan/references/validation.md).

## Task routing

Use [baton-plan](../baton-plan/SKILL.md) for planning, readiness assessment,
implementation and independent execution validation.
For projects with more than one branch in total, also read its
[multi-branch reference](../baton-plan/references/multi-branch.md).
After the first full implementation, use [baton-review-loop](../baton-review-loop/SKILL.md);
[baton-peer-review](../baton-peer-review/SKILL.md) is one requested review pass.
Use [baton-retro](../baton-retro/SKILL.md) for retrospectives.
Explicit invocation includes routing or delegation by another skill; only this
workflow skill supports implicit discovery.

Resolve sibling skills and scripts relative to the loaded skill after resolving
its symlink. When delegating, supply the resolved absolute `SKILL.md` path and
require a complete read. Before spawning, read
[Worker dispatch](references/coordination.md#worker-dispatch).
