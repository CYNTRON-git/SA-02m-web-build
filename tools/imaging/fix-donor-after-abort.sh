#!/bin/bash
# fix-donor-after-abort.sh — восстановление донора после прерванного make-image.
# Запуск: на доноре (ssh/serial) или: ssh root@IP 'bash -s' < fix-donor-after-abort.sh
#   --preflight   только убить orphan dd (перед новым снятием образа)
set -euo pipefail

PREFLIGHT=0
[ "${1:-}" = "--preflight" ] && PREFLIGHT=1

[ "$(id -u)" -ne 0 ] && { echo "ERROR: need root" >&2; exit 1; }

log() { printf '[fix-donor %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

log "останов orphan dd (если есть)"
pkill -TERM -f 'dd if=/dev/mmcblk2' 2>/dev/null || true
sleep 1
pkill -KILL -f 'dd if=/dev/mmcblk2' 2>/dev/null || true
rm -f /zero.fill 2>/dev/null || true

if [ "$PREFLIGHT" -eq 1 ]; then
    log "preflight OK (dd остановлен)"
    exit 0
fi

log "machine-id (если пуст после id reset)"
if [ ! -s /etc/machine-id ]; then
    systemd-machine-id-setup 2>/dev/null || true
    rm -f /var/lib/dbus/machine-id
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
fi

log "ssh host keys + sshd"
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    /usr/bin/ssh-keygen -A
fi
systemctl daemon-reload 2>/dev/null || true
systemctl enable regen-ssh-host-keys.service 2>/dev/null || true
systemctl enable ssh 2>/dev/null || true
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

rm -f /root/.not_logged_in_yet

log "запуск сервисов"
systemctl start nginx fcgiwrap sa02m-flasher php8.3-fpm 2>/dev/null || true
systemctl start mplc4 2>/dev/null || true
sync

log "состояние"
systemctl is-active ssh 2>/dev/null || systemctl is-active sshd
df -hT / | sed 's/^/    /'
[ -s /etc/machine-id ] && echo "    machine-id: OK ($(wc -c < /etc/machine-id) bytes)" || echo "    machine-id: EMPTY"
