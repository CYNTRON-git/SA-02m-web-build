#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# patch-firstboot-image.sh — offline patch of a PiShrink SA-02m .img
#
# Applies first-boot network/watchdog/resize fixes so the next flash works
# without Ethernet cable re-plug and with watchdogs restored after expand.
#
# Usage (WSL/Linux) — through `bash`, since this file is tracked mode 644:
#   sudo bash tools/imaging/patch-firstboot-image.sh \
#       [--bootcmd /path/to/boot.cmd] /path/to/SA-02m-….img
#
# Payload selector: --bootcmd <path> overrides the hardware-verified default.
# It is an ARGUMENT, not an environment variable, because that is the only form
# that survives `sudo` on every host (sudoers `setenv` is off by default, so
# `sudo VAR=value cmd` is refused). SA02M_BOOTCMD still works for a DIRECT,
# non-sudo call and is a convenience only — see
# docs/contracts/uboot-boot-script.md §3.
#
# Idempotent. Does NOT recompress xz (caller may xz afterwards).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

die() { echo "FATAL: $*" >&2; exit 1; }
log() { echo "[patch-firstboot] $*"; }

IMG=""
BOOTCMD_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bootcmd)
            [ $# -ge 2 ] || die "--bootcmd needs a path"
            BOOTCMD_ARG="$2"; shift 2 ;;
        --bootcmd=*)
            BOOTCMD_ARG="${1#*=}"; shift ;;
        --) shift ;;
        -*) die "unknown option: $1" ;;
        *)
            if [ -n "$IMG" ]; then die "unexpected argument: $1"; fi
            IMG="$1"; shift ;;
    esac
done

[ -n "$IMG" ] || die "usage: $0 [--bootcmd PATH] /path/to/image.img"
[ -f "$IMG" ] || die "image not found: $IMG"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

ETC="$REPO_ROOT/etc"
SBIN_SRC="$REPO_ROOT/usr/local/sbin"

# The boot.scr byte format has ONE home — tools/imaging/uboot-script.py; both
# the mkimage-free writer and the validator live there, so this script and
# tools/debian-rootfs/pack-sa02m-image.sh cannot drift apart.
UBOOT_PY="$SCRIPT_DIR/uboot-script.py"

build_bootscr() {
    local out=$1
    local cmd_src default_src norm

    [ -r "$UBOOT_PY" ] || die "missing $UBOOT_PY"
    cmd_src="$(python3 "$UBOOT_PY" resolve --repo-root "$REPO_ROOT" --payload "${BOOTCMD_ARG:-}")" \
        || die "cannot resolve the boot.cmd payload"
    [ -f "$cmd_src" ] || die "missing boot.cmd payload: $cmd_src"
    default_src="$(python3 "$UBOOT_PY" default --repo-root "$REPO_ROOT")" \
        || die "cannot resolve the default boot.cmd payload"
    # Always name the payload actually used — the one line that answers
    # "which script is in this image?" without unpacking it.
    if [ "$cmd_src" = "$default_src" ]; then
        log "boot.cmd payload: $cmd_src (default, hardware-verified)"
    else
        log "WARN: NON-DEFAULT boot.cmd payload in use: $cmd_src"
    fi

    if command -v mkimage >/dev/null 2>&1; then
        norm="$(mktemp /tmp/sa02m-boot.cmd.XXXXXX)"
        python3 "$UBOOT_PY" normalize "$cmd_src" "$norm" \
            || { rm -f "$norm"; die "boot.cmd normalization failed: $cmd_src"; }
        mkimage -C none -A arm -T script -d "$norm" -n "SA-02m" "$out" >/dev/null \
            || { rm -f "$norm"; die "mkimage failed on $norm"; }
        rm -f "$norm"
    else
        log "mkimage absent — using the in-tree reference writer (uboot-script.py)"
        python3 "$UBOOT_PY" build "$cmd_src" "$out" --name "SA-02m" \
            || die "boot.scr build failed from $cmd_src"
    fi

    # Format-level, so it covers mkimage output too: a wrong flag or a future
    # mkimage change is caught exactly like a writer bug. The string grep this
    # replaces is what let two malformed boot.scr artifacts ship.
    python3 "$UBOOT_PY" validate "$out" \
        || die "generated boot.scr failed format validation: $out"
}

mnt=""
boot_mnt=""
loop=$(losetup --partscan -f --show "$IMG") || die "losetup failed"
cleanup() {
    [ -n "$boot_mnt" ] && umount "$boot_mnt" 2>/dev/null || true
    [ -n "$mnt" ] && umount "$mnt" 2>/dev/null || true
    losetup -d "$loop" 2>/dev/null || true
    [ -n "$boot_mnt" ] && rmdir "$boot_mnt" 2>/dev/null || true
    [ -n "$mnt" ] && rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT

mnt=$(mktemp -d /tmp/sa02m-firstboot-XXXXXX)
boot_mnt=$(mktemp -d /tmp/sa02m-boot-XXXXXX)
rootpart="${loop}p2"
bootpart="${loop}p1"
for _ in 1 2 3 4 5 6 7 8; do
    [ -b "$rootpart" ] && break
    # older losetup: /dev/mapper/loopXp2 via kpartx
    sleep 1
done
if [ ! -b "$rootpart" ]; then
    kpartx -av "$loop" >/dev/null 2>&1 || true
    base=$(basename "$loop")
    bootpart="/dev/mapper/${base}p1"
    rootpart="/dev/mapper/${base}p2"
fi
[ -b "$rootpart" ] || die "no p2 on $loop"
mount "$rootpart" "$mnt" || die "mount $rootpart failed"
log "mounted $rootpart -> $mnt"

[ -b "$bootpart" ] || die "no p1 on $loop"
if command -v fsck.vfat >/dev/null 2>&1; then
    fsck.vfat -a "$bootpart" >/dev/null 2>&1 || true
fi
mount -o rw "$bootpart" "$boot_mnt" || die "mount $bootpart failed"
log "mounted $bootpart -> $boot_mnt"

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

# ── FAT boot script: working boards require threadirqs for i2c-2/PCA9536 ────
# The default payload has no DTB fallback chain — it loads exactly zImage +
# sun8i-a40i-sk.dtb. Writing it onto a FAT that lacks either name produces a
# board that reaches U-Boot and stops there (docs/contracts/uboot-boot-script.md).
for _f in zImage sun8i-a40i-sk.dtb; do
    [ -f "$boot_mnt/$_f" ] || die "FAT p1 has no $_f — the boot script requires it by exact name"
done
tmp_bootscr=$(mktemp /tmp/sa02m-boot.scr.XXXXXX)
build_bootscr "$tmp_bootscr"
install -m 644 "$tmp_bootscr" "$boot_mnt/boot.scr"
install -d "$mnt/usr/local/share/sa02m"
install -m 644 "$tmp_bootscr" "$mnt/usr/local/share/sa02m/boot.scr"
rm -f "$tmp_bootscr"
log "boot.scr: installed canonical bootargs with threadirqs"

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
