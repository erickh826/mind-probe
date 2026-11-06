#!/usr/bin/env bash
# Dry run of the backend deploy kit, including the CD path (ci-deploy.sh ->
# fake ssh/scp -> remote-run.sh), with fake sudo, docker, curl, ssh and scp.
# Real bash, tar, sha256sum, flock, setsid and python3. Linux or WSL:
#   bash deploy/backend/dryrun/run.sh [release dir]
# Without a release dir it builds one from HEAD (make-release.sh --unmerged).
set -uo pipefail
kit="$(cd "$(dirname "$0")/.." && pwd)"
work="${DRYRUN_WORK:-$(mktemp -d /tmp/mp-dryrun.XXXXXX)}"
rm -rf "$work"; mkdir -p "$work/bin" "$work/tmp"
if [ -n "${1:-}" ]; then
  rel="$1"
else
  rel="$work/release"
  bash "$kit/make-release.sh" HEAD --unmerged --out "$rel" > /dev/null || { echo 'make-release.sh failed'; exit 1; }
fi
cp "$kit"/dryrun/fakes/{docker,curl,ssh,scp,sudo} "$work/bin/"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/bin/sleep"
chmod +x "$work/bin/"*
cp "$kit"/{preflight,upgrade,rollback,set-admin,activity-check,release-info,remote-run,ci-deploy,make-release}.sh "$work/"
export PATH="$work/bin:$PATH" MP_TMP="$work/tmp" MP_API=https://fake.invalid
# Routes the fake old and new servers report (sorted, as upgrade.sh compares them).
export OLD_PATHS='["/health", "/sessions"]'
export NEW_PATHS='["/health", "/op/login", "/sessions"]'
cp "$rel"/mindprobe-release.{tar,env,sha256} "$MP_TMP/"
commit="$(sed -n 's/^RELEASE_COMMIT=//p' "$MP_TMP/mindprobe-release.env")"
NEW="${commit:0:7}"
PW='a-long-test-password-42'

fresh() {
  export MP_ROOT="$work/opt-$1" STATE="$work/state-$1" MP_BACKUP_DIR="$work/home-$1"
  rm -rf "$MP_ROOT" "$STATE" "$MP_BACKUP_DIR"
  mkdir -p "$MP_ROOT/releases/9579475/server" "$STATE/images" "$MP_BACKUP_DIR"
  touch "$MP_ROOT/releases/9579475/docker-compose.prod.yml"
  printf 'MINDPROBE_WS_TOKEN_SECRET=s\nMINDPROBE_OPERATOR_TOKEN=o\n# comment stays\nMINDPROBE_CORS_ORIGINS=%s\nMINDPROBE_S3_ACCESS_KEY_ID=k\nMINDPROBE_S3_SECRET_ACCESS_KEY=x\n' \
    "https://mind-probe-student.vercel.app,https://mind-probe-wizard.vercel.app,https://mind-probe-teacher.vercel.app,https://mind-probe-op.vercel.app" \
    > "$MP_ROOT/runtime.env"
  chmod 600 "$MP_ROOT/runtime.env"
  echo 9579475868dfefd460031fa45bf1d1ac1348ba45 > "$MP_ROOT/releases/9579475/RELEASE_COMMIT"
  ln -sfn "$MP_ROOT/releases/9579475" "$MP_ROOT/current"
  echo cid-1 > "$STATE/container"
  echo img-old > "$STATE/running_image"
  echo img-old > "$STATE/images/mind-probe-server:latest"
  echo "$MP_ROOT/releases/9579475" > "$STATE/workdir"
  cp "$MP_ROOT/runtime.env" "$STATE/env_at_up"
  printf '%s' "$PW" > "$STATE/admin_password"
}
up() { (bash "$work/upgrade.sh" "$@" > "$STATE/out" 2>&1); rc=$?; }
rb() { (bash "$work/rollback.sh" > "$STATE/out" 2>&1); rc=$?; }
pf() { (bash "$work/preflight.sh" > "$STATE/out" 2>&1); rc=$?; }
admin() { (printf '%s\n' "$@" | bash "$work/set-admin.sh" > "$STATE/out" 2>&1); rc=$?; }
state() { printf 'running=%s from=%s current=%s pre-tag=%s' "$(cat "$STATE/running_image")" "$(basename "$(cat "$STATE/workdir")")" "$(basename "$(readlink -f "$MP_ROOT/current")")" "$(cat "$STATE/images/mind-probe-server:pre-$NEW" 2>/dev/null || echo none)"; }
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass + 1)); echo "PASS $1"; else fail=$((fail + 1)); echo "FAIL $1"; sed 's/^/    /' "$STATE/out" | tail -8; fi; }
expect() {  # expect <label> <rc> <grep pattern> <state>
  local good=yes
  [ "$rc" = "$2" ] || good=no
  grep -q -- "$3" "$STATE/out" || good=no
  [ "$(state)" = "$4" ] || good=no
  if [ $good = yes ]; then pass=$((pass + 1)); echo "PASS $1"; else fail=$((fail + 1)); echo "FAIL $1 (rc=$rc, $(state))"; sed 's/^/    /' "$STATE/out" | tail -8; fi
}
OLD='running=img-old from=9579475 current=9579475'
NEWST="running=img-new from=$NEW current=$NEW pre-tag=img-old"
admin_lines() { grep -c '^MINDPROBE_ADMIN_' "$MP_ROOT/runtime.env"; }

