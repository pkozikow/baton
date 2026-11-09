---
name: baton-plan
description: >-
  Create, validate, refine, or implement scoped project plans with repository
  checks, multi-branch coordination, iterative peer review, and independent implementation QA.
argument-hint: "[args]"
disable-model-invocation: true
---

# Plan

Use this skill only when asked to create, validate, refine or implement a plan, or
validate its implementation.

The workflow has four phases:

1. **Create**: clarify requirements, write or update the plan, validate it with plan reviewers, and commit meaningful plan updates.
2. **Assess plan readiness**: judge whether an existing plan is ready to implement, calling out only issues that would materially block or derail implementation.
3. **Implement**: make the code changes from a ready plan, run focused checks and code review, and commit a candidate.
4. **Validate implementation**: independent workers exercise acceptance and operational risks, report findings to implementers, and recheck fixes.

Bare `/baton-plan validate` always assesses plan readiness, even when implementation
already exists. Only explicit implementation wording, such as
`/baton-plan validate implementation`, selects Phase 4. Phase 4 follows Phase 3 automatically.

Resolve sibling skills and scripts relative to this skill after resolving its symlink. When delegating, supply the resolved absolute `SKILL.md` path and require a complete read. Explicit invocation includes routing or delegation by another skill.

## Plan Scope

If the project uses, or is expected to use, more than one branch in total
(across all participating repositories, e.g. one branch in each of two repos),
read [references/multi-branch.md](references/multi-branch.md) completely before
planning, validation, or implementation. It defines the master
plan, branch subplans, ownership, and separate review cycles.
For single-branch planning, omit the multi-branch reference; framework startup still applies.

## Shared Conventions

- **Project name**: inside the project framework, use `name` from
  `baton status --json` (it exits 3 with empty stdout outside a project).
  Outside the framework, agree on a project name with the user or use the current branch, removing a configured branch prefix only when known.
