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
for pkg in python3 python3-serial python3-yaml; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        log INFO "apt install $pkg"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >> "$LOG_FILE" 2>&1 || \
            log WARN "Не удалось установить $pkg"
    fi
done
# fallback: pip3 install pyserial pyyaml if apt packages unavailable
if ! python3 -c "import serial" 2>/dev/null; then
    log INFO "pip3 install pyserial"
    pip3 install --quiet pyserial 2>>"$LOG_FILE" || true
fi
if ! python3 -c "import yaml" 2>/dev/null; then
    log INFO "pip3 install pyyaml"
    pip3 install --quiet pyyaml 2>>"$LOG_FILE" || true
fi

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
SUDOERS_FILE="/etc/sudoers.d/sa02m-gateway"
if [ ! -f "$SUDOERS_FILE" ]; then
    log INFO "Создаю sudoers: $SUDOERS_FILE"
    cat > "$SUDOERS_FILE" <<'SUDOERS'
# SA-02m gateway CGI sudo rules
www-data ALL=(root) NOPASSWD: /usr/local/sbin/sa02m-gateway-config-apply.sh
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl start sa02m-serial-gateway
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl stop sa02m-serial-gateway
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl restart sa02m-serial-gateway
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl reload sa02m-serial-gateway
SUDOERS
    chmod 0440 "$SUDOERS_FILE"
fi

# ── config-apply helper ────────────────────────────────────────────────────
log INFO "Устанавливаю sa02m-gateway-config-apply.sh"
install -m 0755 -o root -g root \
    "$ETC_DIR/sa02m-gateway-config-apply.sh" \
    /usr/local/sbin/sa02m-gateway-config-apply.sh

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
log INFO "Устанавливаю systemd unit $SVC_NAME.service"
install -m 0644 -o root -g root \
    "$ETC_DIR/sa02m-serial-gateway.service" \
    "/lib/systemd/system/$SVC_NAME.service"

systemctl daemon-reload >> "$LOG_FILE" 2>&1

if systemctl is-enabled --quiet "$SVC_NAME" 2>/dev/null; then
    log INFO "Служба $SVC_NAME уже включена"
else
    log INFO "Включаю $SVC_NAME"
    systemctl enable "$SVC_NAME" >> "$LOG_FILE" 2>&1 || true
fi

# Не запускаем автоматически — пользователь включит нужные порты через веб
log INFO "Служба $SVC_NAME установлена (не запущена — настройте порты через веб-интерфейс)"
log INFO "Для запуска: systemctl start $SVC_NAME"

log INFO "=== [06] sa02m-serial-gateway установлен ==="
