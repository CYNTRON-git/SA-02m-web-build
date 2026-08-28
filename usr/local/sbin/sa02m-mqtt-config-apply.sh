#!/bin/bash
# sa02m-mqtt-config-apply.sh — install the MQTT bridge YAML config as root.
# Usage: sa02m-mqtt-config-apply.sh <tmp_yaml_path>
# Called via sudo from mqtt_config.cgi (www-data).
#
# SECURITY (audit B1 class): the sudoers grant ends in `*`, so www-data chooses
# this argument and the helper used to install ANY caller-named file as the
# root-owned bridge config, with no path pattern and no symlink refusal. Not an
# injection on its own — but the same trust-the-caller shape as H1/H2, and the
# validation belongs HERE, at the privilege boundary, not in the CGI behind it.
#
# The path check alone was TOCTOU (security review 1.0.6.24, F2) — www-data owns
# that /tmp file and could swap in a symlink between `[ -L ]` and `install`,
# which follows symlinks on its source. Closed the same way as the sibling
# gateway helper, whose header carries the full reasoning and the stated
# residual: open ONCE, verify the opened inode through /proc/self/fd, and
# install from a private copy in a 0700 root-owned work dir. Deliberately
# duplicated rather than shared through a library — a privileged helper must
# hold on its own when invoked directly as root with any argv, which is also why
# src_path_ok lives in both files.
#
# Home: usr/local/sbin/ so the file deploys path-identically on every delivery
# path — see the sibling gateway helper's header for the etc/ rename trap this
# avoids. Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh.
set -euo pipefail

SRC="${1:-}"
DST=/etc/sa02m-modbus-mqtt.yaml
MAX_BYTES=1048576   # a bridge config is a few KB; refuse anything absurd

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

# The caller's own mktemp template is the contract: mqtt_config.cgi creates
# /tmp/sa02m-mqcfg-out.XXXXXX (mktemp's X set is [A-Za-z0-9]). The cheap FIRST
# refusal; what makes it hold at USE time is the open-once copy below.
src_path_ok() {
    local p="$1" base
    [ -n "$p" ] || return 1
    case "$p" in /tmp/*) : ;; *) return 1 ;; esac
    base="${p#/tmp/}"
    case "$base" in */*) return 1 ;; esac
    [[ "$base" =~ ^sa02m-mqcfg-out\.[A-Za-z0-9]{6}$ ]] || return 1
    [ -L "$p" ] && return 1
    [ -f "$p" ] || return 1
    return 0
}

if ! src_path_ok "$SRC"; then
    echo "usage: sa02m-mqtt-config-apply <tmp-yaml>; refusing source path — expected a regular, non-symlink /tmp/sa02m-mqcfg-out.XXXXXX file" >&2
    exit 2
fi

# Private work dir: 0700 and root-owned, so www-data cannot enter it or swap
# what is inside it.
WORK=$(mktemp -d /tmp/sa02m-mqcfg-apply.XXXXXX) || {
    echo "ERROR: cannot create the private work dir" >&2
    exit 1
}
chmod 0700 "$WORK"
SAFE_SRC="$WORK/mqtt.yaml"

# Open the caller's path ONCE and copy from that same file description, with
# the opened inode verified through /proc/self/fd — a symlink swapped in after
# src_path_ok resolves to its real target and fails the compare, a FIFO reads
# back as `pipe:[…]`, and `timeout` bounds a blocking open. Single-quoted
# script + argv: the filename is DATA, never shell source.
if ! timeout 10 bash -c '
    exec 8<"$1" || exit 2
    opened=$(readlink "/proc/self/fd/8") || exit 3
    [ "$opened" = "$1" ] || exit 4
    [ -f "/proc/self/fd/8" ] || exit 5
    head -c "$2" <&8
' _ "$SRC" "$MAX_BYTES" >"$SAFE_SRC"; then
    echo "ERROR: refusing source — at the moment it was opened it was not the plain regular file that was checked (symlink or FIFO swapped in), or the read timed out" >&2
    exit 2
fi
if [ "$(wc -c <"$SAFE_SRC")" -ge "$MAX_BYTES" ]; then
    echo "ERROR: MQTT bridge config is larger than $MAX_BYTES bytes — refusing" >&2
    exit 2
fi

install -m 0660 -o root -g www-data "$SAFE_SRC" "$DST"
sync
