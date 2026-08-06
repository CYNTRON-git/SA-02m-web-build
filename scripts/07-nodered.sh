#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 07-nodered.sh  •  Node-RED (Node.js + nodered.service) на СА-02м
#
# Два пути установки, оффлайн имеет приоритет:
#   1. vendor-payload  — заранее собранное дерево node-red 4.1.13 + Node 22
#      armv7l + unit. Работает без интернета; ставится ровно та версия,
#      которую собрали и проверили. Логика распаковки — одна, в
#      etc/sa02m-web-service-ctl.sh (её же вызывает кнопка в веб-панели).
#   2. официальный инсталлятор Node-RED (нужен интернет), с тем же пином
#      --node22 --nodered-version=4.1.13. Best-effort: NodeSource armhf
#      отдаёт свой 22.x, не обязательно тот же, что в payload.
# Нет ни payload, ни интернета — WARN и выход 0: отсутствующий
# опциональный стек не является ошибкой установщика (как 09-mplc.sh).
#
# Подготовка payload: docs/vendor-integrations.md → Node-RED (оффлайн).
# Запуск: sudo bash scripts/07-nodered.sh [--ip 192.168.1.136] [--no-start]
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [07] Установка Node-RED ==="

BASE_DIR="$SCRIPT_DIR/.."
NODERED_USER="${NODERED_USER:-nodered}"
NODERED_PORT="${NODERED_PORT:-1880}"
DEVICE_IP="${IP_ADDRESS:-192.168.1.136}"
NO_START=0
INSTALLER_URL="${NODERED_INSTALLER_URL:-https://raw.githubusercontent.com/node-red/linux-installers/master/deb/update-nodejs-and-nodered}"
# Пин версии. Один из его домов; остальные — etc/sa02m-web-service-ctl.sh,
# scripts/dev/build-nodered-payload.sh, vendor-src/nodered/package.json,
# docs/vendor-integrations.md. Расхождение валит quality-строку
# `nodered-pin-consistency`.
NODERED_PIN_VERSION="4.1.13"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)          DEVICE_IP="$2"; shift 2 ;;
        --port)        NODERED_PORT="$2"; shift 2 ;;
        --nodered-user) NODERED_USER="$2"; shift 2 ;;
        --no-start)    NO_START=1; shift ;;
        *)             shift ;;
    esac
done

# ── Поиск vendor-payload (тот же порядок, что в 09-mplc.sh) ────────────────
# Каталог считается payload'ом только если в нём ЕСТЬ и дерево node-red,
# и unit: половина payload'а — это не payload (иначе «установка» пройдёт,
# ничего не поставив).
nodered_payload_ok() {
    local d=$1 f has_tar=0 has_unit=0
    for f in "$d"/node-red-*.tar.*; do
        [ -f "$f" ] && { has_tar=1; break; }
    done
    for f in "$d/nodered.service" "$d/node-red.service"; do
        [ -f "$f" ] && { has_unit=1; break; }
    done
    [ "$has_tar" -eq 1 ] && [ "$has_unit" -eq 1 ]
}

NODERED_SRC="${SA02M_NODERED_DIR:-}"
if [ -n "$NODERED_SRC" ] && ! nodered_payload_ok "$NODERED_SRC"; then
    log WARN "SA02M_NODERED_DIR=$NODERED_SRC не содержит node-red-*.tar.* и nodered.service — игнорирую"
    NODERED_SRC=""
fi
if [ -z "$NODERED_SRC" ]; then
    for cand in /opt/vendor-installers/nodered "$BASE_DIR/vendor/nodered"; do
        if [ -d "$cand" ] && nodered_payload_ok "$cand"; then
            NODERED_SRC="$cand"
            break
        fi
    done
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

# Лимит памяти Node.js для плат с ~512 MiB RAM. Пишется ДО установки: unit
# читает этот файл при старте, а установка сама и стартует службу.
ENV_FILE="$NODERED_HOME/.node-red/environment"
touch "$ENV_FILE"
grep -q '^NODE_OPTIONS=' "$ENV_FILE" 2>/dev/null \
    || echo 'NODE_OPTIONS=--max-old-space-size=256' >> "$ENV_FILE"
grep -q '^PORT=' "$ENV_FILE" 2>/dev/null \
    || echo "PORT=${NODERED_PORT}" >> "$ENV_FILE"
chown "$NODERED_USER:$NODERED_USER" "$ENV_FILE" 2>/dev/null || true

