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

# Раздел "Дискретный выход, USB-питание и индикация": hw_set.cgi должен ходить
# в /dev/i2c-* (PCA9536). Без членства www-data в группе i2c CGI уходит на
# медленный sudo-fallback, а на устройствах со старым sa02m_hw.conf backend
# ещё и захардкожен в disabled → все кнопки в UI навсегда «Н/Д» + disabled.
if getent group i2c >/dev/null 2>&1; then
    if ! id -nG www-data 2>/dev/null | tr ' ' '\n' | grep -qx i2c; then
        usermod -aG i2c www-data 2>/dev/null \
            && log OK "www-data добавлен в группу i2c (PCA9536 hw_set.cgi)"
    fi
fi
if [ -f /etc/sa02m_hw.conf ] && \
   grep -qE '^[[:space:]]*SA02M_HW_BACKEND=disabled([[:space:]]|$)' /etc/sa02m_hw.conf; then
    cp -a /etc/sa02m_hw.conf "/etc/sa02m_hw.conf.bak.$(date +%s)" 2>/dev/null || true
    sed -i 's/^\([[:space:]]*SA02M_HW_BACKEND=\)disabled\([[:space:]]*\)$/\1i2c_expander\2/' \
        /etc/sa02m_hw.conf
    log OK "sa02m_hw.conf: SA02M_HW_BACKEND disabled → i2c_expander (PCA9536)"
fi
# Session store (login.cgi / auth_check.cgi / flasher): www-data cannot mkdir
# under /run. Full install creates it via 03-webserver.sh tmpfiles (2750
# setgid); update-www-only skips that script — ensure the dir on older images.
SA02M_TMPFILES=/etc/tmpfiles.d/sa02m.conf
if [ -f "$SA02M_TMPFILES" ]; then
    if ! grep -q 'sa02m-web-sessions' "$SA02M_TMPFILES" 2>/dev/null; then
        printf '%s\n' 'd /run/sa02m-web-sessions 2750 www-data www-data -' >>"$SA02M_TMPFILES"
        log OK "tmpfiles: добавлена /run/sa02m-web-sessions"
    else
        sed -i 's|^d /run/sa02m-web-sessions .*|d /run/sa02m-web-sessions 2750 www-data www-data -|' \
            "$SA02M_TMPFILES"
    fi
    if ! grep -q 'sa02m-hw-override' "$SA02M_TMPFILES" 2>/dev/null; then
        printf '%s\n' 'd /run/sa02m-hw-override 0775 www-data www-data -' >>"$SA02M_TMPFILES"
        log OK "tmpfiles: добавлена /run/sa02m-hw-override"
    fi
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create "$SA02M_TMPFILES" 2>/dev/null || true
    fi
elif [ ! -d /run/sa02m-web-sessions ]; then
    install -d -m 2750 -o www-data -g www-data /run/sa02m-web-sessions 2>/dev/null \
        && log OK "создан /run/sa02m-web-sessions"
fi
if [ -d /run/sa02m-web-sessions ]; then
    chmod 2750 /run/sa02m-web-sessions 2>/dev/null || true
    chown www-data:www-data /run/sa02m-web-sessions 2>/dev/null || true
fi

if systemctl is-active --quiet fcgiwrap 2>/dev/null; then
    systemctl restart fcgiwrap 2>/dev/null || true
fi

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

# Offline / shared updater bootstrap (same destinations as 03-webserver.sh; [ -f ] guards)
REPO_ETC="$SCRIPT_DIR/../etc"
REPO_OPT_UPDATE="$REPO_ROOT/opt/sa02m-update"
REPO_SBIN_TREE="$REPO_ROOT/usr/local/sbin"
SYSTEMD_SRC="$REPO_ETC/systemd"
if [ -d "$REPO_OPT_UPDATE/lib" ]; then
    install -d -m 0755 /opt/sa02m-update
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
            "$REPO_OPT_UPDATE/" /opt/sa02m-update/ >/dev/null 2>&1 \
            && log OK "/opt/sa02m-update обновлён" \
            || log WARN "rsync /opt/sa02m-update не удался"
    else
        cp -a "$REPO_OPT_UPDATE/." /opt/sa02m-update/ >/dev/null 2>&1 || true
    fi
fi
install -d -m 0755 /usr/local/libexec
if [ -f "$REPO_ETC/sa02m-update-runner.sh" ]; then
    install -m 755 "$REPO_ETC/sa02m-update-runner.sh" /usr/local/libexec/sa02m-update-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-runner
    log OK "sa02m-update-runner установлен"
fi
if [ -f "$REPO_ETC/sa02m-update-inspect.sh" ]; then
    install -m 755 "$REPO_ETC/sa02m-update-inspect.sh" /usr/local/libexec/sa02m-update-inspect
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-inspect
    log OK "sa02m-update-inspect установлен"
fi
if [ -f "$REPO_ETC/sa02m-factory-reset-runner.sh" ]; then
    install -m 755 "$REPO_ETC/sa02m-factory-reset-runner.sh" /usr/local/libexec/sa02m-factory-reset-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-factory-reset-runner
