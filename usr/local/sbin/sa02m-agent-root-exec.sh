#!/bin/bash
# sa02m-agent-root-exec.sh — run one script as root when that token's cap file exists.
# Usage: sa02m-agent-root-exec.sh <id> </tmp/sa02m-agent-cmd.XXXXXX>
# The script is executed as a file (`bash "$file"`), never as `bash -c "$text"`.
set -euo pipefail

CAP_DIR=/etc/sa02m-agent-api/root-cap
WORK=""
cleanup() {
    if [ -n "$WORK" ]; then rm -rf -- "$WORK"; fi
    return 0
}
trap cleanup EXIT

id_ok() {
    [[ "$1" =~ ^[a-f0-9]{8}$ ]] || return 1
    return 0
}

src_path_ok() {
    local p="$1" base
    [ -n "$p" ] || return 1
    case "$p" in /tmp/*) : ;; *) return 1 ;; esac
    base="${p#/tmp/}"
    case "$base" in */*) return 1 ;; esac
    [[ "$base" =~ ^sa02m-agent-cmd\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

IDENT="${1:-}"
SRC="${2:-}"

if ! id_ok "$IDENT"; then
    echo "refusing token id" >&2
    exit 2
fi
if ! src_path_ok "$SRC"; then
    echo "refusing command file" >&2
    exit 2
fi
if [ ! -f "$CAP_DIR/$IDENT" ] || [ -L "$CAP_DIR/$IDENT" ]; then
    echo "root cap is not granted for this token" >&2
    exit 77
fi

WORK=$(mktemp -d /tmp/sa02m-rootexec.XXXXXX)
chmod 0700 "$WORK"
SAFE="$WORK/cmd.sh"
if ! timeout 10 bash -c '
    exec 8<"$1" || exit 2
    opened=$(readlink "/proc/self/fd/8") || exit 3
    [ "$opened" = "$1" ] || exit 4
    [ -f "/proc/self/fd/8" ] || exit 5
    head -c 262144 <&8
' _ "$SRC" >"$SAFE"; then
    echo "refusing command file" >&2
    exit 2
fi
chmod 0700 "$SAFE"
timeout 120 /bin/bash "$SAFE"
