#!/usr/bin/env bash
# Put the previous backend back on its own image: the directory in
# <root>/rollback-target and the image in <root>/rollback-image, both written
# by upgrade.sh. The database stays. Before relying on that for a release,
# check the older code reads what the newer one wrote (for 444edcc:
# tasks/02_implement/deploy-pr18-pr20/verify/rollback_probe.py, 17/17).
# Restore the pre-upgrade backup only if the database is damaged.
set -euo pipefail
# MP_ROOT / MP_API exist only for the local dry run; leave them unset on the host.
root="${MP_ROOT:-/opt/mindprobe}"
api="${MP_API:-https://95-41-69-24.sslip.io}"
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)
target="$(cat "$root/rollback-target")"
image="$(cat "$root/rollback-image")"
test -d "$target"
echo "Rolling back to $target ($(cat "$target/RELEASE_COMMIT" 2>/dev/null || echo 'no RELEASE_COMMIT')) on $image"
sudo docker image inspect "$image" >/dev/null
sudo docker tag "$image" mind-probe-server:latest
cd "$target"
"${compose[@]}" up -d --no-build --force-recreate server caddy
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 "$api/health"; then
    printf '\nPublic HTTPS health passed.\n'
    ln -sfn "$target" "$root/current"
    "${compose[@]}" ps
    exit 0
  fi
  printf 'Waiting for health (%s/30).\n' "$attempt"
  sleep 2
done
echo 'Rollback health did not pass; inspect: sudo docker compose -p mindprobe-prod logs --tail 200' >&2
exit 1
