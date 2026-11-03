# Installation

## Full install

Clone the repository and run the installer from a clean `main` checkout:

```sh
git clone https://github.com/pkozikow/baton.git
cd baton
./install.sh
baton --help
```

The wrapper checks the branch of its own checkout, regardless of the current
working directory. It refuses feature branches, detached HEADs, dirty checkouts
(including staged and untracked files), and sources without Git metadata before
changing the installed CLI, configuration, skills, or cron.
`--help` remains available on any branch.

Run the same wrapper after updating the checkout. It installs a noneditable copy of
the CLI through `uv tool install --reinstall`, then installs the bundled `baton-*`
skills for every detected provider (see [Skills only](#skills-only)). Editing the checkout never silently changes the
installed CLI. If the command is missing from PATH, run `uv tool update-shell` and
restart your shell.

The wrapper initializes configuration with `config init --preserve-review-loop`,
which keeps saved review-loop settings verbatim without validating them, so
installation succeeds even when they are invalid. Commands that use those settings
reject invalid values and print the repair; see
[Invalid settings and repair](configuration.md#invalid-settings-and-repair).

## CLI only

```sh
uv tool install --reinstall .
```

Run this from the root of a clean `main` checkout; other builds install but refuse
writes (see [Runtime write protection](#runtime-write-protection)). It skips
configuration initialization and skill installation; run `baton config init` and
`baton skills install` yourself if needed.

## Skills only

To install or update only the skills from the installed distribution:

```sh
baton skills install
# Optional isolated or alternate discovery directories:
baton skills install --target /path/to/skills --target /other/path/to/skills
```

The install wrapper accepts repeatable `--skills-target DIR` arguments, which
replace the defaults. Default destinations are always `~/.agents/skills` and
`~/.claude/skills`, plus the skill directory of every other registered provider that
is present: its CLI is on `PATH` (honoring `<NAME>_BIN`) or its skill directory
already exists. Today that adds `~/.gemini/antigravity-cli/skills` for agy; Codex and
Cursor share `~/.agents/skills` and Claude uses `~/.claude/skills`. The command
prints each skipped directory and why.

Every provider should discover each Baton skill exactly once. Installation warns
about unmanaged `baton-*` copies in directories a provider reads, because stale
copies can shadow the installed skills. Move those copies explicitly if needed;
Baton never deletes unmanaged skills. See the registry exposed by
`baton config show --section providers` for each provider's discovery directories.

Task-specific skills are managed symlinks into the installed distribution.
`baton-workflow` is a managed directory with a generated `SKILL.md` and links to
bundled resources. Installation fills its `{{BATON_SLOTS_ROOT}}` description
placeholder with the effective coordinator slot root, preserving its other triggers:
finding/revisiting projects from anywhere and explicitly requested project work.
See [Bundled skills](skills.md#bundled-skills) for the invocation policy.

The description is a snapshot of the configuration and environment used for
installation, including `BATON_CONFIG_FILE` and `BATON_SLOTS_ROOT`. After changing
the slot root, rerun `baton skills install` for the same discovery targets. One
installed workflow description represents one effective configuration; use separate
skill targets for separate configurations. Ad-hoc environment overrides in later
sessions do not update it. The skill still resolves authoritative project metadata
with `baton status --json` when loaded.

Automatic skill selection is performed by the agent and is not a guaranteed
startup hook. If an installed update does not appear, restart the agent session.

Installation records ownership per destination and checks every target before
updating any skill and refuses unrelated directories or links, modified managed
links, edited generated content, or unexpected files in the generated workflow
directory. Move a conflicting path explicitly and rerun. Existing skills under
other prefixes are untouched.

## Retrospective cron job

To additionally install or update the daily retrospective cron job:

```sh
./install.sh --install-cron
# Optional local policy (default: daily 10:00, completed within 3 days):
./install.sh --install-cron --cron-schedule '30 8 * * *' --cron-since 3d
```

The flag manages one marked block in your user crontab, preserving unrelated
entries. Cron changes require an explicit daily or weekly opt-in; a normal install
or reinstall leaves both cron blocks untouched.
The job uses the installed Python distribution, your config file, and the `PATH`,
`USER`, explicit `XDG_STATE_HOME`, and path/provider binary (`<NAME>_BIN`) overrides present at
installation. Cron runs use each CLI's stored login; see
[Retrospectives](retrospectives.md#logins-under-cron) for keychain caveats. Rerun with the flag
to update those captured settings. The schedule follows cron's timezone.

The wrapper command `baton-retro-cron run` is also available for manual execution
and accepts the same options as `baton retro pending`; `--dry-run --json` previews
discovery. Cron output is appended to
`${XDG_STATE_HOME:-~/.local/state}/baton/retro-cron.log`.

### Weekly report cron

Install the independent weekly synthesis job with an explicit registered provider:

```sh
./install.sh --install-weekly-cron --weekly-provider codex
# Optionally install/update both jobs together:
./install.sh --install-cron --install-weekly-cron --weekly-provider claude
# Override the weekly schedule (default: Monday 11:00 in cron's local timezone):
./install.sh --install-weekly-cron --weekly-provider codex --weekly-cron-schedule '30 11 * * 1'
```

The weekly job invokes `baton-retro-cron run-weekly --provider NAME`, using the
installed distribution and the same captured configuration, paths, provider binary
overrides and stored logins as the daily job. Output appends to
`${XDG_STATE_HOME:-~/.local/state}/baton/retro-weekly-cron.log`. Each job has its own
marked crontab block; installing either preserves the other and unrelated entries.
An explicit provider is required because the report spans projects with different
owners. The jobs do not wait for each other; 11:00 follows the default daily 10:00
schedule but does not guarantee the daily run has finished.

To disable only weekly synthesis, use `crontab -e` and remove exactly the block
from `# BEGIN baton weekly retrospective` through
`# END baton weekly retrospective`, inclusive. Preserve the daily retrospective
block and all unrelated entries.

See [Retrospectives](retrospectives.md#weekly-cross-project-reports) for report
selection, previews, output paths and retry behavior.

## Diagnostics: `baton doctor`

Run `baton doctor` to check the installed CLI, effective storage paths and
repository aliases, and configured review models. `baton doctor --json` emits
structured results; any failed check exits with status 1. Checks run independently
so invalid model mappings do not hide path problems.

Installation enables `codex`, `claude` and `agy` in both review-model maps, so
doctor fails when any of those CLIs is missing. To drop a provider, remove it from
both review maps and remove its tier mappings and any worker model, since doctor
checks every configured model and tier target:

```sh
baton config set --remove-plan-review-model agy \
  --remove-implementation-review-model agy --remove-model-tier agy \
  --remove-worker-model agy
```

**Version check.** Compares the installed build's recorded Git revision with local
`main` (or `master` when that is the mainline), without fetching. It reports the
exact ref and revisions. Update the source checkout first if you need to compare
with remote changes. Use `--source /path/to/baton` and `--ref REF` to override
discovery. Builds without verifiable provenance or from a dirty checkout fail this
check; commit changes and rerun `install.sh`.

**Path checks.** All effective root directories must exist. The coordinator
repository, coordinator slots, and repository alias targets must be Git checkout
roots; slots must belong to the configured coordinator repository. The artifact
directory may be a subdirectory of a Git checkout. Code-worktree and
source-repository roots are container directories and need not themselves be Git
repositories. Coordinator slots are `p1` through `p10`.

**Model checks.** Validate selections and required tier mappings, then invoke each
distinct configured provider/model through the same provider review code used by
`baton review run`, requiring a zero exit status.
Configured tier targets are checked even when not currently selected. Literal model
IDs are verified by the provider CLI accepting them and answering a small nonce prompt;
`default` leaves model selection to the provider CLI. Cursor remains opt-in; its
headless reviews and smoke tests accept workspace trust with `--trust` while
retaining read-only ask mode. Requests run
from temporary directories without project context and may consume normal provider
usage. `--timeout SECONDS` sets the total budget per model (default 60 seconds),
shared by Codex reasoning fallbacks, with up to 5 seconds for a timed-out CLI to
exit after SIGTERM before it is killed; terminating doctor kills the CLIs it
started. `--skip-smoke` validates
configuration and executable availability without model requests; skipped inference
is reported explicitly (`complete: false` in JSON), and existing failures still
cause a nonzero exit.

Doctor also validates worker model references and smoke-tests non-default worker
targets alongside review models through the same provider review code. This checks CLI
acceptance; the owner must still verify that its runtime's subagent model selector
supports the resolved ID.

## Runtime write protection

Commands that change configuration, skills, projects, artifacts, blackboards,
tasks, or cron require a trusted runtime. Launching an editor or agent also
requires trust. Direct module entry points enforce the same checks.

When running from source, the checkout containing the loaded Baton package must
be on `main` with no tracked, staged, or untracked changes. The caller's working
directory does not determine trust. Installed distributions must carry build
metadata recording a clean revision built on `main`. Builds from other branches,
dirty builds, and builds with missing or unverifiable provenance refuse writes.
Reinstall from a clean `main` checkout to update older installations without this
metadata. Direct `uv tool install` does not bypass runtime checks.

An installed clean-main build stays trusted when its original source checkout
changes, advances, or is removed. Freshness against the latest mainline remains a
separate `baton doctor` check.

Help, configuration display, project inspection, and dry runs remain available
without trust. A read that would need to repair the registry refuses that repair.
`baton stack status` requires trust because it refreshes the native stack cache.
An alternate config path or explicit skill target does not bypass these checks.

For deliberately isolated development tests, set
`BATON_ALLOW_UNTRUSTED_WRITES=1` on the test invocation. This explicitly disables
write protection; it does not isolate storage. Test fixtures must still redirect
configuration, storage roots, skill targets, and any external commands. Do not
export this override into normal project sessions.

### Isolated WIP config and skill testing

From the WIP Baton code checkout, use a subshell so the override cannot leak into
later project commands:

```sh
(
  baton_wip="$(git rev-parse --show-toplevel)"
  baton_sandbox="$(mktemp -d)"
  trap 'rm -rf -- "$baton_sandbox"' EXIT
  export BATON_ALLOW_UNTRUSTED_WRITES=1
  export BATON_CONFIG_FILE="$baton_sandbox/config.json"
  export BATON_REPO="$baton_sandbox/projects"
  export BATON_SLOTS_ROOT="$baton_sandbox/slots"
  export BATON_WORKTREES_ROOT="$baton_sandbox/worktrees"
  export BATON_REPOS_ROOT="$baton_sandbox/repos"
  export BATON_ARTIFACTS_ROOT="$baton_sandbox/artifacts/projects"
  export HOME="$baton_sandbox/home"
  export XDG_CONFIG_HOME="$baton_sandbox/home/.config"
  export XDG_STATE_HOME="$baton_sandbox/home/.local/state"
  export BATON_CLI="$baton_wip/bin/baton"
  "$BATON_CLI" config init
  "$BATON_CLI" config show
  "$BATON_CLI" skills install --target "$baton_sandbox/skills"
)
```

This checks ephemeral configuration and skill installation. For project lifecycle
tests, prefer the existing fixtures, which also create source repositories and
initialize the artifact Git repository required by an explicit artifact root.
Use temporary repositories rather than registering live source checkouts, and do
not reuse live paths from project status as test roots. Test artifacts stay
separate from the live project's plans, blackboard, tasks and reviews.

Home/config redirection does not isolate the user crontab. Test cron installation
with a fixture crontab executable; do not invoke real cron installation in this
sandbox. External review and retrospective providers also need fixture executables.
`BATON_CLI` pins review helpers to the WIP CLI only within the isolated subshell.
The temporary directory and skill links are removed when the subshell exits.
