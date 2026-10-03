#!/bin/bash
# sa02m-rules-store-apply.sh — apply one JSON command to the scenario store as root.
# Usage: sa02m-rules-store-apply.sh </tmp/sa02m-rules-cmd.XXXXXX>
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
    [[ "$base" =~ ^sa02m-rules-cmd\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

SRC="${1:-}"
if ! src_path_ok "$SRC"; then
    echo "refusing rules command path" >&2
    exit 2
fi

WORK=$(mktemp -d /tmp/sa02m-rules-apply.XXXXXX)
chmod 0700 "$WORK"
SAFE="$WORK/cmd.json"
if ! timeout 10 bash -c '
    exec 8<"$1" || exit 2
    opened=$(readlink "/proc/self/fd/8") || exit 3
    [ "$opened" = "$1" ] || exit 4
    head -c 1048576 <&8
' _ "$SRC" >"$SAFE"; then
    echo "refusing rules command" >&2
    exit 2
fi

PYTHONPATH="/opt/sa02m-rules${PYTHONPATH:+:$PYTHONPATH}" python3 - "$SAFE" <<'PY'
import json, sys
sys.path.insert(0, "/opt/sa02m-rules")
from sa02m_rules.store import apply_command
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    body = json.load(fh)
result = apply_command(body)
if not isinstance(result, (dict, list)):
    result = {"ok": True}
print(json.dumps(result, ensure_ascii=False))
PY
