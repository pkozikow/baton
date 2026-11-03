---
name: baton-retro
description: >-
  Conduct an evidence-based project retrospective, writing or updating retro.md
  with framework, local environment, and per-repository lessons, and archive
  relevant repository notes before cleanup.
argument-hint: "[project-name-or-plan-path]"
disable-model-invocation: true
---

# Project Retrospective

Resolve sibling skills and scripts relative to this skill after resolving its symlink. When delegating, supply the resolved absolute `SKILL.md` path and require a complete read. Explicit invocation includes routing or delegation by another skill.

## Establish Scope

`baton retro` starts a retrospective fork of the project's owner session
and explicitly invokes this skill. When the owner's provider cannot fork, it
starts a fresh session instead: you then have no owner history and rely only on
saved evidence, and the launch prompt says so. The launch prompt supplies the selected
project name, UUID, owner record, and absolute `retro.md` output path. Its
authorization covers the retrospective, relevant note archival, and committing
those artifacts. You are not the owner: inherited history does not confer
ownership. Never claim or replace the owner, change repository code or project
topology, run cleanup, or direct implementation workers as part of this task.
Recommend follow-up work instead.

Use the supplied full UUID selector, not the inherited working directory or a
reusable project name. For interactive invocations, `baton retro show --selector
<full-uuid> --json` returns the selected identity, frozen manifest, artifact path,
and archive status. Verify its UUID, owner, and artifact path against the launch
prompt before any artifact write or commit. Unattended invocations use the supplied
`identity.json` instead; do not run `retro show`, `status`, or other framework
metadata commands. Stop on disagreement; never redirect into a reused slot.

For an active interactive invocation, also read `baton status <selector>
--json` for current worktree/base information. Archived projects use the supplied
frozen manifest instead: their coordinator and code worktree paths can have been
reused, so never call status on their slot, read current notes from those paths,
or recreate the project. Use inherited owner history (forks only), saved plans,
archived notes, recorded review/test evidence, and the saved board. Report missing
pre-cleanup evidence honestly.

An **unattended invocation** runs in a private temporary workspace holding
`identity.json` and copies of the saved evidence (plans, notes and the board's
Markdown export). Read the supplied identity snapshot and only that copied
evidence. The CLI blocks writes, so do not create or modify files, run artifact
commits, or run shell commands the launch prompt rules out: the trusted framework
verifies and publishes the draft without overwriting an existing final file, then
commits it. Include the full project UUID in Outcome. Do not ask for input; record
uncertainties. Unattended runs use saved evidence and do not archive notes from
current repository checkouts. Await any analytical subagents and integrate their
results before returning.

For unattended invocations only, print the complete draft exactly once between these standalone marker lines:

```text
BATON_RETRO_DRAFT_BEGIN
<complete retrospective Markdown>
BATON_RETRO_DRAFT_END
```

Do not repeat the markers in explanations or print multiple drafts. The framework
recovers the draft from this output; report any refused tool call outside the
marker block and continue to respect the supplied boundary.

For direct explicit skill use without a launch prompt, use a supplied plan path or project directory. Otherwise run `baton status --json` (exit 3 outside a project) and take the name, UUID, owner, and `master_path` from it. Outside the framework, use an explicit path or `<artifacts_root>/<project>/plan.md`, taking `paths.artifacts_root` from `baton config show --section paths` (environment overrides are included); ask if the project is ambiguous. Do not invent a plan or infer ownership from conversation history. Report missing identity fields honestly.

## Gather Evidence and Choose the Work Split

Read the supplied coordination `overview.md` and complete `history.md`
first. Use their recorded snapshot sequence, frozen project context and task
assignments/dependencies to identify unresolved work and cancelled blockers. Only
`done` satisfies prerequisites; sealing an archive does not complete tasks. An
active on-demand export may precede the final closure snapshot; report each
sequence honestly without requiring a new retro for every later post. Archived
analysis uses saved Markdown even when the database is unavailable; never open,
create a database or inspect a reused slot to fill missing evidence.

Read the complete master plan, branch subplans (`branches/**/plan.md`), and any
existing `retro.md`. Read saved `validation/` summaries and relevant harness usage
instructions; separate measured acceptance evidence from code-review outcomes.
Report unavailable raw output honestly. For an active interactive framework project, read the board
using `baton blackboard read --json --selector <project>` and the recorded
generation. Never commit the live blackboard.

Use inherited owner context (forks only) alongside relevant commits, diffs, test/CI results,
review findings, and recorded user corrections or debugging attempts. Cover
every participating repository and relevant branch, including retired members.
Inspect another ref with Git rather than switching primary or managed
worktrees. If status reports `base_reliable: false`, report `base_error` and do
not invent a comparison base; continue only evidence gathering that does not
depend on that base. State evidence gaps, such as worker conversations absent
from the owner's history, rather than treating silence as success.

Decide whether to analyze the project yourself or delegate sections to
subagents based on its size, repository count, and amount of independent
investigation. The delegation details are yours to choose, subject to existing
project coordination instructions. Keep section assignments analytical; you
own the final `retro.md`, reconcile overlapping causes and conflicting findings,
and await all delegated work before finalizing it. Separate owner-session forks
are not required for each section.

## Write the Retrospective

Create or update the selected `<project-dir>/retro.md` (in unattended mode, print the draft instead):

