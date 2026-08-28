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
# Home: usr/local/sbin/ so the file deploys path-identically on every delivery
# path — see the sibling gateway helper's header for the etc/ rename trap this
# avoids. Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh.
set -euo pipefail

SRC="${1:-}"
DST=/etc/sa02m-modbus-mqtt.yaml

# The caller's own mktemp template is the contract: mqtt_config.cgi creates
# /tmp/sa02m-mqcfg-out.XXXXXX (mktemp's X set is [A-Za-z0-9]).
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

install -m 0660 -o root -g www-data "$SRC" "$DST"
sync
