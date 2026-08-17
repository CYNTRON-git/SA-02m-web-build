#!/bin/bash
# Privileged helper for Alice CGI (enable/disable client unit).
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  enable)
    # Do not force client_enabled here — CGI/Python already wrote the conf.
    systemctl enable sa02m-alice-client.service >/dev/null 2>&1 || true
    systemctl restart sa02m-alice-client.service
    echo '{"ok":true,"action":"enable"}'
    ;;
  disable)
    systemctl stop sa02m-alice-client.service >/dev/null 2>&1 || true
    systemctl restart sa02m-alice-client.service >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"disable"}'
    ;;
  *)
    echo '{"ok":false,"error":"unknown_action"}'
    exit 1
    ;;
esac