echo "# P: preflight (release $NEW)"
fresh p; pf
ok 'preflight passes on an idle host' '[ $rc = 0 ] && grep -q "Preflight passed" "$STATE/out" && grep -q "OP admin login: not set yet" "$STATE/out"'
chmod 644 "$MP_ROOT/runtime.env"; pf
ok 'preflight fails when runtime.env is not 0600' '[ $rc = 1 ] && grep -q "not mode 0600" "$STATE/out"'
chmod 600 "$MP_ROOT/runtime.env"; touch "$STATE/busy"; pf
ok 'preflight waits while a session is active' '[ $rc = 1 ] && grep -q "Activity: WAIT" "$STATE/out"'

echo '# A: no maintenance confirmation'
fresh a; up
expect 'refuses without --maintenance-confirmed' 2 'maintenance-confirmed' "$OLD pre-tag=none"

echo '# T: tampered release files'
fresh t; cp "$MP_TMP/mindprobe-release.env" "$work/env.keep"
printf 'RELEASE_COMMIT=%040d\n' 0 > "$MP_TMP/mindprobe-release.env"; up --maintenance-confirmed
expect 'checksum mismatch stops before any change' 1 'do not match' "$OLD pre-tag=none"
cp "$work/env.keep" "$MP_TMP/mindprobe-release.env"

echo '# B: build fails, then rerun reuses the release'
fresh b; touch "$STATE/fail_build"; up --maintenance-confirmed
expect 'build failure stops before cutover' 1 'FAILED during: 4/7' "$OLD pre-tag=img-old"
rm "$STATE/fail_build"; up --maintenance-confirmed
expect 'rerun reuses the matching release and upgrades' 0 'Reusing .* matches the tar' "$NEWST"
ok 'step 7 matches public and container routes' 'grep -q "3 routes match" "$STATE/out"'
ok 'step 7 reports admin login not configured' 'grep -q "OP admin login: not configured" "$STATE/out"'
ok 'backup file named after the new release' 'grep -q "pre-$NEW-" "$STATE/out"'
up --maintenance-confirmed
expect 'rerun after success says done' 0 'the upgrade is done' "$NEWST"

echo '# C: activity appears during the build'
fresh c; touch "$STATE/busy"; up --maintenance-confirmed
expect 'recheck after build stops before backup and cutover' 1 'Activity: WAIT' "$OLD pre-tag=img-old"
ok 'no backup while busy' '! grep -q "Database backup" "$STATE/out"'
rm "$STATE/busy"; up --maintenance-confirmed
expect 'rerun once idle upgrades' 0 "Upgrade to $NEW done" "$NEWST"

echo '# D: new containers unhealthy, rollback, then retry'
fresh d; touch "$STATE/unhealthy"; up --maintenance-confirmed
expect 'unhealthy new stack leaves current on the old release' 1 'Run rollback.sh' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
up --maintenance-confirmed
expect 'rerun refuses while the new stack runs' 1 'run rollback.sh first' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
rm "$STATE/unhealthy"; rb
expect 'rollback restores the old image and directory' 0 'Rolling back to' "$OLD pre-tag=img-old"
up --maintenance-confirmed
expect 'retry after rollback upgrades' 0 "Upgrade to $NEW done" "$NEWST"