# ── Путь 1: оффлайн из payload ─────────────────────────────────────────────
if [ -n "$NODERED_SRC" ]; then
    log INFO "Node-RED vendor-payload: $NODERED_SRC ($(du -sh "$NODERED_SRC" 2>/dev/null | awk '{print $1}'))"

    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        # В chroot нет запущенного systemd: распаковывать дерево и «стартовать»
        # службу здесь нечем и незачем — payload уже лежит в образе, установка
        # выполняется на устройстве (кнопкой в панели или повторным install.sh).
        log INFO "Сборка rootfs: payload остаётся в образе, установка Node-RED выполняется на устройстве"
        log OK "=== [07] Node-RED: payload запечён, установка отложена ==="
        exit 0
    fi

    # Логика распаковки живёт в ОДНОМ месте — ctl-скрипте (его же вызывает
    # кнопка «Установить»); дублировать её здесь означало бы два дома, которые
    # разъедутся. 03-webserver.sh кладёт ctl раньше (install.sh: 03 → 07);
    # при отдельном запуске 07 берём копию из дерева репозитория.
    CTL=""
    for cand in /usr/local/sbin/sa02m-web-service-ctl.sh "$BASE_DIR/etc/sa02m-web-service-ctl.sh"; do
        [ -f "$cand" ] && { CTL="$cand"; break; }
    done
    if [ -z "$CTL" ]; then
        log ERR "Не найден sa02m-web-service-ctl.sh — оффлайн-установка Node-RED невозможна"
        exit 1
    fi
    log INFO "Оффлайн-установка Node-RED $NODERED_PIN_VERSION через $CTL"
    # Запомнить состояние ДО установки: ctl поднимает службу сам, и для
    # --no-start её надо будет вернуть в исходное — но только если до нас она
    # не работала. Останавливать чужую работающую службу флаг не должен.
    NR_WAS_ACTIVE=0
    systemctl is-active --quiet nodered.service 2>/dev/null && NR_WAS_ACTIVE=1
    if CTL_OUT=$(SA02M_NODERED_DIR="$NODERED_SRC" sh "$CTL" install node-red 2>>"$LOG_FILE"); then
        log OK "Node-RED установлен из payload: $CTL_OUT"
    else
        log ERR "Оффлайн-установка Node-RED не удалась: ${CTL_OUT:-нет ответа} (подробности в $LOG_FILE)"
        exit 1
    fi
    # ctl уже включил, запустил и проверил службу на уровне потоков (его вердикт
    # — в JSON выше). Повторять рестарт и собственную проверку здесь значило бы
    # завести второй, расходящийся дом для вердикта: у ctl опрос до ~45 с, а
    # фиксированная пауза здесь давала бы WARN на здоровой установке — в том
    # самом канале WARN, который docs/deployment.md назначил признаком
    # пропущенного стека.
    NODERED_VERIFIED_BY_CTL=1
