#!/bin/sh
set -eu

PASS_FILE=${1:-}
CMD_FILE=${2:-}
TIMEOUT_SEC=20

case "$PASS_FILE" in /tmp/sa02m-cmd-pass.*) ;; *) echo "invalid password file" >&2; exit 64 ;; esac
case "$CMD_FILE" in /tmp/sa02m-cmd-cmd.*) ;; *) echo "invalid command file" >&2; exit 64 ;; esac

[ -f "$PASS_FILE" ] && [ ! -L "$PASS_FILE" ] || { echo "invalid password file" >&2; exit 64; }
[ -f "$CMD_FILE" ] && [ ! -L "$CMD_FILE" ] || { echo "invalid command file" >&2; exit 64; }

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required for root password verification" >&2
    exit 69
fi

if ! python3 - "$PASS_FILE" <<'PY'
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import crypt
import spwd
import sys

password = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read().rstrip("\n")
try:
    shadow = spwd.getspnam("root").sp_pwdp
except Exception:
    sys.exit(2)

if not shadow or shadow[0] in ("!", "*"):
    sys.exit(2)

sys.exit(0 if crypt.crypt(password, shadow) == shadow else 1)
PY
then
    echo "root authentication failed" >&2
    exit 77
fi

CMD=$(cat "$CMD_FILE")
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

timeout "$TIMEOUT_SEC" /bin/bash -c "$CMD"
