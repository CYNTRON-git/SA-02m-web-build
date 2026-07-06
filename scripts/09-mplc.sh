#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 09-mplc.sh  •  MasterSCADA MPLC 4D Runtime (armv7hf) на СА-02м
#
# Устанавливает MPLC 4D Runtime (mplc4) через штатный vendor install.sh
# из /opt/vendor-installers/mplc4/. Плагин mplc_cyntron.so (собственный
# драйвер ЦИНТРОН для MPLC) копируется в /opt/mplc4/ после сборки runtime.
#
# Порт по умолчанию: 8082 (nginx-фронтенд MPLC). SA-02m nginx использует
# 9999 — конфликта нет, но 80 намеренно не занимаем, чтобы не мешать
# сторонним UI на промышленных стендах.
#
# Источник vendor-payload:
#   1. $SA02M_MPLC_DIR              — явный путь к каталогу с install.sh.
#   2. /opt/vendor-installers/mplc4 — на устройстве (после pscp с ПК).
#   3. $SCRIPT_DIR/../vendor/mplc4  — рядом с репо.
#
# Отключить: SA02M_SKIP_MPLC=1 ./install.sh
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [09] Установка MasterSCADA MPLC 4D Runtime ==="

BASE_DIR="$SCRIPT_DIR/.."

MPLC_SRC="${SA02M_MPLC_DIR:-}"
if [ -z "$MPLC_SRC" ]; then
    for cand in /opt/vendor-installers/mplc4 "$BASE_DIR/vendor/mplc4"; do
        if [ -d "$cand" ] && [ -f "$cand/install.sh" ] && [ -f "$cand/mplc4.tar.gz" ]; then
            MPLC_SRC="$cand"
            break
        fi
    done
fi

if [ -z "$MPLC_SRC" ] || [ ! -d "$MPLC_SRC" ]; then
    log WARN "MPLC vendor-payload не найден (искали /opt/vendor-installers/mplc4/, vendor/mplc4/)."
    log INFO "Как подготовить дистрибутив: см. docs/vendor-integrations.md → MasterSCADA MPLC."
    log INFO "Пропускаю установку MPLC (это не ошибка)."
    exit 0
fi

for req in install.sh mplc4.tar.gz nginx.tar.gz; do
    if [ ! -f "$MPLC_SRC/$req" ]; then
        log ERR "MPLC vendor-payload $MPLC_SRC/$req не найден — установка невозможна"
        exit 1
    fi
done

log INFO "MPLC vendor-payload: $MPLC_SRC ($(du -sh "$MPLC_SRC" 2>/dev/null | awk '{print $1}'))"

MPLC_HTTP_PORT="${SA02M_MPLC_HTTP_PORT:-8082}"

if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    if ss -H -ltn "sport = :${MPLC_HTTP_PORT}" 2>/dev/null | grep -q ":${MPLC_HTTP_PORT}"; then
        _who=$(ss -H -ltnp "sport = :${MPLC_HTTP_PORT}" 2>/dev/null | awk '{print $NF}' | head -1)
        log WARN "Порт ${MPLC_HTTP_PORT}/TCP уже занят ($_who) — MPLC nginx может не стартовать"
    fi
fi

pkg_install openssl

if dpkg -l | grep -qE '^ii\s+mplc4' 2>/dev/null; then
    log INFO "mplc4 dpkg-пакет обнаружен — vendor install.sh обновит установку"
fi

MPLC_INSTALL_LOG="/var/log/sa02m_mplc_install.log"
log INFO "Запуск vendor install.sh (лог: $MPLC_INSTALL_LOG)"
(
    cd "$MPLC_SRC" || exit 1
    chmod +x ./install.sh
    bash ./install.sh --use-systemd --http-port="$MPLC_HTTP_PORT" --enable-log
) >"$MPLC_INSTALL_LOG" 2>&1
_rc=$?
if [ $_rc -ne 0 ]; then
    log ERR "vendor install.sh завершился с кодом $_rc — см. $MPLC_INSTALL_LOG"
    tail -20 "$MPLC_INSTALL_LOG" 2>/dev/null | while read -r line; do
        log WARN "  $line"
    done
    exit 1
fi
log OK "MPLC 4D Runtime установлен в /opt/mplc4/"

if [ -f "$MPLC_SRC/mplc_cyntron.so" ]; then
    install -m 0755 "$MPLC_SRC/mplc_cyntron.so" /opt/mplc4/mplc_cyntron.so
    log OK "Плагин mplc_cyntron.so установлен в /opt/mplc4/"
else
    log WARN "mplc_cyntron.so не найден в $MPLC_SRC — плагин ЦИНТРОН не установлен"
fi

if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
    systemctl enable mplc4 >>"$LOG_FILE" 2>&1 || true
    systemctl restart mplc4 >>"$LOG_FILE" 2>&1 || true
    sleep 3

    if systemctl is-active --quiet mplc4; then
        log OK "mplc4.service запущен"
    else
        log WARN "mplc4.service не активен — journalctl -u mplc4 -n 40"
    fi

    if ss -H -ltn "sport = :${MPLC_HTTP_PORT}" 2>/dev/null | grep -q ":${MPLC_HTTP_PORT}"; then
        log OK "MPLC nginx слушает порт ${MPLC_HTTP_PORT}/TCP"
    else
        log WARN "MPLC nginx не слушает порт ${MPLC_HTTP_PORT}/TCP"
    fi

    for port in 30750 31550; do
        if ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q ":${port}"; then
            log OK "MPLC ${port}/TCP занят (fcgi/monitor)"
        fi
    done
else
    log INFO "SA02M_ROOTFS_BUILD=1 — mplc4 не запускаем в chroot"
    systemctl --root="${SA02M_ROOTFS_ROOT:-/}" enable mplc4 >>"$LOG_FILE" 2>&1 || true
fi

log OK "=== [09] MPLC 4D Runtime установлен ==="
log INFO "UI:       http://${IP_ADDRESS:-192.168.1.136}:${MPLC_HTTP_PORT}/"
log INFO "Управление из веб-панели СА-02м: Управление → Службы → MPLC4"
