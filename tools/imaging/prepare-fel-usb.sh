#!/bin/bash
# SA-02m — optional FEL/recovery USB (minimal boot, NOT full image)
#
# USB holds only netboot helpers / network flash-receiver autorun that pulls
# current.img.xz from the stand HTTP server. Full image always via Ethernet.
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TFTP_DEFAULT="$SCRIPT_DIR/stand-data/tftpboot"
STAND_ENV="${STAND_ENV:-$SCRIPT_DIR/stand/stand.env}"

usage() {
    cat <<'EOF'
Usage:
  prepare-fel-usb.sh --dest DIR [--server-ip IP] [--tftp-dir DIR]

Writes to DEST (USB mount point):
  boot.scr / fel-boot.scr   U-Boot netboot script
  zImage, sun8i-a40i-sk.dtb, uInitrd-netinstall   (if present in tftp dir)
  autorun.sh                network flash-receiver (HTTP→xz→dd)
  README-FEL-USB.txt

Does NOT copy current.img.xz (must stay on stand HTTP).

Example:
  ./prepare-fel-usb.sh --dest /mnt/c/USB/SA02m-FEL --server-ip 192.168.1.10
EOF
}

DEST=""
SERVER_IP=""
TFTP_DIR="$TFTP_DEFAULT"
HTTP_PORT=8080

if [ -f "$STAND_ENV" ]; then
    # shellcheck disable=SC1090
    set -a; . "$STAND_ENV"; set +a
    SERVER_IP="${SERVER_IP:-${STAND_IP:-}}"
    HTTP_PORT="${HTTP_PORT:-8080}"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --dest)       DEST="$2"; shift 2 ;;
        --server-ip)  SERVER_IP="$2"; shift 2 ;;
        --tftp-dir)   TFTP_DIR="$2"; shift 2 ;;
        --http-port)  HTTP_PORT="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$DEST" ] || { echo "ERROR: --dest required" >&2; usage; exit 2; }
[ -n "$SERVER_IP" ] || { echo "ERROR: --server-ip or STAND_IP in stand.env required" >&2; exit 2; }
mkdir -p "$DEST"

# Copy netboot bits if built
for f in boot.scr fel-boot.scr zImage sun8i-a40i-sk.dtb uInitrd-netinstall; do
    if [ -f "$TFTP_DIR/$f" ]; then
        cp -f "$TFTP_DIR/$f" "$DEST/$f"
        echo "  + $f"
    fi
done

# Network autorun: same idea as flash-receiver but image from HTTP
cat > "$DEST/autorun.sh" <<EOF
#!/bin/sh
# SA-02m FEL-USB network autorun — image from stand HTTP, write eMMC
set -eu
LOG=/var/log/flash-receiver.log
TARGET_DEV=/dev/mmcblk2
IMAGE_URL="http://${SERVER_IP}:${HTTP_PORT}/current.img.xz"
IMG=/tmp/current.img.xz
SHA=/tmp/current.img.xz.sha256

ts() { date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 0; }
log() { echo "[\$(ts)] \$*" | tee -a "\$LOG" >&2; }
die() { log "FATAL: \$*"; exit 1; }

mkdir -p "\$(dirname "\$LOG")" 2>/dev/null || true
log "─── FEL-USB network autorun ───"
log "URL=\$IMAGE_URL"

rmmod -f g_mass_storage 2>/dev/null || true
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog; do
    systemctl stop "\$svc" 2>/dev/null || true
    systemctl mask "\$svc" 2>/dev/null || true
done

# DHCP if needed
ip link set eth0 up 2>/dev/null || true
udhcpc -i eth0 -n -q 2>/dev/null || dhclient eth0 2>/dev/null || true

wget -O "\$IMG" "\$IMAGE_URL" || die "wget image"
if wget -O "\$SHA" "\${IMAGE_URL}.sha256" 2>/dev/null; then
    EXP=\$(awk '{print \$1}' "\$SHA")
    GOT=\$(sha256sum "\$IMG" | awk '{print \$1}')
    [ "\$EXP" = "\$GOT" ] || die "sha256 mismatch"
fi

[ -b "\$TARGET_DEV" ] || die "no \$TARGET_DEV"
xz -dc "\$IMG" | dd of="\$TARGET_DEV" bs=4M conv=fsync
sync; sync
log "reboot"
sleep 2
reboot
EOF
chmod +x "$DEST/autorun.sh"
# FAT-safe duplicate name used by some recovery images
cp -f "$DEST/autorun.sh" "$DEST/flash-receiver-net.sh"
chmod +x "$DEST/flash-receiver-net.sh"

cat > "$DEST/README-FEL-USB.txt" <<EOF
SA-02m optional FEL/recovery USB
================================
Stand HTTP: http://${SERVER_IP}:${HTTP_PORT}/current.img.xz

This stick does NOT contain the full board image.
Operator:
  1) Start stand: start-stand.ps1 (publish image first)
  2) Insert this USB if OTG→PC FEL path unavailable
  3) Power board / enter FEL as usual
  4) Wait for DONE on http://localhost:8765

autorun.sh pulls current.img.xz over Ethernet and dd's eMMC.
EOF

echo "READY FEL-USB: $DEST"
echo "  server: http://${SERVER_IP}:${HTTP_PORT}/current.img.xz"
ls -lah "$DEST"
