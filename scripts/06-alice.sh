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
# Capture prior enable-state BEFORE (re)installing the units — the only reliable
# first-install signal (a freshly-installed unit already reads "disabled").
_alice_prev_cfg=$(systemctl is-enabled sa02m-alice-config.service 2>/dev/null || true)
_alice_prev_cli=$(systemctl is-enabled sa02m-alice-client.service 2>/dev/null || true)

install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-client.service" /etc/systemd/system/
install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-alice-config.service" /etc/systemd/system/
systemctl daemon-reload

# Both Alice services ship DISABLED + STOPPED by default — Operator opt-in
# policy, same philosophy as the CODESYS install-only pattern
# (docs/contracts/kernel-conditional-services.md). But this must be idempotent:
# a device where the operator later ENABLED Alice via the web UI must NOT be
# forcibly re-disabled on a re-install (the upgrade path). So the OFF default is
# applied ONLY on first install (prior state unknown/absent); an operator's
# opt-in is preserved and its code refreshed.
_alice_apply_default() {
    local svc=$1 prev=$2
    if [ -z "$prev" ] || [ "$prev" = unknown ]; then
        systemctl disable "$svc" >/dev/null 2>&1 || true
        systemctl stop "$svc" >/dev/null 2>&1 || true
        log INFO "$svc: выключен по умолчанию (opt-in через веб-интерфейс)"
    elif [ "$prev" = enabled ]; then
        systemctl enable "$svc" >/dev/null 2>&1 || true
        systemctl restart "$svc" >/dev/null 2>&1 || true
        log INFO "$svc: включён оператором — сохраняем (перезапуск на свежем коде)"
    else
        systemctl stop "$svc" >/dev/null 2>&1 || true
        log INFO "$svc: оставлен выключенным ($prev) — состояние задано оператором"
    fi
}
_alice_apply_default sa02m-alice-config.service "$_alice_prev_cfg"
_alice_apply_default sa02m-alice-client.service "$_alice_prev_cli"

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
