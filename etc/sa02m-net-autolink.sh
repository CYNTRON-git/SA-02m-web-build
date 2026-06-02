#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# sa02m-net-autolink.sh  — УСТАРЕЛО / DEPRECATED
#
# Начиная с образа, где используются стабильные предсказуемые имена end0/end1,
# этот скрипт не нужен. Ядро само даёт интерфейсам имена по аппаратному пути
# (end0 = EMAC, end1 = GMAC), не зависящие от MAC. Link-файлы не требуются.
#
# Оставлен для совместимости; в новых установках 01-system.sh маскирует
# sa02m-net-autolink.service и удаляет link-файлы.
#
# — — — исходная документация — — —
# Auto-update systemd-networkd link files when MAC addresses change.
# Runs before systemd-networkd so renamed interfaces (end0/end1) are available
# from the first boot on any hardware the image is cloned to.
#
# Designed for Cyntron A40i-2Eth:
#   end0 → MAC prefix 02:53:xx  (first physical interface)
#   end1 → MAC prefix 12:53:xx  (second physical interface, MSB differs)
# ═══════════════════════════════════════════════════════════════════════════════

LINK_DIR="/etc/systemd/network"
LOG_FILE="/var/log/sa02m_net_autolink.log"

# Link-file definitions: <filename> <link-name> <expected-mac-prefix>
# Order matters: first match wins for each prefix bucket.
LINK_FILES=(
    "10-end0.link end0 02:53:"
    "11-end1.link end1 12:53:"
)

_log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "$ts [$level] $*" >> "$LOG_FILE"
    echo "$ts [$level] $*" >&2
}

# Return list of real Ethernet interfaces sorted by ifindex.
# Criteria:
#   - /sys/class/net/<iface>/type == 1 (Ethernet)
#   - /sys/class/net/<iface>/device exists (physical, not virtual)
#   - name does not match lo, can*, veth*, br*, docker*, dummy*, tunl*
_list_real_eth() {
    local iface type addr dev_link
    for dev_link in /sys/class/net/*/device; do
        [ -e "$dev_link" ] || continue
        iface="${dev_link%/device}"
        iface="${iface##*/}"
        # Skip non-Ethernet
        type="$(cat "/sys/class/net/$iface/type" 2>/dev/null)"
        [ "$type" = "1" ] || continue
        # Skip obviously virtual names
        case "$iface" in
            lo|can*|veth*|br*|docker*|dummy*|tunl*|virbr*) continue ;;
        esac
        echo "$iface"
    done | sort
}

# Read current MAC from /sys/class/net/<iface>/address
_read_mac() {
    local iface="$1"
    local mac
    mac="$(cat "/sys/class/net/$iface/address" 2>/dev/null)"
    echo "${mac,,}"   # lowercase
}

# Read MACAddress from an existing link file
_read_link_mac() {
    local link_path="$1"
    local mac
    mac="$(grep -i '^MACAddress=' "$link_path" 2>/dev/null | head -1 | cut -d= -f2)"
    echo "${mac,,}"
}

# Write a link file
_write_link() {
    local link_path="$1"
    local link_name="$2"
    local mac="$3"
    mkdir -p "$LINK_DIR"
    cat > "$link_path" <<LINKEOF
[Match]
MACAddress=${mac}

[Link]
Name=${link_name}
LINKEOF
}

# ── Main ──────────────────────────────────────────────────────────────────────

mkdir -p "$(dirname "$LOG_FILE")"

_log INFO "sa02m-net-autolink: start"

# Collect real physical Ethernet interfaces sorted by name (end0 < end1, eth0 < eth1, etc.)
mapfile -t REAL_IFACES < <(_list_real_eth)

if [ "${#REAL_IFACES[@]}" -eq 0 ]; then
    _log WARN "No real Ethernet interfaces found in /sys/class/net — skipping"
    exit 0
fi

_log INFO "Physical interfaces found: ${REAL_IFACES[*]}"

