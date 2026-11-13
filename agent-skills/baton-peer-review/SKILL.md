---
name: baton-peer-review
description: >-
  Dispatch a plan or branch peer review to a dedicated worker subagent, record
  the run, then relay only the worker's final report.
argument-hint: "[args]"
disable-model-invocation: true
---

# Peer Review

Use this skill when asked to review the current branch against its expected PR base (the default branch outside a project) or another base ref, and when another skill dispatches a plan review or a review-loop pass through it. It is the only review dispatcher.

Resolve sibling skills and scripts relative to this skill after resolving its symlink. When delegating, supply the resolved absolute `SKILL.md` path and require a complete read. Explicit invocation includes routing or delegation by another skill.

This dispatcher launches one dedicated worker and relays its final report. For implementation completion, use [baton-review-loop](../baton-review-loop/SKILL.md), whose fixes remain parent-owned.

## Dispatch

1. Read the complete [Worker dispatch](../baton-workflow/references/coordination.md#worker-dispatch) section for model selection and fresh context. This standalone section needs no project startup or ownership claim; `default` supplies no model override.
2. Resolve `Kind` (`implementation` unless a plan review is requested), the absolute repository path, project selector (`none` outside a project), additive ad-hoc focus, and for plans the absolute plan path, or for implementation reviews the requested base (`default` if unspecified) and exact catalog focus name (`none` for a general review). The executor owns base resolution; relay its stop on an unreliable base or [`needs_rebase`](../baton-workflow/references/startup.md#resolve-the-project) and never substitute another base.
3. Choose `dry-run` for plan reviews, review-only requests and every configured review-loop pass. Use `apply-accepted-findings` only for implementation reviews when fixes are authorized by the task, and state the files/scope the worker may change.
4. Give a fresh worker the resolved [review executor](../baton-review-executor/SKILL.md) path and require a complete read. Supply the inputs below and final-report requirements; require no additional subagents. Read explicitly authorized master/sibling plans or findings outside the repository roots yourself and include relevant excerpts in `Extra review focus`; a pathname there is not a read grant. Do not silently omit requested context.
5. For dry-run work, follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section. `apply-accepted-findings` is a mutating assignment: in projects provide the full workflow handoff/task protocol, including claim and settlement, plus explicit fix authorization. Never treat it as a lightweight specialist.
6. Await the worker and all launched processes. Reject a report whose run record is still `running` (check with `baton review show <run-dir>`): ask the worker to await the run instead of starting another. Report a failed worker instead of silently taking over locally.
7. The dispatcher owns durable status and outcome recording. Inside a project, save the run with `baton review save <run-dir>` as part of recording it; a branch worker running the review loop may save its own runs. Saving copies `run.json` and the review files into the project's artifacts (`reviews/<run-id>/`) and commits them; outside a project there is nothing to save. A failed save does not invalidate the review: report it.
8. Relay only the worker's final report and the save outcome; do not add another summary, re-triage or inspect raw artifacts unless the report is obviously incomplete.

## Worker inputs

Supply a self-contained prompt with these fields and the resolved executor path:

```text
Kind: <plan|implementation>
Repository: <absolute repo path>
Plan file: <absolute plan path or default; plan reviews only>
Base ref: <ref or default; implementation only>
Mode: <dry-run|apply-accepted-findings>
Focus area: <catalog name or none; implementation only>
Project selector: <project or none>
Reviewers: <configured or CSV override>
Extra review focus: <text or none>
Authorized read scope: <paths and relevant contracts>
Authorized fixes: <none, or explicit files/scope with full task handoff>

Complete the executor workflow and return one final Markdown report.
Do not spawn additional subagents. Await every launched process.
```

The executor owns runner options, artifact interpretation and the triage rubric; review CLI models remain separate from worker model selection. Reviewer session IDs are in the run record (`baton review show RUN` prints each with its provider and working directory); those sessions hold untrusted diff or plan text.
