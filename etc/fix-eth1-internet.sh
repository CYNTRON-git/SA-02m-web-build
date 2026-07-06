#!/bin/bash
# =============================================================================
# fix-eth1-internet.sh — SA-02m-2: восстановление eth1 (GMAC) + интернет
#
# Применяет все накопленные фиксы из BUGLOG для работы второго Ethernet (eth1):
#   1. DTB: GMAC okay + dc1sw regulator-always-on + syscon
#   2. threadirqs в bootargs (boot.scr) — защита от IRQ storm при PCA9536/I2C3
#   3. eth1.conf: allow-hotplug + DHCP + metric 100 + post-up default route
#   4. eth0.conf: metric 200 (eth1 DHCP выигрывает дефолтный маршрут)
#   5. dhclient exit hook: default route при RFC3442 (option 121)
#   6. fix-eth.sh: текущая версия — без bounce, с восстановлением route из lease
#   7. sa02m-eth1-coldboot: oneshot-сервис для cold-boot autoneg PHY
#   8. Удаление устаревшего sa02m-phy-coldboot
#   9. udev: unbind i2c-2 при IRQ storm от PCA9536
#
# Запуск: bash /tmp/fix-eth1-internet.sh
# Требует: root, dtc (apt install device-tree-compiler), mkimage (apt install u-boot-tools)
# =============================================================================

set -uo pipefail

