# Configuration

## Storage roots

New projects use configurable coordinator, code-worktree, and artifact storage.
Inspect or persist root paths with `baton config show` and `baton config set`;
configure roots before running `baton init`, and do not change them while projects
are active.

| Storage | Default | `config set` flag / `paths` key | Environment override |
| --- | --- | --- | --- |
| Coordinator repository | `~/dev/baton-projects` | `--projects-repo` / `projects_repo` | `BATON_REPO` |
| Coordinator slots | `~/dev/baton-wt` | `--slots-root` / `slots_root` | `BATON_SLOTS_ROOT` |
| Code worktrees | `~/dev/baton-worktrees` | `--worktrees-root` / `worktrees_root` | `BATON_WORKTREES_ROOT` |
| Source repositories | `~/dev` | `--repos-root` / `repos_root` | `BATON_REPOS_ROOT` |
| Project artifact directories | `~/dev/baton-artifacts/projects` | `--artifacts-root` / `artifacts_root` | `BATON_ARTIFACTS_ROOT` |

```sh
baton config show
baton config set --projects-repo ~/workspace/coordinator \
  --slots-root ~/workspace/slots --worktrees-root ~/workspace/worktrees
```

Repository names and relative paths given to `create --repos` and `add` resolve
under the source-repository root; absolute paths are used as given.

Settings live in `~/.config/baton/config.json` (`XDG_CONFIG_HOME` and
`BATON_CONFIG_FILE` are supported).
Environment overrides win over saved paths, then the defaults apply.

`baton init` creates the default artifact checkout automatically. It is
local-only; configure a remote separately if publication is needed.

### Custom artifact repository

An explicitly configured artifact root must already be inside a Git checkout;
`baton init` never creates it. Relative to each project directory, the checkout must
ignore `blackboard-lock.md`, `blackboard.sqlite*`, `.blackboard-*.tmp`,
`coordination/blackboard-lock.md`, `coordination/.blackboard-*.tmp` and
`coordination/.snapshot-*.sqlite*`. It must not ignore the sealed evidence in
`coordination/`: `board.sqlite`, `overview.md`, `history.md` and `manifest.json`.
Project creation and `blackboard init` refuse a checkout that does not ignore
`blackboard-lock.md` and `blackboard.sqlite*`; cleanup checks the `coordination/`
rules and refuses a checkout that ignores the evidence. For example, a `.gitignore` in
the artifact root:

```gitignore
/*/blackboard-lock.md
/*/blackboard.sqlite*
/*/.blackboard-*.tmp
/*/.blackboard-*/
/*/coordination/blackboard-lock.md
/*/coordination/.blackboard-*.tmp
/*/coordination/.snapshot-*.sqlite*
```

`/*/.blackboard-*/` also ignores the default `blackboard export` directory.

## Storage names

Registries use `<coordinator-git-common-dir>/baton/v2/`. Explicitly configured
artifact roots need no marker; `baton init` creates, or adopts, only a default
artifact checkout marked with `.baton-artifacts`.

## Branch prefix

The default prefix value is `baton`, so new projects use `baton/<project>`
branches. Set a custom namespace with:

```sh
baton config set --branch-prefix team
baton config show
```

The `branch_prefix` JSON setting accepts letters, digits, dashes and underscores
in slash-separated components (for example, `team/feature`). A trailing slash is
optional. The prefix is recorded with each new project and its worktrees; changing
the setting does not change existing projects, their branches, or subplan paths.
Existing projects keep their prefix when renamed or extended with new members.

## Repository aliases

Repository aliases are optional local configuration; no aliases are built in. Set or
remove them independently of storage paths:

```sh
baton config set --repo-alias api=service-api --repo-alias web=web-client
baton config set --remove-repo-alias web
baton config show
```

Aliases apply to repository arguments in create/add, path, handoff, branch/stack,
retirement, and cleanup overrides. Alias names and targets must be simple repository
names. Targets are canonical names and are resolved once, without chaining. Explicit
paths such as `./api` bypass name aliases; project manifests always retain canonical
repository identities. Updating or removing an alias affects later commands without
rewriting project metadata.

## Review models

Review models are independent for plans and implementations, with one setting per
provider. The keys in each saved review-model map define that workflow's default
reviewer set. Add an entry to enable a provider; remove it with
`--remove-plan-review-model PROVIDER` or `--remove-implementation-review-model
PROVIDER` to stop running that provider by default. An empty map selects no
reviewers. If the entire map is
absent, Baton uses its built-in defaults. Installation and `baton config init`
initialize missing maps and preserve existing maps, including removed providers.

```sh
baton config set --plan-review-model codex=MODEL_ID \
  --implementation-review-model claude=MODEL_ID \
  --implementation-review-model agy=MODEL_ID
baton config set --plan-review-model codex=default
baton config set --remove-implementation-review-model agy
baton config show
```

