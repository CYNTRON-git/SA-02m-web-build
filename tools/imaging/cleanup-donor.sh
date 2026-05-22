#!/bin/bash
# cleanup-donor.sh — фазы 1–4 (без сброса ID / ssh keys).
# Шаги 5–7 (machine-id, host keys, firstrun) выполняет stream-after-cleanup.sh
# в той же ssh-сессии, что и zero-fill + dd — иначе новые подключения к sshd падают.
set -euo pipefail
LC_ALL=C
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: cleanup-donor.sh должен запускаться от root" >&2
    exit 1
fi

log "[1/4] Состояние до cleanup"
df -hT / | sed 's/^/    /'

log "[2/4] Удаление мусора в /root и /home"
rm -rf \
    /root/backup \
    /root/mplc_cyntron_build \
    /root/sa02m-deploy /root/sa02m-deploy.tar.gz \
    /root/cursor_build.swap \
    /root/u-boot-sunxi-with-spl.bin \
    /root/40-usb_modeswitch.rules \
    "/root/-d" "/root/nul" "/root/NUL" \
    "/root/ystemd-analyze critical-chain" \
    /root/.cache /root/.bash_history /root/.viminfo /root/.lesshst \
    /root/.local/share/Trash 2>/dev/null || true

for h in /home/*/; do
    [ -d "$h" ] || continue
    rm -rf "$h/.cache" "$h/.bash_history" "$h/.local/share/Trash" 2>/dev/null || true
done

log "[3/4] Удаление тулчейна (build tools / kernel headers)"
PURGE_PATTERNS=(
    'build-essential' 'dkms' 'make'
    'gcc' 'gcc-1?' 'g++*' 'cpp' 'cpp-1?'
    'gcc-arm-linux-gnueabihf' 'gcc-13-arm-linux-gnueabihf'
    'libgcc-*-dev' 'libstdc++-*-dev'
    'linux-headers-*' 'linux-source-*'
)
PURGE_LIST=()
for pat in "${PURGE_PATTERNS[@]}"; do
    while IFS= read -r pkg; do
        [ -n "$pkg" ] && PURGE_LIST+=("$pkg")
    done < <(dpkg-query -W -f='${Package}\n' "$pat" 2>/dev/null)
done

if [ "${#PURGE_LIST[@]}" -gt 0 ]; then
    apt-get purge -y --auto-remove "${PURGE_LIST[@]}" || true
else
    log "    нет пакетов под удаление"
fi
apt-get autoremove --purge -y || true

log "[4/4] Очистка apt cache, journald, логов, /tmp"
apt-get clean
rm -rf /var/lib/apt/lists/*
mkdir -p /var/lib/apt/lists/partial
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
find /var/log -type f \( -name '*.log' -o -name '*.log.*' -o -name '*.gz' \) \
     -exec truncate -s 0 {} \; 2>/dev/null || true
find /var/log -type f -regex '.*\.[0-9]+\(\.gz\)?$' -delete 2>/dev/null || true
rm -rf /var/tmp/* /tmp/* 2>/dev/null || true

log "    cleanup фазы 1–4 завершены"
df -hT / | sed 's/^/    /'

# Сервис regen ставим заранее (до сброса keys в stream-after-cleanup.sh),
# чтобы после reboot донора sshd мог подняться, если сессия dd прервётся.
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