CHANGED=0
REBOOT_NEEDED=0
FAT_MNT="/mnt/fat"
FAT_DEV="/dev/mmcblk2p1"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log_ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
log_info() { echo -e "      $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_err()  { echo -e "${RED}[ERR]${NC}  $*"; }
log_step() { echo -e "\n--- $* ---"; }

require_root() {
    if [ "$(id -u)" != "0" ]; then
        log_err "Необходим root. Запустите: sudo bash $0"
        exit 1
    fi
}

fat_mount() {
    if ! mountpoint -q "$FAT_MNT" 2>/dev/null; then
        mkdir -p "$FAT_MNT"
        mount "$FAT_DEV" "$FAT_MNT" || { log_err "Не удалось смонтировать $FAT_DEV"; return 1; }
        return 0  # 0 = we mounted it
    fi
    return 1  # already mounted
}

fat_umount() {
    local was_mounted=$1
    [ "$was_mounted" = "0" ] && umount "$FAT_MNT" 2>/dev/null || true
}

# =============================================================================
# ШАГ 1: DTB — GMAC enable + dc1sw always-on + syscon
# =============================================================================
step_dtb() {
    log_step "ШАГ 1: DTB (GMAC / dc1sw)"

    # Проверяем текущее состояние через device-tree в procfs
    local gmac_status
    gmac_status=$(cat /proc/device-tree/soc/ethernet@1c50000/status 2>/dev/null | tr -d '\0' || echo "unknown")

    if [ "$gmac_status" = "okay" ]; then
        log_ok "GMAC уже включён в DTB (status=okay)"
    else
        log_warn "GMAC статус в DTB: '${gmac_status}' — требуется патч"

        # Проверяем наличие dtc
        if ! command -v dtc >/dev/null 2>&1; then
            log_warn "dtc не найден. Установите: apt install device-tree-compiler"
            log_warn "Патч DTB пропущен. GMAC не будет работать без dtc."
        else
            local mounted
            fat_mount && mounted=0 || mounted=1

            local dtb_fat="${FAT_MNT}/sun8i-a40i-sk.dtb"
            if [ ! -f "$dtb_fat" ]; then
                log_err "DTB не найден: $dtb_fat"
                fat_umount $mounted
            else
                log_info "Патч DTB: $dtb_fat"
                cp "$dtb_fat" "/tmp/sun8i-a40i-sk.dtb.bak.$(date +%Y%m%d%H%M%S)"
                cp "$dtb_fat" /tmp/src.dtb

                # Декомпилируем
                dtc -I dtb -O dts -o /tmp/patch.dts /tmp/src.dtb 2>/dev/null || {
                    log_err "dtc: ошибка декомпиляции"
                    fat_umount $mounted
                    return
                }

                # Применяем патч (Python inline)
                python3 <<'PYEOF'
import re, sys

with open("/tmp/patch.dts") as f:
    s = f.read()

changed = []

# dc1sw: regulator-always-on для vcc-gmac-phy
if "dc1sw {" in s:
    parts = s.split("dc1sw {", 1)
    head, rest = parts[0], parts[1]
    body, tail = rest.split("};", 1)
    if "regulator-always-on" not in body:
        body = body.replace(
            'regulator-name = "vcc-gmac-phy";',
            'regulator-always-on;\n\t\t\t\t\t\tregulator-name = "vcc-gmac-phy";',
            1,
        )
        changed.append("dc1sw: regulator-always-on добавлен")
    s = head + "dc1sw {" + body + "};" + tail

# i2c@1c2b800: должен быть okay
node_start = s.find("i2c@1c2b800 {")
if node_start >= 0:
    node_end = s.find("};", node_start) + 2
    node = s[node_start:node_end]
    new_node = re.sub(r'status = "disabled"', 'status = "okay"', node, count=1)
    if new_node != node:
        changed.append("i2c@1c2b800: status okay")
    s = s[:node_start] + new_node + s[node_end:]

# ethernet@1c50000 (GMAC): status okay + syscon fix
node_start = s.find("ethernet@1c50000 {")
if node_start < 0:
    print("ERROR: ethernet@1c50000 not found in DTS", file=sys.stderr)
    sys.exit(1)
node_end = s.find("};", node_start) + 2
node = s[node_start:node_end]
new_node = node
new_node = re.sub(r'status = "disabled"', 'status = "okay"', new_node)
new_node = re.sub(r'syscon = <0x[0-9a-f]+>', 'syscon = <0x02>', new_node)
if new_node != node:
    changed.append("ethernet@1c50000: GMAC okay + syscon=0x02")
s = s[:node_start] + new_node + s[node_end:]

with open("/tmp/patch.dts", "w") as f:
    f.write(s)

if changed:
    print("DTB патч применён:")
    for c in changed:
        print(f"  {c}")
else:
    print("DTB уже корректен, изменений не требуется")
PYEOF
                local py_exit=$?

                if [ $py_exit -ne 0 ]; then
                    log_err "Ошибка Python-патча DTB"
                    fat_umount $mounted
                    return
                fi

                # Перекомпилируем
                dtc -I dts -O dtb -o /tmp/patched.dtb /tmp/patch.dts 2>/dev/null || {
                    log_err "dtc: ошибка компиляции патченного DTS"
                    fat_umount $mounted
                    return
                }

                # Сохраняем в каноническую копию
                mkdir -p /usr/local/share/sa02m
                cp /tmp/patched.dtb /usr/local/share/sa02m/sun8i-a40i-sk.dtb
                # Записываем на FAT
                cp /tmp/patched.dtb "$dtb_fat"
                sync
                fat_umount $mounted
                log_ok "DTB пропатчен и записан на FAT (GMAC enabled)"
                REBOOT_NEEDED=1
                CHANGED=1
            fi
        fi
    fi
}

# =============================================================================
# ШАГ 2: boot.scr — threadirqs в bootargs
# =============================================================================
step_bootscr_threadirqs() {
    log_step "ШАГ 2: boot.scr (threadirqs)"

    # Если threadirqs уже активен в текущей сессии — всё ОК
    if grep -q threadirqs /proc/cmdline 2>/dev/null; then
        log_ok "threadirqs уже активен (/proc/cmdline)"
        return
    fi

    log_warn "threadirqs не найден в /proc/cmdline — патч boot.scr"

    # Canonical boot.scr уже исправлен — просто копируем на FAT
    if [ -f /usr/local/share/sa02m/boot.scr ]; then
        local mounted
        fat_mount && mounted=0 || mounted=1
        local fat_scr="${FAT_MNT}/boot.scr"
        if [ -f "$fat_scr" ]; then
            cp "$fat_scr" "${fat_scr}.bak.$(date +%Y%m%d%H%M%S)"
        fi
        cp /usr/local/share/sa02m/boot.scr "$fat_scr"
        sync
        fat_umount $mounted
        log_ok "boot.scr восстановлен из canonical (с threadirqs)"
        REBOOT_NEEDED=1
        CHANGED=1
        return
    fi

    # Нет canonical — пробуем пропатчить текущий boot.scr через mkimage
    if ! command -v mkimage >/dev/null 2>&1; then
        log_warn "mkimage не найден. Установите: apt install u-boot-tools"
        log_warn "Патч boot.scr пропущен. Без threadirqs возможен IRQ storm на платах с PCA9536."
        return
    fi

    local mounted
    fat_mount && mounted=0 || mounted=1
    local fat_scr="${FAT_MNT}/boot.scr"

    if [ ! -f "$fat_scr" ]; then
        log_warn "boot.scr не найден: $fat_scr"
        fat_umount $mounted
        return
    fi

    # Извлекаем тело скрипта из U-Boot image
    local raw_scr="/tmp/boot.scr.txt"
    dd if="$fat_scr" bs=72 skip=1 of="$raw_scr" 2>/dev/null || {
        log_err "Не удалось извлечь тело boot.scr"
        fat_umount $mounted
        return
    }

    if grep -q threadirqs "$raw_scr" 2>/dev/null; then
        log_ok "threadirqs уже есть в boot.scr (но не активен — нужна перезагрузка)"
        fat_umount $mounted
        return
    fi

    # Добавляем threadirqs к bootargs
    sed -i 's/\(setenv bootargs .*\)"/\1 threadirqs"/' "$raw_scr" 2>/dev/null || true
    # Если формат другой (без кавычек):
    if ! grep -q threadirqs "$raw_scr" 2>/dev/null; then
        sed -i 's/\(bootargs=.*\)/\1 threadirqs/' "$raw_scr" 2>/dev/null || true
    fi

    if grep -q threadirqs "$raw_scr" 2>/dev/null; then
        cp "$fat_scr" "${fat_scr}.bak.$(date +%Y%m%d%H%M%S)"
        mkimage -A arm -O linux -T script -C none -a 0 -e 0 \
            -n "SA-02m boot" -d "$raw_scr" "$fat_scr" >/dev/null 2>&1 || {
            log_err "mkimage: ошибка пересборки boot.scr"
            fat_umount $mounted
            return
        }
        sync
        fat_umount $mounted
        log_ok "boot.scr пропатчен: threadirqs добавлен"
        REBOOT_NEEDED=1
        CHANGED=1
    else
        log_warn "Не удалось добавить threadirqs в boot.scr — проверьте формат скрипта вручную"
        fat_umount $mounted
    fi
}

# =============================================================================
# ШАГ 3: eth1.conf — allow-hotplug + DHCP + metric 100 + post-up route
# =============================================================================
step_eth1_conf() {
    log_step "ШАГ 3: /etc/network/interfaces.d/eth1.conf"

    local conf="/etc/network/interfaces.d/eth1.conf"
    local need_write=0

    if [ ! -f "$conf" ]; then
        log_warn "eth1.conf отсутствует — создаём"
        need_write=1
    else
        # Проверяем ключевые признаки корректной конфигурации
        if grep -q "^auto eth1" "$conf" 2>/dev/null; then
            log_warn "eth1.conf: 'auto eth1' → заменяем на 'allow-hotplug eth1'"
            need_write=1
        fi
        if ! grep -q "metric" "$conf" 2>/dev/null; then
            log_warn "eth1.conf: отсутствует metric"
            need_write=1
        fi
        if ! grep -q "post-up" "$conf" 2>/dev/null; then
            log_warn "eth1.conf: отсутствует post-up default route"
            need_write=1
        fi
        if [ $need_write -eq 0 ]; then
            log_ok "eth1.conf корректен"
            return
        fi
    fi

    mkdir -p /etc/network/interfaces.d
    cat > "$conf" <<'eth1CONF'
allow-hotplug eth1
iface eth1 inet dhcp
    metric 100
    post-up ip route replace default via 192.168.1.1 dev eth1 metric 100 || true
eth1CONF

    log_ok "eth1.conf обновлён (allow-hotplug + DHCP + metric 100 + post-up)"
    CHANGED=1
}

# =============================================================================
# ШАГ 4: eth0.conf — metric 200 (чтобы eth1 DHCP был дефолтным маршрутом)
# =============================================================================
step_eth0_metric() {
    log_step "ШАГ 4: eth0.conf metric 200"

    local conf="/etc/network/interfaces.d/eth0.conf"

    if [ ! -f "$conf" ]; then
        log_warn "eth0.conf не найден — пропуск"
        return
    fi

    if grep -q "metric" "$conf" 2>/dev/null; then
        local m
        m=$(awk '/^[[:space:]]*metric/{print $2; exit}' "$conf")
        if [ "$m" = "200" ]; then
            log_ok "eth0.conf: metric 200 уже установлен"
            return
        fi
        # Заменяем на 200
        sed -i 's/^[[:space:]]*metric.*/    metric 200/' "$conf"
        log_ok "eth0.conf: metric обновлён → 200"
    else
        # Добавляем metric 200 после последней опции iface
        echo "    metric 200" >> "$conf"
        log_ok "eth0.conf: metric 200 добавлен"
    fi
    CHANGED=1

    # Обновляем текущий маршрут если eth0 уже поднят
    if ip link show eth0 | grep -q "state UP" 2>/dev/null; then
        local cur_gw
        cur_gw=$(ip route show default dev eth0 2>/dev/null | awk '{print $3; exit}')
        if [ -n "$cur_gw" ]; then
            ip route replace default via "$cur_gw" dev eth0 metric 200 2>/dev/null || true
            log_info "  eth0 default route: metric обновлён в runtime"
        fi
    fi
}

# =============================================================================
# ШАГ 5: dhclient exit hook — default route при RFC3442 (option 121)
# =============================================================================
step_dhclient_hook() {
    log_step "ШАГ 5: dhclient exit hook (RFC3442 default route)"

    local hook="/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route"

    if [ -f "$hook" ] && grep -q "ip route replace default" "$hook" 2>/dev/null; then
        log_ok "dhclient exit hook уже установлен"
        return
    fi

    mkdir -p /etc/dhcp/dhclient-exit-hooks.d
    cat > "$hook" <<'HOOKEOF'
#!/bin/sh
if [ "$interface" = "eth1" ] && [ -n "$new_routers" ]; then
    case "$reason" in
        BOUND|RENEW|REBIND|REBOOT)
            ip route replace default via $new_routers dev eth1 metric 100 2>/dev/null || true
            ;;
    esac
fi
HOOKEOF
    chmod 755 "$hook"
    log_ok "dhclient exit hook установлен"
    CHANGED=1
}

