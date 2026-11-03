#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
skill_args=()
install_cron=0
cron_schedule="0 10 * * *"
custom_cron_schedule=0
cron_since="3d"
install_weekly_cron=0
weekly_cron_schedule="0 11 * * 1"
custom_weekly_schedule=0
weekly_provider=""
while (( $# )); do
  case "$1" in
    --skills-target)
      [[ $# -ge 2 ]] || { echo "--skills-target requires a directory" >&2; exit 2; }
      skill_args+=(--target "$2"); shift 2 ;;
    --install-cron)
      install_cron=1; shift ;;
    --install-weekly-cron)
      install_weekly_cron=1; shift ;;
    --weekly-cron-schedule)
      [[ $# -ge 2 ]] || { echo "--weekly-cron-schedule requires a quoted five-field schedule" >&2; exit 2; }
      weekly_cron_schedule="$2"; custom_weekly_schedule=1; shift 2 ;;
    --weekly-provider)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { echo "--weekly-provider requires a provider name" >&2; exit 2; }
      weekly_provider="$2"; shift 2 ;;
    --cron-schedule)
      [[ $# -ge 2 ]] || { echo "--cron-schedule requires a quoted five-field schedule" >&2; exit 2; }
      cron_schedule="$2"; custom_cron_schedule=1; shift 2 ;;
    --cron-since)
      [[ $# -ge 2 ]] || { echo "--cron-since requires a duration, such as 3d" >&2; exit 2; }
      cron_since="$2"; custom_cron_schedule=1; shift 2 ;;
    -h|--help)
      echo "Usage: ./install.sh [--skills-target DIR ...] [--install-cron [--cron-schedule '0 10 * * *'] [--cron-since 3d]]"
      echo "       [--install-weekly-cron --weekly-provider NAME [--weekly-cron-schedule '0 11 * * 1']]"
      echo "Install/update the Baton CLI and baton-* skills; cron flags opt in to independent daily/weekly jobs."
      echo "Skills go to ~/.agents/skills, ~/.claude/skills, and the skill directory of every other detected"
      echo "provider (see baton config show); --skills-target replaces those defaults."
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if (( custom_cron_schedule && ! install_cron )); then
  echo "--cron-schedule and --cron-since require --install-cron" >&2; exit 2
fi
if (( custom_weekly_schedule || ${#weekly_provider} )); then
  if (( ! install_weekly_cron )); then
    echo "--weekly-cron-schedule and --weekly-provider require --install-weekly-cron" >&2; exit 2
  fi
fi
if (( install_weekly_cron )) && [[ -z "$weekly_provider" ]]; then
  echo "--install-weekly-cron requires --weekly-provider" >&2; exit 2
fi
if [[ "$(git -C "$root" symbolic-ref --quiet HEAD 2>/dev/null || true)" != refs/heads/main ]]; then
  echo "Refusing to install Baton: $root must be checked out on main." >&2
  exit 1
fi
checkout_status="$(git -C "$root" status --porcelain --untracked-files=all)"
if [[ -n "$checkout_status" ]]; then
  echo "Refusing to install Baton: $root must be clean; commit or preserve local changes first." >&2
  exit 1
fi
command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }
uv tool install --reinstall "$root"
tool_root="$(uv tool dir)"
python="$tool_root/baton/bin/python"
"$python" -I -m baton.configuration init --preserve-review-loop
"$python" -I -m baton.skills_install install ${skill_args[@]+"${skill_args[@]}"}
if (( install_weekly_cron )); then
  "$python" -I -m baton.retro_cron install-weekly --schedule "$weekly_cron_schedule" --provider "$weekly_provider" --check
fi
if (( install_cron )); then
  "$python" -I -m baton.retro_cron install --schedule "$cron_schedule" --since "$cron_since"
fi
if (( install_weekly_cron )); then
  "$python" -I -m baton.retro_cron install-weekly --schedule "$weekly_cron_schedule" --provider "$weekly_provider"
fi
printf '\nInstalled baton. Add %s to PATH if needed (uv tool update-shell).\n' "$(uv tool dir --bin)"
