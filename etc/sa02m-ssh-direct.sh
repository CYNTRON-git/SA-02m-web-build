#!/bin/bash
set -euo pipefail

LOG_FILE="${LOG_FILE:-/var/log/sa02m_fix_ssh.log}"

log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    printf '[%s] %s\n' "$ts" "$*" | tee -a "$LOG_FILE" >/dev/null
}

require_root() {
    if [ "${EUID:-0}" -ne 0 ]; then
        echo "Run as root." >&2
        exit 1
    fi
}

port_ready() {
    command -v ss >/dev/null 2>&1 || return 0
    ss -H -ltn "sport = :22" 2>/dev/null | awk 'NR==1{found=1} END{exit(found?0:1)}'
}

debug_state() {
    log "board_model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
    log "ssh.service state=$(systemctl is-active ssh.service 2>/dev/null || echo unknown)"
    log "ssh.socket state=$(systemctl is-active ssh.socket 2>/dev/null || echo unavailable)"
    if command -v ss >/dev/null 2>&1; then
        log "listeners=$(ss -H -ltn '( sport = :22 )' 2>/dev/null | tr '\n' ';' | sed 's/;$/ /')"
    fi
}

require_root

log "Switching SSH to direct service mode"
debug_state
systemctl unmask ssh.service >/dev/null 2>&1 || true
systemctl disable --now ssh.socket >/dev/null 2>&1 || true
systemctl enable ssh.service >/dev/null 2>&1 || true
systemctl restart ssh.service

for _ in 1 2 3 4 5; do
    if systemctl is-active --quiet ssh.service && port_ready; then
        log "SSH direct mode active"
        debug_state
        exit 0
    fi
    sleep 1
done

log "SSH direct mode did not become ready"
debug_state
exit 1
