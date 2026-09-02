#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 03-webserver.sh  •  nginx + fcgiwrap + sudo + web-app deploy
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [03] Настройка веб-сервера ==="

: "${PORT:=9999}"
: "${WEB_ROOT:=/var/www/network_config}"
: "${AUTH_FILE:=/etc/nginx/.htpasswd}"
: "${ADMIN_PASS:=cyntron}"

ETC_DIR="$SCRIPT_DIR/../etc"
WWW_DIR="$SCRIPT_DIR/../www/network_config"
SYSTEMD_DIR="$ETC_DIR/systemd"

# ── htpasswd ──────────────────────────────────────────────────────────────
log INFO "Создание htpasswd"
mkdir -p /etc/nginx
HASHED=$(openssl passwd -apr1 "$ADMIN_PASS")
echo "admin:$HASHED" > "$AUTH_FILE"
chmod 600 "$AUTH_FILE"

# ── nginx site config ─────────────────────────────────────────────────────
log INFO "Настройка nginx (порт $PORT)"
if [ -f "$ETC_DIR/nginx/network_config.conf" ]; then
    sed "s|__PORT__|$PORT|g; s|__WEB_ROOT__|$WEB_ROOT|g" \
        "$ETC_DIR/nginx/network_config.conf" \
        > /etc/nginx/sites-available/network_config
else
    # Fallback: generate inline
    cat > /etc/nginx/sites-available/network_config <<NGINX
server {
    listen ${PORT} default_server;
    server_name _;
    root ${WEB_ROOT};
    index index.html;

    location /static/ { expires 1d; }
    location = /login.html { try_files \$uri =404; }

    location = /cgi-bin/index.cgi {
        return 302 /;
    }

    location = /cgi-bin/ssh_debug.cgi {
        include        fastcgi_params;
        fastcgi_param  SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param  HTTP_COOKIE     \$http_cookie;
        fastcgi_connect_timeout 5s;
        fastcgi_send_timeout    120s;
        fastcgi_read_timeout    120s;
        fastcgi_pass   unix:/run/fcgiwrap/fcgiwrap.socket;
    }

    location /cgi-bin/ {
        include        fastcgi_params;
        fastcgi_param  SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param  HTTP_COOKIE     \$http_cookie;
        fastcgi_connect_timeout 2s;
        fastcgi_send_timeout    20s;
        fastcgi_read_timeout    20s;
        fastcgi_pass   unix:/run/fcgiwrap/fcgiwrap.socket;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    access_log /var/log/nginx/sa02m_access.log;
    error_log  /var/log/nginx/sa02m_error.log warn;
}
NGINX
fi

# Сокет fcgiwrap всегда /run/fcgiwrap/fcgiwrap.socket — создаётся нашим
# кастомным fcgiwrap.service (RuntimeDirectory=fcgiwrap). Не детектируем
# legacy-сокет здесь: на чистом Ubuntu/Debian apt автозапускает stock
# fcgiwrap.socket с путём /run/fcgiwrap.socket, детекция подставляла бы
# его в nginx.conf, а затем наш сервис поднимал другой путь → 502.

# ── Один vhost на порту $PORT (иначе второй server { listen …; server_name _; } перехватывает запросы → 403)
OUR_SITE_REAL=$(readlink -f /etc/nginx/sites-available/network_config)
SA02M_NGX_DISABLED="/etc/nginx/sites-enabled.sa02m-disabled"
mkdir -p "$SA02M_NGX_DISABLED"

# Старый vhost «network.conf» (часто с return 302 /cgi-bin/index.cgi). Важно: не переименовывать внутри
# sites-enabled — nginx подключает любые имена; переносим каталогом .sa02m-disabled.
shopt -s nullglob
for dead in /etc/nginx/sites-enabled/network.conf*; do
    [ -e "$dead" ] || continue
    dead_real=$(readlink -f "$dead" 2>/dev/null || echo "$dead")
    [ "$dead_real" = "$OUR_SITE_REAL" ] && continue
    log INFO "Убираю из sites-enabled устаревший конфиг: ${dead##*/}"
    mv "$dead" "$SA02M_NGX_DISABLED/${dead##*/}.sa02m-disabled" 2>/dev/null || rm -f "$dead"
done

shopt -s nullglob
for path in /etc/nginx/sites-enabled/*; do
    [ -e "$path" ] || continue
    tgt=$(readlink -f "$path" 2>/dev/null || echo "$path")
    [ -f "$tgt" ] || continue
    [ "$tgt" = "$OUR_SITE_REAL" ] && continue
    if grep -qE "listen([^#;]*[^0-9]|^[^#;]*)${PORT}([[:space:];,]|ssl|,|\$)" "$tgt" 2>/dev/null \
        || grep -qE "listen[[:space:]]+[^#;]*:${PORT}([[:space:];,]|ssl|,|\$)" "$tgt" 2>/dev/null; then
        log INFO "Отключаю посторонний vhost на порту ${PORT}: ${path##*/}"
        mv "$path" "$SA02M_NGX_DISABLED/${path##*/}.sa02m-disabled" 2>/dev/null || rm -f "$path"
    fi
done

