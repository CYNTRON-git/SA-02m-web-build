#!/bin/bash
# sa02m-agent-root-cap.sh — grant or revoke a root-capable flag.
# grant <id> </tmp/sa02m-agent-pass.XXXXXX>   revoke <id>
# The password is checked against root's shadow entry. A mismatch exits 77
# and does not create the cap file.
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
    [[ "$base" =~ ^sa02m-agent-pass\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

VERB="${1:-}"
IDENT="${2:-}"
PASS="${3:-}"

if ! id_ok "$IDENT"; then
    echo "refusing token id" >&2
    exit 2
fi

if [ "$VERB" = "grant" ]; then
    if ! src_path_ok "$PASS"; then
        echo "refusing password file" >&2
        exit 2
    fi
    WORK=$(mktemp -d /tmp/sa02m-capwork.XXXXXX)
    chmod 0700 "$WORK"
    SAFE="$WORK/pass"
    if ! timeout 10 bash -c '
        exec 8<"$1" || exit 2
        opened=$(readlink "/proc/self/fd/8") || exit 3
        [ "$opened" = "$1" ] || exit 4
        head -c 256 <&8
    ' _ "$PASS" >"$SAFE"; then
        echo "refusing password file" >&2
        exit 2
    fi
    # The crypt and spwd modules are deprecated in Python 3.12 (the board) and
    # gone in 3.13: read /etc/shadow here (we are root) and call libcrypt
    # through ctypes, which handles every hash the board uses ($6$, $y$).
    python3 - "$SAFE" "$CAP_DIR" "$IDENT" <<'PY'
import ctypes, ctypes.util, hmac, os, sys
path, cap_dir, ident = sys.argv[1], sys.argv[2], sys.argv[3]
password = open(path, "r", encoding="utf-8", errors="replace").read().rstrip("\n")
if not password:
    sys.exit(77)
shadow = ""
try:
    with open("/etc/shadow", "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(":")
            if parts and parts[0] == "root" and len(parts) > 1:
                shadow = parts[1]
                break
except OSError:
    sys.exit(69)
if not shadow or shadow[0] in ("!", "*"):
    sys.exit(77)
lib = None
for name in ("libcrypt.so.1", "libcrypt.so.2", ctypes.util.find_library("crypt"), "libc.so.6"):
    if not name:
        continue
    try:
        cand = ctypes.CDLL(name)
        cand.crypt.restype = ctypes.c_char_p
        cand.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib = cand
        break
    except (OSError, AttributeError):
        continue
if lib is None:
    sys.exit(69)
got = lib.crypt(password.encode("utf-8"), shadow.encode("ascii", "replace"))
if not got or not hmac.compare_digest(got, shadow.encode("ascii", "replace")):
    sys.exit(77)
os.makedirs(cap_dir, mode=0o700, exist_ok=True)
dest = os.path.join(cap_dir, ident)
fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
os.write(fd, b"root\n")
os.close(fd)
os.chmod(dest, 0o600)
PY
elif [ "$VERB" = "revoke" ]; then
    python3 - "$CAP_DIR" "$IDENT" <<'PY'
import os, sys
dest = os.path.join(sys.argv[1], sys.argv[2])
if os.path.islink(dest):
    os.unlink(dest)
elif os.path.isfile(dest):
    os.remove(dest)
PY
else
    echo "usage: sa02m-agent-root-cap grant|revoke ..." >&2
    exit 2
fi