Removal flags are repeatable. Removing a provider that is absent is a no-op, and
setting and removing the same provider in one command fails. Removing from a map
that is not saved yet starts from the built-in defaults, so the removal persists.

The JSON keys are `plan_review_model` and `implementation_review_model`, each a
provider-to-model map. `"default"` omits the model flag and lets the provider
CLI choose. Direct model IDs are passed explicitly. The reserved values `strong`,
`medium`, and `weak` resolve through that provider's `model_tiers` mapping; an
unmapped tier fails before any review starts. Mappings are shared by the plan and
implementation workflows, while their model selections stay independent.

Model settings in environment variables do not override this policy.
`PLAN_REVIEW_REVIEWERS` and `PEER_REVIEW_REVIEWERS` select registered providers
for one run, independently of the saved default set; providers without a saved
model use their CLI default. Reviewers run in registry order.
`<PROVIDER>_BIN` overrides executables.
`PLAN_REVIEW_TIMEOUT_SECS` / `PEER_REVIEW_TIMEOUT_SECS` override the timeout for
every reviewer, and `PLAN_REVIEW_<PROVIDER>_TIMEOUT_SECS` /
`PEER_REVIEW_<PROVIDER>_TIMEOUT_SECS` override it for one provider. The review
helpers' `--help` lists Codex reasoning controls and other invocation options; see
[Review runners](skills.md#review-runners). Reviewer safety is described in
[Review boundaries](skills.md#review-boundaries).

## Review loop

`review_loop` controls `baton-review-loop`. The built-in focus catalog contains
`performance`, `complexity`, and `security`; names are exact and case-sensitive,
and their guidance ships with Baton. Custom prompts and catalog files are not
supported.

| Key | `config set` flags | Meaning |
| --- | --- | --- |
| `focused_reviews` | `--review-focus NAME` (repeatable; replaces the list), `--no-focused-reviews` | Ordered focused passes, one dispatcher pass per entry; duplicates repeat and `[]` skips the phase. Default: all three |
| `general_rounds` | `--general-review-rounds COUNT` | Maximum general rounds, stopping early on a clean report; `0` skips the phase. Default: 10 |
| `focused_review_models` | `--focused-review-model AREA.PROVIDER=MODEL`, `--remove-focused-review-model AREA.PROVIDER`, `--clear-focused-review-models` | Model for one focus area and provider |

```json
{
  "review_loop": {
    "focused_reviews": ["performance", "complexity", "security"],
    "general_rounds": 10,
    "focused_review_models": {"security": {"codex": "strong"}}
  }
}
```

Add `--selector PROJECT` to save project overrides; project `config set` accepts
only review-loop flags, and `--inherit-review-loop` removes all of them (it is
exclusive with other review-loop flags). `config init` takes no selector. Omitted
settings keep their saved values, and disabling both phases requests zero reviews.

Precedence:

- **Schedule:** a review-loop request may name focuses or a round limit for that
  run only (catalog names only, nothing saved); then project fields; then global
  fields; then built-in defaults. A project's focus list replaces the global list,
  including `[]`.
- **Focused models**, per focus/provider pair: project pair, then global pair,
  then the provider's normal `implementation_review_model`. Overrides never enable
  reviewers or change plan or general review models. Literal model IDs, mapped
  tiers and `default` have their normal meanings.

```sh
baton config set --review-focus performance --review-focus security \
  --general-review-rounds 3
baton config set --no-focused-reviews --general-review-rounds 0
baton config set --selector PROJECT --review-focus security
baton config show --selector PROJECT
baton config set --selector PROJECT --inherit-review-loop
baton config set --focused-review-model security.codex=strong \
  --model-tier codex.strong=MODEL_ID
baton config set --selector PROJECT --remove-focused-review-model security.codex
baton config set --clear-focused-review-models
```

Pair removal restores inheritance at that scope and accepts literal invalid saved
keys for repair; setting and removing the same pair in one command fails.
`--clear-focused-review-models` removes the saved map, including a malformed one,
and is exclusive with pair flags. Dormant pairs must still reference valid
areas, providers, models and mapped tiers.

Full `config show` includes `review_focus_catalog` (name to guidance), effective
`review_loop`, and `resolved_focused_review_models`, including normal-provider
fallbacks. `project_review_loop` contains explicit project settings, `{}` when
absent, or `null` in global show. Resolved `default` remains a sentinel for provider
selection. Display does not write configuration or project records.

### Invalid settings and repair

Full `config show`, ordinary `config init`, prospective `config set`, schedule
resolution, and named focused runs validate the entire review-loop configuration
before writing or launching providers. An invalid setting fails with an error that
names the repair command. Global errors remain errors even when a project overrides
the field; repair global settings first:

```sh
baton config set --review-focus performance --review-focus complexity --review-focus security
# Or skip the focused phase:
baton config set --no-focused-reviews
# Repair malformed model maps or reset invalid project overrides:
baton config set --remove-focused-review-model AREA.PROVIDER
baton config set --clear-focused-review-models
baton config set --selector PROJECT --inherit-review-loop
```

Repairs are applied before validating the complete prospective result; untouched
invalid settings still fail. Ordinary configuration loading, status, blackboard/artifact operations, and
plan/general reviews ignore the review-loop subtree. Narrow read-only metadata
queries, `baton config show --section paths` and
`baton config show --section branch_prefix` (repeatable), validate only requested
fields and do not load the catalog. The `providers`, `worker_model`, and
`resolved_worker_model` sections support worker discovery without resolving the
review loop. Installation uses
`config init --preserve-review-loop` to preserve existing settings without
validating this subtree; ordinary initialization stays strict.

## Model tiers

```sh
baton config set --model-tier codex.strong=MODEL_ID \
  --plan-review-model codex=strong
baton config set --implementation-review-model codex=MODEL_ID
```

Mappings start empty. Add only the tiers you need with repeatable
`--model-tier PROVIDER.TIER=MODEL` arguments; each target must be a literal model
ID, rather than another tier or `default`. Remove them with repeatable
`--remove-model-tier PROVIDER.TIER`, or `--remove-model-tier PROVIDER` for all of
that provider's tiers; removing a tier that a model selection still uses fails. The command above stores
`"model_tiers": {"codex": {"strong": "MODEL_ID"}}`. Configuring a tier alone
does not select a review model or enable an opt-in provider. `baton config show`
includes the configured choices and the effective `resolved_plan_review_model` and
`resolved_implementation_review_model` maps used by the runners.

## Worker models

Worker model preferences are separate delegation guidance for the project owner:

```sh
baton config set --worker-model codex=MODEL_ID
baton config set --worker-model claude=medium --model-tier claude.medium=MODEL_ID
baton config set --remove-worker-model codex   # same as --worker-model codex=default
```

`worker_model` maps provider names to model IDs, `default`, or configured tiers.
Installation fills missing default providers with `default`, preserving existing
choices. `default` means no guidance; the framework does not add a model choice to
worker dispatch.

`baton status --json` exposes the configured `worker_model` and concrete
`resolved_worker_model` maps, matching `baton config show`. A non-default value also
appears as actionable `worker_model_guidance` and in plain status output. Both
forms show guidance only for the recorded owner's provider; unclaimed projects
show all configured guidance until ownership is recorded. This follows project
ownership, regardless of which agent or shell invokes status. The model maps
remain complete for all providers.

Agents receive this dynamic guidance by reading `baton status --json`, as directed
by the skills; there is no separate worker-model prompt injected at session startup.
The planning and peer-review skills apply a model preference only when the worker's
provider has an entry in `worker_model_guidance`. They take its concrete model ID
from `resolved_worker_model` and pass it explicitly when launching workers,
even when the owner runs a different model. These settings are read at invocation
time, so existing projects see updates without rewriting their manifests.

The preference does not change ownership, select the owner model, enable a review
provider, or override explicit user instructions. A runtime that cannot choose the
configured worker model should report that limitation before dispatch. `baton
doctor` validates worker references and smoke-tests non-default targets; see
[Installation](installation.md#diagnostics-baton-doctor).

## Providers

Baton's agent CLIs come from a built-in registry (`baton/providers.py`); users do
not define providers in configuration. The registered providers are `codex`,
`claude`, `agy` (Antigravity) and `cursor`. Each can own a project, resume it, fork
or start a fresh exploratory session, run interactive retrospectives, and receive
skills. `<NAME>_BIN` (for example `CODEX_BIN` or `CURSOR_BIN`, whose default
executable is `agent`) overrides an executable for every launch, including
interactive ones.

A session claim without `--session-id` reads only the selected provider's session
variable: `CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`,
`ANTIGRAVITY_CONVERSATION_ID` or `CURSOR_CONVERSATION_ID`. Every CLI Baton launches
runs without any registered session variable, so a child cannot inherit and claim
the owner's identity. Providers without a native fork (agy, Cursor) start a fresh
session where others fork.

`baton config show --section providers` prints the registry: executables and
override variables, session variables, fork and review settings, skill
directories, and `guidance`. `baton status --json` serves the same guidance as
`provider_guidance`, covering every provider regardless of the project owner: the
claim command, where the session ID comes from, and how to start a subagent
without inherited context. Skills tell agents to apply the entry for their own
provider. Codex and agy reviews use their own invocations (see
[Review boundaries](skills.md#review-boundaries)), so their `review` entry lists no
arguments.

Cursor is opt-in for reviews and worker-model defaults only; Cursor sessions can
always own projects. Installation and `baton config init` do not add it to the
model maps or default reviewer set. Select it for one run with
`PLAN_REVIEW_REVIEWERS=cursor` or `PEER_REVIEW_REVIEWERS=cursor`; an absent model
setting uses the CLI default. Saving a Cursor model with
`--plan-review-model cursor=MODEL_ID` or `--implementation-review-model
cursor=MODEL_ID` adds it to that workflow's default reviewer set.
