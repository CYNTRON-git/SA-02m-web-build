#!/bin/bash
# 14-cyntron-devices.sh — build-host step, NOT an install.sh module.
#
# Copies the generated table generated/devices_tables.py from the shared device
# library https://github.com/CYNTRON-git/devices.git into every opt/ package
# that imports it. The packages must not import each other, so each importer
# gets its own byte-identical copy (the project rule for shared code: copy the
# generated file, never cross-import). The copies are committed; the device
# installers (scripts/05-mqtt.sh, scripts/10-rs485-roster.sh) and the OTA
# allow-list (opt/sa02m-*/) ship them from the tree, so the devices repo is
# never needed on a board or in CI.
#
# COPY ONLY: this script never runs the devices codegen (build_all.py) — the
# devices repo is owned elsewhere; regenerate there, then re-run this script.
#
# Source root, first match wins:
#   1. $CYNTRON_DEVICES_ROOT
#   2. /opt/src/devices
#   3. ../devices (a sibling clone next to this repo)
# A missing root or a missing generated/devices_tables.py is an ERROR (exit 1):
# a silent skip here would ship a stale table without anyone noticing.
#
# Idempotent: the target is written only when its content changes; re-running
# on an up-to-date tree leaves `git status` clean.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEVICES_GIT="https://github.com/CYNTRON-git/devices.git"

die() { echo "14-cyntron-devices: ERROR: $*" >&2; exit 1; }

if [ -n "${CYNTRON_DEVICES_ROOT:-}" ]; then
    ROOT="$CYNTRON_DEVICES_ROOT"
elif [ -d /opt/src/devices ]; then
    ROOT=/opt/src/devices
elif [ -d "$REPO_ROOT/../devices" ]; then
    ROOT="$(cd "$REPO_ROOT/../devices" && pwd)"
else
    die "devices repo not found (set CYNTRON_DEVICES_ROOT or clone $DEVICES_GIT next to this repo)"
fi

SRC="$ROOT/generated/devices_tables.py"
[ -f "$SRC" ] || die "$SRC is missing — run the devices codegen in that repo first"

PY=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || die "no python interpreter to validate the generated table"

# The copy must be a valid module exposing the tables the consumers read
# (bridge_mr02m_map._devices_aliases, rs485-roster model_alias.canonical_model).
"$PY" - "$SRC" <<'EOF' || die "generated table failed validation"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("devices_tables", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name in ("ALIASES", "MR02M_TYPE_NAMES", "IO_CAPS"):
    value = getattr(mod, name, None)
    if not isinstance(value, dict) or not value:
        sys.exit("%s: %s missing or empty" % (sys.argv[1], name))
EOF

# One copy per importing package — exactly the modules that `import devices_tables`.
DESTS=(
    "$REPO_ROOT/opt/sa02m-modbus-mqtt"
    "$REPO_ROOT/opt/sa02m-rs485-roster"
)

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
{
    echo "# generated, do not edit — copied from $DEVICES_GIT generated/devices_tables.py"
    echo "# by scripts/14-cyntron-devices.sh; refresh by re-running that script."
    # LF only (the repo's .gitattributes pins *.py to eol=lf).
    sed 's/\r$//' "$SRC"
} > "$TMP"

for dest in "${DESTS[@]}"; do
    [ -d "$dest" ] || die "destination package missing: $dest"
    target="$dest/devices_tables.py"
    if [ -f "$target" ] && cmp -s "$TMP" "$target"; then
        echo "up to date: ${target#$REPO_ROOT/}"
        continue
    fi
    install -m 0644 "$TMP" "$target"
    echo "copied: ${target#$REPO_ROOT/}"
done
