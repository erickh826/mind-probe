#!/usr/bin/env bash
# Build the backend release that carries PR #18 (review-code limit) and PR #20
# (OP admin login), on this machine (Git Bash). Run after both are merged:
#   bash make-release.sh <feat/m2 commit>
# Dry runs only: bash make-release.sh <any commit> --unmerged
# Writes mindprobe-release.tar, mindprobe-release.env (the commit) and
# mindprobe-release.sha256 (both files) next to this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
commit="$(git -C "$repo" rev-parse --verify "${1:?usage: make-release.sh <commit> [--unmerged]}^{commit}")"
if [ "${2:-}" != "--unmerged" ]; then
  git -C "$repo" fetch -q origin feat/m2
  git -C "$repo" merge-base --is-ancestor "$commit" origin/feat/m2 \
    || { echo "$commit is not on origin/feat/m2. Merge #18 and #20 first."; exit 1; }
fi
# The release must carry both PRs.
git -C "$repo" cat-file -e "$commit:server/app/admin_password.py"
auth="$(git -C "$repo" show "$commit:server/app/auth.py")"
grep -q '^review_issue_limiter = SlidingWindowLimiter()' <<<"$auth" || { echo 'No #18 (review_issue_limiter) in this commit.'; exit 1; }
grep -q '^operator_login_limiter = SlidingWindowLimiter()' <<<"$auth" || { echo 'No #20 (operator_login_limiter) in this commit.'; exit 1; }
git -C "$repo" show "$commit:docker-compose.prod.yml" | grep -q 'MINDPROBE_ADMIN_PASSWORD_HASH:' \
  || { echo 'docker-compose.prod.yml does not pass the admin variables.'; exit 1; }
git -C "$repo" archive --format=tar -o "$here/mindprobe-release.tar" "$commit"
printf 'RELEASE_COMMIT=%s\n' "$commit" > "$here/mindprobe-release.env"
(cd "$here" && sha256sum mindprobe-release.tar mindprobe-release.env > mindprobe-release.sha256)
cat "$here/mindprobe-release.sha256"
echo "Release ${commit:0:7} ready."
