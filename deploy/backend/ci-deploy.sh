#!/usr/bin/env bash
# Runner side of the backend CD workflows (.github/workflows/backend-*.yml).
# Builds the release (preflight, deploy), stages it with the kit in a per-run
# host directory, starts remote-run.sh detached, follows its log, and exits
# with its exit code. Prints no secret values; never use `set -x` here.
#
# Environment:
#   DEPLOY_ACTION            preflight | deploy | rollback
#                            preflight: read-only checks for the commit; changes nothing
#   DEPLOY_COMMIT            full 40-hex feat/m2 commit (preflight, deploy)
#   MAINTENANCE_CONFIRMED    must be "true" for deploy and rollback
#   DEPLOY_RUN_ID            unique per run, e.g. the Actions run id and attempt
#   DEPLOY_HOST / DEPLOY_USER
#   DEPLOY_SSH_KEY_FILE / DEPLOY_KNOWN_HOSTS_FILE   pinned host key; no TOFU.
#                            The file must have an entry for DEPLOY_HOST as written.
#   DEPLOY_REQUIRE_TAILNET   1: DEPLOY_HOST must be a Tailscale IPv4 (100.64.0.0/10),
#                            so CD never goes over the public SSH port. Workflows set it.
#   DEPLOY_CHECK_ONLY        1: validate the inputs above, then exit 0 before
#                            building, connecting or staging anything
#   DEPLOY_OUT               local directory for the release and the log (artifact)
# Dry run / tuning only: DEPLOY_REMOTE_BASE (/tmp), DEPLOY_REMOTE_ROOT
# (/opt/mindprobe), DEPLOY_TIMEOUT_S (2400), DEPLOY_POLL_S (10), and
# DEPLOY_RELEASE_DIR (use prebuilt release files instead of make-release.sh;
# the dry run sets it, the workflows never do).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
action="${DEPLOY_ACTION:?}"
run_id="${DEPLOY_RUN_ID:?}"
out="${DEPLOY_OUT:?}"
remote_base="${DEPLOY_REMOTE_BASE:-/tmp}"
remote_root="${DEPLOY_REMOTE_ROOT:-/opt/mindprobe}"
timeout_s="${DEPLOY_TIMEOUT_S:-2400}"
poll_s="${DEPLOY_POLL_S:-10}"

[[ "$run_id" =~ ^[A-Za-z0-9._-]{1,80}$ ]] || { echo 'DEPLOY_RUN_ID: letters, digits, . _ - only.'; exit 2; }
case "$action" in
  preflight|deploy)
    [[ "${DEPLOY_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || { echo 'commit must be the full 40-character SHA.'; exit 2; } ;;
  rollback) ;;
  *) echo "DEPLOY_ACTION must be preflight, deploy or rollback, not '$action'."; exit 2 ;;
esac
# preflight only reads; deploy and rollback restart the backend.
if [ "$action" != preflight ] && [ "${MAINTENANCE_CONFIRMED:-}" != true ]; then
  echo 'Not confirmed: tick "maintenance window confirmed" (no new sessions, uploads finished). Nothing was changed.'
  exit 2
fi

host="${DEPLOY_HOST:?}"
if [ "${DEPLOY_REQUIRE_TAILNET:-}" = 1 ]; then
  # Tailscale assigns IPv4 addresses from 100.64.0.0/10 (100.64.x.x to 100.127.x.x).
  if ! [[ "$host" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.([0-9]{1,3})\.([0-9]{1,3})$ ]] \
     || [ "${BASH_REMATCH[2]}" -gt 255 ] || [ "${BASH_REMATCH[3]}" -gt 255 ]; then
    echo 'DEPLOY_HOST must be the host'"'"'s Tailscale IPv4 address (100.64.0.0/10), not a public address or name. Nothing was changed.'
    exit 2
  fi
