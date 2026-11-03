# Validate implementation

Use after a committed candidate or an explicit implementation-validation request.
Plan readiness and diff-based code review are separate gates. Scale independent
execution QA to actual risks; small changes may need only a short native-tool run.

## Prepare and dispatch

The parent assigns fresh workers distinct from implementers; branch
implementers hand off candidates rather than starting QA. Outside projects, the
implementing main agent coordinates. If no independent worker is available, report
incomplete coverage; self-validation is no substitute. In projects use the full
[worker/task protocol](../../baton-workflow/references/coordination.md),
not review-only scope. Validators may run authorized tests/services and write scoped
artifacts, but may not edit implementation source, claim ownership, switch branches,
rewrite history, publish or clean up the project. Planning or validation grants no
implementation/publication authority.

Supply a self-contained handoff with:

- Explicit Phase 4 (execution QA) scope, never bare `validate`.
- Resolved `baton-plan/SKILL.md` and this reference to read completely;
  repository instructions and master/subplans.
- Project/agent/task identity and generation, exact registered repository/member,
  worktree/base and committed candidate SHA(s), prerequisite/artifact versions,
  criteria/gaps, environment/tool versions, allowed commands/resources, scoped
  artifact directory and implementer/parent runtime recipients.

Use the registered member worktree and matching task handoff, frozen at the
specified candidate. Before QA, record actual HEAD and initial modified/untracked
paths; stop on unexplained handoff differences (authorized expected files are fine).
Implementers wait for the report and worker/process exit before changing that
checkout. Before release, validators report candidate SHA and remaining
modified/untracked paths; never automatically clean residue or make source commits.
Alternate isolation requires a concrete supported facility with explicit
revision/environment binding. Record every participating revision/artifact version;
the parent owns combined coverage. Existing production, credentials, paid-run
and access restrictions apply; report blockers when authorization/resources are
unavailable. The parent awaits all workers; stop owned services and await
every child process before handoff or history changes.

## Exercise acceptance and investigate anomalies

Independently inspect relevant code/configuration, scripts, CLI commands, fixtures,
CI workflows and documented harnesses; do not rely solely on implementer reports.
Run or minimally compose existing tools first. Create a small project-local harness
only for an uncovered criterion, explaining which native tools were insufficient
and why.

Use planned acceptance criteria and regression budgets. Lifecycle, startup,
repeated work, concurrency and resource changes need operational checks. Expand
coverage for unanticipated risks revealed by anomalies. Compare candidate and
relevant baseline under matched workload size/concurrency, CPU/memory limits,
cache/startup conditions and lifecycle. Record correctness alongside elapsed time,
throughput or other relevant cost; separate failed/aborted work from successful
throughput. Budgets belong to the project; timeouts bound liveness, not regression.
Serial smoke tests cannot rule out load-dependent failures. Justify substitute
workloads and state coverage limits.

Investigate materially slow successful, failed, cancelled and timed-out runs.
Explain suspected mechanisms, what reproductions exercise and rule out. Errors
before useful work isolate the earlier path, including possible queueing, retries
and cleanup; unrelated functional explanations do not dismiss time cost. Keep
unresolved anomalies visible.

## Report, fix and revalidate

Write a concise sanitized Markdown report: commands/tool versions, revisions,
workload/resources, expected versus observed behavior, criterion verdicts,
measurement comparisons, evidence paths, reproducible findings with severity/owner
and remaining gaps. Send findings/reproduction steps directly to implementer and
parent alongside the durable report. Never infer passes from missing evidence.

The parent records accept/reject/defer with reasons, creates a fix task or
reassigns an eligible task, and explicitly dispatches implementers; passive messages
do not restart settled workers. Implementers fix accepted findings and rerun
affected checks/reviews. Revalidation depends on successful fix-task completion and
new committed SHA(s); recheck affected behavior and justified adjacent regressions.
Label prior results accurately after revision/configuration changes and rerun
affected checks.

Report delivery/settlement is separate from acceptance. Material failures or missing
required coverage prevent readiness; deferred optional work stays explicit. Never
silently weaken criteria or indefinitely repeat identical blocked runs. Draft PRs
may label pending validation; merge readiness requires applicable execution evidence.

## Artifacts

In projects use `Path(master_path).parent / "validation"` from status; never write
in coordinator slots. Outside the framework, use `validation/` beside the existing
baton-plan artifact plan; never implicitly create a project.

Give each worker/assignment a scoped subtree for reusable custom harnesses,
fixtures, instructions and concise sanitized reports. Native tools stay in their
repositories; repository-specific commands, fixtures and access restrictions apply.
Raw logs, caches, databases and bulky/transient output belong in private temporary
run directories outside durable artifacts. Git-ignore does not prevent unattended
retro copying. Exclude secrets.

Validators hand scoped files to the parent, who links reports from the plan and
commits them following
[artifact safety](../../baton-workflow/references/startup.md#worktree-and-artifact-safety);
outside the framework follow artifact-repository commit instructions. Never
broadly stage or commit another worker's changes.
