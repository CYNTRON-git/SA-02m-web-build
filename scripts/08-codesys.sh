#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 08-codesys.sh  •  CODESYS Control for Linux ARM SL (v4.20.0.0) на СА-02м
#
# INSTALL-ONLY: ставит CODESYS Control runtime из .deb (armhf), полученного из
# .package-файла IDE (см. docs/vendor-integrations.md), но НЕ включает
# автозапуск и НЕ запускает runtime — политика kernel-conditional служб
# (docs/contracts/kernel-conditional-services.md): CODESYS/CodeMeter стартуют
# только вручную (веб-панель / systemctl); apply-policy снимает автозапуск.
# Активация лицензии выполняется вручную через CODESYS Development System —
# оставляем демо-режим, runtime сам активирует Standard-S при поступлении
# .wbc файла в /var/opt/codesys/.
#
# Источник .deb:
#   1. $SA02M_CODESYS_DEB      — явный путь к armhf .deb (для CI/rootfs build).
#   2. /opt/vendor-installers/codesys/codesyscontrol_*_armhf.deb — на устройстве.
#   3. $SCRIPT_DIR/../vendor/codesys/codesyscontrol_*_armhf.deb — рядом с репо.
#
# Отключить: SA02M_SKIP_CODESYS=1 ./install.sh
#
# Требования:
#   - Порт 11740/TCP свободен (CODESYS Gateway).
#   - Порт  1217/UDP свободен (CODESYS Discovery).
#   - Порт  4840/TCP свободен (OPC UA server, опционально).
#   - Пакет codemeter-lite (WIBU) отсутствует в Debian bullseye main
#     → устанавливаем через --force-depends; runtime стартует в demo-режиме
#     (~2 часа) до активации лицензии через IDE.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [08] Установка CODESYS Control for Linux ARM SL ==="

BASE_DIR="$SCRIPT_DIR/.."
ETC_DIR="$BASE_DIR/etc"

# ── Вердикт политики стеков — ДО поиска .deb и dpkg ────────────────────────
# docs/contracts/installer-refresh-policy.md: refresh никогда не (пере)ставляет
# сторонний стек; установленный получает только sa02m-надстройку (drop-in,
# apt-hold, apply-policy — хвост ниже); отключённый оператором не ставится ни
# в одном режиме без --with-optional.
_VERDICT=$(sa02m_stack_verdict CODESYS)
case "$_VERDICT" in
    skip-disabled)
        log INFO "CODESYS: отключён оператором (/etc/sa02m_stacks.conf) — пропуск; вернуть: кнопка «Установить» в панели или --with-optional"
        exit 0
        ;;
    skip-absent)
        log INFO "refresh: CODESYS не установлен — не ставлю (кнопка «Установить» или --with-optional)"
        exit 0
        ;;
    overlay)
        _cur=$(dpkg -s codesyscontrol 2>/dev/null | awk -F': ' '/^Version:/{print $2; exit}')
        log INFO "refresh: CODESYS установлен (${_cur:-версия неизвестна}) — обновляю только надстройку sa02m, стек не переустанавливаю"
        skip_install=1
        ;;
esac

if [ "${skip_install:-0}" != "1" ]; then
    CODESYS_DEB="${SA02M_CODESYS_DEB:-}"
    if [ -z "$CODESYS_DEB" ]; then
        for cand in \
            /opt/vendor-installers/codesys/codesyscontrol_linuxarm_*_armhf.deb \
            /opt/vendor-installers/codesys/codesyscontrol_*_armhf.deb \
            "$BASE_DIR/vendor/codesys/codesyscontrol_linuxarm_*_armhf.deb" \
            "$BASE_DIR/vendor/codesys/codesyscontrol_*_armhf.deb"; do
            for f in $cand; do
                [ -f "$f" ] && CODESYS_DEB="$f" && break 2
            done
        done
    fi

    if [ -z "$CODESYS_DEB" ] || [ ! -f "$CODESYS_DEB" ]; then
        log WARN "CODESYS .deb не найден (искали /opt/vendor-installers/codesys/, vendor/codesys/)."
        log INFO "Как подготовить дистрибутив: см. docs/vendor-integrations.md → CODESYS Runtime."
        log INFO "Пропускаю установку CODESYS (это не ошибка)."
        exit 0
    fi

    log INFO "CODESYS deb: $CODESYS_DEB ($(stat -c%s "$CODESYS_DEB" 2>/dev/null || echo '?') bytes)"

    if command -v dpkg-deb >/dev/null 2>&1; then
        _arch=$(dpkg-deb --field "$CODESYS_DEB" Architecture 2>/dev/null | tr -d '\r\n')
        _host_arch=$(dpkg --print-architecture 2>/dev/null)
        if [ -n "$_arch" ] && [ "$_arch" != "$_host_arch" ] && [ "$_arch" != "all" ]; then
            log ERR "Архитектура пакета ($_arch) не совпадает с системой ($_host_arch)."
            exit 1
        fi
    fi

    if dpkg -s codesyscontrol >/dev/null 2>&1; then
        _cur=$(dpkg -s codesyscontrol 2>/dev/null | awk -F': ' '/^Version:/{print $2; exit}')
        _new=$(dpkg-deb --field "$CODESYS_DEB" Version 2>/dev/null | tr -d '\r\n')
        if [ -n "$_cur" ] && [ "$_cur" = "$_new" ]; then
            log INFO "codesyscontrol $_cur уже установлен — переустановка не требуется."
            skip_install=1
        fi
    fi
