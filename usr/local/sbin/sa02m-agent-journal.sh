#!/bin/bash
# sa02m-agent-journal.sh — journalctl -u for one allow-listed unit, bounded lines.
# Usage: sa02m-agent-journal.sh <unit> <lines>
set -euo pipefail

# Same list as JOURNAL_UNITS in opt/sa02m-agent-api/sa02m_agent_api/ops.py.
unit_ok() {
    case "$1" in
        sa02m-agent-api|sa02m-rules|sa02m-modbus-mqtt|mosquitto|nodered|mplc4|codesyscontrol|\
        sa02m-alice-client|sa02m-alice-config|sa02m-cloud-control|sa02m-homekit|sa02m-homeconnect|\
        sa02m-flasher|sa02m-devices-api|sa02m-devices-logger|sa02m-telemetry|sa02m-serial-gateway|\
        nginx|fcgiwrap)
            return 0 ;;
    esac
    [[ "$1" =~ ^sa02m-user@[a-z0-9][a-z0-9-]{0,30}$ ]] || return 1
    return 0
}

lines_ok() {
    [[ "$1" =~ ^[0-9]+$ ]] || return 1
    [ "$1" -ge 1 ] && [ "$1" -le 200 ]
}

if ! unit_ok "${1:-}"; then
    echo "refusing unit" >&2
    exit 2
fi
if ! lines_ok "${2:-}"; then
    echo "refusing line count" >&2
    exit 2
fi

journalctl -u "$1" -n "$2" --no-pager -o cat