echo '# E: a release directory that does not match the tar'
fresh e; mkdir -p "$MP_ROOT/releases/$NEW"; echo tampered > "$MP_ROOT/releases/$NEW/README.md"
up --maintenance-confirmed
expect 'mismatched release is not reused or deleted' 1 'does not match the tar' "$OLD pre-tag=img-old"
ok 'mismatched directory left in place' 'test -f "$MP_ROOT/releases/$NEW/README.md"'

echo '# F: rollback tag already names another image'
fresh f; echo img-other > "$STATE/images/mind-probe-server:pre-$NEW"; up --maintenance-confirmed
expect 'existing rollback tag is never moved' 1 'Not moving it' "$OLD pre-tag=img-other"

echo '# G: set-admin'
fresh g; admin admin "$PW" "$PW"
ok 'set-admin refuses on the old image, runtime.env untouched' '[ $rc = 1 ] && grep -q "Run upgrade.sh first" "$STATE/out" && [ "$(admin_lines)" = 0 ]'
up --maintenance-confirmed > /dev/null
ups_before="$(wc -l < "$STATE/ups")"
admin admin "$PW" "different-password-1"
ok 'mismatched passwords change nothing' '[ $rc = 1 ] && grep -q "differ" "$STATE/out" && [ "$(admin_lines)" = 0 ]'
admin admin short short
ok 'short password refused' '[ $rc = 1 ] && grep -q "12 to 1024" "$STATE/out" && [ "$(admin_lines)" = 0 ]'
admin 'bad name' "$PW" "$PW"
ok 'odd username refused' '[ $rc = 1 ] && [ "$(admin_lines)" = 0 ]'
touch "$STATE/bad_hash"; admin admin "$PW" "$PW"
ok 'unexpected hasher output leaves runtime.env alone' '[ $rc = 1 ] && grep -q "unchanged" "$STATE/out" && [ "$(admin_lines)" = 0 ]'
rm "$STATE/bad_hash"; admin admin "$PW" "$PW"
ok 'set-admin succeeds and proves login' '[ $rc = 0 ] && grep -q "OP admin login works" "$STATE/out"'
ok 'runtime.env: two admin lines, other lines kept, still 0600' '[ "$(admin_lines)" = 2 ] && grep -q "^# comment stays" "$MP_ROOT/runtime.env" && grep -q "^MINDPROBE_OPERATOR_TOKEN=o" "$MP_ROOT/runtime.env" && [ "$(stat -c %a "$MP_ROOT/runtime.env")" = 600 ]'
ok 'server restarted after the write' '[ "$(wc -l < "$STATE/ups")" -gt "$ups_before" ] && grep -q "^MINDPROBE_ADMIN_PASSWORD_HASH=scrypt:" "$STATE/env_at_up"'
ok 'password reached the hasher on stdin intact' '[ "$(cat "$STATE/hashed_pw")" = "$(printf %s "$PW" | sha256sum | cut -d" " -f1)" ]'
ok 'backup of runtime.env is 0600 and has no admin lines' 'b="$(ls "$MP_BACKUP_DIR"/runtime.env.bak-* | head -1)"; [ "$(stat -c %a "$b")" = 600 ] && ! grep -q ADMIN "$b"'
ok 'test login revoked with its own token' 'grep -q "^Authorization: Bearer tok-fake-123$" "$STATE/logout_header"'
ok 'no password, hash or token in the output' '! grep -q -e "$PW" -e "scrypt:" -e "tok-fake" "$STATE/out"'
ok 'no password in any docker or curl argv' '! grep -q -- "$PW" "$STATE/log"'
printf '%s' 'another-password-99' > "$STATE/admin_password"
admin admin "another-password-99" "another-password-99"
ok 'rerun replaces the admin lines without duplicates' '[ $rc = 0 ] && [ "$(admin_lines)" = 2 ]'
touch "$STATE/login_fails"; admin admin "$PW" "$PW"
ok 'failed login check exits 1 and points at the backup' '[ $rc = 1 ] && grep -q "HTTP 401" "$STATE/out" && grep -q "restore" "$STATE/out"'