fi

if [ "${skip_install:-0}" != "1" ]; then
    log INFO "dpkg -i --force-depends $CODESYS_DEB (codemeter-lite отсутствует в Debian main)"
    if DEBIAN_FRONTEND=noninteractive dpkg -i --force-depends "$CODESYS_DEB" >>"$LOG_FILE" 2>&1; then
        log OK "codesyscontrol установлен"
    else
        log ERR "dpkg -i завершился с ошибкой — см. $LOG_FILE"
        exit 1
    fi
fi

# Стек на месте — политика фиксирует present (кнопка «Удалить» пишет disabled).
if [ "$_VERDICT" = install ] && dpkg -s codesyscontrol >/dev/null 2>&1; then
    sa02m_stack_policy_set CODESYS present || true
fi

if [ -x "$ETC_DIR/sa02m-apt-hold-codesys.sh" ] || [ -f "$ETC_DIR/sa02m-apt-hold-codesys.sh" ]; then
    bash "$ETC_DIR/sa02m-apt-hold-codesys.sh" >>"$LOG_FILE" 2>&1 || true
else
    apt-mark hold codesyscontrol >>"$LOG_FILE" 2>&1 || true
fi
log INFO "apt-mark hold codesyscontrol — apt-get -f install не снимет пакет"

# Deploy systemd drop-in для codesyscontrol.service (см. подробное описание
# внутри самого файла sa02m.conf). Обязательный шаг для стабильного
# отображения статуса CODESYS в веб-панели после истечения demo-режима.
CODESYS_DROPIN_SRC="$ETC_DIR/systemd/system/codesyscontrol.service.d/sa02m.conf"
CODESYS_DROPIN_DIR_ROOTED="${SA02M_ROOTFS_ROOT:-/}etc/systemd/system/codesyscontrol.service.d"
CODESYS_DROPIN_DST="$CODESYS_DROPIN_DIR_ROOTED/sa02m.conf"
if [ -f "$CODESYS_DROPIN_SRC" ]; then
    install -d -m 0755 "$CODESYS_DROPIN_DIR_ROOTED"
    if install -m 0644 "$CODESYS_DROPIN_SRC" "$CODESYS_DROPIN_DST" >>"$LOG_FILE" 2>&1; then
        log OK  "codesyscontrol systemd drop-in установлен ($CODESYS_DROPIN_DST)"
    else
        log WARN "не удалось установить drop-in для codesyscontrol.service"
    fi
else
    log WARN "не найден $CODESYS_DROPIN_SRC (drop-in для codesyscontrol пропущен)"
fi

if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
    # Autostart policy (docs/contracts/kernel-conditional-services.md):
    # install-only — no enable, no start here. apply-policy disables the SysV
    # rc links the .deb postinst just created; start is a manual act.
    if [ -x /usr/local/sbin/sa02m-kernel-service-guard.sh ]; then
        /usr/local/sbin/sa02m-kernel-service-guard.sh apply-policy >>"$LOG_FILE" 2>&1 || true
    elif [ -f "$ETC_DIR/sa02m-kernel-service-guard.sh" ]; then
        # Standalone run (08 before/without 01-system.sh) — use the repo copy.
        bash "$ETC_DIR/sa02m-kernel-service-guard.sh" apply-policy >>"$LOG_FILE" 2>&1 || true
    fi
    log INFO "Автозапуск CODESYS/CodeMeter отключён политикой; запуск — вручную из веб-панели"

    # No start ⇒ a port/process check would be vacuous: verify by dpkg status.
    if dpkg -s codesyscontrol >/dev/null 2>&1; then
        log OK "codesyscontrol установлен (dpkg: $(dpkg -s codesyscontrol 2>/dev/null | awk -F': ' '/^Version:/{print $2; exit}'))"
    else
        log WARN "codesyscontrol отсутствует в dpkg — установка не удалась, см. $LOG_FILE"
    fi

    if [ -r /var/opt/codesys/codesyscontrol.log ]; then
        if grep -q 'running in demo mode' /var/opt/codesys/codesyscontrol.log; then
            log WARN "CODESYS Runtime работает в DEMO-режиме (~2 часа)."
            log INFO "Для полной лицензии Standard S:"
            log INFO "  1) откройте CODESYS Development System (Windows);"
            log INFO "  2) Devices → Communication → 192.168.1.136:11740;"
            log INFO "  3) License Manager → Activate → введите ticket из docs/codesys-rt/README.md;"
            log INFO "  4) .wbc-файл упадёт в /var/opt/codesys/ автоматически."
        fi
    fi
else
    log INFO "SA02M_ROOTFS_BUILD=1 — codesyscontrol не запускаем в chroot"
    # Disabled-by-default in the baked rootfs too (same autostart policy).
    systemctl --root="${SA02M_ROOTFS_ROOT:-/}" disable codesyscontrol >>"$LOG_FILE" 2>&1 || true
fi

log OK "=== [08] CODESYS Runtime установлен ==="
log INFO "Управление из веб-панели СА-02м: Управление → Службы → CODESYS"
