#!/usr/bin/env bash
#
# pack-sa02m-image.sh — rootfs Debian bullseye → raw eMMC .img → PiShrink → .img.xz
#
# Usage:
#   sudo ./tools/debian-rootfs/pack-sa02m-image.sh \
#       --rootfs ~/build/sa02m-bullseye-rootfs \
#       --out-dir ./out --name sa02m-bullseye-v1.0.3.37 --profile sa02m-1eth
#
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

ROOTFS=""
OUT_DIR="${OUT_DIR:-$REPO_ROOT/tools/imaging/out}"
OUTPUT_NAME=""
PROFILE="sa02m-1eth"
VERSION=""
XZ_LEVEL="9e"
DO_PISHRINK=1
DO_PATCH_WATCHDOG=1
EMMC_BYTES=7818182656
BOOT_MIB=64
UBOOT_BIN="${UBOOT_BIN:-$REPO_ROOT/tools/imaging/boot/u-boot-sunxi-with-spl.bin}"
UBOOT_SEEK_KB="${UBOOT_SEEK_KB:-8}"

usage() {
	sed -n '2,12p' "$0" | sed 's/^# \?//'
	exit 2
}

log() { printf '[pack-sa02m-image] %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
	case "$1" in
		--rootfs) ROOTFS="$2"; shift 2 ;;
		--out-dir) OUT_DIR="$2"; shift 2 ;;
		--name) OUTPUT_NAME="$2"; shift 2 ;;
		--profile) PROFILE="$2"; shift 2 ;;
		--version) VERSION="$2"; shift 2 ;;
		--xz-level) XZ_LEVEL="$2"; shift 2 ;;
		--skip-pishrink) DO_PISHRINK=0; shift ;;
		--no-patch-watchdog) DO_PATCH_WATCHDOG=0; shift ;;
		--uboot) UBOOT_BIN="$2"; shift 2 ;;
		--no-uboot) UBOOT_BIN=""; shift ;;
		-h|--help) usage ;;
		*) die "unknown arg: $1" ;;
	esac
done

[ -n "$ROOTFS" ] || die "--rootfs required"
[ -d "$ROOTFS/etc" ] || die "not a rootfs: $ROOTFS"
[ "$EUID" -eq 0 ] || die "run as root"

if [ -z "$OUTPUT_NAME" ]; then
	OUTPUT_NAME="sa02m-${PROFILE}-bullseye"
	[ -n "$VERSION" ] && OUTPUT_NAME="${OUTPUT_NAME}-v${VERSION}"
fi

