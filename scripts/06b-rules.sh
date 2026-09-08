#!/bin/bash
# SA-02m  •  06b-rules.sh  —  on-board scenario engine
# Installs opt/sa02m-rules, empty JSON store, systemd unit.
# NOT added to sa02m-userspace-watchdog REQUIRED_PROCS.
# Service policy: capture BEFORE the unit file lands, apply AFTER
# (docs/contracts/installer-refresh-policy.md). Raw enable/restart here
# aborted refresh with set -e on an already-active unit (boards 1–2,
# 1.0.5.66 → 1.0.6.37): systemd returned non-zero while Restart=on-failure
# later brought the unit back to active.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [06b-rules] Установка sa02m-rules ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-rules"
UNIT_SRC="$BASE_DIR/etc/systemd/system"
INSTALL_DIR="/opt/sa02m-rules"
ETC_DIR="/etc/sa02m-rules"

python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt

install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0755 -o root -g root "$ETC_DIR"
if [ -d "$OPT_SRC" ]; then
    rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
        "$OPT_SRC/" "$INSTALL_DIR/"
fi
if [ ! -f "$ETC_DIR/scenarios.json" ]; then
    printf '%s\n' '{"scenarios":[],"library":"","runs":[],"notify_queue":[]}' \
        > "$ETC_DIR/scenarios.json"
    chmod 0644 "$ETC_DIR/scenarios.json"
fi

# Capture BEFORE (re)installing the unit — first-install vs restore-exact.
sa02m_svc_capture sa02m-rules.service
if [ -f "$UNIT_SRC/sa02m-rules.service" ]; then
    install -m 0644 "$UNIT_SRC/sa02m-rules.service" /etc/systemd/system/sa02m-rules.service
fi
systemctl daemon-reload
# sa02m stack: first install enable+start; refresh never-widens an operator stop.
sa02m_svc_apply sa02m-rules.service app on
# sa02m-cloud-control holds the same scenario-channel code in memory
# (sa02m_alice -> sa02m_rules.store from /opt/sa02m-rules): without a restart
# a scenario pushed from the cloud is handled by the OLD code (1.0.6.37
# acceptance: trigger/end were silently stripped). The unit is opt-in —
# restart only an active one, never start a stopped one.
sa02m_svc_restart_if_active sa02m-cloud-control.service
log INFO "=== [06b-rules] готово ==="