# Build a MAC→iface map
declare -A MAC_TO_IFACE
for iface in "${REAL_IFACES[@]}"; do
    mac="$(_read_mac "$iface")"
    if [ -n "$mac" ] && [ "$mac" != "00:00:00:00:00:00" ]; then
        MAC_TO_IFACE["$mac"]="$iface"
        _log INFO "  $iface → $mac"
    fi
done

# For each link-file entry, find the best matching interface by MAC prefix
declare -A ASSIGNED_IFACE  # link_name → iface
declare -A ASSIGNED_MAC    # link_name → mac

for entry in "${LINK_FILES[@]}"; do
    read -r filename link_name prefix <<< "$entry"
    matched_iface=""
    matched_mac=""

    # First pass: exact prefix match
    for mac in "${!MAC_TO_IFACE[@]}"; do
        case "$mac" in
            "${prefix}"*)
                matched_mac="$mac"
                matched_iface="${MAC_TO_IFACE[$mac]}"
                break
                ;;
        esac
    done

    if [ -n "$matched_iface" ]; then
        ASSIGNED_IFACE["$link_name"]="$matched_iface"
        ASSIGNED_MAC["$link_name"]="$matched_mac"
        _log INFO "  Link $filename: prefix '$prefix' matched $matched_iface ($matched_mac)"
    else
        _log WARN "  Link $filename: no interface with prefix '$prefix' found"
    fi
done

# If prefix matching failed entirely (non-A40i hardware), fall back to
# positional assignment: sorted interfaces → sorted link entries
ALL_ASSIGNED=0
for entry in "${LINK_FILES[@]}"; do
    read -r _ link_name _ <<< "$entry"
    [ -n "${ASSIGNED_MAC[$link_name]:-}" ] && (( ALL_ASSIGNED++ ))
done

if [ "$ALL_ASSIGNED" -eq 0 ] && [ "${#REAL_IFACES[@]}" -ge 1 ]; then
    _log WARN "MAC prefix matching yielded 0 matches — falling back to positional assignment"
    idx=0
    for entry in "${LINK_FILES[@]}"; do
        read -r _ link_name _ <<< "$entry"
        iface="${REAL_IFACES[$idx]:-}"
        [ -n "$iface" ] || continue
        mac="$(_read_mac "$iface")"
        ASSIGNED_IFACE["$link_name"]="$iface"
        ASSIGNED_MAC["$link_name"]="$mac"
        _log INFO "  Positional: $link_name ← $iface ($mac)"
        (( idx++ ))
    done
fi

# Compare with existing link files and update if needed
CHANGED=0
for entry in "${LINK_FILES[@]}"; do
    read -r filename link_name _ <<< "$entry"
    link_path="$LINK_DIR/$filename"
    new_mac="${ASSIGNED_MAC[$link_name]:-}"

    if [ -z "$new_mac" ]; then
        _log WARN "  No MAC assigned for $link_name — skipping $filename"
        continue
    fi

    if [ -f "$link_path" ]; then
        old_mac="$(_read_link_mac "$link_path")"
        if [ "$old_mac" = "$new_mac" ]; then
            _log INFO "  $filename: MAC unchanged ($new_mac) — OK"
            continue
        fi
        _log INFO "  $filename: MAC changed $old_mac → $new_mac — updating"
    else
        _log INFO "  $filename: not found — creating ($new_mac)"
    fi

    _write_link "$link_path" "$link_name" "$new_mac"
    CHANGED=$(( CHANGED + 1 ))
done

if [ "$CHANGED" -gt 0 ]; then
    _log INFO "Updated $CHANGED link file(s) — restarting systemd-networkd"
    systemctl restart systemd-networkd 2>/dev/null \
        || _log WARN "systemctl restart systemd-networkd failed (may be normal at early boot)"
else
    _log INFO "All link files up to date — nothing to do"
fi

_log INFO "sa02m-net-autolink: done (changed=$CHANGED)"
exit 0
