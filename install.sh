#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Installer  v1.0.3
# Дата: 2026
# Использование: sudo ./install.sh [--ip X.X.X.X] [--port 9999] [--pass cyntron]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LOG_FILE="/var/log/sa02m_install.log"

# ── Parse arguments ────────────────────────────────────────────────────────
export NETMASK="255.255.255.0"
export DNS_SERVERS="77.88.8.8 77.88.8.1"
export NET_IFACE="end0"
export PORT="9999"
export WEB_ROOT="/var/www/network_config"
export ADMIN_PASS="cyntron"
export SA02M_SERIAL_PROFILE=""
export SA02M_HW_VARIANT=""
# IP_ADDRESS and GATEWAY are resolved after lib.sh is sourced (variant-aware defaults)
_ARG_IP=""
_ARG_GW=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --ip)      _ARG_IP="$2";             shift 2 ;;
        --mask)    NETMASK="$2";             shift 2 ;;
        --gw)      _ARG_GW="$2";             shift 2 ;;
        --port)    PORT="$2";                shift 2 ;;
        --pass)    ADMIN_PASS="$2";          shift 2 ;;
        --serial-profile) SA02M_SERIAL_PROFILE="$2"; shift 2 ;;
        --variant) SA02M_HW_VARIANT="$2";   shift 2 ;;
        *)         shift ;;
    esac
done

# ── Init log ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
echo "──────────────────────────────────────────" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Установка СА-02м начата" >> "$LOG_FILE"

source "$SCRIPT_DIR/scripts/lib.sh"
check_root

# Persist variant if explicitly provided, then resolve IP/GW defaults
if [ -n "$SA02M_HW_VARIANT" ]; then
    printf 'SA02M_HW_VARIANT=%s\n' "$SA02M_HW_VARIANT" > /etc/sa02m_hw_variant.conf
    chmod 644 /etc/sa02m_hw_variant.conf
fi
export IP_ADDRESS="${_ARG_IP:-$(sa02m_default_ip)}"
export GATEWAY="${_ARG_GW:-$(sa02m_default_gw)}"
HW_VARIANT=$(sa02m_hw_variant)

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   СА-02м  Installer  v1.0.3          ║"
echo "  ╚══════════════════════════════════════╝"
echo ""
echo "  Вариант: $HW_VARIANT"
echo "  IP    : $IP_ADDRESS"
echo "  Шлюз  : $GATEWAY"
echo "  PORT  : $PORT"
echo "  LOG   : $LOG_FILE"
echo ""

# ── Run modules ────────────────────────────────────────────────────────────
bash "$SCRIPT_DIR/scripts/01-system.sh"
bash "$SCRIPT_DIR/scripts/02-network.sh"
bash "$SCRIPT_DIR/scripts/03-webserver.sh"
bash "$SCRIPT_DIR/scripts/04-flasher.sh"
bash "$SCRIPT_DIR/scripts/05-cloud-agent.sh"

# Root ext4 с commit=600 — сброс на диск, чтобы перезагрузка сразу после
# установки не потеряла изменения (page cache ещё не на диске).
sync

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
log OK "════════════════════════════════════════"
log OK " Установка завершена!"
log OK " URL  : http://${IP_ADDRESS}:${PORT}"
log OK " Логин: admin / ${ADMIN_PASS}"
log OK "════════════════════════════════════════"
echo ""

# ── Check services ─────────────────────────────────────────────────────────
for svc in nginx fcgiwrap sa02m-flasher sa02m-cloud-agent; do
    if systemctl is-active "$svc" &>/dev/null; then
        log OK " ✓ $svc работает"
    else
        log WARN " ✗ $svc не запущен!"
    fi
done
echo ""
