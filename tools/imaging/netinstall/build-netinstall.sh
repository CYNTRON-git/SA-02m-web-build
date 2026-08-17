#!/bin/bash
# SA-02m — build netinstall initramfs + boot.scr for FEL/TFTP stand
#
# Primary path: assemble on a live Armbian donor (same libc/binaries as board)
# via SSH, then pull uInitrd-netinstall into stand-data/tftpboot/.
#
# Usage:
#   ./build-netinstall.sh [--ip 192.168.1.136] [--server-ip 192.168.1.10]
#                         [--http-port 8080] [--out-dir ../stand-data/tftpboot]
#
# Requires on host: ssh, scp (or run from WSL). On donor: cpio, gzip, mkimage
# (u-boot-tools) optional — host mkimage used if donor lacks it.
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGING_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOT_DIR="$IMAGING_DIR/boot"
KEY="${SA02M_SSH_KEY:-$HOME/.ssh/sa02m_sa02}"
DEVICE_IP="${DEVICE_IP:-192.168.1.136}"
SERVER_IP="${SERVER_IP:-192.168.1.10}"
HTTP_PORT="${HTTP_PORT:-8080}"
OUT_DIR="${OUT_DIR:-$IMAGING_DIR/stand-data/tftpboot}"
REMOTE_WORK="/tmp/sa02m-netinstall-build.$$"

usage() {
    cat <<'EOF'
Usage:
  build-netinstall.sh [--ip DONOR] [--server-ip STAND_IP] [--http-port N]
                      [--out-dir DIR] [--key PATH]

Builds:
  uInitrd-netinstall   (cpio+gzip uImage ramdisk)
  boot.scr             (from boot.cmd with SERVER_IP baked in)
Copies into OUT_DIR (default tools/imaging/stand-data/tftpboot/):
  zImage, sun8i-a40i-sk.dtb, uInitrd-netinstall, boot.scr
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)         DEVICE_IP="$2"; shift 2 ;;
        --server-ip)  SERVER_IP="$2"; shift 2 ;;
        --http-port)  HTTP_PORT="$2"; shift 2 ;;
        --out-dir)    OUT_DIR="$2"; shift 2 ;;
        --key)        KEY="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

[ -f "$SCRIPT_DIR/init" ] || { echo "ERROR: missing $SCRIPT_DIR/init" >&2; exit 1; }
[ -f "$SCRIPT_DIR/boot.cmd" ] || { echo "ERROR: missing $SCRIPT_DIR/boot.cmd" >&2; exit 1; }
[ -f "$BOOT_DIR/zImage" ] || {
    echo "ERROR: missing $BOOT_DIR/zImage — run: py -3 tools/imaging/boot/fetch-boot-artifacts.py" >&2
    exit 1
}
[ -f "$BOOT_DIR/sun8i-a40i-sk.dtb" ] || {
    echo "ERROR: missing DTB — run fetch-boot-artifacts.py" >&2
    exit 1
}

SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SCP=(scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
if [ -f "$KEY" ]; then
    SSH+=(-i "$KEY")
    SCP+=(-i "$KEY")
fi
TARGET="root@${DEVICE_IP}"

log() { echo "[build-netinstall] $*"; }

mkdir -p "$OUT_DIR"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/sa02m-ni-XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

# Bake server IP into boot.cmd → boot.scr
sed -e "s/__SERVERIP__/${SERVER_IP}/g" \
    -e "s/__HTTP_PORT__/${HTTP_PORT}/g" \
    "$SCRIPT_DIR/boot.cmd" > "$WORKDIR/boot.cmd"

if command -v mkimage >/dev/null 2>&1; then
    mkimage -C none -A arm -T script -d "$WORKDIR/boot.cmd" "$WORKDIR/boot.scr" >/dev/null
    log "boot.scr via local mkimage"
else
    log "local mkimage missing — will build boot.scr on donor"
fi

# Stage init + remote assemble script
cp -f "$SCRIPT_DIR/init" "$WORKDIR/init"
chmod 755 "$WORKDIR/init"

cat > "$WORKDIR/assemble.sh" <<'REMOTE'
#!/bin/bash
# Assemble netinstall root on Armbian donor (may lack busybox package).
set -euo pipefail
ROOT="$1"
INIT_SRC="$2"
OUT_CPIO="$3"
rm -rf "$ROOT"
mkdir -p "$ROOT"/{bin,sbin,usr/bin,usr/sbin,lib,usr/lib,lib/arm-linux-gnueabihf,usr/lib/arm-linux-gnueabihf,dev,proc,sys,tmp,run,var/run,etc,usr/share/udhcpc}
cp -a "$INIT_SRC" "$ROOT/init"
chmod 755 "$ROOT/init"

copy_bin() {
    local src="$1" dest="$2"
    [ -e "$src" ] || return 1
    mkdir -p "$(dirname "$dest")"
    cp -a "$src" "$dest"
    return 0
}

copy_deps() {
    local bin="$1"
    [ -f "$bin" ] || return 0
    local real libs lib dest
    real="$(readlink -f "$bin" 2>/dev/null || echo "$bin")"
    # Scripts have no ELF deps; ldd may exit 1 — never fail the build
    libs="$(ldd "$real" 2>/dev/null | awk '/=>/ {print $3} /^\// {print $1}' || true)"
    [ -n "$libs" ] || return 0
    while IFS= read -r lib; do
        [ -n "$lib" ] && [ -e "$lib" ] || continue
        dest="$ROOT$lib"
        mkdir -p "$(dirname "$dest")"
        [ -e "$dest" ] || cp -aL "$lib" "$dest"
    done <<EOF
$libs
EOF
}

# Optional: pull busybox-static if missing (needs apt network on donor)
if [ ! -x /bin/busybox ] && [ ! -x /usr/bin/busybox ] && [ ! -x /bin/busybox-static ]; then
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq busybox-static 2>/dev/null || true
    fi
fi

BB=""
for c in /bin/busybox /usr/bin/busybox /bin/busybox-static /usr/bin/busybox-static; do
    if [ -x "$c" ]; then BB="$c"; break; fi
done

if [ -n "$BB" ]; then
    cp -a "$BB" "$ROOT/bin/busybox"
    "$ROOT/bin/busybox" --install -s "$ROOT/bin" 2>/dev/null || true
    for applet in sh ash mount umount mkdir ls cat echo sleep reboot wget sha256sum \
                  ip ifconfig route udhcpc gzip gunzip dd sync date setsid; do
        [ -e "$ROOT/bin/$applet" ] || ln -sf busybox "$ROOT/bin/$applet"
    done
fi

# Always overlay real tools (xz/wget/ip/dd) — more reliable than busybox stubs
copy_bin /usr/bin/xz "$ROOT/usr/bin/xz" || copy_bin /bin/xz "$ROOT/bin/xz" || {
    echo "FATAL: xz not found on donor" >&2
    exit 1
}
copy_bin /usr/bin/wget "$ROOT/usr/bin/wget" || true
copy_bin /usr/bin/sha256sum "$ROOT/usr/bin/sha256sum" || true
copy_bin /bin/ip "$ROOT/bin/ip" || copy_bin /sbin/ip "$ROOT/sbin/ip" || true
copy_bin /bin/dd "$ROOT/bin/dd" || true
copy_bin /bin/mkdir "$ROOT/bin/mkdir" || true
copy_bin /bin/sleep "$ROOT/bin/sleep" || true
copy_bin /bin/mount "$ROOT/bin/mount" || true
copy_bin /bin/umount "$ROOT/bin/umount" || true
copy_bin /usr/bin/env "$ROOT/usr/bin/env" || true
copy_bin /bin/dash "$ROOT/bin/dash" || true
copy_bin /bin/bash "$ROOT/bin/bash" || true
# Helpers used by init (when busybox applets absent)
for pair in \
    "/usr/bin/tee:$ROOT/usr/bin/tee" \
    "/bin/cat:$ROOT/bin/cat" \
    "/bin/sync:$ROOT/bin/sync" \
    "/usr/bin/awk:$ROOT/usr/bin/awk" \
    "/usr/bin/mawk:$ROOT/usr/bin/mawk" \
    "/bin/sed:$ROOT/bin/sed" \
    "/usr/bin/tr:$ROOT/usr/bin/tr" \
    "/usr/bin/head:$ROOT/usr/bin/head" \
    "/bin/date:$ROOT/bin/date" \
    "/usr/bin/setsid:$ROOT/usr/bin/setsid"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    copy_bin "$src" "$dst" || true
done
if [ ! -e "$ROOT/bin/sh" ]; then
    if [ -x "$ROOT/bin/dash" ]; then ln -sf dash "$ROOT/bin/sh"
    elif [ -x "$ROOT/bin/busybox" ]; then ln -sf busybox "$ROOT/bin/sh"
    elif [ -x "$ROOT/bin/bash" ]; then ln -sf bash "$ROOT/bin/sh"
    else echo "FATAL: no shell for initramfs" >&2; exit 1
    fi
fi
# awk symlink if only mawk
if [ ! -e "$ROOT/usr/bin/awk" ] && [ -e "$ROOT/usr/bin/mawk" ]; then
    ln -sf mawk "$ROOT/usr/bin/awk"
fi

# DHCP: prefer udhcpc (busybox), else dhclient (+ minimal conf)
if [ -x /sbin/udhcpc ] || [ -x /usr/sbin/udhcpc ]; then
    copy_bin /sbin/udhcpc "$ROOT/sbin/udhcpc" || copy_bin /usr/sbin/udhcpc "$ROOT/usr/sbin/udhcpc" || true
elif [ -x /sbin/dhclient ] || [ -x /usr/sbin/dhclient ]; then
    copy_bin /sbin/dhclient "$ROOT/sbin/dhclient" || copy_bin /usr/sbin/dhclient "$ROOT/usr/sbin/dhclient"
    mkdir -p "$ROOT/var/lib/dhcp" "$ROOT/etc/dhcp"
    printf '%s\n' 'request subnet-mask, routers, domain-name-servers;' \
        > "$ROOT/etc/dhcp/dhclient.conf"
fi

# Standalone reboot helper (systemctl symlink is useless in initramfs)
cat > "$ROOT/sbin/reboot" <<'RBT'
#!/bin/sh
sync
echo b > /proc/sysrq-trigger 2>/dev/null || true
sleep 2
exit 0
RBT
chmod 755 "$ROOT/sbin/reboot"
ln -sf ../sbin/reboot "$ROOT/bin/reboot" 2>/dev/null || true

# udhcpc default.script (used if busybox udhcpc present)
if [ -f /usr/share/udhcpc/default.script ]; then
    cp -a /usr/share/udhcpc/default.script "$ROOT/usr/share/udhcpc/default.script"
else
    cat > "$ROOT/usr/share/udhcpc/default.script" <<'UDS'
#!/bin/sh
case "$1" in
  deconfig) ip addr flush dev "$interface" 2>/dev/null || true ;;
  renew|bound)
    ip addr flush dev "$interface" 2>/dev/null || true
    ip addr add "$ip"/"$mask" dev "$interface"
    ip link set "$interface" up
    [ -n "$router" ] && ip route replace default via "$router" dev "$interface"
    ;;
