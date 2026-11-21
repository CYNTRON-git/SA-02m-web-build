#!/bin/bash
# Local-only collector entry point; not installed under the web root.
# Metrics and block policy have one home in status.cgi.
set -o pipefail
case "${1:-}" in
    priority|storage|time|uptime|network|load|system|services|hardware|rs485|blocks|coordinator) part=$1 ;;
    *) exit 64 ;;
esac
# Cap even an unexpectedly noisy subprocess; stdout is a RAM temporary file.
ulimit -f 512
unset QUERY_STRING HTTP_COOKIE REQUEST_METHOD GATEWAY_INTERFACE
# shellcheck disable=SC1091
. /var/www/network_config/cgi-bin/status.cgi
STATUS_PART=$part
if [[ "$part" == coordinator ]]; then
    status_port_coordinator
    status_emit_snapshot "{\"port_coordinator\":$STATUS_PORT_COORDINATOR}" "$EPOCHSECONDS" 6
    exit 0
fi
if [[ "$part" != priority && "$part" != blocks && "$part" != hardware ]] && ! status_block_enabled "$part"; then
    printf '{"_status_disabled":true}\n'
    exit 0
fi
status_emit_live "build_${part}_json"
