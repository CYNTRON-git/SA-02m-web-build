#!/bin/bash
# sa02m-gateway-config-apply.sh — install validated gateway config and optionally restart.
# Usage: sa02m-gateway-config-apply.sh <tmp_yaml_path> [restart]
# Called via sudo from gateway_config.cgi.
set -euo pipefail

TMP_SRC="${1:-}"
CONFIG_DST="/etc/sa02m-gateway.yaml"

if [[ -z "$TMP_SRC" || ! -f "$TMP_SRC" ]]; then
    echo "ERROR: source file not provided or missing" >&2
    exit 1
fi

# Basic sanity check: file must be valid YAML with a 'ports' key
python3 - <<PYEOF
import sys, yaml
try:
    with open("$TMP_SRC") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or 'ports' not in data:
        sys.exit("missing 'ports' key")
except Exception as e:
    sys.exit(str(e))
PYEOF

install -m 0660 -o root -g www-data "$TMP_SRC" "$CONFIG_DST"

# Reload config via SIGHUP without full restart
if systemctl is-active --quiet sa02m-serial-gateway 2>/dev/null; then
    systemctl reload sa02m-serial-gateway 2>/dev/null || true
fi
