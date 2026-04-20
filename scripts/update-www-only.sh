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

find "$WEB_ROOT/cgi-bin" -name '*.cgi' -exec chmod 755 {} \;
find "$WEB_ROOT/static" \( -name '*.css' -o -name '*.js' -o -name '*.svg' \) -exec chmod 644 {} \;
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html" 2>/dev/null || true
chown -R www-data:www-data "$WEB_ROOT"

log OK "Веб-интерфейс обновлён: $WEB_ROOT (nginx перезапускать не требуется)"
