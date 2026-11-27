#!/bin/bash
set -o pipefail  # catch masked failures in pipes; set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 10-rs485-roster.sh  •  Bus-free RS-485 roster aggregator + systemd timer
# Reads the MQTT bridge roster + the flasher scan cache, writes one normalized
# cache /run/sa02m-rs485-roster.json for status.cgi?part=rs485. Never opens a port.
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [10] Установка sa02m-rs485-roster ==="

BASE_DIR="$SCRIPT_DIR/.."
ETC_SYSTEMD_DIR="$BASE_DIR/etc/systemd"
OPT_DIR="$BASE_DIR/opt/sa02m-rs485-roster"
INSTALL_DIR="/opt/sa02m-rs485-roster"

if [ ! -d "$OPT_DIR" ]; then
    log ERR "Отсутствует каталог агрегатора $OPT_DIR"
    exit 1
fi

# ── Код агрегатора ─────────────────────────────────────────────────────────
sa02m_svc_capture sa02m-rs485-roster.timer
install -d -m 0755 -o root -g root "$INSTALL_DIR" || exit 1
log INFO "Копирую $OPT_DIR → $INSTALL_DIR"
for module in model_alias.py roster_files.py devices_tables.py providers.py polling_roster.py rs485_roster_cgi.py aggregate.py; do
    mode=0644
    [ "$module" != aggregate.py ] || mode=0755
    sa02m_atomic_install -m "$mode" -o root -g root "$OPT_DIR/$module" "$INSTALL_DIR/$module" || exit 1
done

# ── systemd unit + timer ───────────────────────────────────────────────────
log INFO "Устанавливаю sa02m-rs485-roster.service + .timer"
# Capture BEFORE the unit files land (first-install signal); an operator-stopped
# timer stays stopped (docs/contracts/installer-refresh-policy.md).
sa02m_atomic_install -m 0644 -o root -g root "$ETC_SYSTEMD_DIR/sa02m-rs485-roster.service" /etc/systemd/system/sa02m-rs485-roster.service || exit 1
sa02m_atomic_install -m 0644 -o root -g root "$ETC_SYSTEMD_DIR/sa02m-rs485-roster.timer"   /etc/systemd/system/sa02m-rs485-roster.timer || exit 1
systemctl daemon-reload || exit 1

sa02m_svc_apply sa02m-rs485-roster.timer app on
case "$SA02M_SVC_LAST_RESULT" in
    started|restarted|kept)
        systemctl is-active --quiet sa02m-rs485-roster.timer || exit 1
        /usr/local/libexec/sa02m-python-consumer sa02m-rs485-roster >> "$LOG_FILE" 2>&1 || exit 1
        log OK "sa02m-rs485-roster.timer активен (тик 8 с)" ;;
    enabled|left-inactive) : ;;  # rootfs or the existing stopped timer
    *) log WARN "sa02m-rs485-roster.timer не активирован (journalctl -u sa02m-rs485-roster)" ;;
esac

log OK "=== [10] sa02m-rs485-roster установлен ==="
