#!/usr/bin/env bash
# Runner side of the backend CD workflows (.github/workflows/backend-*.yml).
# Builds the release (deploy only), stages it with the kit in a per-run host
# directory, starts remote-run.sh detached, follows its log, and exits with
# its exit code. Prints no secret values; never use `set -x` here.
#
# Environment:
#   DEPLOY_ACTION            deploy | rollback
#   DEPLOY_COMMIT            full 40-hex feat/m2 commit (deploy)
#   MAINTENANCE_CONFIRMED    must be "true"
#   DEPLOY_RUN_ID            unique per run, e.g. the Actions run id and attempt
#   DEPLOY_HOST / DEPLOY_USER
#   DEPLOY_SSH_KEY_FILE / DEPLOY_KNOWN_HOSTS_FILE   pinned host key; no TOFU
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
  deploy)
    [[ "${DEPLOY_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || { echo 'commit must be the full 40-character SHA.'; exit 2; } ;;
  rollback) ;;
  *) echo "DEPLOY_ACTION must be deploy or rollback, not '$action'."; exit 2 ;;
esac
if [ "${MAINTENANCE_CONFIRMED:-}" != true ]; then
  echo 'Not confirmed: tick "maintenance window confirmed" (no new sessions, uploads finished). Nothing was changed.'
  exit 2
fi

ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=${DEPLOY_KNOWN_HOSTS_FILE:?}" -o ConnectTimeout=15
  -o ServerAliveInterval=15 -i "${DEPLOY_SSH_KEY_FILE:?}")
target="${DEPLOY_USER:?}@${DEPLOY_HOST:?}"
remote() { ssh "${ssh_opts[@]}" "$target" "$@"; }

stage="$out/stage"
rm -rf "$stage"; mkdir -p "$stage"
if [ "$action" = deploy ]; then
  if [ -n "${DEPLOY_RELEASE_DIR:-}" ]; then
    cp "$DEPLOY_RELEASE_DIR"/mindprobe-release.{tar,env,sha256} "$stage/"
  else
    bash "$here/make-release.sh" "$DEPLOY_COMMIT" --out "$stage"
  fi
fi
cp "$here"/{release-info,preflight,activity-check,upgrade,rollback,remote-run}.sh "$stage/"

remote_dir="$remote_base/mindprobe-deploy-$run_id"
echo "== Staging on $DEPLOY_HOST:$remote_dir"
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
[[ "$code" =~ ^[0-9]+$ ]] || code=1
exit "$code"
