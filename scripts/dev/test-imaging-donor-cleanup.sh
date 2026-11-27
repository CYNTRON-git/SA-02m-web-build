#!/bin/bash
# comment-mutation-proof-exempt: executes the donor's selection and deletion
# functions on real files and links under an isolated filesystem prefix.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[[ $EUID = 0 && $(uname -s) = Linux ]] || { echo 'Run in isolated Linux as root' >&2; exit 1; }
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
# Keep only the definitions and argument parser, never run system cleanup phases.
sed '/^# --- main ---/,$d' tools/imaging/cleanup-donor.sh |
    sed -E "s#(^|[[:space:]'\"(=^|])/(boot|etc|home|lib|opt|root|run|tmp|var)\\b#\\1$fixture/\\2#g" > "$fixture/cleanup.sh"
source "$fixture/cleanup.sh" --dry-run

keys="$fixture/etc/sa02m-update/trusted-keys"
state="$fixture/var/lib/sa02m-update/state"
root="$fixture/root"
mkdir -p "$keys/nested" "$state/deployed_dir" "$fixture/etc/ssh" "$fixture/boot" "$root"
touch "$keys/release.pem" "$keys/nested/sentinel" "$state/deployed_version" "$state/other" \
    "$state/deployed_dir/sentinel" "$fixture/etc/ssh/host" "$fixture/boot/sentinel"
ln -s "$fixture/etc/ssh/host" "$keys/linked.pem"
ln -s "$fixture/boot" "$state/deployed_link"
ln -s "$keys/release.pem" "$root/sa02m-deploy-key"
ln -s "$state/deployed_version" "$root/sa02m-deploy-marker"

archive="$root/etc-backup-20261127-160403.tgz"
tar -czf "$archive" -C "$fixture" etc
preserved=("$root/etc-backup-personal.tgz" "$root/etc-backup-2026AB27-160403.tgz"
    "$root/etc-backup-20261127-160403.tgz.save" "$root/other.tgz")
touch "${preserved[@]}"
linked_archive="$root/etc-backup-20261127-160404.tgz"
dir_archive="$root/etc-backup-20261127-160405.tgz"
shared_archive="$root/etc-backup-20261127-160406.tgz"
ln "$root/other.tgz" "$shared_archive"
ln -s "$root/other.tgz" "$linked_archive"
mkdir "$dir_archive"
touch "$dir_archive/sentinel"

assert_denied() {
    local target
    for target in "$@"; do
        is_denied "$target" || { echo "expected DENY $target" >&2; exit 1; }
    done
}
collect_junk
[[ ${#CANDIDATES[@]} = 1 && ${CANDIDATES[0]} = "$archive" ]]
assert_denied "$state/deployed_version" "$keys/release.pem"
echo 'dry-run selects only the offline updater archive'

# The operator flag changes only the two direct-file exceptions.
source "$fixture/cleanup.sh" --purge-update-state
collect_junk
[[ ${#CANDIDATES[@]} = 3 ]]
for expected in "$keys/release.pem" "$state/deployed_version" "$archive"; do candidate_seen "$expected"; done
assert_denied "$fixture/etc/ssh" "$fixture/etc/ssh/host" "$fixture/boot" "$keys" "$state" \
    "$keys/nested" "$keys/nested/sentinel" "$state/other" "$state/deployed_dir" "$state/deployed_dir/sentinel" \
    "$keys/linked.pem" "$state/deployed_link" "$root/sa02m-deploy-key" "$root/sa02m-deploy-marker" \
    "$linked_archive" "$dir_archive" "$shared_archive"
apply_junk
[[ ! -e "$keys/release.pem" && ! -e "$state/deployed_version" && ! -e "$archive" ]]
for keep in "${preserved[@]}" "$fixture/boot/sentinel" "$fixture/etc/ssh/host" "$dir_archive/sentinel" \
    "$keys/nested/sentinel" "$state/other" "$state/deployed_dir/sentinel"; do [[ -f "$keep" ]]; done
for link in "$keys/linked.pem" "$state/deployed_link" "$linked_archive" \
    "$root/sa02m-deploy-key" "$root/sa02m-deploy-marker"; do [[ -L "$link" ]]; done
echo 'purge removes only the selected regular files and preserves denied trees, links and user archives'

# A directory substituted after collection must survive the second guard at rm.
touch "$archive"
CANDIDATES=()
add_candidate "$archive"
rm "$archive"
mkdir "$archive"
touch "$archive/sentinel"
apply_junk
[[ -f "$archive/sentinel" ]]
echo 'apply rechecks the archive type before removal'

# Swap after is_denied, while remove_one measures the size. The final unlink
# itself must reject directories for all three file-only namespaces.
bytes_of() {
    rm -- "$1"
    mkdir "$1"
    touch "$1/sentinel"
    echo 0
}
for name in "$root/etc-backup-20261127-160407.tgz" "$keys/release.pem" "$state/deployed_version"; do
    touch "$name"
    PARTIAL_FAIL=0
    remove_one "$name"
    [[ $PARTIAL_FAIL = 1 && -f "$name/sentinel" ]]
done
echo 'final unlink preserves substituted directories in all three file-only namespaces'
