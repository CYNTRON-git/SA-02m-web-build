#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Обновление только веб-файлов на устройстве (без сброса htpasswd и без
# полного scripts/03-webserver.sh). Запуск на СА-02м из корня репозитория:
#   sudo bash scripts/update-www-only.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

: "${WEB_ROOT:=/var/www/network_config}"
WWW_DIR="$SCRIPT_DIR/../www/network_config"

if [ ! -d "$WWW_DIR" ]; then
    log ERR "Нет каталога $WWW_DIR (ожидается структура репозитория с www/network_config)"
    exit 1
fi

log INFO "Копирование $WWW_DIR → $WEB_ROOT"
mkdir -p "$WEB_ROOT/cgi-bin" "$WEB_ROOT/static/css" "$WEB_ROOT/static/js"
cp -a "$WWW_DIR/." "$WEB_ROOT/"

REPO_ROOT="$SCRIPT_DIR/.."
STATEDIR=/var/lib/sa02m-web-build
if mkdir -p "$STATEDIR" 2>/dev/null; then
    if [ -d "$REPO_ROOT/.git" ] && command -v git >/dev/null 2>&1; then
        if c="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)"; then
            printf '%s\n' "$c" >"$STATEDIR/deployed_commit"
            date -u +%Y-%m-%dT%H:%M:%SZ >"$STATEDIR/deployed_at"
            chmod 644 "$STATEDIR/deployed_commit" "$STATEDIR/deployed_at" 2>/dev/null || true
        fi
    else
        log WARN "Нет git в $REPO_ROOT — в deployed_commit записана пометка по APP_VERSION или unknown"
        appver=""
        if [ -f "$WWW_DIR/static/js/app.js" ]; then
            appver=$(sed -n "s/^const APP_VERSION = '\\([^']*\\)'.*/\\1/p" "$WWW_DIR/static/js/app.js" | head -1) || true
        fi
        if [ -n "$appver" ]; then
            printf 'app-%s\n' "$appver" >"$STATEDIR/deployed_commit"
        else
            printf '%s\n' unknown >"$STATEDIR/deployed_commit"
        fi
        date -u +%Y-%m-%dT%H:%M:%SZ >"$STATEDIR/deployed_at"
        chmod 644 "$STATEDIR/deployed_commit" "$STATEDIR/deployed_at" 2>/dev/null || true
    fi
fi

if [ -f /usr/local/lib/sa02m-web-build-lib.sh ]; then
    # shellcheck disable=SC1091
    . /usr/local/lib/sa02m-web-build-lib.sh
    REPO_ROOT="$SCRIPT_DIR/.."
    if type sync_web_build_conf_from_deploy >/dev/null 2>&1; then
        sync_web_build_conf_from_deploy && log OK "sa02m_web_build.conf синхронизирован с веткой/версией"
    fi
fi

find "$WEB_ROOT/cgi-bin" -name '*.cgi' -exec chmod 755 {} \;
find "$WEB_ROOT/static" \( -name '*.css' -o -name '*.js' -o -name '*.svg' \) -exec chmod 644 {} \;
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html" 2>/dev/null || true
chown -R www-data:www-data "$WEB_ROOT"

