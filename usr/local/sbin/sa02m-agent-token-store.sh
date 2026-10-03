#!/bin/bash
# sa02m-agent-token-store.sh — put or delete a token record as root.
# put < /tmp/sa02m-agent-tok.XXXXXX >   delete <8 hex id>
# The sudoers grant ends in `*`. The path pin and the id check live here.
set -euo pipefail

WORK=""
cleanup() {
    if [ -n "$WORK" ]; then rm -rf -- "$WORK"; fi
    return 0
}
trap cleanup EXIT

src_path_ok() {
    local p="$1" base
    [ -n "$p" ] || return 1
    case "$p" in /tmp/*) : ;; *) return 1 ;; esac
    base="${p#/tmp/}"
    case "$base" in */*) return 1 ;; esac
    [[ "$base" =~ ^sa02m-agent-tok\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

id_ok() {
    [[ "$1" =~ ^[a-f0-9]{8}$ ]] || return 1
    return 0
}

VERB="${1:-}"
ARG="${2:-}"
SAFE=""

if [ "$VERB" = "put" ]; then
    if ! src_path_ok "$ARG"; then
        echo "refusing token record path" >&2
        exit 2
    fi
    WORK=$(mktemp -d /tmp/sa02m-tokstore.XXXXXX)
    chmod 0700 "$WORK"
    SAFE="$WORK/record.json"
    if ! timeout 10 bash -c '
        exec 8<"$1" || exit 2
        opened=$(readlink "/proc/self/fd/8") || exit 3
        [ "$opened" = "$1" ] || exit 4
        [ -f "/proc/self/fd/8" ] || exit 5
        head -c 65536 <&8
    ' _ "$ARG" >"$SAFE"; then
        echo "refusing token record" >&2
        exit 2
    fi
    set -- put "$SAFE"
elif [ "$VERB" = "delete" ]; then
    if ! id_ok "$ARG"; then
        echo "refusing token id" >&2
        exit 2
    fi
    set -- delete "$ARG"
else
    echo "usage: sa02m-agent-token-store put|delete ..." >&2
    exit 2
fi

export PYTHONPATH="/opt/sa02m-agent-api${PYTHONPATH:+:$PYTHONPATH}"
python3 -m sa02m_agent_api.storecli "$@"