fi
if [ -f "$REPO_SBIN_TREE/sa02m-web-backup.sh" ]; then
    install -m 755 "$REPO_SBIN_TREE/sa02m-web-backup.sh" /usr/local/sbin/sa02m-web-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-backup.sh
elif [ -f "$REPO_ETC/sa02m-web-backup.sh" ]; then
    install -m 755 "$REPO_ETC/sa02m-web-backup.sh" /usr/local/sbin/sa02m-web-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-backup.sh
fi
if [ -f "$REPO_SBIN_TREE/sa02m-restore-backup.sh" ]; then
    install -m 755 "$REPO_SBIN_TREE/sa02m-restore-backup.sh" /usr/local/sbin/sa02m-restore-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-restore-backup.sh
elif [ -f "$REPO_ETC/sa02m-restore-backup.sh" ]; then
    install -m 755 "$REPO_ETC/sa02m-restore-backup.sh" /usr/local/sbin/sa02m-restore-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-restore-backup.sh
fi
if [ -d "$REPO_ETC/sa02m-update/trusted-keys" ]; then
    install -d -m 0755 /etc/sa02m-update /etc/sa02m-update/trusted-keys
    for _pem in "$REPO_ETC/sa02m-update/trusted-keys/"*.pem; do
        [ -f "$_pem" ] || continue
        install -m 644 "$_pem" "/etc/sa02m-update/trusted-keys/$(basename "$_pem")"
    done
fi
if [ -d "$REPO_ETC/sa02m-factory-defaults" ]; then
    install -d -m 0755 /usr/share/sa02m-factory-defaults
    cp -a "$REPO_ETC/sa02m-factory-defaults/." /usr/share/sa02m-factory-defaults/ 2>/dev/null || true
fi
if [ -f "$REPO_ETC/tmpfiles.d/sa02m-update.conf" ]; then
    install -m 644 "$REPO_ETC/tmpfiles.d/sa02m-update.conf" /etc/tmpfiles.d/sa02m-update.conf
    sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-update.conf
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-update.conf 2>/dev/null || true
    fi
fi
_upd_daemon_reload=0
for _upd_unit in sa02m-update.service sa02m-update-recover.service sa02m-factory-reset.service; do
    if [ -f "$SYSTEMD_SRC/$_upd_unit" ]; then
        install -m 644 "$SYSTEMD_SRC/$_upd_unit" "/etc/systemd/system/$_upd_unit"
        sed -i 's/\r$//' "/etc/systemd/system/$_upd_unit"
        _upd_daemon_reload=1
    fi