esac
exit 0
UDS
    chmod 755 "$ROOT/usr/share/udhcpc/default.script"
fi

# Collect libs for every real binary we placed
while IFS= read -r b; do
    copy_deps "$b" || true
done < <(find "$ROOT" -type f -executable 2>/dev/null)
for ld in /lib/ld-linux.so.3 /lib/ld-linux-armhf.so.3 \
          /lib/arm-linux-gnueabihf/ld-linux-armhf.so.3 \
          /lib/arm-linux-gnueabihf/ld-*.so*; do
    [ -e "$ld" ] || continue
    dest="$ROOT$ld"
    mkdir -p "$(dirname "$dest")"
    [ -e "$dest" ] || cp -aL "$ld" "$dest"
done

# NSS / resolv bits for glibc getaddrinfo (wget by IP still works without)
copy_bin /lib/arm-linux-gnueabihf/libnss_dns.so.2 \
    "$ROOT/lib/arm-linux-gnueabihf/libnss_dns.so.2" 2>/dev/null || true
copy_bin /lib/arm-linux-gnueabihf/libnss_files.so.2 \
    "$ROOT/lib/arm-linux-gnueabihf/libnss_files.so.2" 2>/dev/null || true
copy_bin /etc/resolv.conf "$ROOT/etc/resolv.conf" 2>/dev/null || \
    echo "nameserver 8.8.8.8" > "$ROOT/etc/resolv.conf"

mknod -m 666 "$ROOT/dev/null" c 1 3 2>/dev/null || true
mknod -m 666 "$ROOT/dev/console" c 5 1 2>/dev/null || true
mknod -m 666 "$ROOT/dev/tty" c 5 0 2>/dev/null || true
mknod -m 666 "$ROOT/dev/ttyS0" c 4 64 2>/dev/null || true