echo '# H: password length and responses that quote the password'
fresh h; up --maintenance-confirmed > /dev/null
untouched() {  # nothing hashed, backed up, written or restarted since $1 ups
  [ "$(admin_lines)" = 0 ] && [ ! -e "$STATE/hashed_pw" ] && [ -z "$(ls -A "$MP_BACKUP_DIR")" ] && [ "$(wc -l < "$STATE/ups")" = "$1" ]
}
ups0="$(wc -l < "$STATE/ups")"
LONG="$(printf 'L%.0s' $(seq 1 1025))"
admin admin "$LONG" "$LONG"
ok 'a 1025-character password is refused before any change' '[ $rc = 1 ] && grep -q "12 to 1024" "$STATE/out" && untouched "$ups0"'
ok 'the overlong password is not printed' '! grep -q "LLLLLLLLLL" "$STATE/out"'
admin admin 'eleven-char' 'eleven-char'
ok 'an 11-character password is refused before any change' '[ $rc = 1 ] && untouched "$ups0"'
WIDE="$(printf '密%.0s' $(seq 1 1024))"
printf '%s' "$WIDE" > "$STATE/admin_password"; admin admin "$WIDE" "$WIDE"
ok '1024 multi-byte characters are accepted (characters, not bytes)' '[ $rc = 0 ] && [ "$(admin_lines)" = 2 ]'
EDGE="$(printf 'e%.0s' $(seq 1 1024))"
printf '%s' "$EDGE" > "$STATE/admin_password"; admin admin "$EDGE" "$EDGE"
ok 'exactly 1024 characters are accepted' '[ $rc = 0 ]'
SECRET='quoted-secret-password-77'
printf '{"detail":[{"type":"string_too_long","loc":["body","password"],"input":"%s"}]}\n422' "$SECRET" > "$STATE/login_response"
admin admin "$SECRET" "$SECRET"
ok 'a 422 that quotes the password: status and fixed text only' '[ $rc = 1 ] && grep -q "HTTP 422 (request rejected as invalid)" "$STATE/out" && ! grep -q -e "$SECRET" -e string_too_long -e input "$STATE/out"'
printf 'Internal error near %s\n500' "$SECRET" > "$STATE/login_response"
admin admin "$SECRET" "$SECRET"
ok 'a 500 body with the password is not printed' '[ $rc = 1 ] && grep -q "HTTP 500 (unexpected answer)" "$STATE/out" && ! grep -q "$SECRET" "$STATE/out"'
printf '{"note":"%s"}\n200' "$SECRET" > "$STATE/login_response"
admin admin "$SECRET" "$SECRET"
ok 'a 200 without a token is refused and its body is not printed' '[ $rc = 1 ] && grep -q "without an operator token" "$STATE/out" && ! grep -q "$SECRET" "$STATE/out"'
rm "$STATE/login_response"

echo '# S: the public API still serves the old release after the swap'
fresh s; touch "$STATE/stale_proxy"; up --maintenance-confirmed
expect 'stale public proxy stops before current switches' 1 'did not reach the new container' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
fresh s2; touch "$STATE/stale_proxy"; OLD_PATHS="$NEW_PATHS" up --maintenance-confirmed
expect 'stale proxy is caught even when both releases have the same routes' 1 'did not reach the new container' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
fresh s3; touch "$STATE/stale_routes"; up --maintenance-confirmed
expect 'route mismatch is still a supplemental stop' 1 'differs from the routes of the new container' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
fresh s4; touch "$STATE/stale_image"; up --maintenance-confirmed
expect 'an image not built from this release stops before current switches' 1 'was built from' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
fresh s5; up --maintenance-confirmed
ok 'release identity is confirmed and the marker is in the build context' 'grep -q "Release $NEW confirmed" "$STATE/out" && [ "$(cat "$MP_ROOT/releases/$NEW/server/RELEASE_COMMIT")" = "$commit" ]'

