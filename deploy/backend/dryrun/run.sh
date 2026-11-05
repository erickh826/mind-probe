#!/usr/bin/env bash
# Dry run of preflight / upgrade / rollback / set-admin with fake sudo, docker
# and curl. Real bash, tar, sha256sum and python3. Run inside WSL:
#   bash run.sh <kit dir> <dir holding mindprobe-release.{tar,env,sha256}>
set -uo pipefail
kit="$1"; rel="$2"
work=/tmp/mp-dryrun-pr18-pr20
rm -rf "$work"; mkdir -p "$work/bin"
cp "$kit"/dryrun/fakes/docker "$kit"/dryrun/fakes/curl "$work/bin/"
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$work/bin/sudo"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/bin/sleep"
chmod +x "$work/bin/"*
cp "$kit"/{preflight,upgrade,rollback,set-admin,activity-check,release-info}.sh "$work/"
export PATH="$work/bin:$PATH" MP_TMP="$work/tmp" MP_API=https://fake.invalid
mkdir -p "$MP_TMP"
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
ok 'step 7 reports admin login not configured yet' 'grep -q "not configured yet" "$STATE/out"'
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

echo "passed=$pass failed=$fail"
[ "$fail" = 0 ]
