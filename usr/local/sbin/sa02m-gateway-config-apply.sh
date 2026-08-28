#!/bin/bash
# sa02m-gateway-config-apply.sh — install a validated gateway config as root.
# Usage: sa02m-gateway-config-apply.sh <tmp_yaml_path>
# Called via sudo from gateway_config.cgi (www-data).
#
# SECURITY (audit B1 / finding H1): this helper runs as root with an argument
# www-data chooses, so it validates that argument ITSELF. It used to build the
# Python validator with an UNQUOTED heredoc (`python3 - <<PYEOF` … open("$TMP_SRC")),
# which made the caller-supplied path Python SOURCE — a filename containing
# `");__import__("os").system("id");#` executed as root. The quoted delimiter
# plus sys.argv below is the idiom every other privileged helper here uses.
#
# The sudoers argument pin is DEFENCE IN DEPTH, never the fix: sudo matches
# command arguments with fnmatch and a `*` is documented to match `/` as well,
# so a prefix pin does not bound a path on its own. The guard below must hold
# even when the helper is invoked directly as root with any argv.
#
# The path check alone was TOCTOU (security review 1.0.6.24, F2): www-data owns
# that /tmp file and could replace it with a symlink AFTER `[ -L ]`/`[ -f ]` and
# BEFORE `install`, which follows symlinks on its source — so any root-readable
# file (/etc/shadow being the obvious one, since the root password gates
# cmd_exec.cgi's root mode) landed in the destination as 0660 root:www-data.
# Closed by never touching the caller's path twice: the file is opened ONCE and
# the copy is taken from THAT file description, with the opened inode verified
# through /proc/self/fd before a byte is read. Everything downstream — the YAML
# validation and the install — runs on a private copy inside a 0700 root-owned
# work dir www-data cannot enter, so validated bytes and installed bytes are the
# same bytes. The bounded `timeout` also removes the FIFO-swap hang the old
# `install` had. Residual, stated rather than papered over: a hard link to a
# file www-data can already read is indistinguishable from the real file — it
# grants nothing new, and fs.protected_hardlinks (Debian default) bars linking
# to anything it cannot read.
#
# Home: usr/local/sbin/ so the file deploys path-identically on every delivery
# path (fresh install, OTA, offline, refresh). It previously lived in etc/,
# where the OTA rename table strips `.sh` — an OTA-delivered fix would have
# landed at /usr/local/sbin/sa02m-gateway-config-apply while sudo and the CGI
# call the `.sh` path, leaving the vulnerable file untouched (the 1.0.6.11 B1
# deploy gap). Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh.
set -euo pipefail

TMP_SRC="${1:-}"
CONFIG_DST="/etc/sa02m-gateway.yaml"
MAX_BYTES=1048576   # a gateway config is a few KB; refuse anything absurd

WORK=""
# `return 0` is load-bearing: an EXIT trap whose last command fails makes THAT
# status the script's exit status, so a bare `[ -n "$WORK" ] && rm` turned every
# `exit 2` refusal into an exit 1 (caught by running the refusal matrix, not by
# reading). The documented refusal code must survive the cleanup.
cleanup() {
    if [ -n "$WORK" ]; then rm -rf -- "$WORK"; fi
    return 0
}
trap cleanup EXIT

# The caller's own mktemp template is the contract: gateway_config.cgi creates
# /tmp/sa02m-gwcfg-out.XXXXXX (mktemp's X set is [A-Za-z0-9]). Anything else —
# another directory, a traversal, a symlink, a non-regular file — is refused.
# This is the cheap FIRST refusal; what makes it hold at USE time is the
# open-once copy below (see the header on TOCTOU).
src_path_ok() {
    local p="$1" base
    [ -n "$p" ] || return 1
    case "$p" in /tmp/*) : ;; *) return 1 ;; esac
    base="${p#/tmp/}"
    case "$base" in */*) return 1 ;; esac
    [[ "$base" =~ ^sa02m-gwcfg-out\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

if ! src_path_ok "$TMP_SRC"; then
    echo "ERROR: refusing source path — expected a regular, non-symlink /tmp/sa02m-gwcfg-out.XXXXXX file" >&2
    exit 2
fi

# Private work dir: 0700 and root-owned, so www-data cannot enter it, cannot
# swap what is inside it, and cannot race anything that follows.
WORK=$(mktemp -d /tmp/sa02m-gwcfg-apply.XXXXXX) || {
    echo "ERROR: cannot create the private work dir" >&2
    exit 1
}
chmod 0700 "$WORK"
SAFE_SRC="$WORK/gateway.yaml"

# Open the caller's path ONCE, prove the thing actually opened is the regular
# file we checked (readlink on /proc/self/fd resolves a swapped-in symlink to
# its real target and a FIFO to `pipe:[…]`, both of which fail the compare),
# then read the bytes from that same file description. `timeout` bounds a
# blocking open. The script is single-quoted and the path arrives as argv, so
# the filename is DATA here, never shell source.
if ! timeout 10 bash -c '
    exec 8<"$1" || exit 2
    opened=$(readlink "/proc/self/fd/8") || exit 3
    [ "$opened" = "$1" ] || exit 4
    [ -f "/proc/self/fd/8" ] || exit 5
    head -c "$2" <&8
' _ "$TMP_SRC" "$MAX_BYTES" >"$SAFE_SRC"; then
    echo "ERROR: refusing source — at the moment it was opened it was not the plain regular file that was checked (symlink or FIFO swapped in), or the read timed out" >&2
    exit 2
fi
if [ "$(wc -c <"$SAFE_SRC")" -ge "$MAX_BYTES" ]; then
    echo "ERROR: gateway config is larger than $MAX_BYTES bytes — refusing" >&2
    exit 2
fi

# Sanity check: the file must be valid YAML carrying a 'ports' key. Quoted
# delimiter + argv — the path is DATA here, never source. Validates the PRIVATE
# copy, so what was validated is what gets installed.
if ! python3 - "$SAFE_SRC" <<'PYEOF'
import sys

import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception as e:
    sys.exit(str(e))
if not isinstance(data, dict) or "ports" not in data:
    sys.exit("missing 'ports' key")
PYEOF
then
    echo "ERROR: gateway config failed validation" >&2
    exit 1
fi

install -m 0660 -o root -g www-data "$SAFE_SRC" "$CONFIG_DST"

# Reload config via SIGHUP without full restart
if systemctl is-active --quiet sa02m-serial-gateway 2>/dev/null; then
    systemctl reload sa02m-serial-gateway 2>/dev/null || true
fi
