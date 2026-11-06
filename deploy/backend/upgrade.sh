#!/usr/bin/env bash
# Replace the Lightsail backend with the staged release (mindprobe-release.*
# in MP_TMP, default /tmp). Run preflight.sh first; the CD workflow runs both
# through remote-run.sh.
#   bash upgrade.sh --maintenance-confirmed
# The flag states that operators will not create or start sessions and that
# students have finished uploading until this script and the smoke checks end.
#
# Keeps runtime.env and the data volume; tags the running image so
# rollback.sh can start it again without a rebuild. Safe to rerun after a
# failure: an existing release directory is reused only if it matches the tar,
# and the rollback tag is never moved to a different image. Prints no secret values.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# MP_TMP: staging directory (see release-info.sh). MP_ROOT / MP_API exist only
# for the local dry run; leave them unset on the host.
api="${MP_API:-https://95-41-69-24.sslip.io}"
root="${MP_ROOT:-/opt/mindprobe}"
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)

if [ "${1:-}" != "--maintenance-confirmed" ]; then
  echo 'Confirm the maintenance window first (no new sessions, uploads finished), then run:'
  echo '  bash upgrade.sh --maintenance-confirmed'
  exit 2
fi

# One mutating operation at a time on this host. remote-run.sh already holds
# the lock on fd 9 and says so; any other entry point takes it here.
if [ "${MP_LOCK_HELD:-}" != 1 ] || ! [ /proc/self/fd/9 -ef "$root/.deploy.lock" ]; then
  exec 9>"$root/.deploy.lock"
  flock -n 9 || { echo 'Another deploy, rollback or admin change is running on this host. Nothing was changed.'; exit 75; }
fi

phase='start'
trap 'echo "FAILED during: $phase. See README.md, \"失敗後重跑\"." >&2' ERR
step() { phase="$1"; printf '\n== %s\n' "$1"; }

step '1/7 Check the release files and the running release'
source "$here/release-info.sh"
echo "New release: $short ($commit)"
previous="$(readlink -f "$root/current")"
test "$previous" != "$release" || { echo "current already points at $short; the upgrade is done."; exit 0; }
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
# Baked into the image (server/ is the build context), so the running container
# can say which release it was built from. Rewritten on every run, also on reuse.
printf '%s\n' "$commit" > "$release/server/RELEASE_COMMIT"

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

step '7/7 Check that the public API is the new release, then switch current'
server_id="$("${compose[@]}" ps -q server)"
serving_dir="$(readlink -f "$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$server_id")")"
[ "$serving_dir" = "$(readlink -f "$release")" ] \
  || { echo "The server container came from $serving_dir, not $release. Run rollback.sh." >&2; exit 1; }
# 1) The container runs an image built from this release's files.
image_commit="$("${compose[@]}" exec -T server cat /app/RELEASE_COMMIT </dev/null 2>/dev/null | tr -d '\r\n' || true)"
if [ "$image_commit" != "$commit" ]; then
  echo "The running image was built from '${image_commit:-unknown}', not $commit. Run rollback.sh." >&2
  exit 1
fi
# 2) A public request reaches that container: a unique probe sent to the public
# HTTPS URL must show up in the new container's own access log. A proxy still
# serving the old release never shows it, whatever routes the two versions share.
probe="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
curl --fail --silent --max-time 10 "$api/health?mp_release_probe=$probe" > /dev/null \
  || { echo 'The public release probe request failed. Run rollback.sh.' >&2; exit 1; }
reached=no
for attempt in 1 2 3 4 5; do
  server_log="$("${compose[@]}" logs --no-color --tail 200 server </dev/null 2>/dev/null || true)"
  if grep -q -- "mp_release_probe=$probe" <<<"$server_log"; then
    reached=yes; break
  fi
  sleep 1
done
if [ "$reached" != yes ]; then
  echo "The public HTTPS endpoint did not reach the new container ($short). It may still serve the previous release. Run rollback.sh." >&2
  exit 1
fi
# 3) Supplemental: same routes publicly as the new container reports for itself.
own="$("${compose[@]}" exec -T server python -c \
  'import json; from app.main import app; print(json.dumps(sorted(app.openapi()["paths"])))' </dev/null)"
public="$(curl --fail --silent --max-time 10 "$api/openapi.json" \
  | python3 -c 'import json, sys; print(json.dumps(sorted(json.load(sys.stdin)["paths"])))')"
if [ "$own" != "$public" ]; then
  echo 'The public /openapi.json differs from the routes of the new container. Run rollback.sh.' >&2
  exit 1
fi
echo "Release $short confirmed: image built from $commit, the public endpoint reached the new container, $(python3 -c 'import json, sys; print(len(json.loads(sys.argv[1])))' "$own") routes match."
"${compose[@]}" exec -T server python -c \
  'import os; print("OP admin login:", "configured" if os.environ.get("MINDPROBE_ADMIN_USERNAME") and os.environ.get("MINDPROBE_ADMIN_PASSWORD_HASH") else "not configured (set-admin.sh)")' </dev/null
ln -sfn "$release" "$root/current"
"${compose[@]}" ps
trap - ERR
echo "Upgrade to $short done. Run the smoke checks in deploy/backend/README.md."
