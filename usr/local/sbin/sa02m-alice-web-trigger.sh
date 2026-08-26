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
    # Every systemctl is bounded: the CGI calls this trigger SYNCHRONOUSLY
    # inside nginx's 20 s fastcgi budget on a shared 8-worker fcgiwrap — an
    # unbounded call on a wedged unit/dbus pins workers and 504s the request
    # (web-code-rigor "timeouts everywhere" floor).
    timeout 10 systemctl unmask sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl enable sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl restart sa02m-alice-client.service || true
    echo '{"ok":true,"action":"enable"}'
    ;;
  disable)
    timeout 10 systemctl stop sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl restart sa02m-alice-client.service >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"disable"}'
    ;;
  restart)
    # Binding edits: the running client's DeviceRegistry is built once and
    # MQTT subs are taken at connect — a restart is how changes apply. The
    # CGI gates this on client_enabled=true; on a disabled client it is a
    # harmless standby cycle (client exits 0). Bounded — see enable.
    timeout 10 systemctl restart sa02m-alice-client.service || true
    echo '{"ok":true,"action":"restart"}'
    ;;
  *)
    echo '{"ok":false,"error":"unknown_action"}'
    exit 1
    ;;
esac
