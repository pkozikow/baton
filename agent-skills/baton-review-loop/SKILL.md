---
name: baton-review-loop
description: >-
  Run configured focused peer reviews followed by a bounded general review loop,
  apply accepted findings, and report the outcome of each enabled phase.
argument-hint: "[review-loop overrides]"
disable-model-invocation: true
---

# Review Loop

Resolve sibling skills and scripts relative to this skill after resolving its symlink. When delegating, supply the resolved absolute `SKILL.md` path and require a complete read. Explicit invocation includes routing or delegation by another skill.

Before dispatching, read the complete [Worker dispatch](../baton-workflow/references/coordination.md#worker-dispatch) section, and follow the complete [Review-only specialists](../baton-workflow/references/coordination.md#review-only-specialists) section for every review worker. These standalone sections require no project startup or ownership claim. The parent owns durable outcomes and code changes, even when the broader task is implementation work.

These are diff-based code reviews. A clean review does not establish runtime
acceptance; follow [baton-plan Phase 4](../baton-plan/references/validation.md) for
independent execution QA after the committed candidate.

## Resolve the Review Schedule

Run `baton status --json` first. Inside a project, take its project name and read
`baton config show --selector <project>`; only when status exits 3 (outside a
project), read global `baton config show`. Propagate every other status or
configuration error before launching any worker. Take the effective `review_loop`:

- `focused_reviews`: an ordered list of exact, case-sensitive built-in catalog
  names. The defaults are `performance`, `complexity`, `security`. Each entry
  requests one dispatcher pass; duplicates request duplicate passes in order.
  An empty list skips the focused phase.
- `general_rounds`: the maximum number of general rounds, default ten. Zero skips
  that phase. Stop early on a completed report with no accepted P0/P1/P2 findings.

Project settings inherit omitted fields and replace the global focused list,
including `[]`. Explicit task instructions override the schedule for this invocation
without changing saved configuration; validate each focus they name against
`review_focus_catalog` before launching workers. Preserve empty lists and zero, and
never silently repair configuration.

Report the selected focuses and general-round limit. If both phases are disabled,
report that zero reviews were requested and finish without launching workers or
review processes. A skipped phase supplies no review evidence.

For enabled phases, read the complete sibling [baton-peer-review](../baton-peer-review/SKILL.md) and use
it as the dispatcher. Each pass may run several configured review providers; the
schedule counts dispatcher passes, not individual provider calls.

## Run the Configured Phases

At any point, stop and report the blocker for a required external dependency,
failed approval or unresolved scope decision, and stop when the user asks.

1. Resolve the repository and base ref. Unless the task specifies another base,
   use `Base ref: default`. The runner resolves the current project entry's PR base
   from `baton status --json`, or the repository default outside a project. If it
   stops on an unreliable base or
   [`needs_rebase`](../baton-workflow/references/startup.md#resolve-the-project),
   stop and report the reason without substituting another base.
2. Launch one dry-run, review-only dispatcher pass for every configured focus.
   Supply `Focus area: <name>` and `Project selector: <project or none>` to
   the dispatcher, including when the base is explicit. The selector scopes focus
   guidance and models only; the base still comes from the current directory's
   status. Pass only the name; never copy catalog text into the handoff or extra
   focus, since the runtime injects it once. Keep any explicitly requested ad-hoc
   `Extra review focus` separate and additive. The focused passes may run in
   parallel, but launch each with a sufficient execution window and await every
   worker and its launched processes before applying fixes or returning; never
   return with a launched review pending. Retain each report's `accept`, `reject`,
   and `defer` triage.
3. As the parent agent, address all accepted focused P0/P1/P2 findings before the
   general phase, staying within the task's scope. Fix accepted P3 findings when
   the benefit warrants the scope; leave rejected or deferred findings unchanged
   and explain why. Run validation and commits required by the repository for
   those changes. If there are no focused reviews, proceed directly to the general
   phase.
4. Run general rounds sequentially, up to `general_rounds`. Each round is a fresh
   dry-run dispatcher pass with `Focus area: none` and no catalog guidance or
   focused model override; review workers must
   not modify files. From the second round on, add the findings earlier rounds
   rejected or deferred to that round's `Extra review focus`, each with its
   one-line rationale, marked "already triaged; re-raise only with new evidence".
   Read its final triaged report, apply accepted fixes as the
   parent under the same rules as step 3, and validate and commit changes before
   another round.
5. Stop the general phase early on a clean round: a completed report with no
   accepted P0/P1/P2 findings and, when two or more reviewers are configured, at
   least two reviewers that produced a review (the report's coverage, N of M).
   Fixing a report's findings does not turn that report into a clean review, and a
   round below that coverage is never the clean stopping round. Otherwise continue
   only while the configured budget remains.
6. If a round cannot be clean for a reason the next round would repeat, such as
   reviewers that keep failing for the same reason (`auth`, `missing_cli`,
   `startup_denied`, or the same failure in consecutive rounds), finish that
   round's fixes and end the general phase there, reporting a coverage blocker
   that names the failing reviewers and reasons. Do not spend the remaining
   rounds on identical runs.
7. If the general budget is exhausted without a clean pass, finish with the limit
   reached, findings fixed or unresolved, and validation status. Do not launch an
   extra confirmation round. If `general_rounds` is zero, finish after the focused
   phase and report that general review was skipped.

Every pass is recorded by its dispatcher: inside a project, each run is saved with
`baton review save` (a branch worker running this loop saves its own runs), so
`baton review list` finds them later.

Finish with the configured and completed focused-pass counts, general rounds used
and limit, each pass's coverage (N of M reviewers, with failed reviewers and their
reasons), accepted fixes, validation, rejected/deferred or unresolved findings,
and the stopping reason. Distinguish a clean general review from a skipped phase,
a coverage blocker or an exhausted budget.
