#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
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
# Источник vendor RUNTIME (mplc4.tar.gz + install.sh):
#   1. $SA02M_MPLC_DIR                — явный путь (override, берётся как есть).
#   2. НАИБОЛЕЕ НОВЫЙ из кандидатов по version.txt (не первый попавшийся!):
#        /opt/vendor-installers/mplc4   — на устройстве (после pscp с ПК),
#        $SCRIPT_DIR/../MPLC4/cyntron   — рядом с репо (единый источник staging).
#      Ничья/непарсибельный version.txt → предпочитаем копию из репозитория.
# Источник ПЛАГИНОВ (mplc_cyntron.so, mplc_protocol_fast_modbus.so) — отдельный
# и авторитетный: $SCRIPT_DIR/../firmware/mplc4 (в git; правильный ABI), с
# vendor-каталогом как fallback. Развязано от runtime: стейл-дроп в vendor-дире
# больше не подсовывает старый драйвер.
#
# Отключить: SA02M_SKIP_MPLC=1 ./install.sh
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [09] Установка MasterSCADA MPLC 4D Runtime ==="

BASE_DIR="$SCRIPT_DIR/.."

# One timeout-policy home shared with the panel. Stage both real launch modes
# for later panel installation, even when this refresh skips the optional stack.
source "$BASE_DIR/usr/local/lib/sa02m-mplc-policy.sh" || exit 1
sa02m_mplc_atomic() { sa02m_atomic_install -m "${3:-0644}" "$1" "$2"; }
sa02m_mplc_sc() {
    if [ -n "${MPLC_DEADLINE:-}" ]; then
        sa02m_mplc_bounded 8 systemctl "$@"
    else
        timeout -k 1 8 systemctl "$@"
    fi
}

_mplc_cancel_failed_install() {
    MPLC_DEADLINE=
    sa02m_mplc_budget_begin || return 1
    if sa02m_mplc_cancel_start && ! sa02m_mplc_runtime_active; then
        log ERR "MPLC: failed operation stopped; autostart policy retained"
    else
        log ERR "MPLC: operation outcome unknown; runtime stop not proven"
    fi
    MPLC_DEADLINE=
    return 1
}

_mplc_apply() {
    # Keep the installer's one capture/apply policy table and timestamp witness.
    # The shared command deadline bounds apply without serializing shell state
    # through eval or launching a second service operation after client timeout.
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        sa02m_svc_apply mplc4.service app on --stack=MPLC
        return $?
    fi
    local wanted=stop expected=${SA02M_SVC_EN[mplc4.service]:-timeout}
    case "$expected:${SA02M_SVC_ACT[mplc4.service]:-timeout}" in
        timeout:*|*:timeout) log ERR "MPLC: pre-install service state unknown"; return 1 ;;
        absent:*|broken:*|*:active|*:activating|*:reloading) wanted=start ;;
    esac
    sa02m_mplc_budget_begin || return 1
    SA02M_SYSTEMCTL_DEADLINE=$((MPLC_DEADLINE - 20))
    sa02m_svc_apply mplc4.service app on --stack=MPLC
    unset SA02M_SYSTEMCTL_DEADLINE
    if sa02m_mplc_wait_terminal "$wanted"; then
        case "$wanted:$_mplc_status" in
            start:terminal\|active\|*)
                sa02m_mplc_runtime_active || wanted=failed ;;
            stop:terminal\|inactive\|*|stop:terminal\|failed\|*)
                ! sa02m_mplc_runtime_active || wanted=failed ;;
            *) wanted=failed ;;
        esac
        case "$expected:${_mplc_status##*|}" in
            absent:enabled|broken:enabled) ;;
            absent:*|broken:*) wanted=failed ;;
            enabled:enabled|enabled-runtime:enabled-runtime|disabled:disabled|masked:masked|masked-runtime:masked-runtime) ;;
            enabled:*|enabled-runtime:*|disabled:*|masked:*|masked-runtime:*) wanted=failed ;;
        esac
        if [ "$wanted" != failed ]; then
            MPLC_DEADLINE=
            return 0
        fi
    fi
    # Preserve Restart=always for future explicit starts, but cancel this failed
    # job inside the original 85s deadline rather than leaving auto-restarts.
    if sa02m_mplc_cancel_start && ! sa02m_mplc_runtime_active; then
        log ERR "MPLC: apply failed; unit stopped"
    else
        log ERR "MPLC: apply failed; terminal runtime state unknown"
    fi
    MPLC_DEADLINE=
    return 1
}
install -d -m 0755 -o root -g root /opt/sa02m-mplc/systemd || exit 1
for _mplc_unit in mplc4.service mplc4-native.service; do
    if ! cmp -s "$BASE_DIR/opt/sa02m-mplc/systemd/$_mplc_unit" "/opt/sa02m-mplc/systemd/$_mplc_unit"; then
        sa02m_mplc_atomic "$BASE_DIR/opt/sa02m-mplc/systemd/$_mplc_unit" "/opt/sa02m-mplc/systemd/$_mplc_unit" || exit 1
    fi