# ── Путь 2: официальный инсталлятор (нужен интернет) ───────────────────────
elif curl -fsS --max-time 15 -I https://registry.npmjs.org/node-red >/dev/null 2>&1 \
     && curl -fsS --max-time 15 -I "$INSTALLER_URL" >/dev/null 2>&1; then
    log INFO "Проверка зависимостей..."
    apt-get update -qq >> "$LOG_FILE" 2>&1 || true
    # build-essential/python3 нужны только этому пути: онлайн-установка может
    # собирать нативные модули. Оффлайн-дерево чистый JS — компилятор не нужен.
    pkg_install curl ca-certificates gnupg build-essential python3

    log INFO "Запуск официального инсталлятора Node-RED (Node.js 22, node-red $NODERED_PIN_VERSION, без Pi-нод)..."
    log INFO "Журнал инсталлятора: /var/log/nodered-install.log"

    INSTALL_ARGS=(
        --confirm-root
        --confirm-install
        --skip-pi
        --no-init
        --node22
        "--nodered-version=${NODERED_PIN_VERSION}"
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

    # Инсталлятор — неприпиненный сторонний скрипт: проверяем, что пин
    # действительно встал, по package.json (НЕ запуская node-red).
    NR_PKG=/usr/lib/node_modules/node-red/package.json
    NR_GOT=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$NR_PKG" 2>/dev/null | head -n1)
    if [ "$NR_GOT" != "$NODERED_PIN_VERSION" ]; then
        log ERR "Установлен node-red ${NR_GOT:-неизвестно}, ожидался пин $NODERED_PIN_VERSION"
        exit 1
    fi
    log OK "node-red $NR_GOT (пин соблюдён)"
# ── Ни payload, ни интернета ───────────────────────────────────────────────
else
    log WARN "Node-RED: нет ни vendor-payload (искали /opt/vendor-installers/nodered/, vendor/nodered/), ни доступа к registry.npmjs.org."
    log INFO "Как подготовить оффлайн-пакет: docs/vendor-integrations.md → Node-RED (оффлайн)."
    log INFO "Пропускаю установку Node-RED (это не ошибка)."
    exit 0
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

if [ "${NODERED_VERIFIED_BY_CTL:-0}" = "1" ]; then
    # Оффлайн-путь: служба уже поднята и проверена ctl-скриптом. Второй рестарт
    # только удлинил бы простой, а вторая проверка разошлась бы с первой.
    if [ "$NO_START" -eq 1 ] && [ "$NR_WAS_ACTIVE" -eq 0 ]; then
        # Флаг обещает «не запускать»: возвращаем в исходное состояние — но
        # только если до запуска скрипта служба НЕ работала (чужую работающую
        # службу флаг ронять не должен).
        sa02m_systemctl stop "$NR_UNIT" >> "$LOG_FILE" 2>&1 || true
        log INFO "$NR_UNIT остановлен обратно (--no-start); включите: systemctl start $NR_UNIT"
    fi
elif [ "$NO_START" -eq 0 ]; then
    RESTART_SINCE=$(date '+%Y-%m-%d %H:%M:%S')
    sa02m_systemctl restart "$NR_UNIT" >> "$LOG_FILE" 2>&1 \
        && log OK "$NR_UNIT запущен" \
        || log WARN "$NR_UNIT не стартовал — journalctl -u $NR_UNIT -n 50"
else
    log INFO "$NR_UNIT не запускался (--no-start); включите: systemctl start $NR_UNIT"
fi

# ── Проверка: порт + УРОВЕНЬ ПОТОКОВ ───────────────────────────────────────
# Открытый 1880 — ложно-зелёный признак: рантайм, поднявшийся с остановленными
# потоками (нет типов нод), слушает точно так же. Судим по журналу.
if ss -H -ltn "sport = :${NODERED_PORT}" 2>/dev/null | grep -q ":${NODERED_PORT}"; then
    log OK "Node-RED слушает порт ${NODERED_PORT}"
else
    log WARN "Порт ${NODERED_PORT} не обнаружен — проверьте: systemctl status $NR_UNIT"
fi

# Вердикт по потокам — только для онлайн-пути: оффлайн его уже вынес ctl.
# Ждём столько же, сколько ждёт ctl (~45 с опроса, а не фиксированная пауза):
# холодный старт Node-RED на A40i — секунды, и короткая пауза давала бы WARN
# на здоровой установке. Маркеры — из каталогов en-US и ru
# (@node-red/runtime/locales/*/runtime.json): рантайм локализует свой лог, и на
# плате с ru-локалью английские литералы не сработали бы вовсе.
if [ "${NODERED_VERIFIED_BY_CTL:-0}" != "1" ] && [ "$NO_START" -eq 0 ] \
   && command -v journalctl >/dev/null 2>&1; then
    NR_STARTED=0
    NR_STOPPED=0
    for _ in $(seq 1 15); do
        sleep 3
        NR_JOURNAL=$(journalctl -u "$NR_UNIT" --since "$RESTART_SINCE" --no-pager -n 500 2>/dev/null)
        if grep -q 'Started flows\|Запущены потоки' <<<"$NR_JOURNAL"; then
            NR_STARTED=1
            break
        fi
        if grep -q 'Waiting for missing types to be registered\|Ожидание регистрации отсутствующих типов\|Failed to load external modules\|Flows stopped in safe mode\|Потоки остановлены в безопасном режиме\|Stopped flows\|Остановлены потоки\|Error loading credentials\|Ошибка при загрузке учетных данных\|Unsupported version of\|Неподдерживаемая версия' <<<"$NR_JOURNAL"; then
            NR_STOPPED=1
            break
        fi
    done
    if [ "$NR_STARTED" -eq 1 ]; then
        log OK "Node-RED: потоки запущены"
    elif [ "$NR_STOPPED" -eq 1 ] || [ -n "$NR_JOURNAL" ]; then
        # Журнал читается и за всё окно опроса не сказал, что потоки пошли.
        log ERR "Node-RED поднялся, но потоки НЕ работают (нет типов нод / не загрузились модули / безопасный режим / ошибка учётных данных / неподдерживаемый Node) — journalctl -u $NR_UNIT"
        exit 1
    else
        log WARN "Node-RED: журнал службы пуст — состояние потоков подтвердить нечем"
    fi
fi

# Версию читаем из package.json, а НЕ запуском `node-red --version`: рантайм с
# неподходящим Node падает на старте, и «проверка» превращается в сбой.
NR_VER_FILE=/usr/lib/node_modules/node-red/package.json
if [ -f "$NR_VER_FILE" ]; then
    nrv=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$NR_VER_FILE" | head -n1)
    [ -n "$nrv" ] && log OK "Node-RED $nrv, пользователь $NODERED_USER"
fi

log OK "=== [07] Node-RED установлен ==="
log INFO "UI: http://${DEVICE_IP}:${NODERED_PORT}/"
log INFO "Управление из веб СА-02м: Управление → Службы → Node-RED"
log WARN "Не выставляйте Node-RED в интернет без adminAuth — см. https://nodered.org/docs/user-guide/runtime/securing-node-red"