# =============================================================================
# ШАГ 6: fix-eth.sh — текущая версия (без bounce, с route из lease)
# =============================================================================
step_fix_eth_sh() {
    log_step "ШАГ 6: /usr/local/bin/fix-eth.sh"

    local dst="/usr/local/bin/fix-eth.sh"

    # Проверяем наличие признаков актуальной версии
    if [ -f "$dst" ] \
       && grep -q "option routers" "$dst" 2>/dev/null \
       && ! grep -q "bounce_done\|bounce_marker" "$dst" 2>/dev/null; then
        log_ok "fix-eth.sh уже актуален (route-из-lease + без bounce)"
        return
    fi

    log_warn "fix-eth.sh устарел — обновляем"

    cat > "$dst" <<'FIXETHEOF'
#!/bin/bash
# fix-eth.sh  —  Static-IP interface recovery
# Called by: udev (ACTION==add/bind) or net-watchdog.service

LOG_FILE="/var/log/fix-eth.log"
LOG_MAX_BYTES=524288
LOCK_DIR="/run/fix-eth"
STATE_DIR="/run/fix-eth-state"
RECOVER_COOLDOWN=60
MAX_LINK_CYCLES=5
PING_COUNT=2
PING_TIMEOUT=3

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

log() {
    local level=$1; shift
    local msg="$*"
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [${level}] ${msg}" >> "$LOG_FILE" 2>/dev/null || true
    echo "[${ts}] [${level}] ${msg}"
}

rotate_log() {
    [ -f "$LOG_FILE" ] || return
    local size; size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( size > LOG_MAX_BYTES )); then
        tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        log INFO "Лог ротирован"
    fi
}