done

# Authoritative, git-tracked plugin source (correct-ABI cyntron + fast_modbus).
# Decoupled from the runtime source so a stale vendor drop can't win here.
MPLC_PLUGIN_SRC="$BASE_DIR/firmware/mplc4"

# Print the version string from <dir>/version.txt (first dotted number), or empty.
_mplc_read_ver() {
    [ -f "$1/version.txt" ] || return 0
    grep -m1 -oE '[0-9]+(\.[0-9]+){1,3}' "$1/version.txt" 2>/dev/null || true
}
# Return 0 iff version $1 is STRICTLY newer than $2 (version sort).
_mplc_ver_gt() {
    [ -n "$1" ] && [ "$1" != "$2" ] \
        && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}
_mplc_valid() { [ -d "$1" ] && [ -f "$1/install.sh" ] && [ -f "$1/mplc4.tar.gz" ]; }

# Runtime source: explicit override wins; otherwise NEWEST-of-candidates, with
# the repo copy preferred on a tie or any parse failure (Fork A1). Delivery of a
# git-archive deploy still needs the vendor drop pscp'd to
# /opt/vendor-installers/mplc4/ first — documented in docs/deployment.md.
VENDOR_CAND=/opt/vendor-installers/mplc4
REPO_CAND="$BASE_DIR/MPLC4/cyntron"
MPLC_SRC="${SA02M_MPLC_DIR:-}"
if [ -z "$MPLC_SRC" ]; then
    _v_ok=0; _r_ok=0
    _mplc_valid "$VENDOR_CAND" && _v_ok=1
    _mplc_valid "$REPO_CAND"   && _r_ok=1
    if [ "$_v_ok" = 1 ] && [ "$_r_ok" = 1 ]; then
        _vver=$(_mplc_read_ver "$VENDOR_CAND")
        _rver=$(_mplc_read_ver "$REPO_CAND")
        if [ -n "$_vver" ] && [ -n "$_rver" ] && _mplc_ver_gt "$_vver" "$_rver"; then
            MPLC_SRC="$VENDOR_CAND"          # vendor strictly newer
        else
            MPLC_SRC="$REPO_CAND"            # repo newer, tie, or a parse failure → repo
        fi
        log INFO "MPLC runtime: vendor=${_vver:-?} / repo=${_rver:-?} → выбран $MPLC_SRC (новейший, ничья→репо)"
    elif [ "$_v_ok" = 1 ]; then
        MPLC_SRC="$VENDOR_CAND"
    elif [ "$_r_ok" = 1 ]; then
        MPLC_SRC="$REPO_CAND"
    fi
fi

