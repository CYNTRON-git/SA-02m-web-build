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
    log WARN "$OPT_DIR отсутствует — пропускаю установку агрегатора"
    exit 0
fi

# ── Код агрегатора ─────────────────────────────────────────────────────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_DIR → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$OPT_DIR/" "$INSTALL_DIR/"
chown -R root:root "$INSTALL_DIR"
find "$INSTALL_DIR" -type d -exec chmod 0755 {} \;
find "$INSTALL_DIR" -type f -exec chmod 0644 {} \;
chmod 0755 "$INSTALL_DIR/aggregate.py" 2>/dev/null || true

# ── systemd unit + timer ───────────────────────────────────────────────────
log INFO "Устанавливаю sa02m-rs485-roster.service + .timer"
install -m 0644 -o root -g root "$ETC_SYSTEMD_DIR/sa02m-rs485-roster.service" /etc/systemd/system/sa02m-rs485-roster.service
install -m 0644 -o root -g root "$ETC_SYSTEMD_DIR/sa02m-rs485-roster.timer"   /etc/systemd/system/sa02m-rs485-roster.timer
systemctl daemon-reload

# Populate the cache once now so the UI has data before the first timer tick.
/usr/bin/python3 "$INSTALL_DIR/aggregate.py" >> "$LOG_FILE" 2>&1 \
    && log OK "первичный ростер /run/sa02m-rs485-roster.json создан" \
    || log INFO "первичный прогон агрегатора без данных (нет источников) — норма"

systemctl enable --now sa02m-rs485-roster.timer >> "$LOG_FILE" 2>&1 \
    && log OK "sa02m-rs485-roster.timer активен (тик 8 с)" \
    || log WARN "sa02m-rs485-roster.timer не активирован (journalctl -u sa02m-rs485-roster)"

log OK "=== [10] sa02m-rs485-roster установлен ==="