( cd "$ROOT" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$OUT_CPIO" )
ls -la "$OUT_CPIO"
# Size sanity: keep under ~64 MiB compressed for TFTP/RAM
sz=$(wc -c < "$OUT_CPIO")
if [ "$sz" -gt 67108864 ]; then
    echo "WARN: initramfs $sz bytes > 64MiB" >&2
fi
REMOTE
chmod 755 "$WORKDIR/assemble.sh"

log "SSH assemble on $DEVICE_IP …"
"${SSH[@]}" "$TARGET" "mkdir -p $REMOTE_WORK && rm -rf $REMOTE_WORK/*"
"${SCP[@]}" "$WORKDIR/init" "$WORKDIR/assemble.sh" "$TARGET:$REMOTE_WORK/"
if [ ! -f "$WORKDIR/boot.scr" ]; then
    "${SCP[@]}" "$WORKDIR/boot.cmd" "$TARGET:$REMOTE_WORK/"
fi

"${SSH[@]}" "$TARGET" "bash $REMOTE_WORK/assemble.sh $REMOTE_WORK/root $REMOTE_WORK/init $REMOTE_WORK/initramfs.cpio.gz"

if [ ! -f "$WORKDIR/boot.scr" ]; then
    "${SSH[@]}" "$TARGET" \
        "mkimage -C none -A arm -T script -d $REMOTE_WORK/boot.cmd $REMOTE_WORK/boot.scr"
    "${SCP[@]}" "$TARGET:$REMOTE_WORK/boot.scr" "$WORKDIR/boot.scr"
fi

"${SCP[@]}" "$TARGET:$REMOTE_WORK/initramfs.cpio.gz" "$WORKDIR/initramfs.cpio.gz"

# Wrap as u-boot ramdisk image when possible
if command -v mkimage >/dev/null 2>&1; then
    mkimage -A arm -T ramdisk -C none \
        -d "$WORKDIR/initramfs.cpio.gz" "$OUT_DIR/uInitrd-netinstall" >/dev/null
elif "${SSH[@]}" "$TARGET" "command -v mkimage >/dev/null"; then
    "${SSH[@]}" "$TARGET" \
        "mkimage -A arm -T ramdisk -C none -d $REMOTE_WORK/initramfs.cpio.gz $REMOTE_WORK/uInitrd-netinstall"
    "${SCP[@]}" "$TARGET:$REMOTE_WORK/uInitrd-netinstall" "$OUT_DIR/uInitrd-netinstall"
else
    # Raw gzip cpio — some U-Boot builds accept it as ramdisk
    cp -f "$WORKDIR/initramfs.cpio.gz" "$OUT_DIR/uInitrd-netinstall"
    log "WARN: no mkimage — installed raw gzip cpio as uInitrd-netinstall"
fi

"${SSH[@]}" "$TARGET" "rm -rf $REMOTE_WORK" || true

cp -f "$WORKDIR/boot.scr" "$OUT_DIR/boot.scr"
cp -f "$BOOT_DIR/zImage" "$OUT_DIR/zImage"
cp -f "$BOOT_DIR/sun8i-a40i-sk.dtb" "$OUT_DIR/sun8i-a40i-sk.dtb"
# Also keep fel uboot next to tftp tree for agent convenience
if [ -f "$BOOT_DIR/u-boot-sunxi-with-spl.bin" ]; then
    cp -f "$BOOT_DIR/u-boot-sunxi-with-spl.bin" "$OUT_DIR/u-boot-sunxi-with-spl.bin"
fi

# FEL-passed script (same commands; used by fel-agent via sunxi-fel uboot … script)
cp -f "$WORKDIR/boot.cmd" "$OUT_DIR/fel-boot.cmd"
if command -v mkimage >/dev/null 2>&1; then
    mkimage -C none -A arm -T script -d "$OUT_DIR/fel-boot.cmd" "$OUT_DIR/fel-boot.scr" >/dev/null
else
    cp -f "$OUT_DIR/boot.scr" "$OUT_DIR/fel-boot.scr"
fi

log "READY: $OUT_DIR"
ls -lah "$OUT_DIR"/zImage "$OUT_DIR"/sun8i-a40i-sk.dtb \
        "$OUT_DIR"/uInitrd-netinstall "$OUT_DIR"/boot.scr "$OUT_DIR"/fel-boot.scr
