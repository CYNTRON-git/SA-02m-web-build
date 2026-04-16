#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Shared functions for install scripts
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE="${LOG_FILE:-/var/log/sa02m_install.log}"

log() {
    local level=${1:-INFO} msg=$2
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    local color reset
    case "$level" in
        OK)   color='\033[0;32m' ;;
        WARN) color='\033[0;33m' ;;
        ERR)  color='\033[0;31m' ;;
        *)    color='\033[0;36m' ;;
    esac
    reset='\033[0m'
    echo -e "${color}[${ts}] [${level}] ${msg}${reset}"
    echo    "[${ts}] [${level}] ${msg}" >> "$LOG_FILE" 2>/dev/null || true
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log ERR "Запустите скрипт от root: sudo $0"
        exit 1
    fi
}

pkg_install() {
    local pkgs=("$@")
    local missing=()
    for p in "${pkgs[@]}"; do
        dpkg -l "$p" &>/dev/null || missing+=("$p")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log INFO "Установка пакетов: ${missing[*]}"
        apt-get install -y "${missing[@]}" >> "$LOG_FILE" 2>&1
    fi
}

svc_enable() {
    systemctl enable "$1" >> "$LOG_FILE" 2>&1 || true
    systemctl start  "$1" >> "$LOG_FILE" 2>&1 || true
}

svc_restart() {
    systemctl restart "$1" >> "$LOG_FILE" 2>&1 || true
}