mkdir -p "$OUT_DIR"
WORK="$(mktemp -d /tmp/pack-sa02m-XXXXXX)"
cleanup() {
	umount "$WORK/root" 2>/dev/null || true
	umount "$WORK/boot" 2>/dev/null || true
	[ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null || true
	rm -rf "$WORK"
}
trap cleanup EXIT

for bin in parted losetup mkfs.vfat mkfs.ext4 rsync xz sha256sum python3; do
	command -v "$bin" >/dev/null || die "missing $bin"
done
[ "$DO_PISHRINK" -eq 1 ] && command -v pishrink.sh >/dev/null || die "missing pishrink.sh"
command -v mkimage >/dev/null || die "missing mkimage (apt install u-boot-tools)"
# fdtput/fdtget (из пакета device-tree-compiler) — используется ниже для
# страховочного снятия chosen/stdout-path из DTB, чтобы kernel не активировал
# earlycon на UART0 (RS-485-0, физическая линия Modbus RTU на SA-02m).
command -v fdtput >/dev/null || die "missing fdtput (apt install device-tree-compiler)"
command -v fdtget >/dev/null || die "missing fdtget (apt install device-tree-compiler)"

KVER="$(ls "$ROOTFS/lib/modules" 2>/dev/null | grep -E 'sa02m' | sort -V | tail -1 || true)"
[ -n "$KVER" ] || die "no sa02m kernel in $ROOTFS/lib/modules"

VMLINUZ="$ROOTFS/boot/vmlinuz-${KVER}"
DTB="$ROOTFS/usr/lib/linux-image-${KVER}/sun8i-r40-sa02m.dtb"
[ -f "$VMLINUZ" ] || VMLINUZ="$ROOTFS/boot/vmlinuz-${KVER}+"
[ -f "$VMLINUZ" ] || die "vmlinuz not found for $KVER"
[ -f "$DTB" ] || die "DTB not found: $DTB"

RAW_IMG="$WORK/${OUTPUT_NAME}.img"
log "rootfs  = $ROOTFS"
log "kernel  = $KVER"
log "output  = $OUT_DIR/${OUTPUT_NAME}-shrunk.img.xz"

log "create sparse image $(numfmt --to=iec --suffix=B "$EMMC_BYTES")"
truncate -s "$EMMC_BYTES" "$RAW_IMG"
parted -s "$RAW_IMG" mklabel msdos
parted -s "$RAW_IMG" mkpart primary fat32 1MiB "${BOOT_MIB}MiB"
parted -s "$RAW_IMG" mkpart primary ext4 "${BOOT_MIB}MiB" 100%
parted -s "$RAW_IMG" set 1 boot on

# ── U-Boot (SPL + proper) в первые ~1 MiB eMMC ─────────────────────────────
# Без этого блока `dd` образа поверх eMMC затирает загрузчик Armbian/Starterkit
# и устройство больше не грузится (нет kernel → нет сети). U-Boot пишется
# ДО partition data (offset 8 KiB для sun8i-r40 / Allwinner A40i).
if [ -n "$UBOOT_BIN" ]; then
	[ -f "$UBOOT_BIN" ] || die "U-Boot binary not found: $UBOOT_BIN (передайте --uboot PATH или --no-uboot для отказа)"
	UBOOT_SZ="$(stat -c%s "$UBOOT_BIN")"
	MAX_UBOOT_SZ=$(( (BOOT_MIB - 1) * 1024 * 1024 ))
	[ "$UBOOT_SZ" -le "$MAX_UBOOT_SZ" ] || die "U-Boot $UBOOT_SZ B > доступного места до FAT (${MAX_UBOOT_SZ} B)"
	log "embed U-Boot ($(numfmt --to=iec --suffix=B "$UBOOT_SZ")) at ${UBOOT_SEEK_KB} KiB from $UBOOT_BIN"
	dd if="$UBOOT_BIN" of="$RAW_IMG" bs=1024 seek="$UBOOT_SEEK_KB" conv=notrunc status=none
else
	log "WARN: --no-uboot: устройство будет полагаться на существующий U-Boot в eMMC (dd образа его затрёт при флеше!)"
fi

LOOP="$(losetup -f --show -P "$RAW_IMG")"
log "loop $LOOP"
mkfs.vfat -F 16 -n BOOT "${LOOP}p1"
mkfs.ext4 -F -L sa02m_root "${LOOP}p2"

mkdir -p "$WORK/root" "$WORK/boot"
mount "${LOOP}p2" "$WORK/root"
mount "${LOOP}p1" "$WORK/boot"

log "rsync rootfs → p2"
RSYNC_EXCLUDES=(
	--exclude=/dev/\*
	--exclude=/proc/\*
	--exclude=/sys/\*
	--exclude=/tmp/\*
	--exclude=/run/\*
	--exclude=/mnt/\*
	--exclude=/media/\*
	--exclude='*.bin'
	--exclude='*.img'
	--exclude='*.img.xz'
	--exclude='opt/sa02m-web-build/tools/imaging/out/*'
	--exclude='opt/sa02m-web-build/.git/*'
)
rsync -aH "${RSYNC_EXCLUDES[@]}" "$ROOTFS"/ "$WORK/root"/

# installer no longer needed on target — saves ~500MB+
if [ -d "$WORK/root/opt/sa02m-web-build" ]; then
	log "prune /opt/sa02m-web-build (installed to system paths)"
	rm -rf "$WORK/root/opt/sa02m-web-build/tools/imaging/out" \
		"$WORK/root/opt/sa02m-web-build/tools/buildroot" \
		"$WORK/root/opt/sa02m-web-build/.git" 2>/dev/null || true
fi

log "FAT boot: zImage + DTB + boot.scr"
cp -f "$VMLINUZ" "$WORK/boot/zImage"
cp -f "$VMLINUZ" "$WORK/boot/zImage.smp"

# ── DTB патч: удалить chosen/stdout-path ────────────────────────────────────
# На SA-02m UART0 = /dev/ttyS0 = физическая RS-485-0 (COM1), к которой
# подключаются Modbus RTU-slave устройства. Если в DTB присутствует
# chosen/stdout-path = "serial0:...", то:
#   1) linux earlycon (при CONFIG_SERIAL_EARLYCON) активирует ранний вывод на
#      UART0 до того как console=tty1 из bootargs вступит в силу — мусор в шине;
#   2) U-Boot proper, читающий DTB, тоже использует stdout-path для выбора
#      собственной консоли.
# Убираем свойство целиком; ttyS0 остаётся доступным для пользовательского
# Modbus (мы не трогаем aliases/serial0). Патч идемпотентный — если свойства
# уже нет (kernel собран из уже-исправленной .dts), fdtput не упадёт.
# См. docs/bugs/BUGLOG.md — запись "no serial debug on any ttyS during boot".
DTB_PATCHED="$WORK/${OUTPUT_NAME}-patched.dtb"
cp -f "$DTB" "$DTB_PATCHED"
if fdtget -l "$DTB_PATCHED" /chosen 2>/dev/null | grep -qw stdout-path; then
	log "DTB: удаляю /chosen/stdout-path (silence UART0 during boot)"
	fdtput -d "$DTB_PATCHED" /chosen stdout-path
	if fdtget -l "$DTB_PATCHED" /chosen 2>/dev/null | grep -qw stdout-path; then
		die "DTB patch failed: /chosen/stdout-path all еще присутствует в $DTB_PATCHED"
	fi
else
	log "DTB: /chosen/stdout-path отсутствует (kernel .dts уже пропатчен)"
fi
DTB="$DTB_PATCHED"

# DTB под primary именем + fallback именами (boot.cmd.sa02m перебирает их)
cp -f "$DTB" "$WORK/boot/sun8i-r40-sa02m.dtb"
cp -f "$DTB" "$WORK/boot/sun8i-a40i-sk.dtb"
cp -f "$DTB" "$WORK/boot/sun8i-a40i-nano2e-none-sk.dtb"
install -d -m 755 "$WORK/root/usr/local/share/sa02m/kernel"
cp -f "$VMLINUZ" "$WORK/root/usr/local/share/sa02m/kernel/zImage.smp"
cp -f "$DTB" "$WORK/root/usr/local/share/sa02m/kernel/sun8i-r40-sa02m.dtb"
cp -f "$DTB" "$WORK/root/usr/local/share/sa02m/kernel/sun8i-a40i-sk.dtb"

BOOT_CMD="$REPO_ROOT/etc/boot.cmd.sa02m"
[ -f "$BOOT_CMD" ] || die "missing $BOOT_CMD"
sed 's/\r$//' "$BOOT_CMD" > "$WORK/boot.cmd"
mkimage -C none -A arm -T script -d "$WORK/boot.cmd" "$WORK/boot/boot.scr" >/dev/null
cp -f "$WORK/boot/boot.scr" "$WORK/root/usr/local/share/sa02m/boot.scr" 2>/dev/null || \
	install -d -m 755 "$WORK/root/usr/local/share/sa02m" && \
	cp -f "$WORK/boot/boot.scr" "$WORK/root/usr/local/share/sa02m/boot.scr"

log "first-boot flags"
rm -f "$WORK/root/var/lib/sa02m-rootfs-expand.done"
echo "uninitialized" > "$WORK/root/etc/machine-id"
rm -f "$WORK/root/var/lib/dbus/machine-id"

# ── Форсированно пересобираем fstab (страховка для старых rootfs) ─────────
# LABEL=… устойчиво к сменам /dev/mmcblkX, nofail на FAT — не роняет
# local-fs.target → networking стартует даже если FAT недоступен.
log "rewrite /etc/fstab (LABEL= + nofail для boot_fat)"
cat > "$WORK/root/etc/fstab" <<'FSTAB'
# SA-02m eMMC layout (Starterkit / Cyntron)
LABEL=sa02m_root  /              ext4  defaults,noatime,errors=remount-ro                              0 1
LABEL=BOOT        /mnt/boot_fat  vfat  defaults,nofail,x-systemd.device-timeout=5s,x-systemd.automount 0 0
FSTAB
install -d -m 755 "$WORK/root/mnt/boot_fat"

# ── Удаляем нелегитимный kernel 6.1.0-*-rt-armmp (остаток случайного apt install) ─
for stale in "$WORK/root/lib/modules"/6.1.0-*-rt-armmp \
             "$WORK/root/boot"/vmlinuz-6.1.0-*-rt-armmp \
             "$WORK/root/boot"/initrd.img-6.1.0-*-rt-armmp \
             "$WORK/root/boot"/System.map-6.1.0-*-rt-armmp \
             "$WORK/root/boot"/config-6.1.0-*-rt-armmp; do
	if [ -e "$stale" ]; then
		log "prune stale: $stale"
		rm -rf "$stale"
	fi
done

if [ "$DO_PATCH_WATCHDOG" -eq 1 ]; then
	log "patch watchdog units (safe first boot after flash)"
	for f in sa02m-userspace-watchdog.service net-watchdog.service sa02m-failure-monitor.service; do
		[ -f "$REPO_ROOT/etc/systemd/$f" ] && \
			cp -f "$REPO_ROOT/etc/systemd/$f" "$WORK/root/etc/systemd/system/$f" || true
		[ -f "$REPO_ROOT/etc/$f" ] && \
			cp -f "$REPO_ROOT/etc/$f" "$WORK/root/etc/systemd/system/$f" || true
	done
	[ -f "$REPO_ROOT/etc/systemd/sa02m-watchdog.conf" ] && \
		install -d -m 755 "$WORK/root/etc/systemd/system.conf.d" && \
		cp -f "$REPO_ROOT/etc/systemd/sa02m-watchdog.conf" \
			"$WORK/root/etc/systemd/system.conf.d/sa02m-watchdog.conf"
	sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' \
		"$WORK/root/etc/systemd/system.conf" 2>/dev/null || true
fi

sync
umount "$WORK/boot"
umount "$WORK/root"
losetup -d "$LOOP"
LOOP=""
trap - EXIT

if [ "$DO_PISHRINK" -eq 1 ]; then
	log "PiShrink"
	pishrink.sh -a -v "$RAW_IMG"
fi

SHRUNK_XZ="$OUT_DIR/${OUTPUT_NAME}-shrunk.img.xz"
SHA_FILE="${SHRUNK_XZ}.sha256"
MANIFEST="${SHRUNK_XZ%.img.xz}.manifest.json"

log "xz -${XZ_LEVEL}"
xz -T0 "-${XZ_LEVEL}" -c "$RAW_IMG" > "$SHRUNK_XZ"
( cd "$OUT_DIR" && sha256sum "$(basename "$SHRUNK_XZ")" > "$(basename "$SHA_FILE")" )

SHA="$(awk '{print $1}' "$SHA_FILE")"
python3 - "$SHRUNK_XZ" "$SHA" "$MANIFEST" "$KVER" "$PROFILE" "$VERSION" <<'PY'
import json, sys, datetime, os
img, sha, out, kver, profile, version = sys.argv[1:7]
doc = {
    "image_name": os.path.basename(img),
    "image_sha256": sha,
    "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pipeline": {"tool": "pack-sa02m-image.sh", "pishrink": True, "base": "debian-bullseye-rootfs"},
    "platform": {"os": "Debian 11 bullseye armhf", "kernel": kver, "board": "Cyntron SA-02m"},
    "serial_profile": profile,
    "partitions": {
        "boot": {"device": "mmcblk2p1", "size_mib": 64, "fstype": "vfat"},
        "root": {"device": "mmcblk2p2", "fstype": "ext4", "label": "sa02m_root"},
    },
}
if version:
    doc["release_version"] = version
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY

log "READY"
echo "  $SHRUNK_XZ ($(numfmt --to=iec --suffix=B "$(stat -c%s "$SHRUNK_XZ")"))"
echo "  $SHA_FILE"
echo "  $MANIFEST"
echo ""
echo "USB flash-receiver:"
echo "  $REPO_ROOT/tools/debian-rootfs/prepare-sa02m-flash-usb.sh --image $SHRUNK_XZ [--dest /path/to/usb]"
