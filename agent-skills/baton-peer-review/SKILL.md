---
name: baton-peer-review
description: >-
  Dispatch a branch peer review to a dedicated worker subagent, then relay only
  the worker's final report.
argument-hint: "[args]"
disable-model-invocation: true
---

# Peer Review

Use this skill when asked to run a peer review of the current branch against its expected PR base (the default branch outside a project) or another base ref.

Resolve sibling skills and scripts relative to this skill after resolving its symlink. When delegating, supply the resolved absolute `SKILL.md` path and require a complete read. Explicit invocation includes routing or delegation by another skill.

This dispatcher launches one dedicated worker and relays its final report. For implementation completion, use [baton-review-loop](../baton-review-loop/SKILL.md), whose fixes remain parent-owned.

## Dispatch

1. Read the complete [Worker dispatch](../baton-workflow/references/coordination.md#worker-dispatch) section for model selection and fresh context. This standalone section needs no project startup or ownership claim; `default` supplies no model override.
2. Resolve the absolute repository path, requested base (`default` if unspecified), project selector (`none` outside a project), exact catalog focus name (`none` for general review), and additive ad-hoc focus. The executor owns base resolution; relay its stop on an unreliable base or [`needs_rebase`](../baton-workflow/references/startup.md#resolve-the-project) and never substitute another base.
3. Choose `dry-run` for review-only requests and every configured review-loop pass. Use `apply-accepted-findings` only when fixes are authorized by the task, and state the files/scope the worker may change.
4. Give a fresh worker the resolved [peer review executor](../baton-peer-review-executor/SKILL.md) path and require a complete read. Supply repository, base, mode, focus, selector, extra focus, authorized read scope and final-report requirements; require no additional subagents.
5. For dry-run work, follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section. `apply-accepted-findings` is a mutating assignment: in projects provide the full workflow handoff/task protocol, including claim and settlement, plus explicit fix authorization. Never treat it as a lightweight specialist.
6. Await the worker and all launched processes. The dispatcher owns durable status/outcome recording. Relay only its final report; do not add another summary, re-triage or inspect raw artifacts unless the report is obviously incomplete. Report a failed worker instead of silently taking over locally.

## Worker inputs

Supply a self-contained prompt with these fields and the resolved executor path:

```text
Repository: <absolute repo path>
Base ref: <ref or default>
Mode: <dry-run|apply-accepted-findings>
Focus area: <catalog name or none>
Project selector: <project or none>
Extra review focus: <text or none>
Authorized read scope: <paths and relevant contracts>
Authorized fixes: <none, or explicit files/scope with full task handoff>

Complete the executor workflow and return one final Markdown report.
Do not spawn additional subagents. Await every launched process.
```

The executor owns runner options and artifact interpretation; review CLI models remain separate from worker model selection.
