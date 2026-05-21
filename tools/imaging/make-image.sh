#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  make-image.sh   v1.1
#  Снимает компактный образ eMMC c донорского SA-02m по ssh и уменьшает
#  его через PiShrink. Запускается на ХОСТЕ (Linux / WSL2 Ubuntu).
#
#  Документация: docs/SA02M_IMAGING_GUIDE.md §9
#
#  Использование:
#      ./make-image.sh [--ip 192.168.1.136] [--key ~/.ssh/sa02m_sa02] \
#                      [--out-dir ./out] [--profile sa02m-1eth] \
#                      [--version 1.0.0] [--no-cleanup] [--no-zerofill] \
#                      [--no-manifest] [--xz-level 1] [--final-xz-level 9e]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
LC_ALL=C

DEVICE_IP="192.168.1.136"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sa02m_sa02}"
OUT_DIR="$(pwd)/out"
DO_CLEANUP=1
DO_ZEROFILL=1
DO_MANIFEST=1
STREAM_XZ_LEVEL="1"
FINAL_XZ_LEVEL="9e"
RELEASE_PROFILE=""
RELEASE_VERSION=""
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10
          -o ServerAliveInterval=30 -o ServerAliveCountMax=6)

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)              DEVICE_IP="$2";        shift 2 ;;
        --key)             SSH_KEY="$2";          shift 2 ;;
        --out-dir)         OUT_DIR="$2";          shift 2 ;;
        --profile)         RELEASE_PROFILE="$2";  shift 2 ;;
        --version)         RELEASE_VERSION="$2";  shift 2 ;;
        --no-cleanup)      DO_CLEANUP=0;          shift ;;
        --no-zerofill)     DO_ZEROFILL=0;         shift ;;
        --no-manifest)     DO_MANIFEST=0;         shift ;;
        --xz-level)        STREAM_XZ_LEVEL="$2";  shift 2 ;;
        --final-xz-level)  FINAL_XZ_LEVEL="$2";   shift 2 ;;
        -h|--help)
            sed -n '1,20p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP_SCRIPT="$SCRIPT_DIR/cleanup-donor.sh"
SSH=(ssh -i "$SSH_KEY" "${SSH_OPTS[@]}" "root@$DEVICE_IP")

STAMP="$(date +%Y%m%d-%H%M)"
RAW_XZ="$OUT_DIR/sa02m-${STAMP}-raw.img.xz"
RAW_IMG="$OUT_DIR/sa02m-${STAMP}-raw.img"

if [ -n "$RELEASE_PROFILE" ] && [ -n "$RELEASE_VERSION" ]; then
    SHRUNK_XZ="$OUT_DIR/${RELEASE_PROFILE}-v${RELEASE_VERSION}-shrunk.img.xz"
else
    SHRUNK_XZ="$OUT_DIR/sa02m-${STAMP}-shrunk.img.xz"
fi

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

collect_donor_metadata() {
    "${SSH[@]}" 'bash -s' <<'REMOTE'
set -euo pipefail
hostname -s 2>/dev/null || echo SA-02
tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown
uname -r
grep '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"' || echo unknown
cat /var/lib/sa02m-web-build/deployed_commit 2>/dev/null || echo unknown
awk -F= '/^SA02M_SERIAL_PROFILE=/{print $2; exit}' /etc/sa02m_serial_profile.conf 2>/dev/null || echo unknown
blkid -s UUID -o value /dev/mmcblk2p1 2>/dev/null || echo unknown
blkid -s UUID -o value /dev/mmcblk2p2 2>/dev/null || echo unknown
dpkg-query -W -f='${Version}\n' armbian-bsp-cli-bananapim2ultra-current 2>/dev/null | head -1 || echo unknown
REMOTE
}

write_manifest() {
    local img_path=$1
    local sha_file="${img_path}.sha256"
    local manifest_path="${img_path%.img.xz}.manifest.json"
    local host_json
    host_json=$(python3 - "$img_path" "$sha256" "$manifest_path" <<'PY'
import json, sys, datetime, os
img, sha_file, out = sys.argv[1:4]
with open(sha_file, encoding="utf-8") as f:
    sha = f.read().split()[0]
meta = os.environ.get("DONOR_META", "").split("\n")
def m(i, default="unknown"):
    return meta[i].strip() if len(meta) > i and meta[i].strip() else default
hostname, board, kernel, os_name, git_commit, serial_profile, boot_uuid, root_uuid, bsp = (
    m(0), m(1), m(2), m(3), m(4), m(5), m(6), m(7), m(8)
)
if os.environ.get("RELEASE_PROFILE"):
    serial_profile = os.environ["RELEASE_PROFILE"]
doc = {
    "image_name": os.path.basename(img),
    "image_sha256": sha,
    "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pipeline": {
        "tool": "make-image.sh",
        "version": "1.1",
        "pishrink": True,
        "zerofill": os.environ.get("PIPE_ZEROFILL") == "1",
        "cleanup": os.environ.get("PIPE_CLEANUP") == "1",
        "final_xz_level": os.environ.get("PIPE_XZ_LEVEL", "9e"),
    },
    "source_device": {
        "ip": os.environ.get("DEVICE_IP", ""),
        "hostname": hostname,
        "emmc_device": "/dev/mmcblk2",
        "emmc_size_gib": 7.28,
    },
    "platform": {
        "board": board,
        "os": os_name,
        "kernel": kernel,
        "armbian_bsp": f"armbian-bsp-cli-bananapim2ultra-current {bsp}" if bsp != "unknown" else "unknown",
    },
    "sa02m_web_build": {"git_commit": git_commit},
    "serial_profile": serial_profile,
    "partitions": {
        "boot": {"device": "mmcblk2p1", "size_mib": 64, "fstype": "vfat", "uuid": boot_uuid},
        "root": {"device": "mmcblk2p2", "fstype": "ext4", "label": "armbi_root", "uuid": root_uuid},
    },
}
if os.environ.get("RELEASE_VERSION"):
    doc["release_version"] = os.environ["RELEASE_VERSION"]
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(out)
PY
)
    log "    manifest: $host_json"
}

