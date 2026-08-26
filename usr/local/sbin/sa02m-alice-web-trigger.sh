#!/bin/bash
# Privileged helper for Alice CGI (enable/disable client unit).
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  enable)
    # Do not force client_enabled here — CGI/Python already wrote the conf.
    # Defensive unmask: cmd_stop's mask SKIPS this unit (real fragment in
    # /etc/systemd/system — unit_can_mask declines), so this only repairs a
    # unit left masked by hand or by legacy states; harmless otherwise.
    systemctl unmask sa02m-alice-client.service >/dev/null 2>&1 || true
    systemctl enable sa02m-alice-client.service >/dev/null 2>&1 || true
    systemctl restart sa02m-alice-client.service
    echo '{"ok":true,"action":"enable"}'
    ;;
  disable)
    systemctl stop sa02m-alice-client.service >/dev/null 2>&1 || true
    systemctl restart sa02m-alice-client.service >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"disable"}'
    ;;
  restart)
    # Binding edits: the running client's DeviceRegistry is built once and
    # MQTT subs are taken at connect — a restart is how changes apply. The
    # CGI gates this on client_enabled=true; on a disabled client it is a
    # harmless standby cycle (client exits 0).
    systemctl restart sa02m-alice-client.service
    echo '{"ok":true,"action":"restart"}'
    ;;
  *)
    echo '{"ok":false,"error":"unknown_action"}'
    exit 1
    ;;
esac
