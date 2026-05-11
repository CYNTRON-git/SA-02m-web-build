#!/bin/bash
set -euo pipefail

HW_CONF="${HW_CONF:-/etc/sa02m_hw.conf}"
STATUS_CONF="${STATUS_CONF:-/etc/sa02m_status_blocks.conf}"
STORAGE_CONF="${STORAGE_CONF:-/etc/sa02m_storage.conf}"
STATE_DIR="${STATE_DIR:-/var/lib/sa02m-working-board}"
LOG_FILE="${LOG_FILE:-/var/log/sa02m_prepare_working_board.log}"
CACHE_DIR="${CACHE_DIR:-/tmp/sa02m_status_cache}"
STORAGE_RULES="${STORAGE_RULES:-/etc/udev/rules.d/99-storage.rules}"
STORAGE_RULES_DISABLED="${STORAGE_RULES_DISABLED:-/etc/udev/rules.d/99-storage.rules.sa02m-disabled}"

mkdir -p "$STATE_DIR"

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

backup_file() {
    local src=$1 base dst
    [ -f "$src" ] || return 0
    base=$(basename "$src")
    dst="${STATE_DIR}/${base}.$(date +%Y%m%d_%H%M%S).bak"
    cp -f "$src" "$dst"
    log "backup created: $dst"
}

set_key_value() {
    local file=$1 key=$2 value=$3 tmp
    tmp="${file}.tmp.$$"
    if [ -f "$file" ]; then
        awk -F= -v key="$key" -v value="$value" '
            BEGIN { done = 0 }
            $1 == key {
                print key "=" value
                done = 1
                next
            }
            { print }
            END {
                if (!done) {
                    print key "=" value
                }
            }
        ' "$file" > "$tmp"
    else
        printf '%s=%s\n' "$key" "$value" > "$tmp"
    fi
    install -m 644 "$tmp" "$file"
    rm -f "$tmp"
}

clear_status_cache() {
    rm -f "$CACHE_DIR"/*.json "$CACHE_DIR"/*.lock 2>/dev/null || true
}

stop_storage_mount_instances() {
    systemctl list-units --all --no-legend 'storage-mount@*.service' 2>/dev/null \
        | awk '{print $1}' \
        | while read -r unit; do
            [ -n "$unit" ] || continue
            systemctl stop "$unit" 2>/dev/null || true
        done
}

disable_storage_rules() {
    if [ -f "$STORAGE_RULES" ]; then
        mv -f "$STORAGE_RULES" "$STORAGE_RULES_DISABLED"
        udevadm control --reload-rules 2>/dev/null || true
        log "storage udev rules disabled"
    fi
}

enable_storage_rules() {
    if [ -f "$STORAGE_RULES_DISABLED" ]; then
        mv -f "$STORAGE_RULES_DISABLED" "$STORAGE_RULES"
        udevadm control --reload-rules 2>/dev/null || true
        log "storage udev rules restored"
    fi
}

cmd_prepare() {
    require_root

    backup_file "$HW_CONF"
    backup_file "$STATUS_CONF"
    backup_file "$STORAGE_CONF"

    set_key_value "$HW_CONF" SA02M_HW_BACKEND disabled
    set_key_value "$HW_CONF" SA02M_I2C_OWNER_UNITS "mplc.service mplc4.service klogic.service klogicd.service"
    set_key_value "$HW_CONF" SA02M_I2C_OWNER_PROCS "mplc mplc4 klogic klogicd"

    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_STORAGE 0
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_TIME 0
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_UPTIME 1
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_NETWORK 1
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_LOAD 1
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_SYSTEM 1
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_SERVICES 0
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_HARDWARE 0
    set_key_value "$STATUS_CONF" SA02M_STATUS_ENABLE_RS485 0

    set_key_value "$STORAGE_CONF" STORAGE_AUTO_FORMAT 0

    clear_status_cache

    if systemctl list-unit-files 'storage-mount@.service' 2>/dev/null | grep -q 'storage-mount@.service'; then
        stop_storage_mount_instances
        umount -l /media/sdcard 2>/dev/null || true
        umount -l /media/usb 2>/dev/null || true
        log "storage-mount instances stopped for safe working-board start"
    fi
    disable_storage_rules

    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now sa02m-failure-monitor.service >/dev/null 2>&1 || true

    log "safe working-board mode applied"
}

cmd_enable_storage() {
    require_root
    enable_storage_rules
    systemctl daemon-reload >/dev/null 2>&1 || true
    log "storage handling re-enabled"
}

cmd_status() {
    echo "SA02M_HW_BACKEND=$(awk -F= '/^SA02M_HW_BACKEND=/{print $2; exit}' "$HW_CONF" 2>/dev/null || true)"
    echo "STORAGE_AUTO_FORMAT=$(awk -F= '/^STORAGE_AUTO_FORMAT=/{print $2; exit}' "$STORAGE_CONF" 2>/dev/null || true)"
    if [ -f "$STATUS_CONF" ]; then
        awk -F= '/^SA02M_STATUS_ENABLE_/ {print $1 "=" $2}' "$STATUS_CONF"
    fi
    systemctl is-enabled sa02m-failure-monitor.service 2>/dev/null || true
    systemctl is-active sa02m-failure-monitor.service 2>/dev/null || true
    if [ -f "$STORAGE_RULES" ]; then
        echo "storage_rules=enabled"
    elif [ -f "$STORAGE_RULES_DISABLED" ]; then
        echo "storage_rules=disabled"
    else
        echo "storage_rules=missing"
    fi
}

case "${1:-prepare}" in
    prepare)
        shift
        cmd_prepare "$@"
        ;;
    enable-storage)
        shift
        cmd_enable_storage "$@"
        ;;
    status)
        shift
        cmd_status "$@"
        ;;
    *)
        cat <<'EOF'
Usage:
  sa02m-prepare-working-board [prepare]
  sa02m-prepare-working-board enable-storage
  sa02m-prepare-working-board status
EOF
        exit 1
        ;;
esac
