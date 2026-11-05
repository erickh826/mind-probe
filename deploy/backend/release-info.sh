# Sourced by preflight.sh, upgrade.sh and rollback.sh. Verifies the uploaded
# release files and sets: commit, short, release, rollback_tag.
tmp="${MP_TMP:-/tmp}"
root="${MP_ROOT:-/opt/mindprobe}"
(cd "$tmp" && sha256sum --quiet -c mindprobe-release.sha256) \
  || { echo "Release files in $tmp do not match mindprobe-release.sha256."; exit 1; }
commit="$(sed -n 's/^RELEASE_COMMIT=\([0-9a-f]\{40\}\)$/\1/p' "$tmp/mindprobe-release.env")"
[ -n "$commit" ] || { echo "$tmp/mindprobe-release.env has no RELEASE_COMMIT."; exit 1; }
short="${commit:0:7}"
release="$root/releases/$short"
tarball="$tmp/mindprobe-release.tar"
rollback_tag="mind-probe-server:pre-$short"
