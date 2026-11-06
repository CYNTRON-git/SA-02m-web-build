#!/bin/sh
set -eu

PASS_FILE=${1:-}
CMD_FILE=${2:-}
TIMEOUT_SEC=15

case "$PASS_FILE" in /tmp/sa02m-cmd-pass.*) ;; *) echo "invalid password file" >&2; exit 64 ;; esac
case "$CMD_FILE" in /tmp/sa02m-cmd-cmd.*) ;; *) echo "invalid command file" >&2; exit 64 ;; esac

[ -f "$PASS_FILE" ] && [ ! -L "$PASS_FILE" ] || { echo "invalid password file" >&2; exit 64; }
[ -f "$CMD_FILE" ] && [ ! -L "$CMD_FILE" ] || { echo "invalid command file" >&2; exit 64; }

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required for root password verification" >&2
    exit 69
fi

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Authentication and execution share the budget; never import from the web tree.
exec timeout -k 2s "$TIMEOUT_SEC" python3 -I - "$PASS_FILE" "$CMD_FILE" <<'PY'
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import crypt
import spwd
import sys
import os

password = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read().rstrip("\n")
try:
    shadow = spwd.getspnam("root").sp_pwdp
except Exception:
    shadow = ""

if not shadow or shadow[0] in ("!", "*") or crypt.crypt(password, shadow) != shadow:
    print("root authentication failed", file=sys.stderr)
    sys.exit(77)

# Shell command substitution previously removed trailing newlines.
with open(sys.argv[2], "r", encoding="utf-8", errors="replace") as command_file:
    command = command_file.read().rstrip("\n")
os.execv("/bin/bash", ["/bin/bash", "-c", command])
PY
