---
name: baton-review-executor
description: >-
  Run one plan or implementation review with `baton review run`, triage the
  reviewers' findings, optionally apply accepted implementation fixes, and
  return a single final report.
disable-model-invocation: true
---

# Review Executor

Use this skill only when another agent explicitly directs you to this `SKILL.md` file.
Do not spawn subagents from this skill. Do not send partial progress unless blocked.

## Inputs

The delegating agent supplies:

- `Kind`: `plan` or `implementation` (default `implementation`).
- `Repository`: absolute repository path (default: the current workspace).
- `Plan file`: absolute plan path (`plan` only; `default` lets the runner pick the
  project's plan).
- `Base ref`: a ref or `default` (`implementation` only).
- `Mode`: `dry-run` (default) or `apply-accepted-findings` (`implementation` only).
- `Focus area`: exact catalog name or `none` (`implementation` only).
- `Project selector`: project or `none`.
- `Reviewers`: `configured` (default) or an explicit CSV override for this run.
- `Extra review focus`: additive text, or `none`.
- `Authorized read scope`: what you may read outside the repository; default none.
- `Authorized fixes`: `none`, or explicit files/scope (required for
  `apply-accepted-findings`).

For a plan review, report a missing repository or plan path instead of guessing a
branch or reviewing a different plan.

## Execution scope

For `dry-run`, follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists)
section. `apply-accepted-findings` requires explicit fix scope and, in projects,
the complete workflow assignment/task protocol with claim and settlement; it is not
a lightweight specialist.

## Run the review

Work inside the repository; for an implementation review, confirm you are on the
branch to review. If `baton review --help` does not list `save`, report that the
installed Baton is too old and must be reinstalled, and stop.

```text
baton review run --kind plan [--reviewers CSV] [--extra-prompt TEXT] [PLAN_FILE]
baton review run --kind implementation [--focus-area NAME] [--selector PROJECT]
    [--reviewers CSV] [--extra-prompt TEXT] [--allow-stale-base] [BASE_REF]
```

- Omit `--reviewers` for `configured`; models come from the configuration and are
  not overridden. Quote paths and focus text as literal arguments.
- Pass `Extra review focus` with `--extra-prompt`. A pathname there is not a read
  grant: when the assignment authorizes context outside the repository roots (a
  master or sibling plan, earlier findings), read it yourself and embed the relevant
  excerpts. Report unavailable context as a coverage gap.
- Implementation: pass a named `Focus area` with `--focus-area NAME` and a supplied
  project with `--selector PROJECT`, also with an explicit base; the selector scopes
  only focus guidance and models, and the runner injects the catalog text once. Omit
  `BASE_REF` for `default`: the runner uses the current entry's expected PR base,
  else `refs/remotes/origin/HEAD`, then `origin/main`. If it stops because that base
  is unreliable, report the reason; never retry with another base unless the
  delegating agent supplies one. If it stops because this stack child is behind its
  predecessor (`needs_rebase`), report that the parent must run `gh stack rebase`,
  and stop; pass `--allow-stale-base` only when explicitly asked.
- `baton review run --help` lists timeouts, size limits, fetch and Codex reasoning
  controls.

Before launching, use the known host permissions to determine whether the selected
reviewer CLIs can access their normal authentication and state. Codex needs write
access to its CLI state directory (`CODEX_HOME`, normally `~/.codex`) during startup
even for a read-only review. If Codex is selected and the parent harness blocks that
access, request the harness's normal approved outer execution permissions for the
first launch when permitted. Do not run a predictably blocked trial first. If the
host already permits normal CLI state access, launch normally.

If a reviewer fails with `startup_denied`, or another unexpected host restriction
blocks reviewer startup or CLI state access, request a normal approved outer launch
and rerun the same command once, only if the failed launch was not already
approved. If approval is unavailable or an approved launch fails, report the
permission blocker and affected coverage. Never loosen the reviewers' read-only
sandbox or `never` approval policy, use permission bypass flags, or change global
permission settings.

The run prints `Maximum duration` before reviewers start (the longest reviewer timeout plus a
shutdown grace); launch it with an execution window at least that long. If the
harness backgrounds it, explicitly await the process. Before reading results, confirm
the run finished: `baton review show <run-dir>` must not report `running`. Never
return while a run you launched is still running. If `show` reports reviewer process
groups still running after an interrupted run, report them with the stop command it
prints.

## Read the results

Take the run directory from the last stdout line, `REVIEW_ARTIFACTS=<dir>`; never
guess a path. `<dir>/run.json` is the run record: status, reviewers with their
outcome and failure reason (`timeout`, `rate_limited`, `auth`, `startup_denied`,
`invalid_output`, `empty`, `missing_cli` or `error`), review paths and each
attempt's session ID. The stdout summary prints one `<provider> review:` line per
launched reviewer (a path, or `failed (<reason>)`) and a final
`Coverage: N of M reviewers produced a review` line. Read every successful review;
`.md.failed` and `.md.invalid` files are failure evidence, not reviews.

The runner already retried each rate-limited or transiently failing reviewer once.
Report the coverage as N of M and name each failed reviewer with its reason. If all
reviewers fail, state that no usable review was obtained.

Implementation runs embed whole per-file diffs while the whole input fits its
budget and list the rest in `run.json` (`input.not_embedded`) and
`<dir>/diffs/INDEX.txt`; reviewers read those from `<dir>/diffs/`. Report
each non-embedded file and whether any reviewer says it read it; a non-embedded
file that no reviewer covered is a coverage gap, not a blocker by itself.

## Triage

Triage each finding as `accept` (correct and worth acting on now), `reject`
(incorrect, speculative, or low-value) or `defer` (likely valid but out of scope).
Prefer concrete, reproducible issues, findings corroborated by two or more
reviewers, and high-impact concerns. When the delegating agent lists findings that
earlier rounds already triaged, keep those decisions unless a reviewer brings new
evidence, and say which findings repeat.

**Implementation:** weigh correctness, security, reliability, data integrity and API
compatibility; preserve each finding's `P0`–`P3` priority. In
`apply-accepted-findings` mode implement only accepted findings worth fixing now
and run targeted validation for the changed files; in `dry-run` do not edit files.

**Plan:** weigh complexity, correctness, sequencing and rollback safety, and whether
the plan's proportional validation can establish acceptance (operational criteria
where lifecycle, startup, repeated work, concurrency or resource use changes).
Material missing coverage blocks readiness; avoid unrelated benchmark demands. Mark
an accepted significant complexity finding (a concrete, substantially smaller design
that meets the same requirements) as blocking, even when the plan is implementable
as written. Reject requests for mechanisms that name no concrete requirement or
failure mode. Flag non-trivial scope additions, and simplifications that reverse a
user's design decision, as requiring user approval. Never edit the plan.

## Report

Return one final Markdown report with:

- a brief summary (for implementation reviews, suitable for the PR) and, for plan
  reviews, blocking findings first;
- the run ID, run directory, final `run.json` status and review file paths;
- coverage as N of M with each failed reviewer's reason, the resolved models, and
  for implementation reviews the non-embedded files and whether reviewers read them;
- accepted, rejected and deferred findings with rationale and reviewer severity or
  priority; for plans, whether accepted simplifications were `code-verified` or
  `plan-only`, and scope decisions needing user approval;
- fixes made and validation results, if any.

The delegating agent owns user decisions, plan edits, commits and durable outcomes,
including `baton review save`.
