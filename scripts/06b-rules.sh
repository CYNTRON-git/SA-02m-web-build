#!/bin/bash
# SA-02m  •  06b-rules.sh  —  on-board scenario engine
# Installs opt/sa02m-rules, empty JSON store, systemd unit.
# NOT added to sa02m-userspace-watchdog REQUIRED_PROCS.
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
if [ -f "$UNIT_SRC/sa02m-rules.service" ]; then
    install -m 0644 "$UNIT_SRC/sa02m-rules.service" /etc/systemd/system/sa02m-rules.service
fi
systemctl daemon-reload
systemctl enable sa02m-rules.service
systemctl restart sa02m-rules.service || systemctl start sa02m-rules.service
# sa02m-cloud-control держит в памяти тот же код сценарного канала
# (sa02m_alice → sa02m_rules.store из /opt/sa02m-rules): без рестарта push
# сценария из облака обрабатывается старым кодом (приёмка 1.0.6.37: молча
# срезались trigger/end). Юнит opt-in — только рестарт активного, никогда
# не стартовать остановленный.
sa02m_svc_restart_if_active sa02m-cloud-control.service
log INFO "=== [06b-rules] готово ==="
