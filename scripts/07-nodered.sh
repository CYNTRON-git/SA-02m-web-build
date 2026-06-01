#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 07-nodered.sh  •  Node-RED (Node.js LTS + nodered.service) на СА-02м
#
# Официальный инсталлятор Node-RED, адаптированный для headless ARM (A40i):
#   - неинтерактивный режим (--confirm-root --confirm-install --skip-pi)
#   - отдельный пользователь nodered (не root)
#   - Node.js 20 LTS (--node20), лимит памяти под ~512 MiB RAM
#   - systemd unit nodered.service + enable + start
#
# Требует интернет на устройстве (apt + nodesource + npm registry).
# Запуск: sudo bash scripts/07-nodered.sh [--ip 192.168.1.136] [--no-start]
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [07] Установка Node-RED ==="

NODERED_USER="${NODERED_USER:-nodered}"
NODERED_PORT="${NODERED_PORT:-1880}"
DEVICE_IP="${IP_ADDRESS:-192.168.1.136}"
NO_START=0
INSTALLER_URL="${NODERED_INSTALLER_URL:-https://raw.githubusercontent.com/node-red/linux-installers/master/deb/update-nodejs-and-nodered}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)          DEVICE_IP="$2"; shift 2 ;;
        --port)        NODERED_PORT="$2"; shift 2 ;;
        --nodered-user) NODERED_USER="$2"; shift 2 ;;
        --no-start)    NO_START=1; shift ;;
        *)             shift ;;
    esac
done

# ── Зависимости ───────────────────────────────────────────────────────────
log INFO "Проверка зависимостей..."
apt-get update -qq >> "$LOG_FILE" 2>&1 || true
pkg_install curl ca-certificates gnupg build-essential python3

if ! curl -fsS --max-time 15 -I https://registry.npmjs.org/node-red >/dev/null 2>&1; then
    log ERR "Нет доступа к registry.npmjs.org — для Node-RED нужен интернет"
    exit 1
fi
if ! curl -fsS --max-time 15 -I "$INSTALLER_URL" >/dev/null 2>&1; then
    log ERR "Не удалось загрузить официальный инсталлятор: $INSTALLER_URL"
    exit 1
fi

# ── Пользователь nodered ────────────────────────────────────────────────────
if ! id "$NODERED_USER" &>/dev/null; then
    log INFO "Создание системного пользователя $NODERED_USER"
    useradd -r -m -d "/home/$NODERED_USER" -s /usr/sbin/nologin "$NODERED_USER" >> "$LOG_FILE" 2>&1 \
        || log WARN "useradd $NODERED_USER — проверьте вручную"
fi
NODERED_HOME=$(getent passwd "$NODERED_USER" | cut -d: -f6)
[ -n "$NODERED_HOME" ] || NODERED_HOME="/home/$NODERED_USER"
mkdir -p "$NODERED_HOME/.node-red"
chown -R "$NODERED_USER:$NODERED_USER" "$NODERED_HOME" 2>/dev/null || true

# ── Официальный инсталлятор (headless) ────────────────────────────────────
log INFO "Запуск официального инсталлятора Node-RED (Node.js 20 LTS, без Pi-нод)..."
log INFO "Журнал инсталлятора: /var/log/nodered-install.log"

INSTALL_ARGS=(
    --confirm-root
    --confirm-install
    --skip-pi
    --no-init
    --node20
    --restart
    "--nodered-user=${NODERED_USER}"
)

if bash <(curl -fsSL "$INSTALLER_URL") "${INSTALL_ARGS[@]}" >> "$LOG_FILE" 2>&1; then
    log OK "Официальный инсталлятор Node-RED завершён"
else
    log ERR "Инсталлятор Node-RED завершился с ошибкой — см. /var/log/nodered-install.log"
    tail -30 /var/log/nodered-install.log 2>/dev/null | while read -r line; do
        log WARN "  $line"
    done
    exit 1
fi