```markdown
# Retrospective: <project>

## Outcome

## Project Framework and Local Environment

## Repository: <repo-name>

## Recommended Follow-ups
```

Use one repository section per participating repository. Aim for roughly 1,000
words for a typical project; scale with the number of repositories and useful
evidence rather than enforcing a rigid cap.

- **Outcome:** Include the project UUID, original owner provider/session ID,
  and retro date. Briefly state what was delivered, what remains unfinished,
  and material departures from the plan. Do not restate the plan.
- **Framework and local environment:** Assess project/branch/worktree lifecycle,
  agent coordination and handoffs, blackboard use, review orchestration, local
  services and databases, dependencies, permissions, credentials setup, tooling,
  repeated manual steps, and avoidable user intervention where evidence warrants.
- **Each repository:** Assess mistaken assumptions, missed existing helpers,
  architecture surprises, implementation rework, debugging and review churn,
  fixtures, tests and CI, validation gaps, and missing or misleading instructions
  or documentation.
- **Follow-ups:** Consolidate a short prioritized table with `Priority`,
  `Target`, `Proposed change`, and `Expected benefit`. Targets should identify
  the relevant repository, script, skill, instructions, or workflow. These are
  recommendations, not permission to implement them.

For each material finding, supply a concrete example or evidence reference,
its impact, the observed cause or clearly labeled suspected cause, and a
specific improvement. Do not invent durations or savings. Classify a finding
where its improvement belongs: missing repository setup docs belong in that
repository; a broken shared launcher belongs in framework/environment.
Cross-reference shared incidents instead of duplicating them.

Assess validation quality against readiness claims: representative workloads,
regression budgets, unresolved or dismissed slow/error/cancel/timeout anomalies,
and whether fixes were revalidated at recorded revisions. For custom harness
capabilities, recommend upstreaming only when reusable beyond the project or
repeatedly filling a native-tool gap; identify the target repository/tool, concrete
capability, evidence and expected benefit. Keep one-off glue local. Recommendations
do not authorize product edits.

Include effective practices worth repeating. Favor consequential incidents and
recurring causes over chronological command logs; routine debugging is not
automatically a process failure. Do not fabricate issues to fill sections, and
say when the available evidence supports no material finding or no change.
Reconcile an existing retro with new evidence, preserving useful observations
and removing duplication. Do not include secrets, credentials, or sensitive
customer data in any artifact, including archived notes.

## Archive Repository Notes

For active interactive invocations, before cleanup removes ignored worktree files, archive relevant repository notes. Archived or unattended invocations use existing archived evidence and skip this collection step. Use `note_directories` from `baton status <selector> --json` as the authoritative configured note directories. Outside the framework, use `BATON_NOTE_DIRS` (path-separated relative directories, default `notes` and `thoughts`) and repository-specific guidance. Obtain all worktree paths from that status.

1. Collect note directories from all participating `entries[]`, plus `retired_entries[]` whose worktrees still exist. Include the primary checkout when relevant, resolving it through Git's common directory. Deduplicate resolved paths. Outside the framework, use the repository directory and its primary checkout when available.
2. Inspect all files, including ignored and untracked files. Select files cited
   by the plan/work, matching the project or issue, or materially informing its
   research, decisions, implementation, or debugging. Exclude unrelated
   research, indexes, and temporary files.
3. Copy relevant files into `<project-dir>/<note-dir>/`, preserving paths relative
   to the source note directory and their modification times (`cp -p`). When
   sources or an existing archive conflict at one path, keep the newest by
   preserved source mtime and report the conflict; if recency is unknowable (an
   archive copy's checkout time is not content recency), keep the existing
   archive and report the conflict. Verify copies
   match their sources. Never move, delete or modify source notes, or remove
   older archived files merely because they are not selected now.
4. Keep existing lines in `<project-dir>/archive-skipped.txt` and add every
   reviewed source file deliberately not copied, including irrelevant files,
   indexes, temporary files, and losing conflicting versions. Use one
   checkout-relative path per line, e.g. `notes/research/other.md`.
   Cleanup checks each ignored note for either identical archived content or
   this skip record. Review sensitive files without copying their contents;
   only record their path when it is safe to do so, and report any remaining
   archival blocker. Do not silently discard evidence or bypass checks.

Do not create an empty archive directory when nothing is relevant. Report
missing retired worktrees or other evidence that could not be inspected.

## Verify, Commit, and Report

Confirm `retro.md` is nonempty, findings are evidence-based, each repository is
covered, and follow-ups agree with the analysis. Recheck archived copies against
selected source files and ensure skipped ignored notes are accounted for.

For an interactive framework project (active or archived), revalidate its UUID
with `baton retro show --selector <full-uuid> --json` and commit only `retro.md`,
archived notes, and `archive-skipped.txt`, using absolute paths:

```sh
baton artifacts commit --selector <full-uuid> -m "Write project retrospective" <paths>...
```

For active projects run this before cleanup; do not invoke cleanup yourself.
Unattended invocations leave publication and commits to the framework. Outside the framework,
follow the artifact repository's worktree and commit rules. Never stage the board or
unrelated changes. Report whether the artifact commit was published, remained
local after a network failure, or was blocked. Do not rebase, merge, or reset
the shared checkout to resolve divergence.

Report the retro path, whether it was created or updated, archival/conflict
outcomes, evidence limitations, and the most useful follow-up recommendations.
