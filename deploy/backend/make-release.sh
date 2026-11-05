#!/usr/bin/env bash
# Build a backend release from a feat/m2 commit.
#   bash deploy/backend/make-release.sh <commit> [--out DIR]
# Dry runs and CI on a pull request only:
#   bash deploy/backend/make-release.sh <any commit> --unmerged [--out DIR]
# Writes mindprobe-release.tar (git archive of the commit), mindprobe-release.env
# (RELEASE_COMMIT) and mindprobe-release.sha256 (both files) into DIR, default
# the current directory. The release is defined by the commit; the tar bytes
# depend on the git version, so compare commits, not checksums, across machines.
set -euo pipefail
repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
commit="$(git -C "$repo" rev-parse --verify "${1:?usage: make-release.sh <commit> [--unmerged] [--out DIR]}^{commit}")"
shift
unmerged=no
out="$PWD"
while [ $# -gt 0 ]; do
  case "$1" in
    --unmerged) unmerged=yes ;;
    --out) out="${2:?--out needs a directory}"; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done
if [ "$unmerged" != yes ]; then
  git -C "$repo" fetch -q origin feat/m2
  git -C "$repo" merge-base --is-ancestor "$commit" origin/feat/m2 \
    || { echo "$commit is not on origin/feat/m2."; exit 1; }
fi
# Anything the host build and the upgrade depend on.
for path in docker-compose.prod.yml Caddyfile server/Dockerfile server/app/main.py; do
  git -C "$repo" cat-file -e "$commit:$path" || { echo "$commit has no $path."; exit 1; }
done
mkdir -p "$out"
git -C "$repo" archive --format=tar -o "$out/mindprobe-release.tar" "$commit"
printf 'RELEASE_COMMIT=%s\n' "$commit" > "$out/mindprobe-release.env"
(cd "$out" && sha256sum mindprobe-release.tar mindprobe-release.env > mindprobe-release.sha256)
cat "$out/mindprobe-release.sha256"
echo "Release ${commit:0:7} (tree $(git -C "$repo" rev-parse "$commit^{tree}")) ready in $out."