- Assume the repository is already on the intended branch.
- Use `master_path` and each entry's `subplan_path` from baton status as the durable plan paths. The durable plan is the working plan. Outside the framework, use a user-supplied path or `<artifacts_root>/<project>/plan.md`, taking `artifacts_root` from `baton config show --section paths` (environment overrides are included).
- Commit plans, subplans and retros with `baton artifacts commit [--selector <project>] -m <message> <path>...`, using absolute paths. Branch workers commit only their own subplan paths; the parent owns the master. Outside the framework, follow the artifact repository's commit instructions.
- In framework projects, read [baton-workflow](../baton-workflow/SKILL.md) and its role-required references. They own identity, paths, artifact safety, board/task handoffs, settlement and lifecycle operations. Branch workers retain the full task protocol; plan reviewers use the review-only scope below.
- Before dispatching, read the complete [Worker dispatch](../baton-workflow/references/coordination.md#worker-dispatch) section, which also applies outside projects without startup or an ownership claim.
- If the plan file already exists, edit it instead of starting from scratch.
- Keep plans brief. Focus on what changes, why, and where; avoid excessive implementation detail.
- Plan proportional validation: acceptance criteria, affected workflows, existing tools,
  test conditions, relevant baselines/regression budgets, required resources/access,
  and branch versus combined coverage. Lifecycle, startup, repeated work, concurrency
  and resource changes need operational criteria. See
  [validation](references/validation.md) when defining execution QA.
- Start from the smallest sufficient solution. Include extra features, metrics, persistence, or rollout complexity only when explicitly required. Before adding a new abstraction, service, persistence, configuration option, or rollout stage, name the current requirement that needs it and check the repository for existing code to reuse. Record this in a line or two, not an alternatives document.
- Assume a single-stage deploy unless the requirements imply otherwise; clarify any need for a more complicated rollout.
- Locate referenced specs from the user or repository guidance and check consistency. Do not edit them unless authorized.

## Phase 1: Create

Ask focused questions only when missing information affects scope or acceptance. Otherwise state useful assumptions and proceed; ask further questions only when new blockers emerge.

Include an issue link when supplied by the user or required by repository guidance. Use the repository's issue-tracking tools only when available and authorized; planning does not require a particular tracker. Preserve any repository-owned plan frontmatter and template.

When drafting or refining the plan:

- Write or update the durable plan path.
- Check any referenced spec for conflicts.
- After the initial draft or a large revision, dispatch a plan review worker as described below, with `Reviewers: configured` and the durable plan path.
- Apply its triaged findings as described below.
- Commit after every meaningful plan update.
- After the initial version and every meaningful update, give an update proportional to the scope and decisions that need attention.

### Plan review delegation

Use the same agent structure as `baton-peer-review`: the main agent coordinates
the task, a dedicated worker runs and triages the reviews, and the runner launches
the configured plan reviewers in parallel.

Follow the required Worker dispatch policy for model selection and fresh context. Give the worker the resolved [plan review executor](../baton-plan-review-executor/SKILL.md) path, absolute repository and plan paths, `Reviewers: configured`, relevant requirements/constraints as `Extra review focus`, authorized read scope, and final-report requirements. Require a complete executor read and no further subagents. Read explicitly authorized master/sibling plans or findings outside repository roots and include relevant excerpts in `Extra review focus`; a pathname there is not a runtime read grant. Do not silently omit requested context.

Follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section.

Use the worker's final triaged report to update the plan; keep raw output and triage in the worker. Do not run the helper or read its raw review artifacts in the main agent unless the worker's report is obviously incomplete. Report failures or partial coverage honestly; do not silently take over a failed worker or run a second triage pass. The executor owns runner invocation, artifact interpretation and the triage rubric.

### Applying review findings (main agent)

- Consult the user before accepting feedback that adds non-trivial scope, or a simplification that reverses a design decision the user made.
- Apply accepted changes to the plan.
- An accepted significant complexity finding blocks readiness, even when the plan is implementable as written, until the plan is simplified or the user explicitly keeps the complexity and the plan records why.

## Phase 2: Assess plan readiness

If asked to validate an existing plan that you did not create, study it from the perspective of the engineer about to implement it.

Focus on:

- blocking errors
- contradictory steps
- ambiguous requirements or acceptance criteria
- avoidable complexity that a better approach could eliminate

Rules:

- Only issues that materially block or derail implementation determine readiness. Missing validation that materially prevents establishing acceptance is a readiness issue.
- Substantial avoidable complexity, where a concrete simpler approach meets the same requirements, is a blocking issue, not a non-blocking ambiguity.
- If something is unclear but non-blocking, label it as a lower-priority ambiguity.
- If the plan is implementable as written, say so explicitly and list assumptions to carry into implementation.
- Be brief.
- If the result is `Not ready` and the user provides clarifications, update the plan accordingly.

Output format:

1. `Ready` or `Not ready`
2. Blocking issues
3. Non-blocking ambiguities
4. Assumptions to carry into implementation

## Phase 3: Implement

- Never merge a PR or enable auto-merge without explicit user approval of that
  merge action. Approval to implement a plan does not authorize a merge, even
  when the plan calls for one. Follow
  [Merging PRs](../baton-workflow/references/branches-lifecycle.md#merging-prs).
- Confirm the plan is ready enough to implement.
- Sync with the branch's expected PR base (`status --json`; the default branch outside the framework) before starting and after any major milestone. For native stack children, follow the multi-branch reference instead of pulling or merging the default branch.
- Make the required code changes and add focused tests for new behavior.
- Run individual tests relevant to changed code while iterating.
- Run all repo-required checks for changed files, including linting, typing, and tests, and fix failures.
- After the first full implementation, read
  [baton-review-loop](../baton-review-loop/SKILL.md) completely and follow it to run
  the review loop.
- Run a final format/lint check and fix failures.
- Commit the candidate and hand it to Phase 4 with exact SHAs,
  prerequisite/artifact versions, checks run and known issues. Branch implementers
  hand candidates to the parent rather than starting QA.
- Publish a draft PR following the repository's conventions: a subagent creates it for an independent branch (or outside the framework); in projects follow [Publishing PRs](../baton-workflow/references/branches-lifecycle.md#publishing-prs). Include the issue link when available.

## Phase 4: Validate implementation

Read and follow [references/validation.md](references/validation.md) completely.
