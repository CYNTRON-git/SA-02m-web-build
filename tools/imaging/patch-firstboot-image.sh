#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# patch-firstboot-image.sh — offline patch of a PiShrink SA-02m .img
#
# Applies first-boot network/watchdog/resize fixes so the next flash works
# without Ethernet cable re-plug and with watchdogs restored after expand.
#
# Usage (WSL/Linux):
#   sudo ./tools/imaging/patch-firstboot-image.sh \
#       /path/to/SA-02m-….img
#
# Idempotent. Does NOT recompress xz (caller may xz afterwards).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMG="${1:-}"

die() { echo "FATAL: $*" >&2; exit 1; }
log() { echo "[patch-firstboot] $*"; }

[ -n "$IMG" ] || die "usage: $0 /path/to/image.img"
[ -f "$IMG" ] || die "image not found: $IMG"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

ETC="$REPO_ROOT/etc"
SBIN_SRC="$REPO_ROOT/usr/local/sbin"

loop=$(losetup --partscan -f --show "$IMG") || die "losetup failed"
cleanup() {
    umount "$mnt" 2>/dev/null || true
    losetup -d "$loop" 2>/dev/null || true
    rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT

mnt=$(mktemp -d /tmp/sa02m-firstboot-XXXXXX)
rootpart="${loop}p2"
for _ in 1 2 3 4 5 6 7 8; do
    [ -b "$rootpart" ] && break
    # older losetup: /dev/mapper/loopXp2 via kpartx
    sleep 1
done
if [ ! -b "$rootpart" ]; then
    kpartx -av "$loop" >/dev/null 2>&1 || true
    base=$(basename "$loop")
    rootpart="/dev/mapper/${base}p2"
fi
[ -b "$rootpart" ] || die "no p2 on $loop"
mount "$rootpart" "$mnt" || die "mount $rootpart failed"
log "mounted $rootpart -> $mnt"

install -d "$mnt/etc/systemd/system" \
           "$mnt/etc/systemd/system.conf.d" \
           "$mnt/etc/systemd/system/basic.target.wants" \
           "$mnt/etc/systemd/system/multi-user.target.wants" \
           "$mnt/usr/local/sbin" \
           "$mnt/usr/local/bin"

# ── scripts ───────────────────────────────────────────────────────────────
install -m 755 "$ETC/sa02m-rootfs-expand.sh"          "$mnt/usr/local/sbin/sa02m-rootfs-expand.sh"
install -m 644 "$ETC/systemd/sa02m-rootfs-expand.service" \
    "$mnt/etc/systemd/system/sa02m-rootfs-expand.service"

install -m 755 "$ETC/fix-eth.sh"                     "$mnt/usr/local/bin/fix-eth.sh"
install -m 644 "$ETC/fix-eth@.service"               "$mnt/etc/systemd/system/fix-eth@.service"
[ -f "$ETC/fix-eth.service" ] && install -m 644 "$ETC/fix-eth.service" \
    "$mnt/etc/systemd/system/fix-eth.service"

install -m 755 "$ETC/net-watchdog.sh"                 "$mnt/usr/local/bin/net-watchdog.sh"
install -m 644 "$ETC/net-watchdog.service"            "$mnt/etc/systemd/system/net-watchdog.service"

install -m 755 "$ETC/sa02m-userspace-watchdog.sh"     "$mnt/usr/local/sbin/sa02m-userspace-watchdog"
install -m 644 "$ETC/systemd/sa02m-userspace-watchdog.service" \
    "$mnt/etc/systemd/system/sa02m-userspace-watchdog.service"
[ -f "$ETC/sa02m_userspace_watchdog.conf" ] && install -m 644 "$ETC/sa02m_userspace_watchdog.conf" \
    "$mnt/etc/sa02m_userspace_watchdog.conf"

install -m 755 "$ETC/sa02m-failure-monitor.sh"        "$mnt/usr/local/sbin/sa02m-failure-monitor"
install -m 644 "$ETC/sa02m-failure-monitor.service"   "$mnt/etc/systemd/system/sa02m-failure-monitor.service"
[ -f "$ETC/sa02m_failure_monitor.conf" ] && install -m 644 "$ETC/sa02m_failure_monitor.conf" \
    "$mnt/etc/sa02m_failure_monitor.conf"

install -m 755 "$SBIN_SRC/sa02m-eth-coldboot.sh"      "$mnt/usr/local/sbin/sa02m-eth-coldboot.sh"
install -m 644 "$ETC/systemd/sa02m-eth-coldboot.service" \
    "$mnt/etc/systemd/system/sa02m-eth-coldboot.service"

# ── RuntimeWatchdog drop-in (sun4i ≤16s → 8s) ─────────────────────────────
if [ -f "$ETC/systemd/sa02m-watchdog.conf" ]; then
    install -m 644 "$ETC/systemd/sa02m-watchdog.conf" \
        "$mnt/etc/systemd/system.conf.d/sa02m-watchdog.conf"
else
    printf '%s\n' '[Manager]' 'RuntimeWatchdogSec=8s' 'RebootWatchdogSec=2min' \
        > "$mnt/etc/systemd/system.conf.d/sa02m-watchdog.conf"
fi
sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' \
    "$mnt/etc/systemd/system.conf" 2>/dev/null || true

# ── Drop permanent /dev/null masks that destroy /etc unit files ───────────
for u in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog \
         sa02m-eth-coldboot sa02m-rootfs-expand; do
    if [ -L "$mnt/etc/systemd/system/${u}.service" ]; then
        tgt=$(readlink "$mnt/etc/systemd/system/${u}.service" 2>/dev/null || true)
        if [ "$tgt" = "/dev/null" ]; then
            rm -f "$mnt/etc/systemd/system/${u}.service"
            log "removed stale mask: ${u}.service"
        fi
    fi
done

# ── Enable expand on multi-user (NOT basic — must not gate networking) ─────
rm -f "$mnt/etc/systemd/system/basic.target.wants/sa02m-rootfs-expand.service"
ln -sfn /etc/systemd/system/sa02m-rootfs-expand.service \
    "$mnt/etc/systemd/system/multi-user.target.wants/sa02m-rootfs-expand.service"
# Mask armbian-resize (Conflicts + symlink) — prevents udev settle storm
ln -sfn /dev/null "$mnt/etc/systemd/system/armbian-resize-filesystem.service"
rm -f "$mnt/etc/systemd/system/basic.target.wants/armbian-resize-filesystem.service" \
      "$mnt/lib/systemd/system/basic.target.wants/armbian-resize-filesystem.service" 2>/dev/null || true

for u in net-watchdog sa02m-userspace-watchdog sa02m-failure-monitor sa02m-eth-coldboot; do
    ln -sfn "/etc/systemd/system/${u}.service" \
        "$mnt/etc/systemd/system/multi-user.target.wants/${u}.service"
done
# eth1-only coldboot superseded by sa02m-eth-coldboot
rm -f "$mnt/etc/systemd/system/multi-user.target.wants/sa02m-eth1-coldboot.service" 2>/dev/null || true

# Force first-boot expand + PHY path on next boot after flash
rm -f "$mnt/var/lib/sa02m-rootfs-expand.done"
touch "$mnt/root/.not_logged_in_yet" 2>/dev/null || true

# Wipe donor cloud enrollment so clones are not "already in cloud"
log "wipe cloud enrollment in image"
mkdir -p "$mnt/etc/sa02m-cloud"
chmod 750 "$mnt/etc/sa02m-cloud"
rm -f "$mnt/etc/sa02m-cloud/device_secret" \
      "$mnt/etc/sa02m-cloud/frpc.toml" \
      "$mnt/etc/sa02m-cloud"/frpc.toml.bak* \
      "$mnt/etc/sa02m-cloud/pair_request" \
      "$mnt/etc/sa02m-cloud/activation_token"
cat > "$mnt/etc/sa02m-cloud/agent.conf" <<'EOF'
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
chmod 640 "$mnt/etc/sa02m-cloud/agent.conf"

sync
log "OK: first-boot patch applied to $(basename "$IMG")"
# umount via trap