echo '# L: one host lock for every mutating entry point'
holdlock() { exec 8>"$MP_ROOT/.deploy.lock"; flock -n 8; }
freelock() { exec 8>&-; }
fresh l1; holdlock; up --maintenance-confirmed; freelock
expect 'direct upgrade.sh refuses while the lock is held' 75 'Another deploy, rollback or admin change' "$OLD pre-tag=none"
ok 'direct upgrade.sh changed nothing (no build, no tag, no start)' '[ ! -e "$STATE/ups" ] && ! grep -q "docker tag" "$STATE/log"'
fresh l2; up --maintenance-confirmed; holdlock; rb; freelock
expect 'direct rollback.sh refuses while the lock is held' 75 'Another deploy, rollback or admin change' "$NEWST"
fresh l3; up --maintenance-confirmed > /dev/null; ups_l3="$(wc -l < "$STATE/ups")"; holdlock; admin admin "$PW" "$PW"; freelock
ok 'direct set-admin.sh refuses while the lock is held and changes nothing' '[ $rc = 75 ] && [ "$(admin_lines)" = 0 ] && [ ! -e "$STATE/hashed_pw" ] && [ "$(wc -l < "$STATE/ups")" = "$ups_l3" ]'
fresh l4; holdlock; MP_LOCK_HELD=1 up --maintenance-confirmed; freelock
expect 'MP_LOCK_HELD alone does not bypass a lock held by someone else' 75 'Another deploy, rollback or admin change' "$OLD pre-tag=none"
fresh l5; up --maintenance-confirmed > /dev/null; rb
expect 'the lock is released after each direct run (rollback right after upgrade)' 0 'Rolling back to' "$OLD pre-tag=img-old"

echo '# CD: ci-deploy.sh -> fake ssh/scp -> remote-run.sh'
# As in the workflows: the host is a Tailscale IPv4 with a pinned key (real ssh-keygen).
TS_HOST=100.101.102.103
ssh-keygen -q -t ed25519 -N '' -C '' -f "$work/hostkey"
printf '%s %s\n' "$TS_HOST" "$(cut -d' ' -f1,2 "$work/hostkey.pub")" > "$work/known_hosts"
cd_env() {  # cd_env <name>; ACTION COMMIT CONFIRMED HOST KNOWN TAILNET CHECK_ONLY override
  CD_OUT="$work/out-$1"
  rm -rf "$CD_OUT"
  env DEPLOY_ACTION="${ACTION:-deploy}" DEPLOY_COMMIT="${COMMIT:-$commit}" MAINTENANCE_CONFIRMED="${CONFIRMED:-true}" \
    DEPLOY_RUN_ID="run-$1" DEPLOY_HOST="${HOST:-$TS_HOST}" DEPLOY_USER=ubuntu \
    DEPLOY_REQUIRE_TAILNET="${TAILNET:-1}" DEPLOY_CHECK_ONLY="${CHECK_ONLY:-}" \
    DEPLOY_SSH_KEY_FILE=/dev/null DEPLOY_KNOWN_HOSTS_FILE="${KNOWN:-$work/known_hosts}" DEPLOY_OUT="$CD_OUT" \
    DEPLOY_REMOTE_BASE="$work/remote-$1" DEPLOY_REMOTE_ROOT="$MP_ROOT" DEPLOY_POLL_S=0 DEPLOY_TIMEOUT_S=60 \
    DEPLOY_RELEASE_DIR="$rel" bash "$work/ci-deploy.sh" > "$STATE/out" 2>&1
  rc=$?
}
untouched_cd() {  # nothing staged on the host, no ssh/scp and no host log for run $1
  [ ! -e "$work/remote-$1" ] && ! grep -q "run-$1" "$STATE/log" 2>/dev/null && [ ! -e "$MP_ROOT/deploy-logs/run-$1.log" ]
}
fresh cd1; cd_env cd1
expect 'CD deploy: preflight, upgrade, exit 0' 0 "Upgrade to $NEW done" "$NEWST"
ok 'CD deploy: preflight ran on the host first' 'grep -q "Preflight passed" "$STATE/out"'
ok 'CD deploy: host log kept and copied for the artifact' 'grep -q "== exit 0" "$MP_ROOT/deploy-logs/run-cd1.log" && cmp -s "$MP_ROOT/deploy-logs/run-cd1.log" "$CD_OUT/deploy-run-cd1.log"'
ok 'CD deploy: staging directory is 0700 and holds the release' '[ "$(stat -c %a "$work/remote-cd1/mindprobe-deploy-run-cd1")" = 700 ] && [ -f "$work/remote-cd1/mindprobe-deploy-run-cd1/mindprobe-release.tar" ]'
ok 'CD deploy: reviewed set-admin.sh is staged byte for byte, and not run' 'cmp -s "$kit/set-admin.sh" "$work/remote-cd1/mindprobe-deploy-run-cd1/set-admin.sh" && [ "$(admin_lines)" = 0 ] && [ ! -e "$STATE/hashed_pw" ]'
ok 'CD deploy: the log says where set-admin.sh is and that it is run by hand' 'grep -q "mindprobe-deploy-run-cd1/set-admin.sh" "$STATE/out" && grep -q "by hand" "$STATE/out"'
ACTION=rollback cd_env cd1-rb
expect 'CD rollback restores the old release' 0 'health passed' "$OLD pre-tag=img-old"

