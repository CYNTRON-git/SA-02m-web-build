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
    listen ${PORT};
    root ${WEB_ROOT};
    index index.html;

    # Static files served directly
    location /static/ { expires 1d; }
    location = /       { try_files /index.html =404; }
    location = /login.html { try_files \$uri =404; }

    # CGI scripts
    location /cgi-bin/ {
        fastcgi_pass  unix:/var/run/fcgiwrap.socket;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        include fastcgi_params;
    }

    # Auth required for everything except login
    location / {
        try_files \$uri \$uri/ /index.html;
    }

    access_log /var/log/nginx/sa02m_access.log;
    error_log  /var/log/nginx/sa02m_error.log;
}
NGINX
fi

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/network_config /etc/nginx/sites-enabled/network_config
nginx -t >> "$LOG_FILE" 2>&1 && log OK "nginx config OK"

# ── Deploy web files ──────────────────────────────────────────────────────
log INFO "Деплой файлов в $WEB_ROOT"
mkdir -p "$WEB_ROOT/cgi-bin" "$WEB_ROOT/static/css" "$WEB_ROOT/static/js"
rm -rf  "${WEB_ROOT:?}/cgi-bin/"*
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
# GPIO для DO / пищалки / аварийного LED (sysfs /sys/class/gpio/gpioN)
# Заполните номер линии для вашей платы; пусто = функция отключена
SA02M_GPIO_DO=
SA02M_GPIO_BEEPER=
SA02M_GPIO_ALARM_LED=
HWCONF
    chmod 644 /etc/sa02m_hw.conf
fi

# ── sudoers for www-data ──────────────────────────────────────────────────
log INFO "Настройка sudoers"
cat > /etc/sudoers.d/sa02m-www <<'SUDO'
www-data ALL=(ALL) NOPASSWD: /usr/bin/tee, /bin/date, /sbin/hwclock, \
    /usr/bin/timedatectl, /sbin/ifdown, /sbin/ifup, /sbin/reboot, \
    /usr/bin/systemctl restart nginx, /usr/bin/systemctl restart fcgiwrap, \
    /usr/bin/systemctl restart networking
SUDO
chmod 440 /etc/sudoers.d/sa02m-www
visudo -cf /etc/sudoers.d/sa02m-www >> "$LOG_FILE" 2>&1 && log OK "sudoers OK"

# ── Start services ────────────────────────────────────────────────────────
svc_enable fcgiwrap
svc_restart nginx
svc_restart fcgiwrap

log OK "=== [03] Веб-сервер запущен на http://<IP>:${PORT} ==="
