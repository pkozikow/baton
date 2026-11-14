# Changelog

## Unreleased

- Model tiers carry a reasoning effort. `baton config set --model-tier-effort
  PROVIDER.TIER=EFFORT` saves one in `model_tier_effort` (`--remove-model-tier-effort
  PROVIDER[.TIER]` removes it), and a tier without one resolves to `high`. Changes
  for existing configurations:
  - Claude and agy reviews whose model is a tier now pass `--effort` (`high` unless
    saved otherwise). An agy tier mapped to a model ID that ends in another effort,
    such as `MODEL-medium`, fails until that effort is saved for the tier.
  - Codex reviews whose model is a tier use the tier's effort instead of
    `CODEX_REASONING_EFFORT` or Codex's `config.toml`; `<PREFIX>_CODEX_REASONING`
    still overrides it. Selections of a literal model or `default` are unchanged.
  - Codex accepts `none`, `xhigh` and `max` from every source, so a `config.toml`
    holding one of them no longer stops a review; `xhigh` and `max` fall back to
    `high`, `medium`, then `low`.
  - `baton status --json` and `baton config show` report `resolved_worker_effort`
    (also as a `--section`), worker model guidance names the effort, and `config
    show` lists `model_tier_effort`.
  - `baton doctor` runs each tier target at its effort, without Codex's fallback.
  - Cursor has no effort setting; its model IDs carry the effort.

## 0.1.0

Initial public release.

- Reviews run on one runner and one executor. `baton review run --kind
  plan|implementation` replaces the `plan-peer-review.sh` and `peer-review.sh`
  scripts, and the `baton-review-executor` skill replaces
  `baton-plan-review-executor` and `baton-peer-review-executor` (`baton skills
  install` removes their managed links); `baton-peer-review` dispatches plan
  reviews too. Every run writes a `run.json` record with each reviewer's outcome,
  failure reason and per-attempt session IDs; `baton review show` and `list` find
  and inspect runs (a run whose runner died shows as `interrupted`), and the
  trusted `baton review save` copies a finished run into the project's artifacts.
  Implementation reviews embed whole per-file diffs while the whole input fits
  `PEER_REVIEW_MAX_INPUT_BYTES` (default 180,000; it replaces
  `PEER_REVIEW_MAX_DIFF_BYTES`) and let reviewers read the rest, listed in
  `diffs/INDEX.txt`, and the repository on demand, instead of cutting the diff;
  prompts state an inspection budget and deadline; rate limits and transient errors
  are retried once within the reviewer's timeout; failures are classified rather
  than reported as `unavailable`; Codex fallbacks share one deadline and keep the
  timed-out attempt's log; a failed `git fetch` is a warning. The review loop needs
  two successful reviewers for a clean general round, ends on persistent reviewer
  failures as a coverage blocker, and passes earlier rejected findings to later
  rounds.

- Codex reviewers start no MCP servers: reviews turn Codex plugins off and disable
  every MCP server `codex mcp list` reports as enabled, and record which ones in the
  run record. Claude reviewers already load none (`--strict-mcp-config`).

- agy review attempts record their progress (tool calls, time of the last one,
  elapsed time, review text streamed, token totals) in the run record, and
  `baton review show` prints it, so a timeout while writing can be told from one
  while still reading.

- `baton config set` removes model entries and tier mappings with repeatable
  `--remove-plan-review-model`, `--remove-implementation-review-model`,
  `--remove-worker-model` (each `PROVIDER`) and `--remove-model-tier
  PROVIDER[.TIER]`; a bare provider removes all of its tiers. Removing from an
  unsaved review map starts from the install defaults. `baton open-plan` opens
  `$VISUAL`, then `$EDITOR`, falling back to `vim`; values split like a shell, so
  `code -w` works. `baton init` also creates the code-worktrees root, so `baton
  doctor` passes straight after it; project creation explains an artifact root
  outside any Git checkout instead of printing Git's raw error; CLI messages say
  provider rather than harness.

