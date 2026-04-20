#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Installer  v1.0.1
# Дата: 2025
# Использование: sudo ./install.sh [--ip X.X.X.X] [--port 9999] [--pass cyntron]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LOG_FILE="/var/log/sa02m_install.log"

# ── Parse arguments ────────────────────────────────────────────────────────
export IP_ADDRESS="192.168.1.136"
export NETMASK="255.255.255.0"
export GATEWAY="192.168.1.1"
export DNS_SERVERS="77.88.8.8 77.88.8.1"
export NET_IFACE="eth0"
export PORT="9999"
export WEB_ROOT="/var/www/network_config"
export ADMIN_PASS="cyntron"

while [[ $# -gt 0 ]]; do
    case $1 in
        --ip)     IP_ADDRESS="$2";  shift 2 ;;
        --mask)   NETMASK="$2";     shift 2 ;;
        --gw)     GATEWAY="$2";     shift 2 ;;
        --port)   PORT="$2";        shift 2 ;;
        --pass)   ADMIN_PASS="$2";  shift 2 ;;
        *)        shift ;;
    esac
done

# ── Init log ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
echo "──────────────────────────────────────────" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Установка СА-02м начата" >> "$LOG_FILE"

source "$SCRIPT_DIR/scripts/lib.sh"
check_root

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   СА-02м  Installer  v1.0.1          ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  IP    : $IP_ADDRESS"
echo "  PORT  : $PORT"
echo "  LOG   : $LOG_FILE"
echo ""

# ── Run modules ────────────────────────────────────────────────────────────
bash "$SCRIPT_DIR/scripts/01-system.sh"
bash "$SCRIPT_DIR/scripts/02-network.sh"
bash "$SCRIPT_DIR/scripts/03-webserver.sh"

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
log OK "════════════════════════════════════════"
log OK " Установка завершена!"
log OK " URL  : http://${IP_ADDRESS}:${PORT}"
log OK " Логин: admin / ${ADMIN_PASS}"
log OK "════════════════════════════════════════"
echo ""

# ── Check services ─────────────────────────────────────────────────────────
for svc in nginx fcgiwrap; do
    if systemctl is-active "$svc" &>/dev/null; then
        log OK " ✓ $svc работает"
    else
        log WARN " ✗ $svc не запущен!"
    fi
done
echo ""