# The version stamp: the payload version whose vendor install.sh last ran on
# this box. Two writers, one format (first dotted version, one line): 09 after
# a successful vendor run, and the ctl's mplc4_install
# (etc/sa02m-web-service-ctl.sh). Makes 09 idempotent — an equal payload never
# re-runs the vendor installer.
MPLC_STAMP=/opt/mplc4/.sa02m-payload-version

# Наша надстройка: unit-обёртка mplc4.service (перенесена из 01-system.sh —
# там она утекала мимо SA02M_SKIP_MPLC) + плагины ЦИНТРОН. Всё cmp-gated:
# без изменений — ноль действий, ноль рестартов. Возвращает 0, если что-то
# изменилось (каналом _MPLC_OVERLAY_CHANGED).
_MPLC_OVERLAY_CHANGED=0
_mplc_install_overlay() {
    local _so
    sa02m_mplc_policy_install "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4.service" || return 1
    sa02m_mplc_wrapper_install "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4.service" "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4-native.service" || return 1
    if [ "$SA02M_MPLC_POLICY_CHANGED" = 1 ]; then
        _MPLC_OVERLAY_CHANGED=1
        if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
            systemctl daemon-reload >>"$LOG_FILE" 2>&1 || return 1
        fi
    fi
    if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
        sa02m_mplc_policy_observe >/dev/null || { log ERR "MPLC timeout policy verification failed"; return 1; }
    fi
    [ -d /opt/mplc4 ] || return 0
    # Plugins from the git-tracked firmware/mplc4 (authoritative, correct ABI);
    # the vendor payload dir is only a fallback.
    for _so in mplc_cyntron.so mplc_protocol_fast_modbus.so; do
        if [ -f "$MPLC_PLUGIN_SRC/$_so" ]; then
            if ! cmp -s "$MPLC_PLUGIN_SRC/$_so" "/opt/mplc4/$_so" 2>/dev/null; then
                install -m 0755 "$MPLC_PLUGIN_SRC/$_so" "/opt/mplc4/$_so"
                _MPLC_OVERLAY_CHANGED=1
                log OK "Плагин $_so установлен в /opt/mplc4/ (из firmware/mplc4)"
            fi
        elif [ -n "$MPLC_SRC" ] && [ -f "$MPLC_SRC/$_so" ]; then
            if ! cmp -s "$MPLC_SRC/$_so" "/opt/mplc4/$_so" 2>/dev/null; then
                install -m 0755 "$MPLC_SRC/$_so" "/opt/mplc4/$_so"
                _MPLC_OVERLAY_CHANGED=1
                log OK "Плагин $_so установлен в /opt/mplc4/ (fallback: vendor-дроп $MPLC_SRC)"
            fi
        else
            log WARN "$_so не найден ни в firmware/mplc4, ни в ${MPLC_SRC:-<нет payload>} — плагин не установлен"
        fi
    done
    return 0
}

# ── Вердикт политики стеков — ДО payload-требований и vendor install.sh ────
# docs/contracts/installer-refresh-policy.md.
_VERDICT=$(sa02m_stack_verdict MPLC)
case "$_VERDICT" in
    skip-disabled)
        log INFO "MPLC: отключён оператором (/etc/sa02m_stacks.conf) — пропуск; вернуть: кнопка «Установить» в панели или --with-optional"
        exit 0
        ;;
    skip-absent)
        log INFO "refresh: MPLC не установлен — не ставлю (кнопка «Установить» или --with-optional)"
        exit 0
        ;;
    overlay)
        log INFO "refresh: MPLC установлен ($(cat "$MPLC_STAMP" 2>/dev/null || echo 'версия неизвестна')) — обновляю только надстройку sa02m, стек не переустанавливаю"
        sa02m_svc_capture mplc4.service
        _mplc_install_overlay || exit 1
        if [ "$_MPLC_OVERLAY_CHANGED" = 1 ]; then
            _mplc_apply || exit 1
        else
            log INFO "MPLC: надстройка sa02m без изменений — служба не трогается"
        fi
        log OK "=== [09] MPLC: refresh-надстройка завершена ==="
        exit 0
        ;;