- Documentation pass: user docs and skills match the current CLI (install order
  and default reviewers, custom artifact repository ignore rules, cleanup and
  artifact-commit flags, blackboard syntax, retro state paths), use consistent
  terminology, and keep each fact in one place with links elsewhere.

- Focused reviews can run on separate models: `config set --focused-review-model
  AREA.PROVIDER=MODEL` (global or with `--selector`) overrides a provider's normal
  implementation model for one catalog area, with `--remove-focused-review-model`
  and `--clear-focused-review-models` for repair. `baton review focus NAME` prints
  the catalog guidance that named review passes inject.

- `baton retro summarize` and the opt-in weekly cron job write a cross-project
  report of recurring retrospective themes, using a fresh session of an explicit
  provider and a frozen seven-day selection of committed retros.

- `baton-plan` Phase 4 runs independent implementation validation: workers
  distinct from the implementers exercise acceptance criteria and operational
  risks, report findings with evidence under `validation/`, and recheck fixes.

- `baton cleanup --force` reports blockers, requires interactive confirmation,
  and releases a reusable slot through best-effort local cleanup. Failed
  preservation or removal is reported as a warning; unsafe resources are left
  untouched. Normal cleanup and PR-closure behavior remain separate.

- Workers can regenerate their handoff after losing context. Every handoff prints
  an exact `Refresh instructions` command. `handoff --task` now also accepts the
  requesting agent's own in-progress task: it omits the claim, prints the current
  version for settle and a task-history read for recovering notes and question
  IDs, and warns rather than refuses when the member needs a rebase. Coordination
  guidance dispatches the handoff output verbatim with its refresh command.

- Blackboard current state is easier to keep and unresolved work harder to miss.
  Every `blackboard read --json` returns the overview `revision`, so
  `read --tail 0 --json` supplies an edit without returning the whole board. New
  boards start with a current-state overview template. `task list --held` lists
  tasks held after a failed settlement. Exported `overview.md` marks held tasks and
  lists unanswered questions. Task handoffs print a `--task`-linked progress
  command. Coordination guidance asks parents to maintain the overview at major
  handoffs, run an end-of-turn check (`blackboard agents`, `task list --held`,
  `task list --blocked`), encode gating prerequisites as dependencies and cancel
  superseded failed attempts with a pointer to their replacement; workers link
  substantive posts to their task.

- Review executors request an approved outer launch upfront when the parent
  harness blocks Codex CLI state access, preserving the child reviewer's
  read-only sandbox and noninteractive approval policy.

- `baton abandon` confirms project state and exact, paginated GitHub PR discovery,
  closes PRs and resumes regular cleanup safely after partial progress. It never
  deletes remote branches. `baton create follow-up --from FULL_UUID` creates a
  fresh project from archived repository sources, with automatic lineage names,
  current default branches and locally committed immediate-predecessor context.
  Status and ownership claims expose that historical context while claim stdout
  remains JSON.

- Reviewers now distinguish fatal Codex startup permission errors from model
  reasoning failures, preserve diagnostics and direct executors to the harness's
  normal approved launch recovery. AGY and plan-review prompts keep artifact
  inspection within granted roots; related plan context is supplied inline.

- Member retirement now accepts closed, unmerged PRs as abandonment evidence for
  the exact local tip, preserves that outcome and blocks open PRs. Abandoned native
  stacks retire as whole groups; existing worktree and archive safeguards remain.

- AGY reviews use native file tools and report headless permission denials. A
  completed review is retained when the CLI hangs during shutdown: Baton grants
  a bounded shutdown grace, then kills and reaps its process group. Malformed
  streams, cancellation and ordinary nonzero exits still fail the reviewer.

- Agent guidance now routes implementation through the configured review loop,
  centralizes worker model selection and branch operations, and uses role-specific
  required reading. Read-only review specialists report through their dispatcher;
  mutating workers retain task claim/settlement. Generated handoffs retain concrete
  identity/task commands and point to branch-worker obligations. Planning asks only
  material questions and gives proportional updates. Public docs correct task
  completion examples and qualify AGY review permissions; CLI defaults are unchanged.

