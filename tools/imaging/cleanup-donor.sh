#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  cleanup-donor.sh   v1.0
#  Готовит донорское устройство (192.168.1.136 по умолчанию) к снятию
#  компактного образа eMMC. Выполняется ВНУТРИ устройства (по ssh -s).
#
#  Что делает:
#    1) Удаляет временный мусор в /root и /home (build-результаты, swap-файлы)
#    2) Удаляет тулчейн (gcc / build-essential / dkms / linux-headers)
#    3) Чистит apt cache, journald, лог-файлы, /tmp, /var/tmp
#    4) Сбрасывает уникальные идентификаторы:
#         /etc/machine-id, /var/lib/dbus/machine-id, ssh host keys
#       (будут регенерированы на клонах при первой загрузке)
#    5) Включает Armbian firstrun (touch /root/.not_logged_in_yet) →
#       при первой загрузке клона armbian-resize-filesystem.service
#       растянет rootfs на всю eMMC
#    6) Регистрирует systemd-сервис regen-ssh-host-keys.service
#       для генерации ssh host keys при первой загрузке клона
#
#  ВНИМАНИЕ: после запуска gcc / make / dkms перестанут быть доступны
#  на доноре. Если планируется снова собирать драйверы — переустановите
#  пакеты из MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md или с apt cache до cleanup.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
LC_ALL=C
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: cleanup-donor.sh должен запускаться от root" >&2
    exit 1
fi

log "[0/7] Состояние до cleanup"
df -hT / | sed 's/^/    /'

# ─────────────────────────────────────────────────────────────────────────
log "[1/7] Удаление мусора в /root и /home"
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

# ─────────────────────────────────────────────────────────────────────────
log "[2/7] Удаление тулчейна (build tools / kernel headers)"
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

# ─────────────────────────────────────────────────────────────────────────
log "[3/7] Очистка apt cache и списков"
apt-get clean
rm -rf /var/lib/apt/lists/*
mkdir -p /var/lib/apt/lists/partial

# ─────────────────────────────────────────────────────────────────────────
log "[4/7] Очистка journald и логов"
journalctl --rotate || true
journalctl --vacuum-time=1s || true
find /var/log -type f \( -name '*.log' -o -name '*.log.*' -o -name '*.gz' \) \
     -exec truncate -s 0 {} \; 2>/dev/null || true
find /var/log -type f -regex '.*\.[0-9]+\(\.gz\)?$' -delete 2>/dev/null || true
rm -rf /var/tmp/* /tmp/* 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────
log "[5/7] Сброс уникальных идентификаторов (machine-id, ssh host keys)"
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*

cat > /etc/systemd/system/regen-ssh-host-keys.service <<'EOF'
[Unit]
Description=Regenerate SSH host keys on first boot (after image clone)
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable regen-ssh-host-keys.service >/dev/null

# ─────────────────────────────────────────────────────────────────────────
log "[6/7] Включение Armbian firstrun (resize + initial setup)"
touch /root/.not_logged_in_yet
sync

# ─────────────────────────────────────────────────────────────────────────
log "[7/7] Состояние после cleanup"
df -hT / | sed 's/^/    /'
echo
echo "    cleanup-donor.sh завершён успешно."
echo "    Следующий шаг (на хосте): zero-fill свободного места + dd образа."
