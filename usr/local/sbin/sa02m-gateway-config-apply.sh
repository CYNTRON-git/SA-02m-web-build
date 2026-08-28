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
# Home: usr/local/sbin/ so the file deploys path-identically on every delivery
# path (fresh install, OTA, offline, refresh). It previously lived in etc/,
# where the OTA rename table strips `.sh` — an OTA-delivered fix would have
# landed at /usr/local/sbin/sa02m-gateway-config-apply while sudo and the CGI
# call the `.sh` path, leaving the vulnerable file untouched (the 1.0.6.11 B1
# deploy gap). Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh.
set -euo pipefail

TMP_SRC="${1:-}"
CONFIG_DST="/etc/sa02m-gateway.yaml"

# The caller's own mktemp template is the contract: gateway_config.cgi creates
# /tmp/sa02m-gwcfg-out.XXXXXX (mktemp's X set is [A-Za-z0-9]). Anything else —
# another directory, a traversal, a symlink, a non-regular file — is refused.
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

# Sanity check: the file must be valid YAML carrying a 'ports' key. Quoted
# delimiter + argv — the path is DATA here, never source.
if ! python3 - "$TMP_SRC" <<'PYEOF'
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

install -m 0660 -o root -g www-data "$TMP_SRC" "$CONFIG_DST"

# Reload config via SIGHUP without full restart
if systemctl is-active --quiet sa02m-serial-gateway 2>/dev/null; then
    systemctl reload sa02m-serial-gateway 2>/dev/null || true
fi