- `baton search` finds current and archived projects from literal keywords and
  phrases in metadata, saved plans, notes and coordination history, with ranked
  excerpts, UUID selectors, text/JSON output and coverage warnings. The workflow
  skill supports discovery from any directory. `session show` and `session fork`
  accept archived projects; exploratory forks use the verified first recorded
  source checkout without reopening the project or changing its owner.

- Plan reviews challenge complexity first and check the plan against the code.
  `plan-peer-review.sh` passes every active project worktree (outside the
  framework, the current checkout) to the new `baton review run --read-dir`,
  which Claude, agy and Cursor receive as `--add-dir`. The prompt no longer
  forbids repository inspection. Reviewers tag findings `code-verified` or
  `plan-only`, and a significant simplification is high severity. Planning and
  the plan-review executor treat accepted significant complexity findings as
  blocking, and the default plan-review timeout rises from 180 to 420 seconds.

- AGY reviews receive the complete prompt and reviewed text as JSON on stdin,
  avoiding file-reading tool turns and command-line prompt size limits. The
  adapter retains its sandbox, validates the successful terminal response and
  preserves raw events in `-agy-stream.jsonl` for diagnosis.

- Workers end tasks with `task settle --outcome succeeded|failed`, once per claim.
  Failed tasks stay held for their assignee until the parent assigns or cancels
  them. `blackboard wait` and `blackboard ask` let agents block on questions,
  replies and settlements from resumable cursors, `blackboard agents` reports
  advisory liveness, and `handoff --task` prints the worker's task commands. The
  coordination skill describes the parent wait loop, decision tasks, and why silence
  never proves a worker exited. Still schema 2: new boards add indexes on
  `entries(parent_id)` and `events(entity_id)`; existing boards keep working
  without them.

- Providers come from one registry (`baton/providers.py`, formerly
  `review_providers.py`). Codex, Claude, Antigravity (`agy`) and Cursor can all claim,
  resume and fork project ownership and run retrospectives. Providers without a
  native fork start a fresh session with no owner history. Claims read only the
  selected provider's session variable, and launched CLIs never inherit session
  variables. Unattended retros copy their evidence into the private workspace, get
  read-only or file-tool-only permissions, return the draft on stdout, and report
  `blocked` for providers without a safe headless mode. `baton status --json` adds
  `provider_guidance`, `baton config show` adds `providers`, default skill targets
  include each detected provider's skill directory, and the retro cron entry
  forwards `USER`. Reverting this change makes agy- or Cursor-owned projects fail
  with an unknown provider.

- Reviews run in Python: `baton review run [--check]` selects, launches and
  collects reviewers through the provider registry, keeping the review scripts'
  options, environment overrides, file names and output lines. The scripts require
  an installed Baton with `review run` and say to reinstall otherwise. Doctor smoke
  tests call the same provider code. `baton config show` drops `review_providers`;
  review invocation metadata is part of `providers`.

- Mutating commands require a clean-main runtime, including direct config, skill,
  and cron entry points. Installed builds retain immutable trust metadata; source
  checkouts are checked at invocation. The installer rejects dirty or non-main
  checkouts before replacing the CLI. Isolated tests can explicitly opt out.

- Review-loop focused reviews and general-round limits are configurable. Focused
  reviews name entries in the built-in catalog (`performance`, `complexity`,
  `security`) rather than free-form prompts. Either phase, or both, can be disabled
  with an empty focus list and zero rounds.

- Review scripts use each workflow's configured provider set. Adding or removing
  review-model entries changes the default reviewers, and initialization preserves
  those choices. Per-run reviewer overrides remain available.

- Baton supports only its current command names, configuration and storage paths,
  project manifests, and SQLite schema 2.
- Removed previous-version command aliases, storage aliases, config fallbacks,
  installer migrations, legacy manifests, Markdown boards, and schema upgrades.
- General tests use canonical Baton names and current-format fixtures.
- Repairs to older installations are performed manually outside this repository.
