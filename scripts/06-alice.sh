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
# Probe the IMPORT first: dpkg -s misses a pip-installed module living at
# /usr/local/lib/pythonX/dist-packages (this retried paho even when already
# importable). Already-satisfied ⇒ skip silently. Bounded/offline-aware via
# lib.sh (never hangs on dead mirrors, never aborts under `set -e`).
if ! dpkg -s python3 >/dev/null 2>&1; then
    sa02m_pkg_install_tier optional python3
fi
python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt
python3 -c "import yaml"      2>/dev/null || sa02m_pkg_install_tier optional python3-yaml
sa02m_pip_install socketio "python-socketio[client]"

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
# 0770: the CGI (www-data) writes client/devices confs via atomic tmp+rename,
# which needs WRITE on the dir, not just on the conf files.
install -d -m 0770 -o root -g www-data /etc/sa02m-alice
# State dir is www-data-owned: the enroll write (device key/cert, pending
# claim) runs as www-data inside sa02m_alice_api.cgi. Idempotent migration:
# install -d re-asserts owner/mode on an existing dir, file contents untouched.
install -d -m 0700 -o www-data -g www-data /var/lib/sa02m-alice
install -d -m 0755 -o root -g root /run/sa02m-alice
# Boot-persistent home for both dirs (donor cleanup wipes /var/lib state).
install -m 0644 -o root -g root \
    "$BASE_DIR/etc/tmpfiles.d/sa02m-alice.conf" /etc/tmpfiles.d/sa02m-alice.conf
sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-alice.conf
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-alice.conf 2>/dev/null || true
fi

for f in sa02m-alice-client.conf sa02m-alice-devices.conf sa02m-alice-server.conf; do
    if [ ! -f "/etc/sa02m-alice/$f" ]; then
        install -m 0640 -o root -g www-data "$ETC_SRC/$f" "/etc/sa02m-alice/$f"
        log OK "создан /etc/sa02m-alice/$f"
    else
        log INFO "/etc/sa02m-alice/$f уже есть — не перезаписываю"
    fi
done
# www-data reads AND writes client/devices confs directly (CGI atomic write —
# hence the 0770 dir above); server.conf stays 0640 root:www-data read-only.
chmod 0640 /etc/sa02m-alice/*.conf 2>/dev/null || true
chgrp www-data /etc/sa02m-alice /etc/sa02m-alice/*.conf 2>/dev/null || true
# Allow www-data to update devices/client via API running as www-data in CGI
chmod 0660 /etc/sa02m-alice/sa02m-alice-client.conf \
           /etc/sa02m-alice/sa02m-alice-devices.conf 2>/dev/null || true

# ── systemd ────────────────────────────────────────────────────────────────
# Capture prior state BEFORE (re)installing the units — the only reliable
# first-install signal (a freshly-installed unit already reads "disabled").
# Both Alice services ship DISABLED + STOPPED by default (`app off`) — Operator
# opt-in policy, same philosophy as the CODESYS install-only pattern
# (docs/contracts/kernel-conditional-services.md). The OFF default applies ONLY
# on first install; an operator's opt-in (enabled and/or running) is preserved
# and a running client is restarted on the fresh code
# (docs/contracts/installer-refresh-policy.md).
sa02m_svc_capture sa02m-alice-config.service sa02m-alice-client.service

install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-client.service" /etc/systemd/system/
install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-config.service" /etc/systemd/system/
systemctl daemon-reload

sa02m_svc_apply sa02m-alice-config.service app off
sa02m_svc_apply sa02m-alice-client.service app off

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
sa02m_harden_sudoers "$SUDOERS_FILE"

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

log OK "=== [06-alice] sa02m-alice установлен (обе службы выключены по умолчанию) ==="
log INFO "sa02m-alice-config и sa02m-alice-client: opt-in через веб-интерфейс (не запускаются сами)"
log INFO "UI: Управление → Яндекс Алиса. Документация: docs/ALICE_INTEGRATION.md"
