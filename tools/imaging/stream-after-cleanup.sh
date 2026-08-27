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

# Страховка: make-image.sh маскирует watchdog до stream; повторяем на случай ручного запуска.
for _svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog sa02m-watchdog-feed; do
    systemctl stop "$_svc" 2>/dev/null || true
    systemctl mask "$_svc" 2>/dev/null || true
done
systemctl set-property --runtime Manager RuntimeWatchdogSec=0 2>/dev/null || true

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

prepare_firstboot_resize() {
    # Only sa02m-rootfs-expand — dual-enable with armbian-resize floods udev for
    # ~3 min (partprobe settle timeout), breaks ifupdown-pre, delays networking,
    # and leaves PHY/ARP needing a cable re-plug on first boot after flash.
    log "firstrun resize: enable sa02m-rootfs-expand; disable armbian-resize (no dual resize)"
    rm -f /var/lib/sa02m-rootfs-expand.done
    systemctl stop armbian-resize-filesystem.service 2>/dev/null || true
    systemctl disable armbian-resize-filesystem.service 2>/dev/null || true
    systemctl mask armbian-resize-filesystem.service 2>/dev/null || true
    systemctl enable sa02m-rootfs-expand.service 2>/dev/null || true
}

wipe_cloud_enrollment() {
    # Cloned images must not ship donor cloud identity (device_secret / frpc /
    # enrolled=true) — otherwise every new board pretends to be the donor in
    # cloud.cyntron.ru. Offline wipe only; cloud-side detach is operator task.
    log "сброс cloud enrollment (agent.conf / device_secret / frpc)"
    systemctl stop sa02m-cloud-frpc.service 2>/dev/null || true
    systemctl stop sa02m-cloud-agent.service 2>/dev/null || true
    rm -f /etc/sa02m-cloud/device_secret \
          /etc/sa02m-cloud/frpc.toml \
          /etc/sa02m-cloud/frpc.toml.bak* \
          /etc/sa02m-cloud/pair_request \
          /etc/sa02m-cloud/activation_token \
          /run/sa02m-cloud-status.json
    mkdir -p /etc/sa02m-cloud
    chmod 750 /etc/sa02m-cloud
    cat > /etc/sa02m-cloud/agent.conf <<'EOF'
[cloud]
api_url = https://cloud.cyntron.ru/api/v1
server_host = cloud.cyntron.ru
enrolled = false
device_id =
heartbeat_interval = 30

[device]
serial =
web_port = 9999
EOF
    chmod 640 /etc/sa02m-cloud/agent.conf
}

wipe_alice_enrollment() {
    # The gateway identifies a controller by its mTLS DN alone, so two boards
    # holding one device.crt.pem are ONE controller to alice.cyntron.ru — a
    # customer's board would surface in the donor's «Дом с Алисой». The device
    # document goes too: its uuid4 ids identify BINDINGS, and the donor's bench
    # bindings must not clone at all. Wiped HERE, before dd, so the private key
    # never enters the raw stream or any intermediate artifact.
    # ca.crt.pem STAYS — it is the shared gateway CA, not board identity.
    # Capture is the OPPOSITE policy to factory reset, which deliberately
    # preserves a board's OWN certs. Clear-list home:
    # docs/contracts/image-identity-reset.md.
    log "сброс Alice identity (device.crt/key, pending_claim, привязки)"
    timeout 10 systemctl stop sa02m-alice-client.service 2>/dev/null || true
    timeout 10 systemctl disable sa02m-alice-client.service 2>/dev/null || true
    # The globs cover the ATOMIC-WRITE SIDECARS by shape, not by name: a crash
    # or ENOSPC mid-link strands device.key.pem.tmp (api.py writes <path>.tmp
    # then os.replace) or a mkstemp .alice-XXXXXX holding the donor's bindings.
    # Literal names alone would clone the private key one character off the
    # list. Same shape as the cloud twin's frpc.toml.bak* row above.
    # `*.tmp` cannot match ca.crt.pem, which must survive.
    rm -f /var/lib/sa02m-alice/device.crt.pem \
          /var/lib/sa02m-alice/device.key.pem \
          /var/lib/sa02m-alice/pending_claim.json \
          /var/lib/sa02m-alice/*.tmp \
          /etc/sa02m-alice/.alice-* \
          /run/sa02m-alice/status.json \
          /run/sa02m-alice/*.tmp
    # Every file guarded: an absent file must be a no-op, never an abort under
    # `set -euo pipefail` — a donor that never linked has no client.conf, and
    # the legacy flat layout is absent on any modern board. Aborting here would
    # kill the capture BEFORE dd.
    local _f
    for _f in /etc/sa02m-alice/sa02m-alice-devices.conf \
              /etc/sa02m-alice-devices.conf; do
        [ -f "$_f" ] || continue
        printf '%s\n' '{' '  "rooms": [],' '  "devices": []' '}' > "$_f"
    done
    for _f in /etc/sa02m-alice/sa02m-alice-client.conf \
              /etc/sa02m-alice-client.conf; do
        [ -f "$_f" ] || continue
        grep -q 'client_enabled' "$_f" 2>/dev/null || continue
        sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' "$_f"
    done
}

prepare_clone_ids() {
    log "сброс machine-id (ssh keys — непосредственно перед dd)"
    truncate -s 0 /etc/machine-id
    rm -f /var/lib/dbus/machine-id
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
    install_regen_ssh_service
    wipe_cloud_enrollment
    wipe_alice_enrollment
    prepare_firstboot_resize
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
