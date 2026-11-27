#!/bin/sh
set -eu

PASS_FILE=${1:-}
CMD_FILE=${2:-}
TIMEOUT_SEC=15

case "$PASS_FILE" in
    /tmp/sa02m-cmd-pass.*) ;;
    /tmp/sa02m-cmd-v1-pass.*) [ "$#" -eq 3 ] || exit 64 ;;
    *) echo "invalid password file" >&2; exit 64 ;;
esac
case "$CMD_FILE" in /tmp/sa02m-cmd-cmd.*) ;; *) echo "invalid command file" >&2; exit 64 ;; esac

[ -f "$PASS_FILE" ] && [ ! -L "$PASS_FILE" ] || { echo "invalid password file" >&2; exit 64; }
[ -f "$CMD_FILE" ] && [ ! -L "$CMD_FILE" ] || { echo "invalid command file" >&2; exit 64; }

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required for root password verification" >&2
    exit 69
fi

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Authentication, lock waiting and execution share the existing total budget.
exec timeout -k 2s "$TIMEOUT_SEC" /usr/bin/python3 -I /usr/local/lib/sa02m-root-auth.py "$@"
