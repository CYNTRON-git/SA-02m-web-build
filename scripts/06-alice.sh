#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  06-alice.sh  —  Yandex Alice controller (clean-room)
# Устанавливает opt/sa02m-alice, конфиги, systemd units, CGI и sudoers.
# По умолчанию client_enabled=false — клиент выходит 0 до включения в UI.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [06-alice] Установка sa02m-alice ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-alice"
ETC_SRC="$BASE_DIR/etc/sa02m-alice"
UNIT_SRC="$BASE_DIR/etc/systemd/system"
WEB_CGI="${WEB_ROOT:-/var/www/network_config}/cgi-bin"
INSTALL_DIR="/opt/sa02m-alice"

# ── Python deps (socketio optional until client_enabled) ───────────────────
for pkg in python3 python3-paho-mqtt python3-yaml; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        log INFO "apt install $pkg"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >>"$LOG_FILE" 2>&1 || \
            log WARN "Не удалось установить $pkg"
    fi
done
if ! python3 -c "import socketio" 2>/dev/null; then
    log INFO "pip3 install python-socketio (client)"
    pip3 install --break-system-packages --quiet "python-socketio[client]" >>"$LOG_FILE" 2>&1 || \
        log WARN "python-socketio не установлен — клиент сообщит missing_deps при enable"
fi

# ── Package tree ───────────────────────────────────────────────────────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_SRC → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
    "$OPT_SRC/" "$INSTALL_DIR/"
# Keep tests on device for optional host-like checks
if [ -d "$OPT_SRC/tests" ]; then
    rsync -a --delete --exclude '__pycache__' "$OPT_SRC/tests/" "$INSTALL_DIR/tests/"
fi

# ── Runtime dirs ───────────────────────────────────────────────────────────
install -d -m 0750 -o root -g root /etc/sa02m-alice
install -d -m 0750 -o root -g root /var/lib/sa02m-alice
install -d -m 0755 -o root -g root /run/sa02m-alice

for f in sa02m-alice-client.conf sa02m-alice-devices.conf sa02m-alice-server.conf; do
    if [ ! -f "/etc/sa02m-alice/$f" ]; then
        install -m 0640 -o root -g www-data "$ETC_SRC/$f" "/etc/sa02m-alice/$f"
        log OK "создан /etc/sa02m-alice/$f"
    else
        log INFO "/etc/sa02m-alice/$f уже есть — не перезаписываю"
    fi
done
# www-data needs read on conf for CGI status; write via Python as root through trigger/API
chmod 0640 /etc/sa02m-alice/*.conf 2>/dev/null || true
chgrp www-data /etc/sa02m-alice /etc/sa02m-alice/*.conf 2>/dev/null || true
# Allow www-data to update devices/client via API running as www-data in CGI
chmod 0660 /etc/sa02m-alice/sa02m-alice-client.conf \
           /etc/sa02m-alice/sa02m-alice-devices.conf 2>/dev/null || true

# ── systemd ────────────────────────────────────────────────────────────────
install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-client.service" /etc/systemd/system/
install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-config.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sa02m-alice-client.service >/dev/null 2>&1 || true
systemctl enable sa02m-alice-config.service >/dev/null 2>&1 || true
# Start config API (localhost); client stays in standby (exit 0) until enabled
systemctl restart sa02m-alice-config.service >/dev/null 2>&1 || \
    log WARN "sa02m-alice-config не запущен"
systemctl restart sa02m-alice-client.service >/dev/null 2>&1 || true

# ── Privileged CGI helper + sudoers ────────────────────────────────────────
install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-alice-web-trigger.sh" \
    /usr/local/sbin/sa02m-alice-web-trigger.sh
sed -i 's/\r$//' /usr/local/sbin/sa02m-alice-web-trigger.sh

SUDOERS_FILE="/etc/sudoers.d/sa02m-alice"
cat >"$SUDOERS_FILE" <<'SUDOERS'
# SA-02m Alice CGI
www-data ALL=(root) NOPASSWD: /usr/local/sbin/sa02m-alice-web-trigger.sh
SUDOERS
chmod 0440 "$SUDOERS_FILE"
sed -i 's/\r$//' "$SUDOERS_FILE"
if visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    log OK "sudoers sa02m-alice OK"
else
    log WARN "visudo отклонил $SUDOERS_FILE"
fi

# ── CGI ────────────────────────────────────────────────────────────────────
for cgi in sa02m_alice_api.cgi sa02m_alice_topics.cgi; do
    install -m 0755 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/cgi-bin/$cgi" \
        "$WEB_CGI/$cgi"
    sed -i 's/\r$//' "$WEB_CGI/$cgi"
done

# ── WWW assets (alice.js may also arrive via www-only update) ──────────────
WEB_ROOT_DIR="${WEB_ROOT:-/var/www/network_config}"
if [ -f "$BASE_DIR/www/network_config/static/js/app/alice.js" ]; then
    install -d -m 0755 "$WEB_ROOT_DIR/static/js/app"
    install -m 0644 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/static/js/app/alice.js" \
        "$WEB_ROOT_DIR/static/js/app/alice.js"
fi

log OK "=== [06-alice] sa02m-alice установлен (client_enabled=false по умолчанию) ==="
log INFO "UI: Управление → Яндекс Алиса. Документация: docs/ALICE_INTEGRATION.md"
