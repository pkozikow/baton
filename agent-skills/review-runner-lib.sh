#!/usr/bin/env bash

# Prints one value from `baton status --json` for the cwd:
# `name` (the project name), `plan` (the default plan path below the projects
# root: the current entry's subplan when the project has several branches, else
# the master plan), `repos` (every active entry's worktree, one per line) or
# `base` (the current entry's diffable PR base).
# Returns 3 outside a framework project (or outside any entry for `base`) so
# callers can fall back; any other failure, including an unreliable base, is
# reported on stderr and must stop the caller. For `base`, returns 4 after
# printing the base when the entry reports `needs_rebase: true` (a stack child
# behind its predecessor).
review_project_value() {
  local field="$1"
  # Use only the installed Baton CLI: a branch checkout's copy of these scripts must
  # never run that branch's unreleased Baton CLI against
  # real project state. BATON_CLI pins one explicitly, e.g. in tests.
  local project_sh="${BATON_CLI:-$(command -v baton || true)}"
  local status_json status_err rc=0

  if [[ -z "$project_sh" || ! -x "$project_sh" ]]; then
    echo "Warning: baton not found; using the default branch base." >&2
    return 3
  fi
  status_err="$(mktemp)"
  status_json="$("$project_sh" status --json 2>"$status_err")" || rc=$?
  if (( rc != 0 )); then
    if (( rc != 3 )); then
      cat "$status_err" >&2
    fi
    rm -f "$status_err"
    return "$rc"
  fi
  rm -f "$status_err"

  python3 - "$field" "$status_json" <<'PY'
import json
import re
import sys

field, data = sys.argv[1], json.loads(sys.argv[2])
if field == "repos":
    # Every active entry's worktree, one per line, for plan reviewers to inspect.
    for entry in data.get("entries") or []:
        print(entry["path"])
    raise SystemExit(0)
if field in ("name", "plan"):
    name = data["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        sys.exit(f"Error: invalid project name from baton status: {name!r}")
    entry = data.get("current_entry")
    if field == "plan":
        # A branch worker owns its subplan; only the parent, from the slot, works on the master.
        if entry is not None and len(data.get("entries") or []) > 1:
            print(entry["subplan_path"])
        else:
            print(data["master_path"])
    else:
        print(name)
    raise SystemExit(0)
entry = data.get("current_entry")
if entry is None:
    raise SystemExit(3)
if not entry.get("base_reliable"):
    reason = entry.get("base_error") or "; ".join(data.get("errors") or []) or "no reason reported"
    sys.exit(f"Error: expected PR base of {entry['branch']} is unreliable: {reason}")
print(entry["base_ref"])
if entry["needs_rebase"] is True:
    raise SystemExit(4)
PY
}

# Creates a private directory for one review run and prints its path. The
# label only helps humans find it; mktemp keeps concurrent runs apart.
review_make_run_dir() {
  local kind="$1"
  local label tmp_root="${TMPDIR:-/tmp}"
  label="$(review_slugify "$2")"
  # Keep the tail of long labels: it holds the branch or the plan's project.
  if (( ${#label} > 100 )); then
    label="${label:${#label}-100}"
  fi
  while [[ "$label" == -* ]]; do
    label="${label#-}"
  done
  tmp_root="${tmp_root%/}"
  # Each run keeps its full diff; drop this kind's runs older than a week so they do not pile up.
  find "$tmp_root" -maxdepth 1 -type d -name "${kind}.*" -user "$(id -u)" -mtime +7 \
    -exec rm -rf {} + 2>/dev/null || true
  mktemp -d "${tmp_root}/${kind}.${label:-run}.XXXXXX"
}

# Reports a run's artifacts: the directory and each non-empty review file on
# stderr, then `REVIEW_ARTIFACTS=<dir>` as the last stdout line for callers
# that parse it.
review_report_artifacts() {
  local run_dir="$1"
  shift
  local path
  echo "Artifacts: ${run_dir}" >&2
  for path in "$@"; do
    if [[ -s "$path" ]]; then
      echo "Review file: ${path}" >&2
    fi
  done
  echo "REVIEW_ARTIFACTS=${run_dir}"
}

# Resolves the installed Baton CLI into review_cli and checks that it can run
# reviews. Like review_project_value, it never falls back to a branch checkout's
# own CLI; BATON_CLI pins one explicitly, e.g. in tests.
review_require_cli() {
  review_cli="${BATON_CLI:-$(command -v baton || true)}"
  if [[ -z "$review_cli" || ! -x "$review_cli" ]]; then
    echo "Error: baton is required to run reviews; install baton or set BATON_CLI." >&2
    return 1
  fi
  if ! "$review_cli" review run --help >/dev/null 2>&1; then
    echo "Error: installed Baton too old; reinstall Baton to get 'baton review run'." >&2
    return 1
  fi
}

review_slugify() {
  printf '%s' "$1" | tr '/[:space:]' '--' | tr -cs '[:alnum:]._-' '-'
}

review_is_non_negative_int() {
  [[ "$1" =~ ^[0-9]+$ ]]
}
