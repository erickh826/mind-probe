#!/usr/bin/env bash
# Replace the Lightsail backend with the release in /tmp/mindprobe-release.*
# (PR #18 + #20). Run preflight.sh first.
#   bash /tmp/upgrade.sh --maintenance-confirmed
# The flag states that operators will not create or start sessions and that
# students have finished uploading until this script, set-admin.sh and the
# smoke checks end.
#
# Keeps runtime.env and the data volume; tags the running image so
# rollback.sh can start it again without a rebuild. Safe to rerun after a
# failure: an existing release directory is reused only if it matches the tar,
# and the rollback tag is never moved to a different image. Prints no secret values.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# MP_ROOT / MP_TMP / MP_API exist only for the local dry run; leave them unset on the host.
api="${MP_API:-https://95-41-69-24.sslip.io}"
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)

if [ "${1:-}" != "--maintenance-confirmed" ]; then
  echo 'Confirm the maintenance window first (no new sessions, uploads finished), then run:'
  echo '  bash upgrade.sh --maintenance-confirmed'
  exit 2
fi

phase='start'
trap 'echo "FAILED during: $phase. See README.md, \"失敗後重跑\"." >&2' ERR
step() { phase="$1"; printf '\n== %s\n' "$1"; }

step '1/7 Check the release files and the running release'
source "$here/release-info.sh"
echo "New release: $short ($commit)"
previous="$(readlink -f "$root/current")"
test "$previous" != "$release" || { echo "current already points at $short; the upgrade is done. Next: set-admin.sh."; exit 0; }
cd "$previous"
server_id="$("${compose[@]}" ps -q server)"
test -n "$server_id" || { echo 'No running server container.'; exit 1; }
running_dir="$(readlink -f "$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$server_id")")"
if [ "$running_dir" != "$previous" ]; then
  echo "The running stack came from $running_dir, not $previous (current)."
  echo "If an earlier run already started $short, run rollback.sh first."
  exit 1
fi
printf '%s\n' "$previous" > "$root/rollback-target"
printf '%s\n' "$rollback_tag" > "$root/rollback-image"
echo "Previous release: $previous (recorded in $root/rollback-target)"

step '2/7 Keep the running image for rollback'
running_image="$(sudo docker inspect --format '{{.Image}}' "$server_id")"
if tagged="$(sudo docker image inspect --format '{{.Id}}' "$rollback_tag" 2>/dev/null)"; then
  if [ "$tagged" != "$running_image" ]; then
    echo "$rollback_tag already names a different image than the running one. Not moving it."
    echo 'Check which image is the pre-upgrade one before going on (README.md, "失敗後重跑").'
    exit 1
  fi
  echo "$rollback_tag already names the running image."
else
  sudo docker tag "$running_image" "$rollback_tag"
  echo "Tagged the running image as $rollback_tag"
fi

step '3/7 Unpack the release'
if [ -e "$release" ] && [ -n "$(ls -A "$release")" ]; then
  # Left by an earlier run. Reuse it only if it is exactly this tar.
  check="$(mktemp -d)"
  tar -xf "$tarball" -C "$check"
  if diff -r --exclude=.env --exclude=RELEASE_COMMIT "$check" "$release" >/dev/null \
      && [ "$(cat "$release/RELEASE_COMMIT" 2>/dev/null)" = "$commit" ]; then
    echo "Reusing $release from an earlier run; it matches the tar."
    rm -rf "$check"
  else
    rm -rf "$check"
    echo "$release exists but does not match the tar. Move it aside (README.md, \"失敗後重跑\"); do not delete it."
    exit 1
  fi
else
  mkdir -p "$release"
  tar -xf "$tarball" -C "$release"
  printf '%s\n' "$commit" > "$release/RELEASE_COMMIT"
fi
ln -sfn "$root/runtime.env" "$release/.env"

step '4/7 Build the new image (the old container keeps serving)'
(cd "$release" && "${compose[@]}" build server)

step '5/7 Recheck activity, then back up the database'
# The build takes minutes and the old site kept serving; check again now.
bash "$here/activity-check.sh"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$previous"
"${compose[@]}" exec -T server python - "$short" "$stamp" <<'PY'
import sqlite3, sys
from pathlib import Path
Path('/data/backups').mkdir(exist_ok=True)
target = f'/data/backups/pre-{sys.argv[1]}-{sys.argv[2]}.db'
src = sqlite3.connect('/data/mindprobe.db')
dst = sqlite3.connect(target)
src.backup(dst)
assert dst.execute('pragma integrity_check').fetchone()[0] == 'ok'
print('Database backup:', target, 'integrity ok')
PY

step "6/7 Start $short"
# From here on a failure leaves the new containers running: run rollback.sh.
cd "$release"
"${compose[@]}" config --quiet
"${compose[@]}" up -d --no-build
healthy=no
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 "$api/health"; then
    printf '\nPublic HTTPS health passed.\n'
    healthy=yes
    break
  fi
  printf 'Waiting for health (%s/30).\n' "$attempt"
  sleep 2
done
if [ "$healthy" != yes ]; then
  echo 'Health did not pass. Run rollback.sh.' >&2
  exit 1
fi

step '7/7 Check routes and the new code, then switch current'
curl --fail --silent --max-time 10 "$api/openapi.json" | python3 -c '
import json, sys
paths = json.load(sys.stdin)["paths"]
need = ["/op/login", "/op/logout", "/sessions/{session_id}/review-codes",
        "/sessions/{session_id}/join-codes", "/recording/{session_id}/claim", "/sessions"]
missing = [p for p in need if p not in paths]
assert not missing, f"missing routes: {missing}"
print("Routes present:", ", ".join(need))'
"${compose[@]}" exec -T server python - <<'PY'
import os
from app import auth
assert hasattr(auth, 'review_issue_limiter'), 'review-code limit (#18) missing from the running image'
configured = bool(os.environ.get('MINDPROBE_ADMIN_USERNAME')) and bool(os.environ.get('MINDPROBE_ADMIN_PASSWORD_HASH'))
print('Review-code limit present. OP admin login:', 'configured' if configured else 'not configured yet (run set-admin.sh)')
PY
ln -sfn "$release" "$root/current"
"${compose[@]}" ps
trap - ERR
echo "Upgrade to $short done. Next: bash /tmp/set-admin.sh, then the smoke checks in README.md."
