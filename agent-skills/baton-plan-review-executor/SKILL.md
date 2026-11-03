---
name: baton-plan-review-executor
description: >-
  Run the internal plan review executor, triage reviewer findings, and return
  a single final report without modifying the plan.
disable-model-invocation: true
---

# Plan Review Executor

Use this skill only when another agent explicitly directs you to this `SKILL.md` file.

Resolve sibling scripts relative to this skill after resolving its symlink.

Follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section.

## Inputs

- `Repository`: absolute repository path.
- `Plan file`: absolute path to the plan being reviewed.
- `Reviewers`: `configured` by default, using the providers in `plan_review_model`
  from `baton config show` (providers with `default_enabled: false` are opt-in). A CSV list is an explicit per-run override.
- `Extra review focus`: relevant requirements and constraints, or none.
- `Authorized read scope`: paths and context the dispatcher authorized; read
  nothing else outside the repository.

If the repository or plan path is missing, report the missing input instead of
guessing a branch or reviewing a different plan.

The plan is embedded by the helper. Reviewers may inspect only the listed
repository roots; plan links do not authorize reads of related artifacts.
A pathname in `Extra review focus` is not a runtime read grant. When the
assignment explicitly authorizes related master/sibling plans or findings
outside repository roots, the parent or executor reads that authorized context
and embeds relevant excerpts in `Extra review focus` / `PLAN_REVIEW_EXTRA_PROMPT`
before launching the helper. Do not silently omit requested context. Report
unavailable context as a coverage gap.

Before launching the helper, use the known host permissions to determine whether
the selected reviewer CLIs can access their normal authentication and state.
Codex needs write access to its CLI state directory (`CODEX_HOME`, normally
`~/.codex`) during startup even for a read-only review. If Codex is selected and
the parent harness blocks that access, request the harness's normal approved
outer execution permissions for the first helper launch when permitted. Do not
run a predictably blocked trial first. If the host already permits normal CLI
state access, launch normally.

If an unexpected host restriction blocks reviewer startup or CLI state access,
request a normal approved outer launch and rerun the same helper once, only if
the failed launch was not already approved. If approval is unavailable or an
approved launch fails, report the permission blocker and affected review
coverage. Reasoning fallback cannot repair host permissions. Never loosen the
child reviewer's read-only sandbox or `never` approval policy, use permission
bypass flags, or change global permission settings.

## Workflow

1. Work inside the provided repository. Do not spawn additional subagents.
2. Run `bash <resolved-baton-plan-directory>/scripts/plan-peer-review.sh <plan-file>`,
   leaving `PLAN_REVIEW_REVIEWERS` unset for `configured` or omitted reviewers.
   Set it only for an explicit CSV override. Set `PLAN_REVIEW_EXTRA_PROMPT` to
   the extra review focus, if any. Quote paths and
   focus text as literal arguments/environment values. Reviewers also inspect
   the project repositories and each may take the full plan-review timeout
   (`PLAN_REVIEW_TIMEOUT_SECS`, default 420 seconds) per attempt, with Codex
   retrying at lower reasoning, so launch with a sufficient execution window, explicitly await any yielded process, and return only after completion.
3. Let the runner launch the requested reviewers in parallel. Models come from
   `plan_review_model` in the configuration; do not override them. Report the
   resolved models the runner prints. The script's `--help` covers model and
   reasoning controls.
4. Read every successful review artifact reported by the runner. Each run uses
   its own new directory: take it from the runner's last stdout line,
   `REVIEW_ARTIFACTS=<dir>` (stderr also prints `Artifacts: <dir>` and one
   `Review file: <path>` line per review); never guess a `/tmp` path. Reviews
   are named `<plan-slug>-plan-review-<provider>.md` (Codex adds its model:
   `-codex-<model>.md`), and the stdout summary marks a failed reviewer as
   `<provider> review: unavailable`; its non-empty output is kept as `.md.failed`
   and agy output missing a required heading as `.md.invalid`. Do not triage
   those files as reviews. If the runner reports that the installed Baton is too
   old, report that Baton must be reinstalled and stop. Triage
   findings as `accept` (correct and worth adding now), `reject` (incorrect,
   speculative, or low-value), or `defer` (valid but out of scope).
5. Check that proportional validation can establish acceptance, including operational
   criteria where lifecycle, startup, repeated work, concurrency or resource use changes.
   Material missing coverage blocks readiness; avoid unrelated benchmark demands.
   Favor high-impact complexity, correctness, sequencing, rollback safety,
   corroborated findings, and concrete testable recommendations. Mark an
   accepted significant complexity finding (a concrete, substantially smaller
   design that meets the same requirements) as blocking, even when the plan is
   implementable as written. Reject requests to add mechanisms that name no
   concrete requirement or failure mode. Flag non-trivial scope additions, and
   simplifications that reverse a user's design decision, as requiring user
   approval. Do not edit the plan or implement fixes.
6. Return one final markdown report with a brief summary, blocking findings
   first, the artifact directory and review paths, accepted/rejected/deferred
   findings with rationale and reviewer severity, whether accepted
   simplifications were verified against code (`code-verified`) or the plan
   only (`plan-only`), scope decisions needing user approval, and reviewer
   failures or coverage gaps, including reviewers that could not inspect the
   repositories.
   Report partial failures while triaging successful reviews. If all reviewers
   fail, report that no usable review was obtained.

Do not send partial progress unless blocked. The main agent owns user decisions,
plan edits, commits, and the final planning summary.
