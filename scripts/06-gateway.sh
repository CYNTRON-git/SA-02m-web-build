#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 06-gateway.sh  •  Установка sa02m-serial-gateway (RS-485→Ethernet шлюз)
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [06] Установка sa02m-serial-gateway ==="

BASE_DIR="$SCRIPT_DIR/.."
ETC_DIR="$BASE_DIR/etc"
OPT_SRC="$BASE_DIR/opt/sa02m-serial-gateway"
WEB_CGI="${WEB_ROOT:-/var/www/network_config}/cgi-bin"

INSTALL_DIR="/opt/sa02m-serial-gateway"
SVC_NAME="sa02m-serial-gateway"

# ── Python зависимости ────────────────────────────────────────────────────
sa02m_pkg_install_tier optional python3 python3-serial python3-yaml
# fallback: pip, если apt-версии нет (уже импортируемые — тихий no-op)
sa02m_pip_install serial pyserial
sa02m_pip_install yaml pyyaml

# ── Каталог установки ─────────────────────────────────────────────────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_SRC → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$OPT_SRC/" "$INSTALL_DIR/"
find "$INSTALL_DIR" -type f -name "*.py" -exec chmod 0755 {} \;

# ── /etc конфигурация ────────────────────────────────────────────────────
if [ ! -f /etc/sa02m-gateway.yaml ]; then
    log INFO "Создаю /etc/sa02m-gateway.yaml (все порты отключены)"
    install -m 0660 -o root -g www-data "$ETC_DIR/sa02m-gateway.yaml" /etc/sa02m-gateway.yaml
else
    log INFO "/etc/sa02m-gateway.yaml уже существует — оставляю без изменений"
fi

# ── sudo правило для CGI ──────────────────────────────────────────────────
# Устанавливается ЦЕЛИКОМ из коммитнутого etc/sudoers.d/sa02m-gateway (audit B1):
# прежний heredoc был под `[ ! -f ]`, поэтому на плате, где файл уже есть, пин
# не появлялся НИКОГДА — ни при refresh, ни при OTA. Коммитнутый файл сходится
# по построению и доезжает по всем трём путям доставки.
log INFO "Устанавливаю sudoers: /etc/sudoers.d/sa02m-gateway (целиком)"
sa02m_install_sudoers "$ETC_DIR/sudoers.d/sa02m-gateway" /etc/sudoers.d/sa02m-gateway

# ── config-apply helper ────────────────────────────────────────────────────
# Источник — usr/local/sbin/ (не etc/): путь установки совпадает с путём в
# репозитории, поэтому OTA и оффлайн-пакет кладут файл ровно туда, где его
# вызывают sudoers и gateway_config.cgi.
log INFO "Устанавливаю sa02m-gateway-config-apply.sh"
install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-gateway-config-apply.sh" \
    /usr/local/sbin/sa02m-gateway-config-apply.sh
sed -i 's/\r$//' /usr/local/sbin/sa02m-gateway-config-apply.sh

# ── CGI скрипты ──────────────────────────────────────────────────────────
log INFO "Устанавливаю CGI: gateway_config.cgi, gateway_status.cgi, gateway_ctrl.cgi"
for cgi in gateway_config.cgi gateway_status.cgi gateway_ctrl.cgi; do
    install -m 0755 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/cgi-bin/$cgi" \
        "$WEB_CGI/$cgi"
done

# ── Web assets ────────────────────────────────────────────────────────────
WEB_JS="${WEB_ROOT:-/var/www/network_config}/static/js"
log INFO "Устанавливаю gateway.js"
install -m 0644 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/static/js/gateway.js" \
    "$WEB_JS/gateway.js"

# ── index.html и app.js ────────────────────────────────────────────────────
log INFO "Обновляю index.html, app.js, main.css"
WEB_STATIC="${WEB_ROOT:-/var/www/network_config}/static"
install -m 0644 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/index.html" \
    "${WEB_ROOT:-/var/www/network_config}/index.html"
install -m 0644 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/static/js/app.js" \
    "$WEB_JS/app.js"
install -m 0644 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/static/css/main.css" \
    "$WEB_STATIC/css/main.css"

# ── systemd unit ──────────────────────────────────────────────────────────
# Capture prior state BEFORE (re)installing the unit — a RUNNING gateway must be
# restarted so it picks up the fresh .py just rsync'd (same stale-code class as
# the MQTT bridge); a stopped gateway is left stopped (never force-started).
sa02m_svc_capture "$SVC_NAME.service"
log INFO "Устанавливаю systemd unit $SVC_NAME.service"
install -m 0644 -o root -g root \
    "$ETC_DIR/sa02m-serial-gateway.service" \
    "/lib/systemd/system/$SVC_NAME.service"

systemctl daemon-reload >> "$LOG_FILE" 2>&1

# First install: enabled, not started (ports are off until configured in the
# web UI). Upgrade: prior state restored exactly, a running gateway restarted
# on fresh code.
sa02m_svc_apply "$SVC_NAME.service" app enabled
if [ "$SA02M_SVC_LAST_RESULT" = restarted ]; then
    log OK "Служба $SVC_NAME перезапущена на свежем коде (была активна)"
else
    log INFO "Служба $SVC_NAME установлена (не запущена — настройте порты через веб-интерфейс)"
    log INFO "Для запуска: systemctl start $SVC_NAME"
fi

log INFO "=== [06] sa02m-serial-gateway установлен ==="
