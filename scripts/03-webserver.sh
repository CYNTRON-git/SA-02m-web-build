#!/bin/bash
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

    location /cgi-bin/ {
        include        fastcgi_params;
        fastcgi_param  SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param  HTTP_COOKIE     \$http_cookie;
        fastcgi_pass   unix:/run/fcgiwrap.socket;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    access_log /var/log/nginx/sa02m_access.log;
    error_log  /var/log/nginx/sa02m_error.log warn;
}
NGINX
fi

# Сокет fcgiwrap: в шаблоне /run; если на образе только /var/run — подставить существующий путь
ACTIVE_FCGI="/run/fcgiwrap.socket"
[ -S "$ACTIVE_FCGI" ] || ACTIVE_FCGI="/var/run/fcgiwrap.socket"
if [ -S "$ACTIVE_FCGI" ]; then
    sed -i "s|unix:/run/fcgiwrap.socket|unix:${ACTIVE_FCGI}|g" /etc/nginx/sites-available/network_config
else
    log WARN "Сокет fcgiwrap не найден (/run и /var/run) — проверьте unit fcgiwrap.socket"
fi

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

# Permissions
find "$WEB_ROOT/cgi-bin" -name "*.cgi" -exec chmod 755 {} \;
find "$WEB_ROOT/static"  \( -name "*.css" -o -name "*.js" -o -name "*.svg" \) -exec chmod 644 {} \;
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html"
chown -R www-data:www-data "$WEB_ROOT"

# ── GPIO hw.conf ──────────────────────────────────────────────────────────
if [ ! -f /etc/sa02m_hw.conf ]; then
    log INFO "Создание /etc/sa02m_hw.conf (шаблон)"
    cat > /etc/sa02m_hw.conf <<'HWCONF'
# GPIO для DO / пищалки / аварийного LED / питания USB (sysfs /sys/class/gpio/gpioN)
# Заполните номер линии для вашей платы; пусто = функция отключена
SA02M_GPIO_DO=
SA02M_GPIO_BEEPER=
SA02M_GPIO_ALARM_LED=
SA02M_GPIO_USB_POWER=
HWCONF
    chmod 644 /etc/sa02m_hw.conf
fi

# ── sudoers for www-data ──────────────────────────────────────────────────
log INFO "Настройка sudoers"
cat > /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/bin/tee, /bin/date, /sbin/hwclock, \
    /usr/bin/timedatectl, /sbin/ifdown, /sbin/ifup, /sbin/reboot, \
    /usr/bin/systemctl restart nginx, /usr/bin/systemctl restart fcgiwrap, \
    /usr/bin/systemctl restart networking, /usr/sbin/i2cget, /usr/bin/i2cget, \
    /usr/local/sbin/sa02m-set-storage-auto-format
SUDO
chmod 440 /etc/sudoers.d/sa02m-www
visudo -cf /etc/sudoers.d/sa02m-www >> "$LOG_FILE" 2>&1 && log OK "sudoers OK"

# ── Учётные данные веб-интерфейса (/etc/sa02m_web.env) ─────────────────────
if [ -f "$SCRIPT_DIR/../etc/sa02m-commit-web-env.sh" ]; then
    install -m 755 "$SCRIPT_DIR/../etc/sa02m-commit-web-env.sh" /usr/local/sbin/sa02m-commit-web-env
else
    log WARN "Нет etc/sa02m-commit-web-env.sh — смена пароля через веб будет недоступна"
fi
if [ ! -f /etc/sa02m_web.env ]; then
    {
        echo "SA02M_WEB_USER=admin"
        echo "SA02M_WEB_PASS=${ADMIN_PASS}"
    } > /tmp/sa02m_web.env.bootstrap
    install -m 640 -o root -g www-data /tmp/sa02m_web.env.bootstrap /etc/sa02m_web.env
    rm -f /tmp/sa02m_web.env.bootstrap
    log INFO "Создан /etc/sa02m_web.env (логин admin)"
fi

grep -q 'sa02m-commit-web-env' /etc/sudoers.d/sa02m-www 2>/dev/null || {
    printf '\nwww-data ALL=(ALL) NOPASSWD: /usr/local/sbin/sa02m-commit-web-env\n' >> /etc/sudoers.d/sa02m-www
    chmod 440 /etc/sudoers.d/sa02m-www
    visudo -cf /etc/sudoers.d/sa02m-www >> "$LOG_FILE" 2>&1 || log WARN "visudo после доп. правила — проверьте sudoers"
}

# ── Start services ────────────────────────────────────────────────────────
svc_enable fcgiwrap
svc_restart nginx
svc_restart fcgiwrap

log OK "=== [03] Веб-сервер запущен на http://<IP>:${PORT} ==="