for f in /etc/nginx/conf.d/*.conf; do
    [ -f "$f" ] || continue
    case "$f" in *.sa02m-disabled) continue ;; esac
    grep -qE "listen([^#;]*[^0-9]|^[^#;]*)${PORT}([[:space:];,]|ssl|,|\$)" "$f" 2>/dev/null \
        || grep -qE "listen[[:space:]]+[^#;]*:${PORT}([[:space:];,]|ssl|,|\$)" "$f" 2>/dev/null || continue
    if grep -qE "listen([^#;]*[^0-9]|^[^#;]*)(80|443)([[:space:];,]|ssl|,|\$)" "$f" 2>/dev/null \
        || grep -qE "listen[[:space:]]+[^#;]*:(80|443)([[:space:];,]|ssl|,|\$)" "$f" 2>/dev/null; then
        log WARN "conf.d/$(basename "$f") содержит порт ${PORT} и также 80/443 — удалите вручную блок listen ${PORT}"
        continue
    fi
    log INFO "Отключаю conf.d на порту ${PORT}: $(basename "$f")"
    mv "$f" "${f}.sa02m-disabled" 2>/dev/null || true
done
shopt -u nullglob

rm -f /etc/nginx/sites-enabled/default \
    /etc/nginx/sites-enabled/network_config \
    /etc/nginx/sites-enabled/000-sa02m-network_config
ln -sf /etc/nginx/sites-available/network_config /etc/nginx/sites-enabled/000-sa02m-network_config
nginx -t >> "$LOG_FILE" 2>&1 && log OK "nginx config OK"

# ── Deploy web files ──────────────────────────────────────────────────────
case "${WEB_ROOT:-}" in
    ""|"/") log ERROR "WEB_ROOT пустой или небезопасен — отказ"; exit 1 ;;
esac

log INFO "Деплой файлов в $WEB_ROOT"
mkdir -p "$WEB_ROOT"

# Полная очистка каталога веб-приложения (остатки старого проекта / чужие cgi, html, static)
if [ -d "$WEB_ROOT" ]; then
    log INFO "Очистка $WEB_ROOT перед копированием"
    find "$WEB_ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
fi

mkdir -p "$WEB_ROOT/cgi-bin" "$WEB_ROOT/static/css" "$WEB_ROOT/static/js"
cp -r "$WWW_DIR/." "$WEB_ROOT/"

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
        # Expected on a git-archive deploy (no .git in the tarball) — the
        # APP_VERSION fallback below is correct and sufficient. INFO, not WARN,
        # so it stops reading as a defect in log reviews.
        log INFO "Нет .git в $REPO_ROOT (штатно для git-archive деплоя) — deployed_commit по APP_VERSION"
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

# Permissions
find "$WEB_ROOT/cgi-bin" -name "*.cgi" -exec chmod 755 {} \;
find "$WEB_ROOT/static"  \( -name "*.css" -o -name "*.js" -o -name "*.svg" \) -exec chmod 644 {} \;
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html"
chown -R www-data:www-data "$WEB_ROOT"

if [ -f "$SCRIPT_DIR/../etc/sa02m-web-root-cmd.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-root-cmd.sh" /usr/local/sbin/sa02m-web-root-cmd.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-root-cmd.sh
else
    log WARN "Нет etc/sa02m-web-root-cmd.sh — root-режим командной строки недоступен"
fi

# ── GPIO hw.conf ──────────────────────────────────────────────────────────
if [ ! -f /etc/sa02m_hw.conf ]; then
    log INFO "Создание /etc/sa02m_hw.conf (шаблон)"
    cat > /etc/sa02m_hw.conf <<'HWCONF'
# Backend: auto | i2c_expander | gpio_sysfs | disabled
# По умолчанию — i2c_expander: плата СА-02м всегда несёт PCA9536 на bus 2
# addr 0x41, и раздел UI "Дискретный выход, USB-питание и индикация" завязан
# на его каналы (do / beeper / alarm_led). `disabled` оставляет кнопки в UI
# «Н/Д» + disabled — использовать только для отладки без доступа к I2C.
# auto: если GPIO-пины заданы явно, используется sysfs GPIO; иначе PCA9536.
SA02M_HW_BACKEND=i2c_expander

# PCA9536 (I2C bus 2, addr 0x41). Для занятых шин используется flock + timeout.
SA02M_I2C_EXP_BUS=2
SA02M_I2C_EXP_ADDR=0x41
SA02M_I2C_LOCK_FILE=/run/lock/sa02m-pca9536.lock
SA02M_I2C_LOCK_WAIT_SEC=1
SA02M_I2C_TIMEOUT_SEC=1
SA02M_I2C_OWNER_UNITS="mplc.service mplc4.service klogic.service klogicd.service"
SA02M_I2C_OWNER_PROCS="mplc mplc4 klogic klogicd klogic-sa02"
SA02M_I2C_RESPECT_OWNER=1
SA02M_BEEPER_WEB_OVERRIDE_SEC=7
SA02M_BEEPER_OVERRIDE_FILE=/run/sa02m-hw-override/beeper.env
SA02M_BEEPER_OVERRIDE_WORKER=/usr/local/sbin/sa02m-beeper-override.sh
SA02M_I2C_ACTIVE_LOW_MASK=auto
# bit3 = Blue LED for KLogic; web does not expose it but must keep it as output.
SA02M_I2C_EXTRA_OUTPUT_MASK=0x08
SA02M_I2C_BIT_DO=1
SA02M_I2C_BIT_BEEPER=2
SA02M_I2C_BIT_ALARM_LED=0
SA02M_I2C_BIT_USB_POWER=

# Fallback для старых ревизий, где каналы заведены в sysfs GPIO.
# Заполните только если хотите принудительно использовать gpio_sysfs.
SA02M_GPIO_DO=
SA02M_GPIO_BEEPER=
SA02M_GPIO_ALARM_LED=
SA02M_GPIO_USB_POWER=
# Питание USB через libgpiod (как gpioset 0 268=1). Очистите LINE, если не используется.
# 0 = без инверсии (gpioset 268=1 → UI «ВКЛ»); 1 = инвертировать (gpioset 268=0 → UI «ВКЛ»).
SA02M_USB_POWER_INVERT=0
SA02M_GPIO_USB_GPIOD_CHIP=0
SA02M_GPIO_USB_GPIOD_LINE=268
HWCONF
    chmod 644 /etc/sa02m_hw.conf
else
    # Upgrade: старый шаблон ставил SA02M_HW_BACKEND=disabled как "безопасный
    # дефолт" — из-за этого CGI hw_set отвечал gpio_not_configured, а UI
    # дизейблил все кнопки раздела "Дискретный выход, USB-питание и индикация".
    # Плата всегда несёт PCA9536, поэтому автоматически включаем i2c_expander,
    # если пользователь ранее явно не выбрал gpio_sysfs / auto.
    if grep -qE '^[[:space:]]*SA02M_HW_BACKEND=disabled([[:space:]]|$)' /etc/sa02m_hw.conf; then
        cp -a /etc/sa02m_hw.conf "/etc/sa02m_hw.conf.bak.$(date +%s)" 2>/dev/null || true
        sed -i 's/^\([[:space:]]*SA02M_HW_BACKEND=\)disabled\([[:space:]]*\)$/\1i2c_expander\2/' \
            /etc/sa02m_hw.conf
        log OK "sa02m_hw.conf: SA02M_HW_BACKEND disabled → i2c_expander (PCA9536)"
    fi
    if grep -qE '^SA02M_I2C_OWNER_PROCS=' /etc/sa02m_hw.conf \
       && ! grep -qE '^SA02M_I2C_OWNER_PROCS=.*klogic-sa02' /etc/sa02m_hw.conf; then
        cp -a /etc/sa02m_hw.conf "/etc/sa02m_hw.conf.bak.owner-procs.$(date +%s)" 2>/dev/null || true
        sed -i 's/^\(SA02M_I2C_OWNER_PROCS="[^"]*\)"/\1 klogic-sa02"/' /etc/sa02m_hw.conf
        log OK "sa02m_hw.conf: добавлен I2C owner process klogic-sa02"
    fi
    if ! grep -qE '^SA02M_I2C_EXTRA_OUTPUT_MASK=' /etc/sa02m_hw.conf; then
        cp -a /etc/sa02m_hw.conf "/etc/sa02m_hw.conf.bak.extra-output.$(date +%s)" 2>/dev/null || true
        sed -i '/^SA02M_I2C_ACTIVE_LOW_MASK=/a SA02M_I2C_EXTRA_OUTPUT_MASK=0x08' /etc/sa02m_hw.conf
        log OK "sa02m_hw.conf: bit3 PCA9536 оставлен output для KLogic blue LED"
    fi
    if ! grep -qE '^SA02M_BEEPER_WEB_OVERRIDE_SEC=' /etc/sa02m_hw.conf; then
        cp -a /etc/sa02m_hw.conf "/etc/sa02m_hw.conf.bak.beeper-override.$(date +%s)" 2>/dev/null || true
        sed -i '/^SA02M_I2C_RESPECT_OWNER=/a SA02M_BEEPER_WEB_OVERRIDE_SEC=7\nSA02M_BEEPER_OVERRIDE_FILE=/run/sa02m-hw-override/beeper.env\nSA02M_BEEPER_OVERRIDE_WORKER=/usr/local/sbin/sa02m-beeper-override.sh' /etc/sa02m_hw.conf
        log OK "sa02m_hw.conf: beeper web override TTL = 7s"
    fi
fi

if [ ! -f /etc/sa02m_status_blocks.conf ] && [ -f "$ETC_DIR/sa02m_status_blocks.conf" ]; then
    install -m 644 "$ETC_DIR/sa02m_status_blocks.conf" /etc/sa02m_status_blocks.conf
fi

if [ -f "$ETC_DIR/sa02m-hw-backend-guard.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-hw-backend-guard.sh" /usr/local/sbin/sa02m-hw-backend-guard
fi
if [ -f "$ETC_DIR/sa02m-status-blocks-guard.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-status-blocks-guard.sh" /usr/local/sbin/sa02m-status-blocks-guard
fi
if [ -f "$ETC_DIR/sa02m-prepare-working-board.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-prepare-working-board.sh" /usr/local/sbin/sa02m-prepare-working-board
fi
if [ -f "$ETC_DIR/sa02m-failure-monitor.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-failure-monitor.sh" /usr/local/sbin/sa02m-failure-monitor
fi
# Keeps the extension: the sudoers below grants this exact path, and apply.cgi
# falls back to retire-to-comments while the helper is absent (older deploys).
if [ -f "$ETC_DIR/sa02m-conf-rm.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-conf-rm.sh" /usr/local/sbin/sa02m-conf-rm.sh
fi
# Pinned root-write / GPIO helpers (audit B1): the only file-write and GPIO
# capability www-data holds via sudoers — replaces the former raw tee/gpioset/
# kill grants. CRLF-strip because the repo often transits Windows.
if [ -f "$ETC_DIR/sa02m-iface-conf-write.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-iface-conf-write.sh" /usr/local/sbin/sa02m-iface-conf-write.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-iface-conf-write.sh
fi
if [ -f "$ETC_DIR/sa02m-ensure-eth1-dhcp-hook.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-ensure-eth1-dhcp-hook.sh" /usr/local/sbin/sa02m-ensure-eth1-dhcp-hook.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-ensure-eth1-dhcp-hook.sh
fi
if [ -f "$ETC_DIR/dhcp/dhclient-exit-hooks.d/eth1-default-route" ]; then
    mkdir -p /etc/dhcp/dhclient-exit-hooks.d
    install -m 755 "$ETC_DIR/dhcp/dhclient-exit-hooks.d/eth1-default-route" \
        /etc/dhcp/dhclient-exit-hooks.d/eth1-default-route
    sed -i 's/\r$//' /etc/dhcp/dhclient-exit-hooks.d/eth1-default-route
fi
if [ -f "$ETC_DIR/sa02m-usb-power.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-usb-power.sh" /usr/local/sbin/sa02m-usb-power.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-usb-power.sh
fi
if [ -f "$ETC_DIR/sa02m_failure_monitor.conf" ] && [ ! -f /etc/sa02m_failure_monitor.conf ]; then
    install -m 644 "$ETC_DIR/sa02m_failure_monitor.conf" /etc/sa02m_failure_monitor.conf
fi
if [ -f "$ETC_DIR/sa02m-failure-monitor.service" ]; then
    install -m 644 "$ETC_DIR/sa02m-failure-monitor.service" /etc/systemd/system/sa02m-failure-monitor.service
fi

# ── util-linux-extra (hwclock) ────────────────────────────────────────────
if ! command -v hwclock >/dev/null 2>&1; then
    log INFO "Установка util-linux-extra (hwclock)"
    sa02m_pkg_install_tier optional util-linux-extra
fi

# ── tmpfiles.d: lock file for PCA9536 I2C flock (www-data owned) + the web
#    session store (www-data owned; holds per-login session tokens, recreated
#    on boot since /run is tmpfs). Without this dir the CGI cannot mint or
#    validate sessions and login fails — provision it here, not lazily.
#    The session dir MUST be 2750 (setgid, group www-data): the sa02m-flasher
#    daemon runs in the www-data group and reads the session files by group, so
#    the dir has to be group-traversable. A weaker 0700 here makes the daemon
#    return 401 for otherwise-valid sessions (it cannot traverse the dir). ─────
cat > /etc/tmpfiles.d/sa02m.conf <<'EOF'
# 0666: root (stand/beeper) and www-data (CGI) must both flock; sticky /run/lock
# rejects bash `exec 9>`(O_CREAT) on foreign-owned files — lib_hw opens RDWR.
f /run/lock/sa02m-pca9536.lock 0666 root www-data -
d /var/lib/sa02m-web-build 0755 root root -
d /run/sa02m-web-sessions 2750 www-data www-data -
d /run/sa02m-hw-override 0775 www-data www-data -
d /var/lib/sa02m-update 0755 root root -
d /var/lib/sa02m-update/incoming 0770 root www-data -
d /var/lib/sa02m-update/staging 0750 root root -
d /var/lib/sa02m-update/rollback 0750 root root -
d /var/lib/sa02m-update/state 0750 root root -
d /var/lib/sa02m-update/runner 0750 root root -
d /var/lib/sa02m-update/backup-export 0750 root root -
d /etc/sa02m-update/trusted-keys 0755 root root -
d /var/lib/sa02m-mplc 0755 root root -
d /var/lib/sa02m-mplc/incoming 0770 root www-data -
d /var/lib/sa02m-mplc/backups 0700 root root -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/sa02m.conf >> "$LOG_FILE" 2>&1 || true

# Login-throttle state dir. Committed file, not another heredoc line: only a
# committed etc/tmpfiles.d/ file reaches an already-installed board through OTA
# / the offline package (see the conf's own header). Without it the throttle in
# lib_web_auth.sh fails open — www-data cannot mkdir under root-owned /run.
if [ -f "$ETC_DIR/tmpfiles.d/sa02m-web-login.conf" ]; then
    install -m 644 "$ETC_DIR/tmpfiles.d/sa02m-web-login.conf" /etc/tmpfiles.d/sa02m-web-login.conf
    sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-web-login.conf
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-web-login.conf >>"$LOG_FILE" 2>&1 || true
    fi
    log OK "tmpfiles sa02m-web-login.conf (/run/sa02m-web-login)"
fi

if [ -f "$ETC_DIR/sa02m-beeper-override.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-beeper-override.sh" /usr/local/sbin/sa02m-beeper-override.sh
fi

# ── /dev/i2c-* доступ для www-data (PCA9536 / hw_set.cgi без sudo) ───────
# /dev/i2c-N создаётся ядром как root:i2c 0660. Без членства www-data в
# группе i2c CGI hw_set / status уходит по sudo-fallback (медленнее и
# ломается при отсутствии sudoers). Идемпотентно: usermod -aG не
# дублирует запись, если пользователь уже в группе.
if getent group i2c >/dev/null 2>&1; then
    if ! id -nG www-data 2>/dev/null | tr ' ' '\n' | grep -qx i2c; then
        usermod -aG i2c www-data >> "$LOG_FILE" 2>&1 \
            && log OK "www-data добавлен в группу i2c (PCA9536 hw_set.cgi)"
    fi
fi

# ── sudoers for www-data (single-home, installed wholesale) ────────────────
# The COMPLETE pinned grant lives in ONE committed file (etc/sudoers.d/sa02m-www),
# installed via sa02m_install_sudoers (install -m 0440 + CRLF strip + visudo -cf).
# A wholesale install OVERWRITES any older drop-in, so an installer re-run
# REMOVES a stale dangerous grant — audit B1 retired the former unpinned
# tee/ifup/ifdown/kill/i2cset/gpioset that let a web session become root without
# the root password. No heredoc, no append-if-missing, no .fragment (they drifted
# and never removed a stale grant).
log INFO "Настройка sudoers"
sa02m_install_sudoers "$ETC_DIR/sudoers.d/sa02m-www" /etc/sudoers.d/sa02m-www
sa02m_cleanup_b1_deploy_artifacts

# ── Учётные данные веб-интерфейса (/etc/sa02m_web.env) ─────────────────────
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-auth-lib.sh" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-web-auth-lib.sh" /usr/local/lib/sa02m-web-auth-lib.sh
    sed -i 's/\r$//' /usr/local/lib/sa02m-web-auth-lib.sh
else
    log WARN "Нет etc/sa02m-web-auth-lib.sh — безопасная запись sa02m_web.env недоступна"
fi
# ── Политика сторонних стеков (общая lib для установщика и service-ctl) ────
if [ -f "$SCRIPT_DIR/../etc/sa02m-stacks-policy.sh" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-stacks-policy.sh" /usr/local/lib/sa02m-stacks-policy.sh
    sed -i 's/\r$//' /usr/local/lib/sa02m-stacks-policy.sh
else
    log WARN "Нет etc/sa02m-stacks-policy.sh — service-ctl не будет записывать /etc/sa02m_stacks.conf"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-repair-web-env.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-repair-web-env.sh" /usr/local/sbin/sa02m-repair-web-env
    sed -i 's/\r$//' /usr/local/sbin/sa02m-repair-web-env
else
    log WARN "Нет etc/sa02m-repair-web-env.sh — repair sa02m_web.env недоступен"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-commit-web-env.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-commit-web-env.sh" /usr/local/sbin/sa02m-commit-web-env
else
    log WARN "Нет etc/sa02m-commit-web-env.sh — смена пароля через веб будет недоступна"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-update-check.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-update-check.sh" /usr/local/sbin/sa02m-web-update-check
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-update-check
else
    log WARN "Нет etc/sa02m-web-update-check.sh — таймер проверки обновлений веб-UI недоступен"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-build-lib.sh" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-web-build-lib.sh" /usr/local/lib/sa02m-web-build-lib.sh
    sed -i 's/\r$//' /usr/local/lib/sa02m-web-build-lib.sh
else
    log WARN "Нет etc/sa02m-web-build-lib.sh — авто-ветка для проверки обновлений недоступна"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-update-apply.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-update-apply.sh" /usr/local/sbin/sa02m-web-update-apply
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-update-apply
else
    log WARN "Нет etc/sa02m-web-update-apply.sh — применение обновлений веб-UI из GitHub недоступно"
fi
# ── Offline updater bootstrap (release N): runner/keys/units/backup/reset ──
if [ -d "$SCRIPT_DIR/../opt/sa02m-update" ]; then
    mkdir -p /opt/sa02m-update
    rsync -a --delete "$SCRIPT_DIR/../opt/sa02m-update/" /opt/sa02m-update/ 2>/dev/null \
        || cp -a "$SCRIPT_DIR/../opt/sa02m-update/." /opt/sa02m-update/
    find /opt/sa02m-update -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    log OK "opt/sa02m-update установлен"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-update-runner.sh" ]; then
    install -d -m 755 /usr/local/libexec
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-update-runner.sh" /usr/local/libexec/sa02m-update-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-runner
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-update-inspect.sh" ]; then
    install -d -m 755 /usr/local/libexec
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-update-inspect.sh" /usr/local/libexec/sa02m-update-inspect
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-inspect
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-backup.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-backup.sh" /usr/local/sbin/sa02m-web-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-backup.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-restore-backup.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-restore-backup.sh" /usr/local/sbin/sa02m-restore-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-restore-backup.sh
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-factory-reset-runner.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-factory-reset-runner.sh" /usr/local/libexec/sa02m-factory-reset-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-factory-reset-runner
fi
mkdir -p /etc/sa02m-update/trusted-keys
if [ -d "$SCRIPT_DIR/../etc/sa02m-update/trusted-keys" ]; then
    install -m 644 "$SCRIPT_DIR/../etc/sa02m-update/trusted-keys/"*.pem /etc/sa02m-update/trusted-keys/ 2>/dev/null || true
fi
for _upd_unit in sa02m-update.service sa02m-update-recover.service sa02m-factory-reset.service; do
    if [ -f "$SYSTEMD_DIR/$_upd_unit" ]; then
        install -m 644 "$SYSTEMD_DIR/$_upd_unit" "/etc/systemd/system/$_upd_unit"
        sed -i 's/\r$//' "/etc/systemd/system/$_upd_unit"
    fi
done
if [ -f /etc/systemd/system/sa02m-update-recover.service ]; then
    sa02m_svc_apply sa02m-update-recover.service infra
fi
if [ -d "$SCRIPT_DIR/../etc/sa02m-factory-defaults" ]; then
    mkdir -p /usr/share/sa02m-factory-defaults/current
    cp -a "$SCRIPT_DIR/../etc/sa02m-factory-defaults/." /usr/share/sa02m-factory-defaults/current/ 2>/dev/null || true
fi

# ── Offline / shared updater bootstrap (release N; [ -f ] guards) ───────────
# Source → install destinations (plan §2.12):
#   opt/sa02m-update/              → /opt/sa02m-update/
#   etc/sa02m-update-runner.sh     → /usr/local/libexec/sa02m-update-runner
#   etc/sa02m-update-inspect.sh    → /usr/local/libexec/sa02m-update-inspect
#   usr/local/sbin/sa02m-web-backup.sh | etc/sa02m-web-backup.sh → /usr/local/sbin/
#   usr/local/sbin/sa02m-restore-backup.sh → /usr/local/sbin/
#   etc/sa02m-factory-reset-runner.sh → /usr/local/libexec/sa02m-factory-reset-runner
#   etc/systemd/sa02m-update*.service → /etc/systemd/system/
#   etc/tmpfiles.d/sa02m-update.conf → /etc/tmpfiles.d/
#   etc/sa02m-update/trusted-keys/*.pem → /etc/sa02m-update/trusted-keys/
#   etc/sa02m-factory-defaults/    → /usr/share/sa02m-factory-defaults/
#   etc/sudoers.d/sa02m-www → /etc/sudoers.d/sa02m-www (single committed file, B1)
REPO_ROOT="${REPO_ROOT:-$SCRIPT_DIR/..}"
UPDATE_OPT_SRC="$REPO_ROOT/opt/sa02m-update"
if [ -d "$UPDATE_OPT_SRC/lib" ]; then
    log INFO "Установка /opt/sa02m-update"
    install -d -m 0755 /opt/sa02m-update
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
            "$UPDATE_OPT_SRC/" /opt/sa02m-update/ >>"$LOG_FILE" 2>&1 \
            && log OK "/opt/sa02m-update обновлён" \
            || log WARN "rsync /opt/sa02m-update не удался"
    else
        cp -a "$UPDATE_OPT_SRC/." /opt/sa02m-update/ >>"$LOG_FILE" 2>&1 \
            && log OK "/opt/sa02m-update скопирован" \
            || log WARN "cp /opt/sa02m-update не удался"
    fi
fi
install -d -m 0755 /usr/local/libexec
if [ -f "$ETC_DIR/sa02m-update-runner.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-update-runner.sh" /usr/local/libexec/sa02m-update-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-runner
    log OK "sa02m-update-runner → /usr/local/libexec/sa02m-update-runner"
elif [ -f "$REPO_ROOT/usr/local/libexec/sa02m-update-runner" ]; then
    install -m 755 "$REPO_ROOT/usr/local/libexec/sa02m-update-runner" /usr/local/libexec/sa02m-update-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-runner
    log OK "sa02m-update-runner → /usr/local/libexec/sa02m-update-runner"
fi
if [ -f "$ETC_DIR/sa02m-update-inspect.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-update-inspect.sh" /usr/local/libexec/sa02m-update-inspect
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-inspect
    log OK "sa02m-update-inspect → /usr/local/libexec/sa02m-update-inspect"
elif [ -f "$REPO_ROOT/usr/local/libexec/sa02m-update-inspect" ]; then
    install -m 755 "$REPO_ROOT/usr/local/libexec/sa02m-update-inspect" /usr/local/libexec/sa02m-update-inspect
    sed -i 's/\r$//' /usr/local/libexec/sa02m-update-inspect
    log OK "sa02m-update-inspect → /usr/local/libexec/sa02m-update-inspect"
fi
if [ -f "$ETC_DIR/sa02m-factory-reset-runner.sh" ]; then
    install -m 755 "$ETC_DIR/sa02m-factory-reset-runner.sh" /usr/local/libexec/sa02m-factory-reset-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-factory-reset-runner
    log OK "sa02m-factory-reset-runner → /usr/local/libexec/sa02m-factory-reset-runner"
elif [ -f "$REPO_ROOT/usr/local/libexec/sa02m-factory-reset-runner" ]; then
    install -m 755 "$REPO_ROOT/usr/local/libexec/sa02m-factory-reset-runner" /usr/local/libexec/sa02m-factory-reset-runner
    sed -i 's/\r$//' /usr/local/libexec/sa02m-factory-reset-runner
fi
_backup_src=""
if [ -f "$REPO_ROOT/usr/local/sbin/sa02m-web-backup.sh" ]; then
    _backup_src="$REPO_ROOT/usr/local/sbin/sa02m-web-backup.sh"
elif [ -f "$ETC_DIR/sa02m-web-backup.sh" ]; then
    _backup_src="$ETC_DIR/sa02m-web-backup.sh"
fi
if [ -n "$_backup_src" ]; then
    install -m 755 "$_backup_src" /usr/local/sbin/sa02m-web-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-web-backup.sh
    log OK "sa02m-web-backup.sh → /usr/local/sbin/sa02m-web-backup.sh"
fi
_restore_src=""
if [ -f "$REPO_ROOT/usr/local/sbin/sa02m-restore-backup.sh" ]; then
    _restore_src="$REPO_ROOT/usr/local/sbin/sa02m-restore-backup.sh"
elif [ -f "$ETC_DIR/sa02m-restore-backup.sh" ]; then
    _restore_src="$ETC_DIR/sa02m-restore-backup.sh"
fi
if [ -n "$_restore_src" ]; then
    install -m 755 "$_restore_src" /usr/local/sbin/sa02m-restore-backup.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-restore-backup.sh
    log OK "sa02m-restore-backup.sh → /usr/local/sbin/sa02m-restore-backup.sh"
fi
if [ -d "$ETC_DIR/sa02m-update/trusted-keys" ]; then
    install -d -m 0755 /etc/sa02m-update /etc/sa02m-update/trusted-keys
    for _pem in "$ETC_DIR/sa02m-update/trusted-keys/"*.pem; do
        [ -f "$_pem" ] || continue
        install -m 644 "$_pem" "/etc/sa02m-update/trusted-keys/$(basename "$_pem")"
    done
    log OK "trusted-keys → /etc/sa02m-update/trusted-keys/"
fi
if [ -d "$ETC_DIR/sa02m-factory-defaults" ]; then
    install -d -m 0755 /usr/share/sa02m-factory-defaults
    cp -a "$ETC_DIR/sa02m-factory-defaults/." /usr/share/sa02m-factory-defaults/ >>"$LOG_FILE" 2>&1 \
        && log OK "factory-defaults → /usr/share/sa02m-factory-defaults/" \
        || log WARN "factory-defaults copy failed"
fi
if [ -f "$ETC_DIR/tmpfiles.d/sa02m-update.conf" ]; then
    install -m 644 "$ETC_DIR/tmpfiles.d/sa02m-update.conf" /etc/tmpfiles.d/sa02m-update.conf
    sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-update.conf
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-update.conf >>"$LOG_FILE" 2>&1 || true
    fi
    log OK "tmpfiles sa02m-update.conf"
fi
for _upd_unit in sa02m-update.service sa02m-update-recover.service sa02m-factory-reset.service; do
    if [ -f "$SYSTEMD_DIR/$_upd_unit" ]; then
        install -m 644 "$SYSTEMD_DIR/$_upd_unit" "/etc/systemd/system/$_upd_unit"
        sed -i 's/\r$//' "/etc/systemd/system/$_upd_unit"
        log OK "unit $_upd_unit"
    fi
done
# (The former sa02m-www.fragment merge is gone — its pinned update lines are now
# part of the single committed etc/sudoers.d/sa02m-www installed above; audit B1.)

if [ -f /usr/local/lib/sa02m-web-build-lib.sh ]; then
    # shellcheck disable=SC1091
    . /usr/local/lib/sa02m-web-build-lib.sh
    if type sync_web_build_conf_from_deploy >/dev/null 2>&1; then
        sync_web_build_conf_from_deploy && log OK "sa02m_web_build.conf синхронизирован с веткой/версией"
    fi
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-reboot.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-reboot.sh" /usr/local/sbin/sa02m-web-reboot.sh
else
    log WARN "Нет etc/sa02m-web-reboot.sh — перезагрузка из веб может не сработать при сбое systemd"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-restart-services.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-restart-services.sh" /usr/local/sbin/sa02m-web-restart-services.sh
else
    log WARN "Нет etc/sa02m-web-restart-services.sh — перезапуск служб из веб без расширенных fallback"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-web-service-ctl.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-web-service-ctl.sh" /usr/local/sbin/sa02m-web-service-ctl.sh
else
    log WARN "Нет etc/sa02m-web-service-ctl.sh — управление прикладными службами из веб недоступно"
fi
# ── MPLC4 project deploy («Обновление проекта MPLC»): helper + Python module ──
if [ -f "$SCRIPT_DIR/../etc/sa02m-mplc-project-deploy.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-mplc-project-deploy.sh" /usr/local/sbin/sa02m-mplc-project-deploy.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-mplc-project-deploy.sh
    log OK "sa02m-mplc-project-deploy.sh → /usr/local/sbin"
else
    log WARN "Нет etc/sa02m-mplc-project-deploy.sh — развёртывание проекта MPLC из веб недоступно"
fi
if [ -d "$SCRIPT_DIR/../opt/sa02m-mplc/lib" ]; then
    install -d -m 0755 /opt/sa02m-mplc
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
            "$SCRIPT_DIR/../opt/sa02m-mplc/" /opt/sa02m-mplc/ >>"$LOG_FILE" 2>&1 \
            && log OK "/opt/sa02m-mplc установлен" || log WARN "rsync /opt/sa02m-mplc не удался"
    else
        cp -a "$SCRIPT_DIR/../opt/sa02m-mplc/." /opt/sa02m-mplc/ >>"$LOG_FILE" 2>&1 \
            && log OK "/opt/sa02m-mplc скопирован" || log WARN "cp /opt/sa02m-mplc не удался"
    fi
    find /opt/sa02m-mplc -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-kernel-select.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-kernel-select.sh" /usr/local/sbin/sa02m-kernel-select.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-kernel-select.sh
    mkdir -p /usr/local/share/sa02m/kernel
    if [ -x /usr/local/sbin/sa02m-kernel-select.sh ]; then
        /usr/local/sbin/sa02m-kernel-select.sh init >> "$LOG_FILE" 2>&1 || \
            log WARN "sa02m-kernel-select init — выполните после деплоя zImage"
    fi
else
    log WARN "Нет etc/sa02m-kernel-select.sh — переключение ядра из веб недоступно"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-cpu-profile.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-cpu-profile.sh" /usr/local/sbin/sa02m-cpu-profile.sh
    sed -i 's/\r$//' /usr/local/sbin/sa02m-cpu-profile.sh
else
    log WARN "Нет etc/sa02m-cpu-profile.sh — управление частотой CPU из веб недоступно"
fi
if [ -f "$SCRIPT_DIR/../etc/sa02m-set-cpu-profile" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-set-cpu-profile" /usr/local/sbin/sa02m-set-cpu-profile
    sed -i 's/\r$//' /usr/local/sbin/sa02m-set-cpu-profile
fi
for _cpu_unit in sa02m-cpu-profile.service; do
    if [ -f "$SYSTEMD_DIR/$_cpu_unit" ]; then
        install -m 644 "$SYSTEMD_DIR/$_cpu_unit" "/etc/systemd/system/$_cpu_unit"
        sa02m_svc_apply "$_cpu_unit" infra
    fi
done
if [ -x /usr/local/sbin/sa02m-cpu-profile.sh ]; then
    /usr/local/sbin/sa02m-cpu-profile.sh init >> "$LOG_FILE" 2>&1 || true
fi
if [ -f "$SCRIPT_DIR/sa02m-rs485-stats.sh" ]; then
    install -m 755 "$SCRIPT_DIR/sa02m-rs485-stats.sh" /usr/local/sbin/sa02m-rs485-stats.sh
else
    log WARN "Нет scripts/sa02m-rs485-stats.sh — RS-485 TX/RX в дашборде без sudo-helper"
fi
for _wu_unit in sa02m-web-update-check.service sa02m-web-update-check.timer; do
    if [ -f "$SYSTEMD_DIR/$_wu_unit" ]; then
        install -m 644 "$SYSTEMD_DIR/$_wu_unit" "/etc/systemd/system/$_wu_unit"
    fi
done
if [ ! -f /etc/sa02m_web.env ]; then
    if [ -f /usr/local/lib/sa02m-web-auth-lib.sh ]; then
        # shellcheck disable=SC1091
        . /usr/local/lib/sa02m-web-auth-lib.sh
        # Store the initial password hashed (S5); web_auth_write picks PASS_HASH
        # for a $6$ value. Fall back to plaintext only if hashing is unavailable.
        _bootstrap_hash=$(web_auth_hash "${ADMIN_PASS}")
        web_auth_write admin "${_bootstrap_hash:-$ADMIN_PASS}" > /tmp/sa02m_web.env.bootstrap
    else
        {
            echo "SA02M_WEB_USER='admin'"
            printf "SA02M_WEB_PASS='%s'\n" "$(printf '%s' "${ADMIN_PASS}" | sed "s/'/'\\\\''/g")"
        } > /tmp/sa02m_web.env.bootstrap
    fi
    install -m 640 -o root -g www-data /tmp/sa02m_web.env.bootstrap /etc/sa02m_web.env
    rm -f /tmp/sa02m_web.env.bootstrap
    log INFO "Создан /etc/sa02m_web.env (логин admin)"
fi
if [ -x /usr/local/sbin/sa02m-repair-web-env ]; then
    /usr/local/sbin/sa02m-repair-web-env >> "$LOG_FILE" 2>&1 || \
        log WARN "sa02m-repair-web-env завершился с ошибкой"
fi

# (The sa02m-commit-web-env grant is part of the single committed
# etc/sudoers.d/sa02m-www installed above — no post-hoc append; audit B1.)

# ── fcgiwrap: prefork service вместо узкого socket-activation ──────────────
if [ -f "$SYSTEMD_DIR/fcgiwrap.service" ]; then
    install -m 644 "$SYSTEMD_DIR/fcgiwrap.service" /etc/systemd/system/fcgiwrap.service
    # Останавливаем ВСЕ варианты stock socket-activation (Ubuntu/Debian),
    # чтобы их сокет-файлы (/run/fcgiwrap.socket) исчезли до старта нашего сервиса.
    for _sock_unit in fcgiwrap.socket fcgiwrap@.socket; do
        systemctl stop    "$_sock_unit" >> "$LOG_FILE" 2>&1 || true
        systemctl disable "$_sock_unit" >> "$LOG_FILE" 2>&1 || true
        systemctl mask    "$_sock_unit" >> "$LOG_FILE" 2>&1 || true
    done
    # Удаляем осиротевшие сокет-файлы вручную (systemd иногда не убирает их при stop)
    rm -f /run/fcgiwrap.socket /var/run/fcgiwrap.socket
    systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
fi

systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true

if [ -f /etc/systemd/system/sa02m-web-update-check.timer ]; then
    sa02m_svc_apply sa02m-web-update-check.timer infra start
    # One-shot check now (no enable — the timer owns the schedule).
    sa02m_svc_kick sa02m-web-update-check.service
fi
# (sa02m-update-recover is asserted once, above — next to its unit install.)

if [ -x /usr/local/sbin/sa02m-prepare-working-board ] && [ "${SA02M_PREPARE_WORKING_BOARD:-0}" = "1" ]; then
    log INFO "Включение безопасного режима для рабочей платы"
    /usr/local/sbin/sa02m-prepare-working-board prepare >> "$LOG_FILE" 2>&1 || \
        log WARN "Не удалось принудительно включить safe mode через sa02m-prepare-working-board"
fi

# 11-devices runs from install.sh ONLY (after 05-mqtt, gated by
# SA02M_SKIP_DEVICES) — the call that used to live here predated that gate and
# leaked past the skip (both landed in the same squash b9f3ad4).

# ── Start services ────────────────────────────────────────────────────────
sa02m_svc_apply fcgiwrap.service infra start restart
sa02m_svc_apply nginx.service infra start restart
sa02m_svc_apply sa02m-failure-monitor.service infra start

# ── Верификация: сокет fcgiwrap должен появиться в течение 5 с ──────────────
FCGI_SOCK="/run/fcgiwrap/fcgiwrap.socket"
_waited=0
while [ $_waited -lt 5 ]; do
    [ -S "$FCGI_SOCK" ] && break
    sleep 1
    _waited=$(( _waited + 1 ))
done
if [ -S "$FCGI_SOCK" ]; then
    log OK "fcgiwrap сокет готов: $FCGI_SOCK"
else
    log WARN "fcgiwrap сокет не найден после 5 с — проверьте: systemctl status fcgiwrap"
    log WARN "Возможная причина: stock fcgiwrap.socket мешал установке. Попробуйте:"
    log WARN "  systemctl mask fcgiwrap.socket && systemctl restart fcgiwrap"
fi

log OK "=== [03] Веб-сервер запущен на http://<IP>:${PORT} ==="