esac

if [ -z "$MPLC_SRC" ] || [ ! -d "$MPLC_SRC" ]; then
    log WARN "MPLC vendor-payload не найден (искали /opt/vendor-installers/mplc4/, MPLC4/cyntron/)."
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

# Состояние — ДО vendor install.sh: он сам делает `systemctl enable mplc4` +
# start (MPLC4/cyntron/install.sh:1529-1531); apply ПОСЛЕ восстанавливает
# операторское состояние точно (остановленный mplc4 возвращается остановленным
# даже при реальном обновлении).
sa02m_svc_capture mplc4.service

MPLC_INSTALL_LOG="/var/log/sa02m_mplc_install.log"
_payload_ver=$(_mplc_read_ver "$MPLC_SRC")
_stamp_ver=$(cat "$MPLC_STAMP" 2>/dev/null || true)
if [ -n "$_payload_ver" ] && [ -n "$_stamp_ver" ] && [ "$_payload_ver" = "$_stamp_ver" ]; then
    log INFO "MPLC $_payload_ver уже установлен из этого payload — vendor install.sh не запускаю"
else
    if [ -d /opt/mplc4 ] && [ -z "$_stamp_ver" ]; then
        log INFO "версия установленного MPLC неизвестна (нет метки) — выполняю vendor install.sh"
    fi
    # Preflight the persistent policy before the vendor can start its unit.
    sa02m_mplc_policy_install "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4.service" || exit 1
    sa02m_mplc_wrapper_install "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4.service" "$BASE_DIR/opt/sa02m-mplc/systemd/mplc4-native.service" || exit 1
    if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
        systemctl daemon-reload >>"$LOG_FILE" 2>&1 || exit 1
        sa02m_mplc_policy_observe >/dev/null || { log ERR "MPLC timeout policy verification failed"; exit 1; }
    fi
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
        [ -n "${SA02M_ROOTFS_BUILD:-}" ] || _mplc_cancel_failed_install
        exit 1
    fi
    if [ -n "$_payload_ver" ] && [ -d /opt/mplc4 ]; then
        printf '%s\n' "$_payload_ver" > "$MPLC_STAMP" 2>/dev/null || true
    fi
    log OK "MPLC 4D Runtime установлен в /opt/mplc4/"
fi

# sa02m-надстройка: unit-обёртка + плагины ЦИНТРОН (cmp-gated — см. функцию).
_mplc_install_overlay || { [ -n "${SA02M_ROOTFS_BUILD:-}" ] || _mplc_cancel_failed_install; exit 1; }

# Apply: первая установка ⇒ enable+start (штатный рантайм изделия — контракт
# kernel-conditional-services §3); обновление ⇒ точное восстановление
# операторского состояния (ROOTFS-ветка внутри helper'а: enable через --root).
_mplc_apply || exit 1

if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
    case "$SA02M_SVC_LAST_RESULT" in
        started|restarted)
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
            ;;
    esac
else
    log INFO "SA02M_ROOTFS_BUILD=1 — mplc4 не запускаем в chroot"
fi

# Стек на месте — политика фиксирует present (кнопка «Удалить» пишет disabled).
# Тот же признак, что у live-пробы (sa02m_stack_installed MPLC): runtime-payload,
# а не обёртки — полуудалённая установка не должна записываться как present.
if [ -x /opt/mplc4/start_mplc4.sh ]; then
    sa02m_stack_policy_set MPLC present || true
fi

log OK "=== [09] MPLC 4D Runtime установлен ==="
log INFO "UI:       http://${IP_ADDRESS:-192.168.1.136}:${MPLC_HTTP_PORT}/"
log INFO "Управление из веб-панели СА-02м: Управление → Службы → MPLC4"
