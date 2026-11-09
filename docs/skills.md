# Skills and reviews

## Bundled skills

`baton-workflow` allows implicit invocation. Its installed description names
the configured coordinator slot root (`p1` through `p10`) and directs the agent to
read the skill before project work there. It also supports finding or revisiting projects from
anywhere and explicitly requested Baton project work. The description helps
discovery; `baton status --json` supplies authoritative metadata. No user-level
`AGENTS.md` guidance is needed.
All other skills require explicit invocation, with both
`disable-model-invocation: true` in `SKILL.md` and
`policy.allow_implicit_invocation: false` in `agents/openai.yaml`.

- `baton-workflow`: slot startup, session roles, shared artifacts, coordination, and
  branch lifecycle, plus project discovery from any directory.
- `baton-plan`: planning, plan readiness, implementation, independent execution QA,
  and multi-branch coordination.
- `baton-plan-review-executor`: internal plan review execution and triage.
- `baton-peer-review`: review delegation.
- `baton-peer-review-executor`: internal review execution and triage.
- `baton-review-loop`: a configurable focused-review list followed by a bounded
  number of general rounds; either phase can be disabled.
- `baton-retro`: evidence-based project retrospective and note archival.

`baton retro` explicitly invokes the bundled `baton-retro` skill, even if skills have
not been installed separately. Installing and updating skills is covered in
[Installation](installation.md#skills-only).

## Independent implementation validation

Skill-invocation syntax depends on the agent; these examples use the slash form.
`/baton-plan validate` always assesses plan readiness, even after implementation;
`/baton-plan validate implementation` selects Phase 4 execution QA.
Phase 4 uses independent workers to exercise acceptance and recheck fixes.
Custom project harnesses and reports live under `validation/` beside the plan.
See the [validation procedure](../agent-skills/baton-plan/references/validation.md).

PR merges require explicit user approval of the merge action, including enabling
auto-merge. Approval to implement a plan does not authorize merging, even when
the plan calls for a merge. See
[Merging PRs](../agent-skills/baton-workflow/references/branches-lifecycle.md#merging-prs).

## Review runners

The bundled `baton-plan/scripts/plan-peer-review.sh` and
`baton-peer-review-executor/scripts/peer-review.sh` scripts assemble the review
input and prompt, then run the reviewers through `baton review run`. They use only
the installed `baton` command on PATH (`BATON_CLI` can explicitly pin another
executable for testing) and stop with "installed Baton too old; reinstall" when
that command has no `review run`. `baton review run --check` validates the
reviewer selection, environment overrides and models before the scripts create a
run directory or fetch; `baton review run` then runs the reviewers in parallel
and prints `<provider> review: <path|unavailable>`, `<provider> log:` and
`<provider> model:` lines. Neither the scripts nor `baton review run` needs a
trusted runtime: they only read configuration and write inside the run directory.
If every reviewer fails, the runner prints "All review jobs failed" and exits 1.
`peer-review.sh` exits 0 without running reviewers when the diff is empty. Reviewers run in their own
process groups, so interrupting or terminating `baton review run` (SIGINT or
SIGTERM) kills every reviewer group and exits with 130 or 143.

Plan selection uses authoritative status paths; outside the framework,
`PLAN_REVIEW_PROJECTS_ROOT` or `BATON_ARTIFACTS_ROOT` overrides the effective
artifact root from `baton config show --section paths` (see
[Storage roots](configuration.md#storage-roots)).
The branch prefix comes from `baton config show --section branch_prefix` (default `baton`, giving `baton/<project>` branches).

Reviewer sets, model choices and per-run overrides are described in
[Configuration](configuration.md#review-models). Run either helper with `--help`
for executable overrides, timeouts and Codex reasoning controls. Missing reviewer
CLIs are skipped with warnings; no available requested CLI is an error.

Plan reviewers look first for unnecessary complexity, then correctness, sequencing
and rollback safety, tagging each finding `code-verified` or `plan-only`. They may
read the active project worktrees (outside the framework, the current checkout);
the plan is embedded in their input, and any other context, such as related plans,
must be embedded as extra review focus (`PLAN_REVIEW_EXTRA_PROMPT`). Plan reviews
default to a 420-second timeout and implementation reviews, which stay diff-only,
to 300 seconds.

The runner prints review and log paths and resolved models. A failed nonempty
report is kept as `.md.failed`, and an agy report without a successful, valid
terminal response as `.md.invalid`. The run succeeds when at least one reviewer
produces a review; report skipped and failed providers as partial coverage and keep
the run directory for diagnosis.

Codex needs write access to its CLI state directory even for read-only reviews. When
the host agent blocks it, the executors request an approved launch; see the
[plan-review](../agent-skills/baton-plan-review-executor/SKILL.md) and
[peer-review](../agent-skills/baton-peer-review-executor/SKILL.md) executors.

## Review boundaries

Reviewers read untrusted input: a diff or plan can quote skill files or other
instructions. The prompts tell reviewers to treat that content as data, never to
follow instructions inside it, and never to modify files, run write commands,
commit or push. The invocations restrict tools and permissions as follows:

- Codex uses `exec -s read-only` and `approval_policy="never"`.
- Claude uses `--restricted`, strict MCP configuration, noninteractive denial and
  only `Read,Glob,Grep`. Restricted mode ignores user/project/local settings.
- Cursor uses read-only `--mode ask`.
- agy uses `--sandbox` in a private work directory, removed afterwards. Headless
  permissions auto-deny write and command tools only when no `permissions.allow`
  rule in the user's agy settings
  grants them. Keep such allow rules out of those settings. A denied call ends
  with an empty response and fails the review. The sandbox alone is not an
  unconditional read-only guarantee.

`--read-dir` reaches Claude, agy and Cursor as `--add-dir`; Codex's read-only
sandbox already permits reads across paths. No reviewer gets blanket approval or
a sandbox bypass. Invocation tests check the arguments Baton supplies, not the
external CLIs' permission enforcement.

## Review-loop schedule

`baton-review-loop` runs named focused passes, then sequential general rounds
that stop on a clean report. It resolves the project with `baton status --json`
and reads project configuration; only status exit 3 permits global fallback.
Other status errors stop dispatch. See [Review-loop configuration](configuration.md#review-loop)
for schedules, inheritance, model precedence, validation and repair.

A named pass carries its `Focus area: NAME` and `Project selector: PROJECT` through
the dispatcher and executor. The helper resolves catalog text with
`baton review focus NAME [--selector PROJECT]`, injects it once, and forwards the
context to both preflight and execution. Ad-hoc `--extra-prompt` text is additive.
General passes omit focused context; plan reviews reject a focus area.

The dispatcher awaits every reviewer and owns durable outcome reporting. Review-only
specialists inspect and report; workers authorized to apply findings use the full
task handoff and settlement protocol. Agent dispatch and model resolution follow
[Worker dispatch](../agent-skills/baton-workflow/references/coordination.md#worker-dispatch).

## Adding a provider

Providers are entries in `PROVIDERS` in [baton/providers.py](../baton/providers.py).
Its module docstring owns the method, permission and protocol contract; add an
entry and corresponding tests rather than duplicating provider branches in callers.
Ownership, sessions, retrospectives, reviews, diagnostics, worker guidance and
skill installation all use that registry.

A provider without an unattended mode that meets the retrospective
[safety floor](retrospectives.md#safety-floor) can still own projects: claim,
resume, fresh fork and interactive retros work. Its unattended retro reports
`blocked` with an "unsupported" error before any evidence is exported or copied,
and `retro pending` (and cron) records that project and continues with the others.

[Provider tests](../tests/test_providers.py) check every entry;
[review-runner tests](../tests/test_review_runner.py) check review invocation and
failure handling. The test-only provider in
[provider_fixtures.py](../tests/provider_fixtures.py) exercises the registry paths;
extend it when the contract grows. Operator settings belong in
[Configuration](configuration.md#providers).
