#!/bin/bash
# SA-02m • make-image.sh v1.3
# cleanup (фазы 1–4) → одна ssh-сессия: zerofill + id reset + dd → PiShrink → xz
set -euo pipefail
LC_ALL=C

DEVICE_IP="192.168.1.136"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sa02m_sa02}"
OUT_DIR="$(pwd)/out"
DO_CLEANUP=1
DO_ZEROFILL=1
DO_ID_RESET=1
DO_MANIFEST=1
KEEP_RAW_IMG=0
STREAM_XZ_LEVEL="1"
FINAL_XZ_LEVEL="9e"
RELEASE_PROFILE=""
RELEASE_VERSION=""
OUTPUT_NAME=""
EMMC_BYTES=7818182656
WORK="${TMPDIR:-/tmp}/sa02m-make-image-$$"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o ServerAliveInterval=15 -o ServerAliveCountMax=9999
          -o TCPKeepAlive=yes)

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)              DEVICE_IP="$2";        shift 2 ;;
        --key)             SSH_KEY="$2";          shift 2 ;;
        --out-dir)         OUT_DIR="$2";          shift 2 ;;
        --profile)         RELEASE_PROFILE="$2";  shift 2 ;;
        --version)         RELEASE_VERSION="$2";  shift 2 ;;
        --name)            OUTPUT_NAME="$2"; KEEP_RAW_IMG=1; shift 2 ;;
        --no-cleanup)      DO_CLEANUP=0;          shift ;;
        --no-zerofill)     DO_ZEROFILL=0;         shift ;;
        --no-id-reset)     DO_ID_RESET=0;         shift ;;
        --no-manifest)     DO_MANIFEST=0;         shift ;;
        --keep-raw-img)    KEEP_RAW_IMG=1;        shift ;;
        --xz-level)        STREAM_XZ_LEVEL="$2";  shift 2 ;;
        --final-xz-level)  FINAL_XZ_LEVEL="$2";   shift 2 ;;
        -h|--help)
            grep '^#' "$0" | head -20 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP_SCRIPT="$SCRIPT_DIR/cleanup-donor.sh"
STREAM_SCRIPT="$SCRIPT_DIR/stream-after-cleanup.sh"
FIX_DONOR_SCRIPT="$SCRIPT_DIR/fix-donor-after-abort.sh"
SSH=(ssh -i "$SSH_KEY" "${SSH_OPTS[@]}" "root@$DEVICE_IP")

STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT_DIR" "$WORK"
RAW_IMG="$WORK/sa02m-${STAMP}-raw.img"
RAW_XZ="$OUT_DIR/sa02m-${STAMP}-raw.img.xz"

if [ -n "$OUTPUT_NAME" ]; then
    SHRUNK_XZ="$OUT_DIR/${OUTPUT_NAME}.img.xz"
    FINAL_IMG="$OUT_DIR/${OUTPUT_NAME}.img"
elif [ -n "$RELEASE_PROFILE" ] && [ -n "$RELEASE_VERSION" ]; then
    SHRUNK_XZ="$OUT_DIR/${RELEASE_PROFILE}-v${RELEASE_VERSION}-shrunk.img.xz"
    FINAL_IMG="$OUT_DIR/${RELEASE_PROFILE}-v${RELEASE_VERSION}-shrunk.img"
else
    SHRUNK_XZ="$OUT_DIR/sa02m-${STAMP}-shrunk.img.xz"
    FINAL_IMG="$OUT_DIR/sa02m-${STAMP}-shrunk.img"
fi

cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

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
    python3 - "$img_path" "$sha_file" "$manifest_path" <<'PY'
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
        "version": "1.3",
        "pishrink": True,
        "zerofill": os.environ.get("PIPE_ZEROFILL") == "1",
        "cleanup": os.environ.get("PIPE_CLEANUP") == "1",
        "id_reset_in_stream": os.environ.get("PIPE_ID_RESET") == "1",
        "final_xz_level": os.environ.get("PIPE_XZ_LEVEL", "9e"),
    },
    "source_device": {"ip": os.environ.get("DEVICE_IP", ""), "hostname": hostname, "emmc_device": "/dev/mmcblk2", "emmc_size_gib": 7.28},
    "platform": {"board": board, "os": os_name, "kernel": kernel,
                 "armbian_bsp": f"armbian-bsp-cli-bananapim2ultra-current {bsp}" if bsp != "unknown" else "unknown"},
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
    log "    manifest: ${img_path%.img.xz}.manifest.json"
}

log "[0/6] Проверка окружения"
for bin in ssh xz e2fsck resize2fs parted truncate sha256sum dd python3; do
    command -v "$bin" >/dev/null || die "не найден '$bin'"
done
command -v pishrink.sh >/dev/null || die "не найден pishrink.sh"
[ -r "$SSH_KEY" ] || die "ssh-ключ не найден: $SSH_KEY"
[ -r "$CLEANUP_SCRIPT" ] || die "не найден $CLEANUP_SCRIPT"
[ -r "$STREAM_SCRIPT" ] || die "не найден $STREAM_SCRIPT"

