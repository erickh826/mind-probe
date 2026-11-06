#!/usr/bin/env bash
# Read-only checks on the Lightsail host before replacing the backend.
# Exit 0: safe to upgrade now. Exit 1: something is in progress or misconfigured.
# Prints no secret values.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/release-info.sh"
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)
current="$(readlink -f "$root/current")"
printf 'Current release: %s (%s)\n' "$current" "$(cat "$current/RELEASE_COMMIT" 2>/dev/null || echo 'no RELEASE_COMMIT')"
printf 'New release:     %s (%s)\n' "$release" "$commit"
if [ "$current" = "$release" ]; then
  echo 'current already points at the new release; nothing to upgrade. Run set-admin.sh if login is not set up.'
  exit 1
fi
cd "$current"

# rollback.sh restarts from the directory the running stack came from;
# `current` must be that directory.
server_id="$("${compose[@]}" ps -q server)"
test -n "$server_id" || { echo 'No running server container'; exit 1; }
running_dir="$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$server_id")"
running_dir="$(readlink -f "$running_dir")"
if [ "$running_dir" != "$current" ]; then
  printf 'Running stack was started from %s, but current points at %s. Fix the symlink before upgrading.\n' "$running_dir" "$current"
  exit 1
fi
echo "Running stack directory matches current."

python3 - "$root/runtime.env" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))
problems = []
if path.stat().st_mode & 0o777 != 0o600:
    problems.append('runtime.env is not mode 0600')
origins = set(values.get('MINDPROBE_CORS_ORIGINS', '').split(','))
expected = {f'https://mind-probe-{app}.vercel.app' for app in ('student', 'wizard', 'teacher', 'op')}
if origins != expected:
    problems.append(f'CORS origins differ from the four Vercel apps: missing {sorted(expected - origins)}, extra {sorted(origins - expected)}')
for key in ('MINDPROBE_WS_TOKEN_SECRET', 'MINDPROBE_OPERATOR_TOKEN', 'MINDPROBE_S3_ACCESS_KEY_ID', 'MINDPROBE_S3_SECRET_ACCESS_KEY'):
    if not values.get(key):
        problems.append(f'{key} is empty')
if problems:
    print('Runtime configuration: FAIL'); [print(' -', p) for p in problems]; sys.exit(1)
admin = 'set' if values.get('MINDPROBE_ADMIN_USERNAME') and values.get('MINDPROBE_ADMIN_PASSWORD_HASH') else 'not set yet (set-admin.sh after the upgrade)'
print(f'Runtime configuration: ok (values withheld). OP admin login: {admin}')
PY
test -w "$root/runtime.env" || { echo "runtime.env is not writable by $(id -un); set-admin.sh will need that."; exit 1; }

df -h / | tail -1
# Docker images and the data volume share the root filesystem.
avail_kb="$(df --output=avail -k / | tail -1 | tr -d ' ')"
if [ "$avail_kb" -lt $((5 * 1024 * 1024)) ]; then
  echo 'Less than 5 GB free on /; free space before building a new image.'
  exit 1
fi
echo "Release files verified (sha256)."

bash "$here/activity-check.sh"
echo 'Preflight passed.'
