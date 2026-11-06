#!/usr/bin/env bash
# Set the OP admin account, restart the server, and prove POST /op/login works.
# Run after upgrade.sh, inside the same maintenance window:
#   bash /tmp/set-admin.sh
# Prompts for the username (shown as typed) and the password (hidden, typed
# twice). The password goes to the running server's own hasher on stdin;
# neither it nor the hash is printed, put in argv, or written anywhere but the
# MINDPROBE_ADMIN_PASSWORD_HASH line. Server responses are never echoed: a
# validation error from /op/login can quote the request, password included.
# Rerun to change the password. The static operator token is untouched.
set -euo pipefail
# MP_ROOT / MP_API / MP_BACKUP_DIR exist only for the local dry run; leave them unset on the host.
root="${MP_ROOT:-/opt/mindprobe}"
api="${MP_API:-https://95-41-69-24.sslip.io}"
backup_dir="${MP_BACKUP_DIR:-$HOME}"
env_file="$root/runtime.env"
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)
# One mutating operation at a time on this host (it rewrites runtime.env and
# restarts the server). Same lock as remote-run.sh, upgrade.sh and rollback.sh.
if [ "${MP_LOCK_HELD:-}" != 1 ] || ! [ /proc/self/fd/9 -ef "$root/.deploy.lock" ]; then
  exec 9>"$root/.deploy.lock"
  flock -n 9 || { echo 'Another deploy, rollback or admin change is running on this host. Nothing was changed.'; exit 75; }
fi
cd "$(readlink -f "$root/current")"

"${compose[@]}" exec -T server python -c 'import app.admin_password' </dev/null \
  || { echo 'The running server has no app.admin_password. Run upgrade.sh first.'; exit 1; }
test -w "$env_file" || { echo "$env_file is not writable by $(id -un)."; exit 1; }

read -r -p 'Admin username: ' username
[[ "$username" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo 'Username: letters, digits, dot, underscore, hyphen; at most 64.'; exit 1; }
read -r -s -p 'Admin password (12-1024 characters): ' password; echo
# Same bounds as POST /op/login, counted in Unicode characters like the API.
# Checked before anything is hashed, backed up, written or restarted.
MP_P="$password" python3 -c 'import os, sys; sys.exit(0 if 12 <= len(os.environ["MP_P"]) <= 1024 else 1)' \
  || { echo 'The password must be 12 to 1024 characters. Nothing was changed.'; exit 1; }
read -r -s -p 'Again: ' again; echo
[ "$password" = "$again" ] || { echo 'The two entries differ. Nothing was changed.'; exit 1; }
unset again

hash="$(printf '%s' "$password" | "${compose[@]}" exec -T server python -c \
  'import sys; from app.admin_password import hash_password; print(hash_password(sys.stdin.read()))')"
[[ "$hash" =~ ^scrypt:[0-9]+:[0-9]+:[0-9]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$ ]] \
  || { echo 'The hasher printed something unexpected; runtime.env is unchanged.'; exit 1; }

backup="$backup_dir/runtime.env.bak-$(date -u +%Y%m%dT%H%M%SZ)"
(umask 077 && cp "$env_file" "$backup")
echo "Previous runtime.env saved as $backup (mode 0600)."
# In place, so the file keeps its owner and mode; only the two admin lines change.
MP_ADMIN_USERNAME="$username" MP_ADMIN_HASH="$hash" python3 - "$env_file" <<'PY'
import os, sys
path = sys.argv[1]
new = {'MINDPROBE_ADMIN_USERNAME': os.environ['MP_ADMIN_USERNAME'],
       'MINDPROBE_ADMIN_PASSWORD_HASH': os.environ['MP_ADMIN_HASH']}
with open(path) as handle:
    lines = [line for line in handle.read().splitlines() if line.split('=', 1)[0].strip() not in new]
lines += [f'{key}={value}' for key, value in new.items()]
with open(path, 'w') as handle:
    handle.write('\n'.join(lines) + '\n')
print('runtime.env: admin username and password hash written (values withheld).')
PY
unset hash

echo 'Restarting the server with the new environment.'
"${compose[@]}" up -d --no-build
for attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 5 "$api/health" >/dev/null; then
    echo 'Public HTTPS health passed.'
    break
  fi
  [ "$attempt" = 30 ] && { echo "Health did not pass. Restore with: cp $backup $env_file, then rerun 'up -d' (README.md)."; exit 1; }
  sleep 2
done

body="$(MP_U="$username" MP_P="$password" python3 -c 'import json, os; print(json.dumps({"username": os.environ["MP_U"], "password": os.environ["MP_P"]}))')"
unset password
response="$(printf '%s' "$body" | curl --silent --max-time 10 -H 'Content-Type: application/json' \
  --data-binary @- -w '\n%{http_code}' "$api/op/login")"
unset body
status="${response##*$'\n'}"
if [ "$status" != 200 ]; then
  unset response
  # Fixed messages only. The response body is never printed.
  case "$status" in
    401) why='username or password not accepted' ;;
    422) why='request rejected as invalid' ;;
    429) why='too many attempts; wait a minute' ;;
    000) why='no response' ;;
    *) why='unexpected answer' ;;
  esac
  echo "POST /op/login answered HTTP $status ($why)."
  echo "The static operator token still works. Rerun set-admin.sh, or restore $backup (README.md)."
  exit 1
fi
token="$(printf '%s' "${response%$'\n'*}" | python3 -c 'import json, sys; body = json.load(sys.stdin); assert body["token_type"] == "operator"; print(body["access_token"])' 2>/dev/null)" \
  || { unset response; echo "POST /op/login answered 200 without an operator token. Restore $backup if unsure (README.md)."; exit 1; }
unset response
# Do not leave a working login behind: revoke the one just made.
printf 'Authorization: Bearer %s\n' "$token" | curl --fail --silent --max-time 10 -H @- -X POST "$api/op/logout" >/dev/null
unset token
echo "OP admin login works for '$username' (test login revoked). The OP login form (#21) can now be merged."