carrier_up() {
    local iface=$1
    [ -f "/sys/class/net/${iface}/carrier" ] || return 1
    [ "$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null)" = "1" ]
}

has_ip() {
    local iface=$1
    ip -4 addr show dev "$iface" 2>/dev/null | grep -q 'inet '
}

debug_iface_state() {
    local iface=$1 addr route carrier oper
    carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo "?")
    oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo "unknown")
    addr=$(ip -4 addr show dev "$iface" 2>/dev/null | awk '/inet /{print $2}' | tr '\n' ' ')
    route=$(ip route show dev "$iface" 2>/dev/null | tr '\n' ';')
    log INFO "$iface: carrier=${carrier} operstate=${oper} addr='${addr:-none}' routes='${route:-none}'"
}

iface_bootstrap_in_progress() {
    local iface=$1
    local unit state substate
    for unit in networking.service "ifup@${iface}.service"; do
        state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)
        substate=$(systemctl show -p SubState --value "$unit" 2>/dev/null)
        case "${state}:${substate}" in
            activating:*|active:running|active:start*|deactivating:*)
                return 0
                ;;
        esac
    done
    return 1
}

get_gateway() {
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"
    [ -f "$conf" ] && awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf"
}

state_file_for_iface() { echo "${STATE_DIR}/${1}.last_ok_target"; }

remember_target_success() {
    local iface=$1 target=$2
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$target" > "$(state_file_for_iface "$iface")"
}

target_was_reachable_before() {
    local iface=$1 target=$2
    local sf; sf=$(state_file_for_iface "$iface")
    [ -f "$sf" ] && [ "$(cat "$sf" 2>/dev/null)" = "$target" ]
}

gateway_warned_state_file() { echo "${STATE_DIR}/${1}.gw_unreachable_warned"; }

