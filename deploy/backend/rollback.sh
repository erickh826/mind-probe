#!/usr/bin/env bash
# Put the previous backend back on its own image. The database stays: the new
# release only adds audit rows (operator_login, and operator token revocations
# under the pseudo session __operator__), which 9579475 ignores (checked with
# verify/rollback_probe.py). Restore the pre-upgrade backup only if the
# database is damaged. On the old code login tokens stop working; the static
# operator token keeps working, so the current OP page is unaffected.
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