fi
known="${DEPLOY_KNOWN_HOSTS_FILE:?}"
if ! ssh-keygen -F "$host" -f "$known" > /dev/null 2>&1; then
  echo 'DEPLOY_KNOWN_HOSTS has no host key for DEPLOY_HOST as written (it must start with the same address). Nothing was changed.'
  exit 2
fi
if [ "${DEPLOY_CHECK_ONLY:-}" = 1 ]; then
  echo "Inputs ok: $action$( [ "$action" = rollback ] || printf ' %s' "$DEPLOY_COMMIT" )."
  exit 0
fi

ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$known" -o ConnectTimeout=15
  -o ServerAliveInterval=15 -i "${DEPLOY_SSH_KEY_FILE:?}")
target="${DEPLOY_USER:?}@$host"
remote() { ssh "${ssh_opts[@]}" "$target" "$@"; }

stage="$out/stage"
rm -rf "$stage"; mkdir -p "$stage"
if [ "$action" != rollback ]; then
  if [ -n "${DEPLOY_RELEASE_DIR:-}" ]; then
    cp "$DEPLOY_RELEASE_DIR"/mindprobe-release.{tar,env,sha256} "$stage/"
  else
    bash "$here/make-release.sh" "$DEPLOY_COMMIT" --out "$stage"
  fi
fi
cp "$here"/{release-info,preflight,activity-check,upgrade,rollback,remote-run,set-admin}.sh "$stage/"

remote_dir="$remote_base/mindprobe-deploy-$run_id"
echo "== Staging on $host:$remote_dir"
remote "mkdir -p '$remote_dir' && chmod 700 '$remote_dir'"
scp "${ssh_opts[@]}" -q "$stage"/* "$target:$remote_dir/"

flag=''
[ "$action" = deploy ] && flag='--maintenance-confirmed'
echo "== Starting $action on the host (detached; run $run_id)"
remote "setsid nohup bash '$remote_dir/remote-run.sh' $action '$run_id' $flag > /dev/null 2>&1 < /dev/null &"

log_remote="$remote_root/deploy-logs/$run_id.log"
log_local="$out/deploy-$run_id.log"
: > "$log_local"
shown=0
failures=0
deadline=$(( $(date +%s) + timeout_s ))
code=''
while :; do
  if remote "cat '$log_remote' 2>/dev/null || true" > "$log_local.part"; then
    failures=0
    mv "$log_local.part" "$log_local"
    size="$(wc -c < "$log_local")"
    if [ "$size" -gt "$shown" ]; then
      tail -c +"$((shown + 1))" "$log_local"
      shown="$size"
    fi
    code="$(remote "cat '$remote_root/deploy-logs/$run_id.exit' 2>/dev/null || true" || true)"
    [ -n "$code" ] && break
  else
    failures=$((failures + 1))
    echo "(ssh poll failed $failures time(s); the run continues on the host)"
    [ "$failures" -ge 30 ] && { echo "Lost the host. Check $log_remote there."; exit 1; }
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "No result after ${timeout_s}s. The run may still be going: check $log_remote on the host."
    exit 124
  fi
  sleep "$poll_s"
done
# The exit file is written after the log's last line; show whatever came in between.
if remote "cat '$log_remote'" > "$log_local.part"; then
  mv "$log_local.part" "$log_local"
  tail -c +"$((shown + 1))" "$log_local"
fi
echo "== Host finished $action with exit $code. Log: $log_remote (copy in the workflow artifact)."
# Staged from this reviewed kit, never run by CD. The target release (e.g. one
# older than deploy/backend) does not have to contain it.
if [ "$action" = preflight ]; then
  echo "== Preflight only: no image, release, backup, container or current changed. Exit 0 means a deploy of this commit could start now."
fi
if [ "$action" = deploy ] && [ "$code" = 0 ]; then
  echo "== set-admin.sh is staged at $remote_dir/set-admin.sh on the host. To set or change the OP admin login, run it there by hand: bash '$remote_dir/set-admin.sh'"
fi
[[ "$code" =~ ^[0-9]+$ ]] || code=1
exit "$code"
