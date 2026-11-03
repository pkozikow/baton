---
name: baton-peer-review-executor
description: >-
  Run the internal review executor, triage findings, optionally apply accepted
  fixes, and return a single final report.
disable-model-invocation: true
---

# Peer Review Executor

Use this skill only when another agent explicitly directs you to this `SKILL.md` file.
Do not spawn subagents from this skill. Do not send partial progress unless blocked.

Resolve sibling scripts relative to this skill after resolving its symlink.

## Execution scope and runner

For `dry-run`, follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section. For `apply-accepted-findings`, require explicit fix scope and, in projects, the complete workflow assignment/task protocol with claim and settlement. This mode is not a lightweight specialist.

Run `bash <resolved-skill-directory>/scripts/peer-review.sh` from the repository:

```text
peer-review.sh [--focus-area NAME] [--selector PROJECT] [--extra-prompt TEXT] [--allow-stale-base] [BASE_REF]
```

Use `--help` for operator options. If the installed Baton is too old, report that it needs reinstalling and stop.

## Expected Inputs

The delegating agent should provide:
- `Repository`
- `Base ref`
- `Mode`: `dry-run` or `apply-accepted-findings`
- `Focus area`: exact catalog name or `none`
- `Project selector`: project selector or `none`
- `Extra review focus`
- `Authorized read scope`
- `Authorized fixes`: `none`, or explicit files/scope (required for
  `apply-accepted-findings`)

If any input is missing, assume:
- `Repository`: current workspace
- `Base ref`: `default`
- `Mode`: `dry-run`
- `Focus area`: none
- `Project selector`: none
- `Extra review focus`: none
- `Authorized read scope`: the repository only
- `Authorized fixes`: none

For a named `Focus area`, pass `--focus-area NAME`; pass a supplied project with
`--selector PROJECT`, including with an explicit base. Omit `--focus-area` for a
general review. `--selector` scopes only focus guidance and models; the base always
comes from `baton status --json` in the current directory. The script resolves
named guidance and injects it once.

Pass explicit ad-hoc `Extra review focus` to the script with `--extra-prompt`.

When `Base ref` is `default`, run the script without `BASE_REF`. It then uses the current project entry's expected PR base from `baton status --json`; outside the framework, the discovered default (`refs/remotes/origin/HEAD`), then `origin/main` only if that is missing. If the script stops because the expected PR base is unreliable, report its reason and stop; never retry with another base unless the delegating agent supplies one.

If the script stops because this stack child is behind its predecessor (`needs_rebase` in `baton status --json`), report that the parent must run `gh stack rebase` before this review, and stop. Do not rebase yourself. Pass `--allow-stale-base` only when the delegating agent explicitly asks to review the stale branch anyway.

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

## Outputs

Take the private run directory from `REVIEW_ARTIFACTS=<dir>` in stdout and review paths from the runner's summary (`Artifacts:` and `Review file:` are also on stderr). Never guess paths. Read every listed successful review; `.md.failed` and `.md.invalid` files are failure evidence, not reviews. agy output requires `## Summary`, `## Findings`, `## Suggested Fixes`, and `## Verdict`; incomplete narration is invalid. Logs, diff/truncated review input and prompts remain in that directory.

If a reviewer fails (missing CLI, timeout, auth or runtime errors) or cannot access the repository, report it and continue triage with successful reviewers. If all fail, state that no usable review was obtained.

## Runner configuration

Use the script's `--help` for fetch, provider, timeout, diff-size, model and
reasoning controls. Models come from the configuration; leave reviewer/model
defaults unchanged unless the assignment supplies an override. Report the selected
focus and the resolved models the runner prints.

## Workflow

1. Work inside the provided repository and confirm you are on the branch to review.
2. Launch the script with a sufficient execution window to generate diff and reviewer artifacts. Explicitly await any yielded process; return only after completion.
   The runner may launch reviewers in parallel internally, but await the whole
   run before reading reports. Note the directory from its `REVIEW_ARTIFACTS=`
   line.
3. Read every successful review file listed in the script's output.
4. Triage each finding as one of:
   - `accept`: correct and worth fixing now
   - `reject`: incorrect, speculative, or low-value
   - `defer`: likely valid but not appropriate for this change
5. When findings conflict, prefer:
   - issues that are concrete and reproducible
   - issues corroborated by 2+ reviewers
   - high-impact correctness, security, reliability, data integrity, and API compatibility concerns
6. If `Mode` is `dry-run`, do not edit files; skip to step 8. If `Mode` is
   `apply-accepted-findings`, implement only accepted findings that are worth fixing now.
7. If files changed, run targeted validation for those changes.
8. Return one final markdown report that includes:
   - a high-level summary for the PR
   - the artifact directory and review file paths
   - accepted, rejected, and deferred findings with rationale, preserving each finding's `P0`/`P1`/`P2`/`P3` priority
   - fixes made, if any
   - validation results