fresh cd2; CONFIRMED=false cd_env cd2
expect 'CD refuses without maintenance confirmation, nothing staged' 2 'Not confirmed' "$OLD pre-tag=none"
ok 'CD unconfirmed: no host directory, no log' '[ ! -e "$work/remote-cd2" ] && [ ! -e "$MP_ROOT/deploy-logs" ]'
ACTION=rollback CONFIRMED=false cd_env cd2-rb
expect 'CD rollback also needs the confirmation' 2 'Not confirmed' "$OLD pre-tag=none"
COMMIT="${commit:0:7}" cd_env cd2-short
expect 'CD refuses a short commit' 2 '40-character' "$OLD pre-tag=none"

fresh cd3; touch "$STATE/busy"; cd_env cd3
expect 'CD stops at preflight while a session is active' 1 'Activity: WAIT' "$OLD pre-tag=none"
ok 'CD activity stop: upgrade never started' '! grep -q "== 1/7" "$STATE/out"'

fresh cd4; exec 9>"$MP_ROOT/.deploy.lock"; flock -n 9; cd_env cd4; exec 9>&-
expect 'CD refuses while another run holds the host lock' 75 'Another deploy or rollback is running' "$OLD pre-tag=none"

fresh cd5; touch "$STATE/sudo_password"; cd_env cd5
expect 'CD stops when sudo would ask for a password' 1 'passwordless sudo' "$OLD pre-tag=none"

fresh cd6; touch "$STATE/unhealthy"; cd_env cd6
expect 'CD reports an unhealthy cutover and leaves current alone' 1 'Run rollback.sh' "running=img-new from=$NEW current=9579475 pre-tag=img-old"
rm "$STATE/unhealthy"; ACTION=rollback cd_env cd6-rb
expect 'CD rollback after a failed cutover' 0 'health passed' "$OLD pre-tag=img-old"

fresh cd7; rel_old="$work/release-old"; mkdir -p "$rel_old/x"
tar -xf "$rel/mindprobe-release.tar" -C "$rel_old/x" && rm -rf "$rel_old/x/deploy" && tar -cf "$rel_old/mindprobe-release.tar" -C "$rel_old/x" .
cp "$rel/mindprobe-release.env" "$rel_old/"; (cd "$rel_old" && sha256sum mindprobe-release.tar mindprobe-release.env > mindprobe-release.sha256)
rel_keep="$rel"; rel="$rel_old"; cd_env cd7; rel="$rel_keep"
expect 'CD deploys a target commit that predates deploy/backend' 0 "Upgrade to $NEW done" "$NEWST"
ok 'older target: its tar has no set-admin.sh, yet the kit stages the reviewed one' '! tar -tf "$rel_old/mindprobe-release.tar" | grep -q "set-admin.sh" && cmp -s "$kit/set-admin.sh" "$work/remote-cd7/mindprobe-deploy-run-cd7/set-admin.sh"'

