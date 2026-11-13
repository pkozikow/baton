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
- `baton-peer-review`: the review dispatcher for plan reviews, branch peer reviews
  and review-loop passes; it also saves each run.
- `baton-review-executor`: internal plan and implementation review execution and
  triage.
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

## Review runs

`baton review run --kind plan [PLAN_FILE]` and `baton review run --kind
implementation [BASE_REF]` resolve what to review, write the reviewer input and
prompt into a new private run directory under `$TMPDIR` (runs of the same kind
older than seven days are pruned), run the selected reviewers in parallel and print
`<provider> review: <path|failed (reason)>`, `<provider> log:`, `<provider> model:`
and a `Coverage: N of M reviewers produced a review` line, then
`REVIEW_ARTIFACTS=<dir>` last. It needs no trusted runtime: it only reads
configuration and project metadata and writes inside its run directory. A run
succeeds when at least one reviewer produces a review; otherwise it prints "All
review jobs failed" and exits 1. Reviewers run in their own process groups, so
interrupting or terminating the run (SIGINT or SIGTERM) kills every reviewer group
and exits with 130 or 143. The skills invoke the `baton` on `PATH`, which the same
installation provides.

Plan selection uses authoritative status paths; outside the framework,
`PLAN_REVIEW_PROJECTS_ROOT` or `BATON_ARTIFACTS_ROOT` overrides the effective
artifact root from `baton config show --section paths` (see
[Storage roots](configuration.md#storage-roots)).
The branch prefix comes from `baton config show --section branch_prefix` (default `baton`, giving `baton/<project>` branches).

Reviewer sets, model choices and per-run overrides are described in
[Configuration](configuration.md#review-models). `baton review run --help` lists
executable overrides, timeouts, size limits and Codex reasoning controls. Missing
reviewer CLIs are skipped with warnings and count against coverage; no available
requested CLI is an error.

Plan reviewers look first for unnecessary complexity, then correctness, sequencing
and rollback safety, tagging each finding `code-verified` or `plan-only`. They may
read the active project worktrees (outside the framework, the current checkout);
the plan is embedded in their input, and any other context, such as related plans,
must be embedded as extra review focus (`--extra-prompt`). Plan reviews default to
a 420-second timeout and implementation reviews to 300 seconds. Both prompts state
an inspection budget and the reviewer's deadline, so a reviewer writes its review
from the evidence it has instead of running out of time.

The branch is the unit of an implementation review. The run writes the full diff
and one diff per changed file under `<run-dir>/diffs/` (the file's path plus
`.diff`), with `diffs/INDEX.txt` listing every changed file, its diff size and
whether it was embedded. The prompt names the base, merge base and head commits and
lists the changed files (a list too long for the input is summarized by directory
and points to the index). `PEER_REVIEW_MAX_INPUT_BYTES` (default 180,000) bounds the
whole reviewer input: prompt, listing and embedded diffs. Whole per-file diffs are
embedded while they fit, source code first, then documentation and repository meta
files (Markdown, reStructuredText, text, `.gitignore`, license files), then data and
schema files (YAML, JSON, CSV, XML, snapshots), then generated, lock and binary files. The default stays below the
size at which agy silently drops the rest of its message (about 191,000 bytes), and
every provider gets the same input. Reviewers open the other diffs, and the
surrounding code, on demand: they may read the repository and the diffs directory.
The record lists what was and was not embedded, and the stderr warning names the
index; executors report whether reviewers read the non-embedded diffs. A failed
`git fetch` (for example in a restricted sandbox) is a recorded warning, and the
review uses the local base; a base ref that names no commit stops the run.

A failed reviewer is classified as `timeout`, `rate_limited`, `auth`,
`startup_denied`, `invalid_output`, `empty`, `missing_cli` or `error` from its exit
status and its log or stream; agy's error payload is kept, so a rate limit that ran
into the deadline is reported as `rate_limited`, and another stream error stays as
the timeout's detail. A rate-limited reviewer, or one
that hit a transient server or network error, is retried once after a short
backoff within its own timeout; timeouts, `auth`, `missing_cli` and
`startup_denied` are not retried. Codex's lower-reasoning fallbacks share the
reviewer's timeout, and the log of an attempt that timed out is kept. A failed
nonempty report is kept as `.md.failed`, including the review text agy streamed
before it timed out or was interrupted, and an agy report without a successful,
valid terminal response as `.md.invalid`. The run prints its maximum duration once
its input is ready.

Codex needs write access to its CLI state directory even for read-only reviews. When
the host agent blocks it, the executor requests an approved launch; see the
[review executor](../agent-skills/baton-review-executor/SKILL.md).

### Run records and sessions

Every run writes `run.json` in its run directory when it starts (`status:
running`, with the runner's PID and start time) and rewrites it when it ends
(`completed`, `failed`, `interrupted` or `no_changes`). It records the kind, focus,
times, project, the launching agent session (from the provider's session variable,
which in a subagent may be its parent's), what was reviewed (plan path, or
repository, branch, base, merge base and head commits, embedded and non-embedded
files), warnings, and per reviewer the provider, model, outcome, failure reason,
duration, review, log and working directory, with one entry per attempt carrying
its session ID and process group. An attempt is recorded when it starts and its
session ID as soon as it is known, so a killed run still names its sessions. Each
Codex fallback and each retry is a separate attempt; the accepted one is marked.
Claude reviewers get a fresh session ID per attempt, Codex reports its own in its
log and agy in its stream; Cursor exposes none. agy runs in a private temporary
directory that its attempt removes, so that directory is recorded apart from the
working directory and `show` says whether it is still present. A run that fails
before its reviewers start records its error, and `show` names each failed
reviewer's `.md.failed` or `.md.invalid` output.
Command lines and prompts are not recorded.

`baton review show RUN` (a run ID, run directory or `run.json`) prints the record,
marking paths that no longer exist, and each attempt's provider, session ID and
working directory, so a reviewer session can be reopened with that provider's own
CLI. Reviewer sessions hold the untrusted diff or plan text they reviewed. A
`running` record whose runner process is gone, or whose PID now belongs to another
process, is shown as `interrupted`; this is how a dispatcher confirms that a
backgrounded run finished. A runner killed outright (SIGKILL) cannot stop its
reviewers, so `show` names each reviewer process group of an interrupted run that is
still alive, with the command to stop it when its leader is still the process Baton
started (otherwise it only says the group may still exist); nothing stops them
automatically.
`--json` prints the record with its effective status and those process groups.

Reopening a reviewer session interactively first asks to trust its folder in
Claude and agy. agy completion is not reliable on large inputs: in replays it
finished 4 of 7 plan reviews within 420 seconds and 1 of 5 large implementation
reviews within 300 seconds, using most of its timeout even when it finished.

`baton review save RUN_DIR [--selector PROJECT]` is a separate, trusted step: it
copies a finished run's `run.json` and review files (`.md`, `.md.failed`,
`.md.invalid`) once into the project's artifacts at `reviews/<run-id>/` and commits
them like `baton artifacts commit`. It refuses a run that is still running or that
belongs to another project, and checks the artifact checkout before copying
anything; a failed save leaves no copy behind, and empty review files left by a
killed run are not saved. Saving again is a no-op that retries
the commit; an uncommitted copy left by a failed save is replaced.
Outside a project there is nothing to save (exit 3). Prompts, diffs and logs stay in
the temporary run directory. `baton review list [--selector PROJECT] [--json]` lists
the project's saved runs and its unsaved runs still in `$TMPDIR` (outside a project,
every unsaved run) with kind, start time, coverage and status.

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

The read roots (project worktrees for plan reviews; the repository and the diffs
directory for implementation reviews) reach Claude, agy and Cursor as `--add-dir`;
Codex's read-only sandbox already permits reads across paths. No reviewer gets blanket approval or
a sandbox bypass. Invocation tests check the arguments Baton supplies, not the
external CLIs' permission enforcement.

## Review-loop schedule

`baton-review-loop` runs named focused passes, then sequential general rounds
that stop on a clean report. It resolves the project with `baton status --json`
and reads project configuration; only status exit 3 permits global fallback.
Other status errors stop dispatch. See [Review-loop configuration](configuration.md#review-loop)
for schedules, inheritance, model precedence, validation and repair.

A named pass carries its `Focus area: NAME` and `Project selector: PROJECT` through
the dispatcher and executor to `baton review run --focus-area NAME --selector
PROJECT`, which resolves the catalog text (`baton review focus NAME` prints it) and
injects it once. Ad-hoc `--extra-prompt` text is additive. General passes omit
focused context; plan reviews reject a focus area.

A general round is clean only with no accepted P0/P1/P2 findings and at least two
reviewers that produced a review (one when only one reviewer is configured). A
round that cannot be clean for a reason the next round would repeat, such as a
reviewer that keeps failing, ends the general phase after its fixes as a coverage
blocker. From the second round on, earlier rounds' rejected and deferred findings
are passed as extra focus, so reviewers re-raise them only with new evidence.

The dispatcher awaits every reviewer and owns durable outcome reporting, including
saving each run inside a project. Review-only
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