# ── 0) Проверка зависимостей ────────────────────────────────────────────
log "[0/6] Проверка окружения"
for bin in ssh xz e2fsck resize2fs parted truncate sha256sum dd python3; do
    command -v "$bin" >/dev/null || die "не найден '$bin' в PATH"
done
command -v pishrink.sh >/dev/null || die "не найден pishrink.sh — см. docs/SA02M_IMAGING_GUIDE.md §7.1"
[ -r "$SSH_KEY" ] || die "ssh-ключ не найден: $SSH_KEY"
[ -r "$CLEANUP_SCRIPT" ] || die "не найден $CLEANUP_SCRIPT"
mkdir -p "$OUT_DIR"

log "    Проверка ssh до $DEVICE_IP"
"${SSH[@]}" "uname -nrm" || die "ssh до $DEVICE_IP не работает"

log "    Сбор метаданных донора (до cleanup)"
DONOR_META="$(collect_donor_metadata)"
export DONOR_META DEVICE_IP RELEASE_PROFILE RELEASE_VERSION
export PIPE_CLEANUP="$DO_CLEANUP" PIPE_ZEROFILL="$DO_ZEROFILL" PIPE_XZ_LEVEL="$FINAL_XZ_LEVEL"

# ── 1) Cleanup ───────────────────────────────────────────────────────────
if [ "$DO_CLEANUP" -eq 1 ]; then
    log "[1/6] Cleanup на доноре"
    "${SSH[@]}" 'bash -s' < "$CLEANUP_SCRIPT"
else
    log "[1/6] Cleanup пропущен (--no-cleanup)"
fi

# ── 2) Zero-fill ─────────────────────────────────────────────────────────
if [ "$DO_ZEROFILL" -eq 1 ]; then
    log "[2/6] Zero-fill свободного места rootfs"
    "${SSH[@]}" 'set -e; \
        dd if=/dev/zero of=/zero.fill bs=4M status=none 2>/dev/null || true; \
        sync; rm -f /zero.fill; sync; df -hT /'
else
    log "[2/6] Zero-fill пропущен (--no-zerofill)"
fi

# ── 3) Stream dd → xz ────────────────────────────────────────────────────
log "[3/6] Снятие образа: dd /dev/mmcblk2 → $RAW_XZ"
"${SSH[@]}" '
    set -e
    systemctl stop nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm 2>/dev/null || true
    rmmod -f g_mass_storage 2>/dev/null || true
    sync; sync
    dd if=/dev/mmcblk2 bs=4M status=none
' | xz "-T0" "-${STREAM_XZ_LEVEL}" -v -c > "$RAW_XZ"

log "    raw xz size: $(numfmt --to=iec --suffix=B "$(stat -c%s "$RAW_XZ")")"

# ── 4) PiShrink ──────────────────────────────────────────────────────────
log "[4/6] Распаковка raw → PiShrink"
xz -d -k -v "$RAW_XZ"
sudo pishrink.sh -a -v "$RAW_IMG"

log "    финальное xz (-T0 -${FINAL_XZ_LEVEL})"
xz "-T0" "-${FINAL_XZ_LEVEL}" -v -f "$RAW_IMG"
mv "${RAW_IMG}.xz" "$SHRUNK_XZ"

# ── 5) sha256 ────────────────────────────────────────────────────────────
log "[5/6] sha256sum"
( cd "$OUT_DIR" && sha256sum "$(basename "$SHRUNK_XZ")" > "$(basename "$SHRUNK_XZ").sha256" )

# ── 6) manifest ──────────────────────────────────────────────────────────
if [ "$DO_MANIFEST" -eq 1 ]; then
    log "[6/6] manifest.json"
    write_manifest "$SHRUNK_XZ"
else
    log "[6/6] manifest пропущен (--no-manifest)"
fi

FINAL_SIZE=$(stat -c%s "$SHRUNK_XZ")
echo
echo "    ════════════════════════════════════════════════════════════════"
echo "    READY: $SHRUNK_XZ"
echo "           размер: $(numfmt --to=iec --suffix=B "$FINAL_SIZE")"
echo "           sha256: $(awk '{print $1}' "${SHRUNK_XZ}.sha256")"
[ "$DO_MANIFEST" -eq 1 ] && echo "           manifest: ${SHRUNK_XZ%.img.xz}.manifest.json"
echo "    ════════════════════════════════════════════════════════════════"
echo
echo "    USB для приёмника:"
echo "      ./prepare-flash-media.sh --image $SHRUNK_XZ [--dest /mnt/c/USB/SA02m]"
