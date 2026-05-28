#!/bin/bash
# Install MQTT bridge YAML config from a temp file (root only).
set -euo pipefail
SRC=${1:-}
DST=/etc/sa02m-modbus-mqtt.yaml
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
    echo "usage: sa02m-mqtt-config-apply <tmp-yaml>" >&2
    exit 1
fi
install -m 0660 -o root -g www-data "$SRC" "$DST"
sync
