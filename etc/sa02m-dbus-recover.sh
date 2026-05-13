#!/bin/bash
# Восстановление цепочки systemctl ↔ D-Bus на Armbian после зависаний SSH (часто после
# старта тяжёлых служб вроде MPLC4). Рекомендации: SSH через ssh.service; при сбое —
# перезапуск systemd-logind / dbus (может оборвать текущую SSH-сессию).
#
# Лог по умолчанию: /var/log/sa02m_dbus_recover.log
#
# Использование:
#   sa02m-dbus-recover check          — диагностика (все вызовы с timeout)
#   sa02m-dbus-recover fix-ssh        — отключить ssh.socket, включить ssh.service
#   SA02M_CONFIRM_DBUS_RESTART=1 sa02m-dbus-recover fix-dbus
#                                     — перезапуск logind и dbus (осторожно)

set -u

LOG_FILE="${LOG_FILE:-/var/log/sa02m_dbus_recover.log}"
T="${SA02M_SYSTEMCTL_TIMEOUT_SEC:-40}"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE" >/dev/null 2>&1 || printf '%s\n' "$*"
}

sc() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$T" systemctl "$@"
    else
        systemctl "$@"
    fi
}

require_root() {
    if [ "${EUID:-0}" -ne 0 ]; then
        echo "Запуск от root: sudo $0" >&2
        exit 1
    fi
}

cmd_check() {
    log "=== check (timeout ${T}s per call) ==="
    local u
    for u in dbus.service systemd-logind.service ssh.service ssh.socket; do
        log "$u: $(sc is-active "$u" 2>/dev/null || echo 'timeout/err')"
    done
    log "dbus ActiveState: $(sc show -p ActiveState --value dbus.service 2>/dev/null | head -n1 || echo '?')"
}

cmd_fix_ssh() {
    log "=== fix-ssh: прямой режим ssh.service (отключаем ssh.socket) ==="
    if [ -x /usr/local/sbin/sa02m-ssh-direct ]; then
        LOG_FILE="$LOG_FILE" /usr/local/sbin/sa02m-ssh-direct
    else
        sc unmask ssh.service 2>/dev/null || true
        sc disable --now ssh.socket 2>/dev/null || true
        sc enable ssh.service 2>/dev/null || true
        sc restart ssh.service || { log "ssh.service restart failed"; return 1; }
    fi
}

cmd_fix_dbus() {
    if [ "${SA02M_CONFIRM_DBUS_RESTART:-}" != "1" ]; then
        log "Отказ: fix-dbus может разорвать SSH. С консоли или: SA02M_CONFIRM_DBUS_RESTART=1 $0 fix-dbus"
        log "Сначала попробуйте: $0 fix-ssh"
        exit 2
    fi
    log "=== fix-dbus: restart systemd-logind, затем dbus (сессия SSH может оборваться) ==="
    sc restart systemd-logind.service 2>/dev/null || log "systemd-logind restart: ошибка или таймаут"
    sleep 2
    sc restart dbus.service 2>/dev/null || log "dbus restart: ошибка или таймаут"
    log "fix-dbus завершён"
}

usage() {
    printf '%s\n' \
        "Использование: $0 check | fix-ssh | fix-dbus" \
        "  check    — dbus, systemd-logind, ssh (через timeout)" \
        "  fix-ssh  — как Armbian: disable ssh.socket, enable ssh.service" \
        "  fix-dbus — перезапуск logind+dbus; нужен SA02M_CONFIRM_DBUS_RESTART=1"
}

main() {
    require_root
    case "${1:-}" in
        check)    cmd_check ;;
        fix-ssh)  cmd_fix_ssh ;;
        fix-dbus) cmd_fix_dbus ;;
        *)        usage; exit 1 ;;
    esac
}

main "$@"