echo '# CP: CD preflight (read-only)'
fresh cp1; ACTION=preflight CONFIRMED=false cd_env cp1
expect 'CD preflight passes without the maintenance checkbox' 0 'Preflight passed' "$OLD pre-tag=none"
ok 'CD preflight: no upgrade phase, release dir, build, tag, backup, restart or rollback record' '! grep -q "== 1/7" "$STATE/out" && [ ! -e "$MP_ROOT/releases/$NEW" ] && [ ! -e "$STATE/ups" ] && ! grep -q -E "^docker (build|tag)|compose.* (build|up)|backup" "$STATE/log" && [ ! -e "$MP_ROOT/rollback-target" ] && [ ! -e "$MP_ROOT/rollback-image" ]'
ok 'CD preflight: says nothing changed, does not offer set-admin, log kept' 'grep -q "Preflight only: no image" "$STATE/out" && ! grep -q "set-admin.sh is staged" "$STATE/out" && grep -q "== exit 0" "$MP_ROOT/deploy-logs/run-cp1.log"'
fresh cp2; touch "$STATE/busy"; ACTION=preflight cd_env cp2
expect 'CD preflight reports activity' 1 'Activity: WAIT' "$OLD pre-tag=none"
fresh cp3; exec 9>"$MP_ROOT/.deploy.lock"; flock -n 9; ACTION=preflight cd_env cp3; exec 9>&-
expect 'CD preflight waits for the host lock like any other run' 75 'Another deploy or rollback is running' "$OLD pre-tag=none"
fresh cp4; touch "$STATE/sudo_password"; ACTION=preflight cd_env cp4
expect 'CD preflight stops when sudo would ask for a password' 1 'passwordless sudo' "$OLD pre-tag=none"
fresh cp5; ACTION=preflight COMMIT="${commit:0:7}" cd_env cp5
expect 'CD preflight refuses a short commit' 2 '40-character' "$OLD pre-tag=none"
ACTION=bogus cd_env cp5b
expect 'CD refuses an unknown action' 2 'must be preflight, deploy or rollback' "$OLD pre-tag=none"
ok 'CD refusals staged nothing' 'untouched_cd cp5 && untouched_cd cp5b'

echo '# CT: Tailscale address and pinned host key'
fresh ct
for h in 95.41.69.24 100.128.0.1 100.63.255.1 100.64.0.300 mindprobe-prod 95-41-69-24.sslip.io; do
  HOST="$h" cd_env "ct-$h"
  expect "CD refuses DEPLOY_HOST=$h (not a Tailscale IPv4)" 2 'Tailscale IPv4' "$OLD pre-tag=none"
  ok "CD DEPLOY_HOST=$h: nothing staged, no ssh" "untouched_cd 'ct-$h'"
done
HOST=100.127.255.254 cd_env ct-nokey
expect 'CD refuses a tailnet address that known_hosts does not list' 2 'no host key for DEPLOY_HOST' "$OLD pre-tag=none"
KNOWN=/dev/null cd_env ct-empty
expect 'CD refuses an empty known_hosts' 2 'no host key for DEPLOY_HOST' "$OLD pre-tag=none"
ok 'CD host-key refusals staged nothing' 'untouched_cd ct-nokey && untouched_cd ct-empty'
cp "$work/known_hosts" "$work/known_hosts_hashed"; ssh-keygen -q -H -f "$work/known_hosts_hashed" > /dev/null 2>&1
KNOWN="$work/known_hosts_hashed" CHECK_ONLY=1 cd_env ct-hashed
ok 'a hashed known_hosts entry is accepted' '[ $rc = 0 ] && ! grep -q "$TS_HOST" "$work/known_hosts_hashed"'
CHECK_ONLY=1 cd_env ct-check
expect 'check-only mode: inputs ok, exit 0' 0 "Inputs ok: deploy $commit" "$OLD pre-tag=none"
ok 'check-only mode: nothing built, staged or contacted' 'untouched_cd ct-check && [ ! -e "$work/out-ct-check/stage" ]'
CHECK_ONLY=1 CONFIRMED=false cd_env ct-check2
expect 'check-only mode refuses an unconfirmed deploy (before joining the tailnet)' 2 'Not confirmed' "$OLD pre-tag=none"
ACTION=rollback CHECK_ONLY=1 CONFIRMED=false cd_env ct-check3
expect 'check-only mode refuses an unconfirmed rollback' 2 'Not confirmed' "$OLD pre-tag=none"

echo '# W: workflow guards'
wf="$(cd "$kit/../.." && pwd)/.github/workflows"
if [ -d "$wf" ]; then
  while IFS= read -r line; do
    echo "$line"
    case "$line" in PASS*) pass=$((pass + 1)) ;; *) fail=$((fail + 1)) ;; esac
  done < <(python3 "$kit/dryrun/check-workflows.py" "$wf" 2>&1 || true)
else
  echo 'FAIL workflows directory not found'; fail=$((fail + 1))
fi

echo "passed=$pass failed=$fail"
[ "$fail" = 0 ]