# ── settings.js: доступ по IP шлюза (0.0.0.0) ─────────────────────────────
SETTINGS_JS="$NODERED_HOME/.node-red/settings.js"
NR_DEFAULT=/usr/lib/node_modules/node-red/settings.js
if [ ! -f "$SETTINGS_JS" ] && [ -f "$NR_DEFAULT" ]; then
    cp "$NR_DEFAULT" "$SETTINGS_JS"
    chown "$NODERED_USER:$NODERED_USER" "$SETTINGS_JS"
    log INFO "Создан settings.js из шаблона Node-RED"
fi
if [ -f "$SETTINGS_JS" ]; then
    if grep -qE '^[[:space:]]*uiHost:' "$SETTINGS_JS"; then
        sed -i 's/^\([[:space:]]*uiHost:\)[[:space:]]*.*/\1 "0.0.0.0",/' "$SETTINGS_JS"
    elif grep -qE '^[[:space:]]*//[[:space:]]*uiHost:' "$SETTINGS_JS"; then
        sed -i 's/^\([[:space:]]*\)\/\/[[:space:]]*uiHost:.*/\1uiHost: "0.0.0.0",/' "$SETTINGS_JS"
    else
        sed -i '/uiPort:/a\    uiHost: "0.0.0.0",' "$SETTINGS_JS"
    fi
    chown "$NODERED_USER:$NODERED_USER" "$SETTINGS_JS"
    log OK "settings.js: uiHost=0.0.0.0 (доступ http://<IP>:${NODERED_PORT})"
fi

# Лимит памяти Node.js для плат с ~512 MiB RAM
ENV_FILE="$NODERED_HOME/.node-red/environment"
touch "$ENV_FILE"
grep -q '^NODE_OPTIONS=' "$ENV_FILE" 2>/dev/null \
    || echo 'NODE_OPTIONS=--max-old-space-size=256' >> "$ENV_FILE"
grep -q '^PORT=' "$ENV_FILE" 2>/dev/null \
    || echo "PORT=${NODERED_PORT}" >> "$ENV_FILE"
chown "$NODERED_USER:$NODERED_USER" "$ENV_FILE" 2>/dev/null || true

# ── systemd: enable (+ start) ───────────────────────────────────────────────
NR_UNIT=""
for u in nodered.service node-red.service; do
    if systemctl cat "$u" &>/dev/null; then
        NR_UNIT="$u"
        break
    fi
done

if [ -z "$NR_UNIT" ]; then
    log ERR "Не найден unit nodered.service / node-red.service после установки"
    exit 1
fi

sa02m_systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
sa02m_systemctl enable "$NR_UNIT" >> "$LOG_FILE" 2>&1 \
    && log OK "$NR_UNIT включён (systemctl enable)" \
    || log WARN "Не удалось enable $NR_UNIT"

if [ "$NO_START" -eq 0 ]; then
    sa02m_systemctl restart "$NR_UNIT" >> "$LOG_FILE" 2>&1 \
        && log OK "$NR_UNIT запущен" \
        || log WARN "$NR_UNIT не стартовал — journalctl -u $NR_UNIT -n 50"
    sleep 2
else
    log INFO "$NR_UNIT не запускался (--no-start); включите: systemctl start $NR_UNIT"
fi

# ── Проверка порта ─────────────────────────────────────────────────────────
if ss -H -ltn "sport = :${NODERED_PORT}" 2>/dev/null | grep -q ":${NODERED_PORT}"; then
    log OK "Node-RED слушает порт ${NODERED_PORT}"
else
    log WARN "Порт ${NODERED_PORT} не обнаружен — проверьте: systemctl status $NR_UNIT"
fi

if command -v node-red &>/dev/null; then
    nrv=$(node-red --version 2>/dev/null | head -1 || true)
    [ -n "$nrv" ] && log OK "Node-RED $nrv, пользователь $NODERED_USER"
fi

log OK "=== [07] Node-RED установлен ==="
log INFO "UI: http://${DEVICE_IP}:${NODERED_PORT}/"
log INFO "Управление из веб СА-02м: Управление → Службы → Node-RED"
log WARN "Не выставляйте Node-RED в интернет без adminAuth — см. https://nodered.org/docs/user-guide/runtime/securing-node-red"
