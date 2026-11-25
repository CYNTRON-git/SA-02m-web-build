#!/bin/bash
# Node-RED installer policy; archive validation and activation have one helper.
set -o pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root
BASE_DIR="$SCRIPT_DIR/.."
NODERED_USER="${NODERED_USER:-nodered}"
NODERED_PORT="${NODERED_PORT:-1880}"
DEVICE_IP="${IP_ADDRESS:-192.168.1.136}"
NO_START=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip|--port|--nodered-user)
            if [ "$#" -lt 2 ]; then
                log ERR "Node-RED: отсутствует значение $1"
                exit 2
            fi ;;
    esac
    case "$1" in
        --ip) DEVICE_IP="$2"; shift 2 ;;
        --port) NODERED_PORT="$2"; shift 2 ;;
        --nodered-user) NODERED_USER="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        *) shift ;;
    esac
done

# Refresh never replaces or enables an optional third-party runtime.
case "$(sa02m_stack_verdict NODERED)" in
    skip-disabled) log INFO "Node-RED отключён оператором — пропуск"; exit 0 ;;
    skip-absent) log INFO "refresh: Node-RED отсутствует — пропуск"; exit 0 ;;
    overlay) log INFO "refresh: существующий Node-RED сохраняется; runtime обновляется только явной установкой"; exit 0 ;;
esac
if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
    log INFO "Сборка rootfs: проверенный payload устанавливается на устройстве; службы не запускаются"
    exit 0
fi
HELPER=/usr/local/lib/sa02m-vendor-install.py
if [ ! -f "$HELPER" ]; then
    log WARN "Node-RED: отсутствует общий установщик; сначала выполните 03-webserver.sh"
    exit 0
fi
NODERED_SRC="${SA02M_NODERED_DIR:-/opt/vendor-installers/nodered}"
if [ ! -d "$NODERED_SRC" ] && [ -d "$BASE_DIR/vendor/nodered" ]; then
    NODERED_SRC="$BASE_DIR/vendor/nodered"
fi
sa02m_svc_capture nodered.service node-red.service
MODE=--preserve
if [ "${SA02M_SVC_EN[nodered.service]-}" = absent ] && [ "$NO_START" = 0 ]; then
    MODE=--activate
fi
if OUT=$(/usr/bin/python3 -I "$HELPER" install node-red "$MODE" --user "$NODERED_USER" --port "$NODERED_PORT" --offline-dir "$NODERED_SRC" 2>>"$LOG_FILE"); then
    if ! printf '%s' "$OUT" | /usr/bin/python3 -I -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") is True and d.get("id")=="node-red" else 1)'; then
        log ERR "Node-RED: установщик не подтвердил результат"
        exit 1
    fi
else
    ERROR=$(printf '%s' "$OUT" | /usr/bin/python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("error","install_failed"))' 2>/dev/null) || ERROR=install_failed
    case "$ERROR" in
        payload_unavailable|manifest_unavailable)
            log WARN "Node-RED: проверенный payload недоступен ($ERROR); существующий runtime сохранён"
            exit 0 ;;
        *) log ERR "Node-RED: установка не выполнена ($ERROR); результат: $OUT"; exit 1 ;;
    esac
fi
# The helper already restarted a previously running service exactly once.
# Its timestamp witness makes the canonical policy application a no-op restart.
if [ "$NO_START" = 1 ]; then
    sa02m_svc_apply nodered.service app enabled norestart --stack=NODERED
else
    sa02m_svc_apply nodered.service app on --stack=NODERED
fi
sa02m_stack_policy_set NODERED present || {
    log ERR "Node-RED: runtime проверен, но политика стека не сохранена"
    exit 1
}
log OK "Node-RED: установлен проверенный runtime ($OUT)"
log INFO "UI: http://${DEVICE_IP}:${NODERED_PORT}/"