done
if [ "$_upd_daemon_reload" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
fi
if [ -f /etc/systemd/system/sa02m-update-recover.service ]; then
    systemctl enable sa02m-update-recover.service 2>/dev/null || true
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
if [ -f "$SCRIPT_DIR/../etc/sa02m-beeper-override.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-beeper-override.sh" /usr/local/sbin/sa02m-beeper-override.sh
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
    if ! grep -q 'systemctl start sa02m-update.service' /etc/sudoers.d/sa02m-www; then
        cat >> /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl start sa02m-update.service
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl stop sa02m-update.service
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl reset-failed sa02m-update.service
SUDO
    fi
    if ! grep -q '/usr/local/libexec/sa02m-update-inspect' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(root) NOPASSWD: /usr/local/libexec/sa02m-update-inspect\n' >> /etc/sudoers.d/sa02m-www
    fi
    if ! grep -q '/usr/local/sbin/sa02m-web-backup.sh' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(root) NOPASSWD: /usr/local/sbin/sa02m-web-backup.sh\n' >> /etc/sudoers.d/sa02m-www
    fi
    if [ -f "$REPO_ETC/sudoers.d/sa02m-www.fragment" ]; then
        while IFS= read -r _line || [ -n "$_line" ]; do
            case "$_line" in
                ''|\#*) continue ;;
            esac
            if ! grep -Fqx "$_line" /etc/sudoers.d/sa02m-www 2>/dev/null; then
                printf '%s\n' "$_line" >>/etc/sudoers.d/sa02m-www
            fi
        done <"$REPO_ETC/sudoers.d/sa02m-www.fragment"
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
    if ! grep -q 'sa02m-web-root-cmd.sh' /etc/sudoers.d/sa02m-www; then
        printf '\nwww-data ALL=(root) NOPASSWD: /usr/local/sbin/sa02m-web-root-cmd.sh *\n' >> /etc/sudoers.d/sa02m-www
    fi
    if [ -f "$SCRIPT_DIR/sa02m-rs485-stats.sh" ]; then
        install -m 755 "$SCRIPT_DIR/sa02m-rs485-stats.sh" /usr/local/sbin/sa02m-rs485-stats.sh
    fi
    if [ -f "$SCRIPT_DIR/../etc/sa02m-web-root-cmd.sh" ]; then
        install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-root-cmd.sh" /usr/local/sbin/sa02m-web-root-cmd.sh
        sed -i 's/\r$//' /usr/local/sbin/sa02m-web-root-cmd.sh
    fi
    chmod 440 /etc/sudoers.d/sa02m-www
    visudo -cf /etc/sudoers.d/sa02m-www >/dev/null
fi

# Cloud agent web trigger (pair/token) — needed for Management → Облако
REPO_ETC="$SCRIPT_DIR/../etc"
REPO_SBIN="$SCRIPT_DIR/../usr/local/sbin"
if [ -f "$REPO_SBIN/sa02m-cloud-web-trigger.sh" ]; then
    install -m 755 "$REPO_SBIN/sa02m-cloud-web-trigger.sh" /usr/local/sbin/sa02m-cloud-web-trigger.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-cloud-web-trigger.sh
    log OK "sa02m-cloud-web-trigger.sh обновлён"
fi
# Reinstall + CRLF-strip + visudo-validate ALL sa02m-* sudoers we ship, not just
# cloud: a CRLF in ANY sudoers.d file is a visudo syntax error that breaks sudo
# globally (cloud enrollment, flasher, mqtt all fail). A www-only deploy that
# fixed only sa02m-cloud left a broken sa02m-mqtt/-flasher and sudo stayed dead.
for _sud in sa02m-cloud sa02m-flasher sa02m-mqtt; do
    if [ -f "$REPO_ETC/sudoers.d/$_sud" ]; then
        install -m 0440 -o root -g root "$REPO_ETC/sudoers.d/$_sud" "/etc/sudoers.d/$_sud"
        sed -i 's/\r$//' "/etc/sudoers.d/$_sud"
        visudo -cf "/etc/sudoers.d/$_sud" >/dev/null 2>&1 \
            && log OK "sudoers $_sud OK" \
            || log WARN "visudo отклонил sudoers.d/$_sud"
    fi
done
# Final aggregate check so a broken sudoers set is surfaced by the deploy.
visudo -c >/dev/null 2>&1 || log WARN "visudo -c: sudoers set has an error — проверьте /etc/sudoers.d"

# Bus-free RS-485 roster aggregator — fleet card «Опрос модулей RS-485» and the
# cloud heartbeat both read /run/sa02m-rs485-roster.json. Full install.sh runs
# scripts/10-rs485-roster.sh; www-only updates used to skip it, so an older
# image can have a live MQTT bridge roster under /run/sa02m-modbus-mqtt/ while
# the cloud card shows no modules at all.
if [ -f "$SCRIPT_DIR/10-rs485-roster.sh" ]; then
    bash "$SCRIPT_DIR/10-rs485-roster.sh" \
        && log OK "sa02m-rs485-roster установлен/обновлён" \
        || log WARN "10-rs485-roster.sh завершился с ошибкой"
fi

# Modbus→MQTT bridge — full install.sh runs 05-mqtt.sh; www-only used to skip
# it, so CE poll_power_s / name fixes never reached field devices on web update.
MQTT_OPT="$REPO_ROOT/opt/sa02m-modbus-mqtt"
BRIDGE_DIR=/opt/sa02m-modbus-mqtt
if [ -d "$BRIDGE_DIR" ] && [ -f "$MQTT_OPT/modbus_mqtt_bridge.py" ]; then
    install -m 0755 -o root -g root \
        "$MQTT_OPT/modbus_mqtt_bridge.py" "$BRIDGE_DIR/modbus_mqtt_bridge.py"
    sed -i 's/\r$//' "$BRIDGE_DIR/modbus_mqtt_bridge.py" 2>/dev/null || true
    if [ -f "$MQTT_OPT/mqtt_bus_scan.py" ]; then
        install -m 0755 -o root -g root \
            "$MQTT_OPT/mqtt_bus_scan.py" "$BRIDGE_DIR/mqtt_bus_scan.py"
        sed -i 's/\r$//' "$BRIDGE_DIR/mqtt_bus_scan.py" 2>/dev/null || true
    fi
    if systemctl is-active --quiet sa02m-modbus-mqtt.service 2>/dev/null; then
        systemctl restart sa02m-modbus-mqtt.service \
            && log OK "sa02m-modbus-mqtt: код обновлён, сервис перезапущен" \
            || log WARN "sa02m-modbus-mqtt: код обновлён, restart не удался"
    else
        log OK "sa02m-modbus-mqtt: код обновлён (сервис не активен — без restart)"
    fi
fi

# Devices tab (DTV / CE-02m-3): API :8765 + SQLite logger + nginx /api/devices*
if [ -f "$SCRIPT_DIR/11-devices.sh" ] && [ -d "$REPO_ROOT/opt/sa02m-devices/sa02m_devices" ]; then
    bash "$SCRIPT_DIR/11-devices.sh" \
        && log OK "sa02m-devices (API+logger) установлен/обновлён" \
        || log WARN "11-devices.sh завершился с ошибкой"
fi

log OK "Веб-интерфейс обновлён: $WEB_ROOT"
