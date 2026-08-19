#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 11-devices.sh  •  вкладка «Устройства» (ДТВ / СЭ-02м-3): API + logger
#   opt/sa02m-devices → /opt/sa02m-devices
#   systemd: sa02m-devices-api, sa02m-devices-logger
#   nginx: /api/devices* → 127.0.0.1:8765 (из etc/nginx/network_config.conf)
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
check_root

REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$REPO_ROOT/opt/sa02m-devices"
ETC_DIR="$REPO_ROOT/etc"
: "${PORT:=9999}"
: "${WEB_ROOT:=/var/www/network_config}"

if [ ! -d "$OPT_SRC/sa02m_devices" ]; then
    log ERR "Нет $OPT_SRC/sa02m_devices — пропуск"
    exit 1
fi

log INFO "Установка /opt/sa02m-devices"
install -d -m 0755 /opt/sa02m-devices
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
        "$OPT_SRC/" /opt/sa02m-devices/
else
    find /opt/sa02m-devices -mindepth 1 -delete 2>/dev/null || true
    cp -a "$OPT_SRC/." /opt/sa02m-devices/
fi
find /opt/sa02m-devices -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
chmod 755 /opt/sa02m-devices/sa02m_devices_api.py /opt/sa02m-devices/sa02m_devices_logger.py
sed -i 's/\r$//' /opt/sa02m-devices/sa02m_devices_api.py \
    /opt/sa02m-devices/sa02m_devices_logger.py 2>/dev/null || true
log OK "/opt/sa02m-devices установлен"

if [ -f "$ETC_DIR/default/sa02m-devices" ]; then
    install -m 644 "$ETC_DIR/default/sa02m-devices" /etc/default/sa02m-devices
    sed -i 's/\r$//' /etc/default/sa02m-devices
fi

if [ -f "$ETC_DIR/tmpfiles.d/sa02m-devices.conf" ]; then
    install -m 644 "$ETC_DIR/tmpfiles.d/sa02m-devices.conf" /etc/tmpfiles.d/sa02m-devices.conf
    sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-devices.conf
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-devices.conf || true
    fi
fi
install -d -m 0755 /var/lib/sa02m-stand

# Capture BEFORE the unit files land (first-install signal); operator stops
# survive the upgrade (docs/contracts/installer-refresh-policy.md).
sa02m_svc_capture sa02m-devices-api.service sa02m-devices-logger.service
for unit in sa02m-devices-api.service sa02m-devices-logger.service; do
    if [ -f "$ETC_DIR/systemd/$unit" ]; then
        install -m 644 "$ETC_DIR/systemd/$unit" "/etc/systemd/system/$unit"
        sed -i 's/\r$//' "/etc/systemd/system/$unit"
    fi
done
systemctl daemon-reload
_DEV_OK=1
for unit in sa02m-devices-api.service sa02m-devices-logger.service; do
    sa02m_svc_apply "$unit" app on
    case "$SA02M_SVC_LAST_RESULT" in
        started|restarted|kept|left-inactive|left-masked) : ;;
        *) _DEV_OK=0 ;;
    esac
done
if [ "$_DEV_OK" = 1 ]; then
    log OK "sa02m-devices-api + logger: состояние применено"
else
    log WARN "не удалось запустить sa02m-devices-* — см. journalctl -u sa02m-devices-api"
fi

# nginx proxy /api/devices* (если в репо есть полный conf)
if [ -f "$ETC_DIR/nginx/network_config.conf" ]; then
    sed "s|__PORT__|$PORT|g; s|__WEB_ROOT__|$WEB_ROOT|g" \
        "$ETC_DIR/nginx/network_config.conf" \
        > /etc/nginx/sites-available/network_config
    rm -f /etc/nginx/sites-enabled/network_config \
        /etc/nginx/sites-enabled/000-sa02m-network_config
    ln -sf /etc/nginx/sites-available/network_config \
        /etc/nginx/sites-enabled/000-sa02m-network_config
    if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx \
            && log OK "nginx: /api/devices* → :8765" \
            || log WARN "nginx reload не удался"
    else
        log WARN "nginx -t failed после обновления network_config"
    fi
fi
