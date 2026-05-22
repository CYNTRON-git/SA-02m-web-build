#!/bin/bash
# stream-after-cleanup.sh — одна ssh-сессия после cleanup-donor.sh (фазы 1–4).
# Порядок: zero-fill → сброс ID / ssh keys / firstrun → stop services → dd stdout.
# Логи только в stderr; stdout = бинарный образ /dev/mmcblk2.
#
# Аргументы:
#   --no-zerofill   пропустить zero-fill
#   --no-id-reset   не сбрасывать machine-id / ssh keys (только тестовые снимки)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

DO_ZEROFILL=1
DO_ID_RESET=1

while [ $# -gt 0 ]; do
    case "$1" in
        --no-zerofill) DO_ZEROFILL=0; shift ;;
        --no-id-reset) DO_ID_RESET=0; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[stream %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

IMAGING_LOCK="${IMAGING_LOCK:-/run/sa02m-imaging.lock}"
log "блокировка userspace watchdog ($IMAGING_LOCK)"
date -Iseconds >"$IMAGING_LOCK" 2>/dev/null || echo 1 >"$IMAGING_LOCK"
sync

install_regen_ssh_service() {
    cat > /etc/systemd/system/regen-ssh-host-keys.service <<'EOF'
[Unit]
Description=Regenerate SSH host keys on first boot (after image clone)
DefaultDependencies=no
After=local-fs.target
Before=ssh.service sshd.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable regen-ssh-host-keys.service 2>/dev/null || true
}

prepare_clone_ids() {
    log "сброс machine-id (ssh keys — непосредственно перед dd)"
    truncate -s 0 /etc/machine-id
    rm -f /var/lib/dbus/machine-id
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
    install_regen_ssh_service
    touch /root/.not_logged_in_yet
    sync
}

drop_ssh_host_keys() {
    rm -f /etc/ssh/ssh_host_*
    sync
}

if [ "$DO_ZEROFILL" = "1" ]; then
    log "zero-fill свободного места"
    dd if=/dev/zero of=/zero.fill bs=4M status=none 2>/dev/null || true
    sync
    rm -f /zero.fill
    sync
    df -hT / >&2
fi

if [ "$DO_ID_RESET" = "1" ]; then
    log "rc.local: ssh-keygen при boot если keys отсутствуют (страховка донора)"
    cat > /etc/rc.local <<'EOF'
#!/bin/bash
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    /usr/bin/ssh-keygen -A
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
fi
exit 0
EOF
    chmod +x /etc/rc.local
    systemctl enable rc-local.service 2>/dev/null || true
    prepare_clone_ids
fi

log "остановка сервисов перед dd"
systemctl stop nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm 2>/dev/null || true
rmmod -f g_mass_storage 2>/dev/null || true

# Disable NIC offloads that cause out-of-order packet delivery under eMMC DMA load.
# TSO/GSO batching on Allwinner GMAC creates packet reordering, which triggers
# massive duplicate ACKs and TCP congestion collapse during sustained dd transfer.
ethtool -K eth0 gso off gro off tso off 2>/dev/null || true
log "eth0 offloads: GSO/GRO/TSO отключены (предотвращение out-of-order TCP)"

# Prevent CPU from entering deep sleep (schedutil/ondemand drops freq → slow SSH).
# Set performance governor for all CPUs; restore on script exit.
_PREV_GOV=""
_cpu_governor_restore() {
    [ -n "$_PREV_GOV" ] || return 0
    for cpu in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
        echo "$_PREV_GOV" > "$cpu" 2>/dev/null || true
    done
}
trap _cpu_governor_restore EXIT
_gov_file="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
if [ -f "$_gov_file" ]; then
    _PREV_GOV=$(cat "$_gov_file" 2>/dev/null || true)
    for cpu in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
        echo performance > "$cpu" 2>/dev/null || true
    done
    log "cpu governor: ${_PREV_GOV} → performance (для dd)"
fi

sync
sync

if [ "$DO_ID_RESET" = "1" ]; then
    log "удаление ssh host keys (сразу перед dd, текущая сессия не рвётся)"
    drop_ssh_host_keys
fi

log "dd /dev/mmcblk2 -> stdout (не закрывайте ssh до конца передачи)"
exec dd if=/dev/mmcblk2 bs=1M status=none