if [ -f "$SCRIPT_DIR/../etc/sa02m-web-update-check.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-update-check.sh" /usr/local/sbin/sa02m-web-update-check
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-update-check
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-build-lib.sh" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-web-build-lib.sh" /usr/local/lib/sa02m-web-build-lib.sh
    sed -i 's/\r$//' /usr/local/lib/sa02m-web-build-lib.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-update-apply.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-update-apply.sh" /usr/local/sbin/sa02m-web-update-apply
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-update-apply
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-auth-lib.sh" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-web-auth-lib.sh" /usr/local/lib/sa02m-web-auth-lib.sh
    sed -i 's/\r$//' /usr/local/lib/sa02m-web-auth-lib.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-repair-web-env.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-repair-web-env.sh" /usr/local/sbin/sa02m-repair-web-env
    sed -i 's/\r$//' /usr/local/sbin/sa02m-repair-web-env
    /usr/local/sbin/sa02m-repair-web-env 2>/dev/null || true
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-reboot.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-reboot.sh" /usr/local/sbin/sa02m-web-reboot.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-restart-services.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-restart-services.sh" /usr/local/sbin/sa02m-web-restart-services.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-pre-start.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-pre-start.sh" /usr/local/sbin/sa02m-pre-start.sh
fi

# USB / microSD: storage-mount (как в scripts/01-system.sh), чтобы веб «Автоформат» работал без полного install.sh
ETC_REPO="$SCRIPT_DIR/../etc"
if [ -f "$ETC_REPO/storage-mount.sh" ] && [ -f "$ETC_REPO/sa02m-set-storage-auto-format" ]; then
    log INFO "Синхронизация storage-mount из $ETC_REPO"
    install -m 755 "$ETC_REPO/storage-mount.sh" /usr/local/bin/storage-mount.sh
    install -m 755 "$ETC_REPO/sa02m-set-storage-auto-format" /usr/local/sbin/sa02m-set-storage-auto-format
    # Репозиторий часто синхронизируется с Windows: удаляем CRLF у shebang helper-скрипта.
    sed -i 's/\r$//' /usr/local/sbin/sa02m-set-storage-auto-format
    if [ -f "$ETC_REPO/systemd/storage-mount@.service" ]; then
        install -m 644 "$ETC_REPO/systemd/storage-mount@.service" /etc/systemd/system/storage-mount@.service
    fi
    if [ -f "$ETC_REPO/udev/99-storage.rules" ]; then
        install -m 644 "$ETC_REPO/udev/99-storage.rules" /etc/udev/rules.d/99-storage.rules
    fi
    mkdir -p /media/usb /media/sdcard
    chmod 777 /media/usb /media/sdcard 2>/dev/null || true
    if [ ! -f /etc/sa02m_storage.conf ] && [ -f "$ETC_REPO/sa02m_storage.conf" ]; then
        install -m 644 "$ETC_REPO/sa02m_storage.conf" /etc/sa02m_storage.conf
    fi
    udevadm control --reload-rules 2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload 2>/dev/null || true
    fi
    rm -f /tmp/sa02m_status_cache/storage.json /tmp/sa02m_status_cache/system.json \
        /tmp/sa02m_status_cache/storage.json.lock /tmp/sa02m_status_cache/system.json.lock 2>/dev/null || true
    log OK "storage-mount обновлён (/usr/local/bin/storage-mount.sh)"
fi

# Runtime-каталоги веб-сессий + rate-limit и синхронизация демона (иначе новая
# схема авторизации разойдётся с демоном → блокировка входа / 401 на /api/flasher).
ETC_REPO2="$SCRIPT_DIR/../etc"
if [ -f "$ETC_REPO2/tmpfiles.d/sa02m-web-sessions.conf" ]; then
    install -m 644 "$ETC_REPO2/tmpfiles.d/sa02m-web-sessions.conf" /etc/tmpfiles.d/sa02m-web-sessions.conf
    command -v systemd-tmpfiles >/dev/null 2>&1 && \
        systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-web-sessions.conf 2>/dev/null || true
fi
for _rt in /run/sa02m-web-sessions /run/sa02m-web-login; do
    [ -d "$_rt" ] || install -d -m 2750 -o www-data -g www-data "$_rt" 2>/dev/null || true
done
ITF=/etc/sa02m-web-internal-token
if [ ! -s "$ITF" ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$ITF"
    chmod 640 "$ITF"; chown root:www-data "$ITF" 2>/dev/null || true
fi
if [ -s "$ITF" ] && [ -f /etc/sa02m_flasher.conf ]; then
    _it=$(tr -d '[:space:]' < "$ITF")
    if [ -n "$_it" ]; then
        if grep -q '^INTERNAL_TOKEN=' /etc/sa02m_flasher.conf; then
            sed -i "s|^INTERNAL_TOKEN=.*|INTERNAL_TOKEN=${_it}|" /etc/sa02m_flasher.conf
        else
            printf 'INTERNAL_TOKEN=%s\n' "$_it" >> /etc/sa02m_flasher.conf
        fi
    fi
fi
if [ -f "$ETC_REPO2/sa02m-commit-web-env.sh" ]; then
    install -m 755 "$ETC_REPO2/sa02m-commit-web-env.sh" /usr/local/sbin/sa02m-commit-web-env
    sed -i 's/\r$//' /usr/local/sbin/sa02m-commit-web-env
fi
if [ -d "$SCRIPT_DIR/../opt/sa02m-flasher/sa02m_flasher" ] && [ -d /opt/sa02m-flasher ]; then
    cp -a "$SCRIPT_DIR/../opt/sa02m-flasher/sa02m_flasher/." /opt/sa02m-flasher/sa02m_flasher/ 2>/dev/null || true
    chown -R sa02m-flasher:sa02m-flasher /opt/sa02m-flasher/sa02m_flasher 2>/dev/null || true
    systemctl try-restart sa02m-flasher.service 2>/dev/null || true
    log OK "Демон sa02m-flasher синхронизирован и перезапущен"
fi

if [ -f /etc/sudoers.d/sa02m-www ]; then
    log INFO "Синхронизация sudoers для restart/reboot CGI"
    if ! grep -q '/usr/bin/systemctl restart fix-eth.service' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/sbin/reboot, /usr/bin/systemctl reboot, /usr/bin/systemctl restart networking.service, /usr/bin/systemctl restart fix-eth.service
SUDO
    fi
    if ! grep -q '/usr/bin/gpioset\|/usr/sbin/gpioset' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/sbin/gpioset, /usr/bin/gpioset, /usr/sbin/gpioget, /usr/bin/gpioget
SUDO
    fi
    if ! grep -q '/usr/bin/kill\|/bin/kill' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /bin/kill, /usr/bin/kill
SUDO
    fi
    if ! grep -q '/usr/local/sbin/sa02m-web-update-check' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-web-update-check
SUDO
    fi
    if ! grep -q '/usr/local/sbin/sa02m-web-update-apply' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-web-update-apply
SUDO
    fi
    if ! grep -q '/usr/bin/date' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/bin/date\n' >> /etc/sudoers.d/sa02m-www
    fi
    if ! grep -q 'sa02m-set-storage-auto-format' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-set-storage-auto-format\n' >> /etc/sudoers.d/sa02m-www
    fi
    if ! grep -q 'sa02m-web-reboot' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-web-reboot.sh\n' >> /etc/sudoers.d/sa02m-www
    fi
    if ! grep -q 'sa02m-web-restart-services' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-web-restart-services.sh\n' >> /etc/sudoers.d/sa02m-www
    fi
    if ! grep -q 'sa02m-rs485-stats.sh' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-rs485-stats.sh\n' >> /etc/sudoers.d/sa02m-www
    fi
    if [ -f "$SCRIPT_DIR/sa02m-rs485-stats.sh" ]; then
        install -m 755 "$SCRIPT_DIR/sa02m-rs485-stats.sh" /usr/local/sbin/sa02m-rs485-stats.sh
    fi
    chmod 440 /etc/sudoers.d/sa02m-www
    visudo -cf /etc/sudoers.d/sa02m-www >/dev/null
fi

# Root ext4 смонтирован с commit=600 (сброс журнала раз в 10 мин). Без явного
# flush перезагрузка вскоре после обновления теряет все изменения — форсируем.
sync
log OK "Веб-интерфейс обновлён: $WEB_ROOT (изменения сброшены на диск; nginx перезапускать не требуется)"
