#!/usr/bin/env bash
# Host side of the CD workflow. ci-deploy.sh stages the kit in a per-run
# directory and starts this detached, so a dropped SSH connection cannot
# interrupt a cutover halfway.
#   remote-run.sh deploy <run id> [--maintenance-confirmed]   preflight.sh, then upgrade.sh
#   remote-run.sh rollback <run id>                           rollback.sh
# Output goes to <root>/deploy-logs/<run id>.log; the exit code is written to
# <run id>.exit last, which is what ci-deploy.sh waits for. Exit 75 means
# another deploy or rollback holds the host lock.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="${MP_ROOT:-/opt/mindprobe}"
action="${1:?usage: remote-run.sh deploy|rollback <run id> [--maintenance-confirmed]}"
run_id="${2:?missing run id}"
confirm="${3:-}"
[[ "$run_id" =~ ^[A-Za-z0-9._-]{1,80}$ ]] || { echo "bad run id"; exit 2; }
logs="$root/deploy-logs"
mkdir -p "$logs"
log="$logs/$run_id.log"

(
  set -euo pipefail
  # One deploy or rollback at a time on this host, whoever starts it.
  exec 9>"$root/.deploy.lock"
  flock -n 9 || { echo 'Another deploy or rollback is running on this host. Nothing was changed.'; exit 75; }
  echo "== $(date -u +%FT%TZ) $action, run $run_id, as $(id -un)"
  sudo -n true 2>/dev/null || { echo 'sudo needs a password here; the deploy user needs passwordless sudo. Nothing was changed.'; exit 1; }
  case "$action" in
    deploy)
      export MP_TMP="$here"
      bash "$here/preflight.sh"
      bash "$here/upgrade.sh" "$confirm"
      ;;
    rollback)
      bash "$here/rollback.sh"
      ;;
    *) echo "unknown action: $action"; exit 2 ;;
  esac
) > "$log" 2>&1 < /dev/null
code=$?
echo "== exit $code" >> "$log"
printf '%s\n' "$code" > "$logs/$run_id.exit.tmp"
mv "$logs/$run_id.exit.tmp" "$logs/$run_id.exit"
exit "$code"
