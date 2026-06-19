#!/bin/bash
# Root helper for web dashboard RS-485 stats (www-data cannot read /proc/tty/driver/serial).
set -euo pipefail

usage() {
    echo "Usage: $0 driver | inuse <device-path> | inuse-batch <device-path>..." >&2
    exit 2
}

tty_in_use() {
    local dev=$1
    [ -n "$dev" ] || return 1
    if command -v fuser >/dev/null 2>&1; then
        fuser "$dev" >/dev/null 2>&1 && return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof "$dev" >/dev/null 2>&1 && return 0
    fi
    return 1
}

case "${1:-}" in
    driver)
        exec cat /proc/tty/driver/serial
        ;;
    inuse)
        dev=${2:-}
        [ -n "$dev" ] || usage
        tty_in_use "$dev"
        ;;
    inuse-batch)
        shift
        [ "$#" -gt 0 ] || usage
        for dev in "$@"; do
            if tty_in_use "$dev"; then
                printf '%s:1\n' "$dev"
            else
                printf '%s:0\n' "$dev"
            fi
        done
        ;;
    *)
        usage
        ;;
esac