check_connectivity() {
    local iface=$1
    local iface_upper target_source custom_ping_var target warn_file
    iface_upper=$(echo "$iface" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
    target_source="gateway"
    custom_ping_var="WATCHDOG_PING_${iface_upper}"
    target="${!custom_ping_var:-}"

    if [ -n "$target" ]; then
        target_source="custom"
    else
        target=$(get_gateway "$iface")
    fi

    [ "$target" = "skip" ] && return 0

    if [ -z "$target" ]; then
        return 0
    fi

    warn_file=$(gateway_warned_state_file "$iface")
    local _pc="$PING_COUNT" _pt="$PING_TIMEOUT"
    if [ "$target_source" = "gateway" ] \
       && ! target_was_reachable_before "$iface" "$target" \
       && [ -f "$warn_file" ]; then
        _pc=1; _pt=1
    fi

    if ping -c "$_pc" -W "$_pt" -q "$target" >/dev/null 2>&1; then
        remember_target_success "$iface" "$target"
        if [ -f "$warn_file" ]; then
            log INFO "$iface: связь со шлюзом ${target} восстановлена"
            rm -f "$warn_file"
        fi
        return 0
    fi

    if [ "$target_source" = "gateway" ] && ! target_was_reachable_before "$iface" "$target"; then
        if [ ! -f "$warn_file" ]; then
            log INFO "$iface: шлюз ${target} недоступен с момента старта; считаем рабочим по carrier+IP"
            mkdir -p "$STATE_DIR"; : > "$warn_file"
        fi
        return 0
    fi

    if [ ! -f "$warn_file" ]; then
        log WARN "$iface: хост ${target} недоступен"
        mkdir -p "$STATE_DIR"; : > "$warn_file"
    fi
    return 1
}

acquire_lock() {
    local iface=$1
    mkdir -p "$LOCK_DIR"
    local lock="${LOCK_DIR}/${iface}.lock"
    if [ -f "$lock" ]; then
        local last_ts; last_ts=$(cat "$lock" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        if (( now - last_ts < RECOVER_COOLDOWN )); then
            log INFO "$iface: cooldown активен ($(( RECOVER_COOLDOWN - (now - last_ts) ))с)"
            return 1
        fi
    fi
    date +%s > "$lock"
    return 0
}

release_lock() { rm -f "${LOCK_DIR}/${1}.lock"; }

recover_iface() {
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"

    [ -f "$conf" ] || return 0
    [ -d "/sys/class/net/${iface}" ] || return 0

    if ! carrier_up "$iface"; then
        local oper; oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)
        local _cf="${LOCK_DIR}/${iface}.link_cycle_count"
        local _cc; _cc=$(cat "$_cf" 2>/dev/null || echo 0)
        if [ "$oper" = "down" ] && [ "$_cc" -lt "${MAX_LINK_CYCLES:-5}" ]; then
            _cc=$(( _cc + 1 ))
            mkdir -p "$LOCK_DIR"
            printf '%s\n' "$_cc" > "$_cf"
            log INFO "$iface: carrier=0 operstate=down, link cycle ${_cc}/${MAX_LINK_CYCLES:-5}"
            ip link set "$iface" down 2>/dev/null || true
            sleep 1
            ip link set "$iface" up   2>/dev/null || true
            sleep 1
            mii-tool -r "$iface" 2>/dev/null || ethtool -r "$iface" 2>/dev/null || true
            sleep 3
        else
            log INFO "$iface: нет физического линка (carrier=0, cycles=${_cc})"
            debug_iface_state "$iface"
        fi
        return 0
    fi

    if iface_bootstrap_in_progress "$iface"; then
        log INFO "$iface: ifupdown в процессе, пропуск"
        ( sleep 5; [ -x /usr/local/bin/sa02m-eth0-led.sh ] && /usr/local/bin/sa02m-eth0-led.sh ) &
        disown 2>/dev/null || true
        return 0
    fi

    if ! has_ip "$iface"; then
        log WARN "$iface: нет IP-адреса"
    else
        local iface_type
        iface_type=$(awk '/^[[:space:]]*iface[[:space:]]/{print $4; exit}' "$conf" 2>/dev/null)
        if [ "$iface_type" = "dhcp" ] && ! ip route show default dev "$iface" | grep -q .; then
            local gw metric
            gw=$(awk '/option routers/{gsub(/;/,"",$3); print $3; exit}' \
                 "/var/lib/dhcp/dhclient.${iface}.leases" 2>/dev/null)
            metric=$(awk '/^[[:space:]]*metric/{print $2; exit}' "$conf" 2>/dev/null)
            if [ -n "$gw" ]; then
                log INFO "$iface: default route отсутствует, восстанавливаем via ${gw}"
                ip route add default via "$gw" dev "$iface" ${metric:+metric $metric} 2>/dev/null || true
            fi
        fi
        check_connectivity "$iface"
        local grat_marker="${STATE_DIR}/${iface}.grat_arp_done"
        if [ ! -f "$grat_marker" ] && command -v python3 >/dev/null 2>&1 \
           && [ -f /usr/local/bin/sa02m-grat-arp.py ]; then
            touch "$grat_marker"
            python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
            (
                for _rep in 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                    sleep 1
                    python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
                done
            ) &
            disown 2>/dev/null || true
        fi
        [ -x /usr/local/bin/sa02m-eth0-led.sh ] && /usr/local/bin/sa02m-eth0-led.sh || true
        return 0
    fi

    acquire_lock "$iface" || return 0

    log INFO "$iface: начало восстановления"
    debug_iface_state "$iface"

    if command -v ifdown >/dev/null 2>&1; then
        ifdown "$iface" 2>/dev/null || true
        sleep 1
        ifup  "$iface" 2>/dev/null
    else
        ip link set "$iface" down 2>/dev/null || true
        sleep 1
        ip link set "$iface" up   2>/dev/null || true
        sleep 2
        local ip2 nm gw2
        ip2=$(awk '/^[[:space:]]*address/{print $2; exit}' "$conf")
        nm=$(awk '/^[[:space:]]*netmask/{print $2; exit}' "$conf")
        gw2=$(awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf")
        [ -n "$ip2" ] && ip addr add "${ip2}/${nm:-24}" dev "$iface" 2>/dev/null || true
        [ -n "$gw2" ] && ip route add default via "$gw2" dev "$iface" 2>/dev/null || true
    fi

    sleep 2

    if has_ip "$iface"; then
        log INFO "$iface: восстановлен, IP=$(ip -4 addr show dev "$iface" | awk '/inet /{print $2}')"
        debug_iface_state "$iface"
        release_lock "$iface"
        local grat_marker="${STATE_DIR}/${iface}.grat_arp_done"
        if [ ! -f "$grat_marker" ] && command -v python3 >/dev/null 2>&1 \
           && [ -f /usr/local/bin/sa02m-grat-arp.py ]; then
            touch "$grat_marker"
            python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
            (
                for _rep in 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                    sleep 1
                    python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
                done
            ) &
            disown 2>/dev/null || true
        fi
        [ -x /usr/local/bin/sa02m-eth0-led.sh ] && /usr/local/bin/sa02m-eth0-led.sh || true
    else
        log ERROR "$iface: восстановление не удалось"
        debug_iface_state "$iface"
        release_lock "$iface"
    fi
}

rotate_log
mkdir -p "$LOCK_DIR"
mkdir -p "$STATE_DIR"

if [ -n "$1" ]; then
    recover_iface "$1"
else
    for conf in /etc/network/interfaces.d/end*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        recover_iface "$iface"
    done
fi
FIXETHEOF

    chmod 755 "$dst"
    log_ok "fix-eth.sh обновлён"
    CHANGED=1
}

# =============================================================================
# ШАГ 7: sa02m-eth1-coldboot — oneshot-сервис для cold-boot autoneg
# =============================================================================
step_eth1_coldboot() {
    log_step "ШАГ 7: sa02m-eth1-coldboot"

    # Скрипт
    local sh_dst="/usr/local/bin/sa02m-eth1-coldboot.sh"
    local svc_dst="/etc/systemd/system/sa02m-eth1-coldboot.service"

    local need_sh=0 need_svc=0

    if [ ! -f "$sh_dst" ] || ! grep -q "ethtool -r" "$sh_dst" 2>/dev/null; then
        need_sh=1
    fi
    if [ ! -f "$svc_dst" ]; then
        need_svc=1
    fi

    if [ $need_sh -eq 1 ]; then
        cat > "$sh_dst" <<'CBEOF'
#!/bin/bash
# SA-02m: eth1 cold-boot autoneg recovery (single ethtool -r if no carrier)
IFACE=eth1
WAIT_INITIAL=30
WAIT_AUTONEG=15

log() { echo "[$(date '+%H:%M:%S')] sa02m-eth1-coldboot: $*"; }

sleep "$WAIT_INITIAL"

carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "${IFACE}: carrier already up — nothing to do"
    exit 0
fi

log "${IFACE}: no carrier after ${WAIT_INITIAL}s — restarting autoneg (ethtool -r)"
ip link set "$IFACE" up 2>/dev/null || true
ethtool -r "$IFACE" 2>/dev/null || mii-tool -r "$IFACE" 2>/dev/null || true

sleep "$WAIT_AUTONEG"

carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "${IFACE}: link UP after autoneg restart"
else
    log "${IFACE}: still no carrier after autoneg restart — check cable or power-cycle"
fi
CBEOF
        chmod 755 "$sh_dst"
        log_ok "sa02m-eth1-coldboot.sh установлен"
        CHANGED=1
    else
        log_ok "sa02m-eth1-coldboot.sh уже актуален"
    fi

    if [ $need_svc -eq 1 ]; then
        cat > "$svc_dst" <<'SVCEOF'
[Unit]
Description=SA-02m: eth1 cold-boot autoneg recovery (single ethtool -r if no carrier)
After=network.target networking.service
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=no
ExecStart=/usr/local/bin/sa02m-eth1-coldboot.sh

[Install]
WantedBy=multi-user.target
SVCEOF
        log_ok "sa02m-eth1-coldboot.service установлен"
        CHANGED=1
    else
        log_ok "sa02m-eth1-coldboot.service уже существует"
    fi
}

# =============================================================================
# ШАГ 8: удаление устаревшего sa02m-phy-coldboot
# =============================================================================
step_legacy_cleanup() {
    log_step "ШАГ 8: удаление устаревшего sa02m-phy-coldboot"

    local removed=0

    if [ -f /etc/systemd/system/sa02m-phy-coldboot.service ]; then
        systemctl stop sa02m-phy-coldboot 2>/dev/null || true
        systemctl disable sa02m-phy-coldboot 2>/dev/null || true
        rm -f /etc/systemd/system/sa02m-phy-coldboot.service
        log_ok "sa02m-phy-coldboot.service удалён"
        removed=1; CHANGED=1
    fi

    if [ -f /usr/local/bin/sa02m-phy-coldboot.sh ]; then
        rm -f /usr/local/bin/sa02m-phy-coldboot.sh
        log_ok "sa02m-phy-coldboot.sh удалён"
        removed=1; CHANGED=1
    fi

    [ $removed -eq 0 ] && log_ok "Устаревших компонентов не найдено"
}

# =============================================================================
# ШАГ 9: udev — unbind i2c-2 при IRQ storm от PCA9536
# =============================================================================
step_udev_i2c2() {
    log_step "ШАГ 9: udev i2c-2 unbind (PCA9536 IRQ storm)"

    local rules_dst="/etc/udev/rules.d/50-sa02m-i2c2-unbind.rules"
    local sh_dst="/usr/local/sbin/sa02m-i2c2-unbind.sh"

    local need_rules=0 need_sh=0

    if [ ! -f "$rules_dst" ] || ! grep -q "1c2b800.i2c" "$rules_dst" 2>/dev/null; then
        need_rules=1
    fi
    if [ ! -f "$sh_dst" ]; then
        need_sh=1
    fi

    if [ $need_rules -eq 1 ]; then
        cat > "$rules_dst" <<'RULESEOF'
# SA-02m: unbind i2c-2 controller immediately when the adapter appears.
# The PCA9536 expansion board pulls SDA low on power-up, which causes
# mv64xxx_i2c/i2c-sunxi to start bus-recovery IRQ retries → CPU spike →
# watchdog starvation → reboot.
ACTION=="add", SUBSYSTEM=="platform", KERNEL=="1c2b800.i2c", \
    RUN{program}+="/usr/local/sbin/sa02m-i2c2-unbind.sh"
ACTION=="add", SUBSYSTEM=="i2c", KERNEL=="i2c-2", \
    RUN{program}+="/usr/local/sbin/sa02m-i2c2-unbind.sh"
RULESEOF
        log_ok "50-sa02m-i2c2-unbind.rules установлен"
        CHANGED=1
    else
        log_ok "50-sa02m-i2c2-unbind.rules уже актуален"
    fi

    if [ $need_sh -eq 1 ]; then
        mkdir -p /usr/local/sbin
        cat > "$sh_dst" <<'UNBINDEOF'
#!/bin/sh
# Unbind i2c-2 controller to stop IRQ storm from PCA9536 (SDA low on startup)
[ -f /run/sa02m-i2c2-recovered ] && exit 0

if [ "${SUBSYSTEM:-}" = "platform" ] && [ -n "${KERNEL:-}" ]; then
    PDEV="${KERNEL}"
else
    PDEV=$(printf '%s' "${DEVPATH:-}" | sed 's|.*/soc/\([^/]*\)/.*|\1|')
fi

if [ -z "$PDEV" ] || [ "$PDEV" = "${DEVPATH:-}" ]; then
    PDEV=$(readlink /sys/class/i2c-adapter/i2c-2 2>/dev/null \
           | sed 's|.*soc/\([^/]*\)/.*|\1|')
fi

if [ -z "$PDEV" ]; then
    logger -t sa02m-i2c2 "unbind: could not resolve i2c-2 platform device"
    exit 1
fi

for drv in /sys/bus/platform/drivers/mv64xxx_i2c \
           /sys/bus/platform/drivers/i2c-sunxi; do
    [ -f "${drv}/unbind" ] || continue
    printf '%s' "$PDEV" > "${drv}/unbind" 2>/dev/null || continue
    logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbound via udev"
    exit 0
done

logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbind: driver not found"
UNBINDEOF
        chmod 755 "$sh_dst"
        log_ok "sa02m-i2c2-unbind.sh установлен"
        CHANGED=1
    else
        log_ok "sa02m-i2c2-unbind.sh уже существует"
    fi
}

# =============================================================================
# ШАГ 10: перезагрузка конфигурации и применение
# =============================================================================
step_reload_apply() {
    log_step "ШАГ 10: reload + применение изменений"

    systemctl daemon-reload 2>/dev/null || true

    # Включаем новые сервисы
    if [ -f /etc/systemd/system/sa02m-eth1-coldboot.service ]; then
        systemctl enable sa02m-eth1-coldboot 2>/dev/null || true
        log_info "  sa02m-eth1-coldboot.service включён"
    fi

    # Перезагружаем udev правила
    udevadm control --reload-rules 2>/dev/null || true
    log_info "  udev правила перезагружены"

    # Если eth1 уже существует — пробуем поднять через allow-hotplug
    if [ -d /sys/class/net/eth1 ]; then
        # Проверяем carrier
        local carrier
        carrier=$(cat /sys/class/net/eth1/carrier 2>/dev/null || echo 0)
        if [ "$carrier" = "1" ]; then
            log_info "  eth1 carrier UP — поднимаем ifup"
            ifup eth1 2>/dev/null || true
            sleep 2
        else
            log_warn "  eth1 carrier DOWN — PHY не связан с партнёром"
            log_warn "  Запускаем однократный autoneg restart..."
            ip link set eth1 up 2>/dev/null || true
            ethtool -r eth1 2>/dev/null || mii-tool -r eth1 2>/dev/null || true
            log_info "  Ожидание 15 с..."
            sleep 15
            carrier=$(cat /sys/class/net/eth1/carrier 2>/dev/null || echo 0)
            if [ "$carrier" = "1" ]; then
                log_info "  eth1 carrier UP после autoneg restart"
                ifup eth1 2>/dev/null || true
                sleep 2
            else
                log_warn "  eth1 всё ещё без carrier — может потребоваться power-cycle"
            fi
        fi
    else
        log_warn "  eth1 не найден в системе — DTB должен быть пропатчен и применён (reboot)"
    fi
}

# =============================================================================
# ШАГ 11: диагностика и итог
# =============================================================================
step_report() {
    log_step "ШАГ 11: состояние eth1"

    local ip gw carrier
    carrier=$(cat /sys/class/net/eth1/carrier 2>/dev/null | tr -d '\0' || echo "?")
    ip=$(ip -4 addr show dev eth1 2>/dev/null | awk '/inet /{print $2}')
    gw=$(ip route show default dev eth1 2>/dev/null | awk '{print $3; exit}')

    echo ""
    echo "  eth1 carrier   : ${carrier}"
    echo "  eth1 IP        : ${ip:-нет}"
    echo "  eth1 def route : ${gw:-нет}"
    echo "  /proc/cmdline  : $(cat /proc/cmdline 2>/dev/null | grep -o 'threadirqs' || echo 'нет threadirqs')"
    echo ""

    if [ -n "$ip" ] && [ -n "$gw" ]; then
        # Проверяем интернет через eth1
        if ping -c 2 -W 3 -I eth1 8.8.8.8 >/dev/null 2>&1; then
            log_ok "Интернет через eth1: ДОСТУПЕН (ping 8.8.8.8 OK)"
        else
            log_warn "eth1 имеет IP и gateway, но ping 8.8.8.8 не прошёл"
            log_info "  Проверьте default route: ip route show"
        fi
    elif [ "$carrier" = "1" ] && [ -z "$ip" ]; then
        log_warn "eth1 carrier UP, но IP не получен — DHCP не ответил"
        log_info "  Попробуйте: dhclient -v eth1"
    elif [ "$carrier" != "1" ]; then
        if grep -q "disabled" /proc/device-tree/soc/ethernet@1c50000/status 2>/dev/null; then
            log_err "GMAC отключён в DTB — требуется перезагрузка после патча DTB"
        else
            log_warn "eth1 нет физического линка — проверьте кабель"
            log_info "  Или нужен power-cycle для PHY холодного старта"
        fi
    fi

    echo ""
    if [ $REBOOT_NEEDED -eq 1 ]; then
        echo -e "${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║  ТРЕБУЕТСЯ ПЕРЕЗАГРУЗКА для применения изменений DTB/boot.scr${NC}"
        echo -e "${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
        echo "  Выполните: reboot"
    elif [ $CHANGED -eq 1 ]; then
        log_ok "Изменения применены. Рекомендуется перезагрузка для чистой проверки."
        echo "  Выполните: reboot"
    else
        log_ok "Все компоненты уже актуальны."
    fi
    echo ""
}

# =============================================================================
# MAIN
# =============================================================================
require_root

echo ""
echo "============================================================"
echo " SA-02m-2: fix-eth1-internet.sh"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

step_dtb
step_bootscr_threadirqs
step_eth1_conf
step_eth0_metric
step_dhclient_hook
step_fix_eth_sh
step_eth1_coldboot
step_legacy_cleanup
step_udev_i2c2
step_reload_apply
step_report