log "    ssh → $DEVICE_IP (ожидание до 5 мин после reboot)"
WAIT_SCRIPT="$SCRIPT_DIR/wait-donor.sh"
if [ -x "$WAIT_SCRIPT" ] || [ -f "$WAIT_SCRIPT" ]; then
    IP="$DEVICE_IP" bash "$WAIT_SCRIPT" || die "донор недоступен по ssh"
else
    "${SSH[@]}" "uname -nrm" || die "ssh до $DEVICE_IP не работает"
fi

if [ -r "$FIX_DONOR_SCRIPT" ]; then
    log "    preflight: orphan dd на доноре"
    "${SSH[@]}" 'bash -s -- --preflight' < "$FIX_DONOR_SCRIPT" || true
fi

log "    метаданные донора"
DONOR_META="$(collect_donor_metadata)"
export DONOR_META DEVICE_IP RELEASE_PROFILE RELEASE_VERSION
export PIPE_CLEANUP="$DO_CLEANUP" PIPE_ZEROFILL="$DO_ZEROFILL" PIPE_ID_RESET="$DO_ID_RESET" PIPE_XZ_LEVEL="$FINAL_XZ_LEVEL"

if [ "$DO_CLEANUP" -eq 1 ]; then
    log "[1/6] Cleanup на доноре (фазы 1–4, ssh остаётся рабочим)"
    "${SSH[@]}" 'bash -s' < "$CLEANUP_SCRIPT"
else
    log "[1/6] Cleanup пропущен (--no-cleanup)"
fi

STREAM_ARGS=()
[ "$DO_ZEROFILL" -eq 0 ] && STREAM_ARGS+=(--no-zerofill)
[ "$DO_ID_RESET" -eq 0 ] && STREAM_ARGS+=(--no-id-reset)

log "[2/6] Zero-fill + id reset + dd (одна ssh-сессия, stdout → raw)"
log "    ⚠ после начала dd не прерывайте — новый ssh на доноре будет недоступен до reboot"
rm -f "$RAW_IMG"
"${SSH[@]}" "bash -s -- ${STREAM_ARGS[*]}" < "$STREAM_SCRIPT" > "$RAW_IMG"

RAW_SIZE=$(stat -c%s "$RAW_IMG")
log "    raw: $(numfmt --to=iec --suffix=B "$RAW_SIZE")"
[ "$RAW_SIZE" -eq "$EMMC_BYTES" ] || die "размер raw $RAW_SIZE != $EMMC_BYTES"

log "[3/6] Архив raw (опционально) + PiShrink"
XZ_TMP="$WORK/$(basename "$RAW_XZ")"
xz "-T0" "-${STREAM_XZ_LEVEL}" -v -c "$RAW_IMG" > "$XZ_TMP"
cp -f "$XZ_TMP" "$RAW_XZ"
sudo pishrink.sh -a -v "$RAW_IMG"

log "[4/6] Финальный xz (-T0 -${FINAL_XZ_LEVEL})"
rm -f "$SHRUNK_XZ" "$FINAL_IMG"
cp -f "$RAW_IMG" "$FINAL_IMG"
FINAL_XZ_TMP="$WORK/$(basename "$SHRUNK_XZ")"
xz "-T0" "-${FINAL_XZ_LEVEL}" -v -c "$FINAL_IMG" > "$FINAL_XZ_TMP"
cp -f "$FINAL_XZ_TMP" "$SHRUNK_XZ"
[ "$KEEP_RAW_IMG" -eq 1 ] || rm -f "$FINAL_IMG"

log "[5/6] sha256"
( cd "$OUT_DIR" && sha256sum "$(basename "$SHRUNK_XZ")" > "$(basename "$SHRUNK_XZ").sha256" )

if [ "$DO_MANIFEST" -eq 1 ]; then
    log "[6/6] manifest.json"
    write_manifest "$SHRUNK_XZ"
else
    log "[6/6] manifest пропущен"
fi

if [ "$KEEP_RAW_IMG" -eq 1 ]; then
    cp -f "$RAW_IMG" "$FINAL_IMG"
fi

FINAL_SIZE=$(stat -c%s "$SHRUNK_XZ")
echo
echo "    ════════════════════════════════════════════════════════════════"
echo "    READY: $SHRUNK_XZ"
echo "           размер: $(numfmt --to=iec --suffix=B "$FINAL_SIZE")"
echo "           sha256: $(awk '{print $1}' "${SHRUNK_XZ}.sha256")"
[ "$KEEP_RAW_IMG" -eq 1 ] && echo "           raw img: $FINAL_IMG"
[ "$DO_MANIFEST" -eq 1 ] && echo "           manifest: ${SHRUNK_XZ%.img.xz}.manifest.json"
echo "    ════════════════════════════════════════════════════════════════"
echo "    После снятия образа донор без ssh host keys — выполните reboot."
echo "    USB: ./prepare-flash-media.sh --image $SHRUNK_XZ [--dest /mnt/c/USB/SA02m]"
