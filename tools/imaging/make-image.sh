#!/bin/bash
# SA-02m • make-image.sh v1.4
# cleanup (фазы 1–4) → одна ssh-сессия: zerofill + id reset + dd → PiShrink → xz
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${SA02M_DEVICE_ENV:-$REPO_ROOT/tools/sa02m-device.env}"
if [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
fi

DEVICE_IP="${SA02M_HOST:-192.168.1.136}"
SSH_USER="${SA02M_USER:-root}"
SSH_PASS="${SA02M_PASS:-cyntron}"
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
        --password)        SSH_PASS="$2";         shift 2 ;;
        --user)            SSH_USER="$2";         shift 2 ;;
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

CLEANUP_SCRIPT="$SCRIPT_DIR/cleanup-donor.sh"
STREAM_SCRIPT="$SCRIPT_DIR/stream-after-cleanup.sh"
FIX_DONOR_SCRIPT="$SCRIPT_DIR/fix-donor-after-abort.sh"
WATCHDOG_SCRIPT="$REPO_ROOT/etc/sa02m-userspace-watchdog.sh"
WATCHDOG_CONF="$REPO_ROOT/etc/sa02m_userspace_watchdog.conf"

# Prefer SSH key; fall back to password from tools/sa02m-device.env (sshpass).
if [ ! -r "$SSH_KEY" ] && [ -r "$REPO_ROOT/private/.ssh/sa02m_sa02" ]; then
    mkdir -p "$(dirname "$SSH_KEY")"
    cp -f "$REPO_ROOT/private/.ssh/sa02m_sa02" "$SSH_KEY"
    chmod 600 "$SSH_KEY"
fi

die() { echo "ERROR: $*" >&2; exit 1; }
log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

SSH_AUTH="key"
if [ -r "$SSH_KEY" ]; then
    SSH=(ssh -i "$SSH_KEY" "${SSH_OPTS[@]}" "${SSH_USER}@${DEVICE_IP}")
    SCP=(scp -i "$SSH_KEY" "${SSH_OPTS[@]}")
else
    command -v sshpass >/dev/null || die "нет ssh-ключа ($SSH_KEY) и нет sshpass — установите sshpass или положите ключ"
    export SSHPASS="$SSH_PASS"
    SSH=(sshpass -e ssh "${SSH_OPTS[@]}" -o PreferredAuthentications=password -o PubkeyAuthentication=no "${SSH_USER}@${DEVICE_IP}")
    SCP=(sshpass -e scp "${SSH_OPTS[@]}" -o PreferredAuthentications=password -o PubkeyAuthentication=no)
    SSH_AUTH="password"
fi

STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT_DIR" "$WORK"
RAW_IMG="$WORK/sa02m-${STAMP}-raw.img"
# Intermediate raw.xz stays under WORK only — never force-copy multi-GB xz onto OUT_DIR
# (OUT_DIR on /mnt/c / drvfs often has no room after a successful stream).
RAW_XZ="$WORK/sa02m-${STAMP}-raw.img.xz"

if [ -n "$OUTPUT_NAME" ]; then
    SHRUNK_NAME="${OUTPUT_NAME}"
elif [ -n "$RELEASE_PROFILE" ] && [ -n "$RELEASE_VERSION" ]; then
    SHRUNK_NAME="${RELEASE_PROFILE}-v${RELEASE_VERSION}-shrunk"
else
    SHRUNK_NAME="sa02m-${STAMP}-shrunk"
fi
SHRUNK_XZ="$OUT_DIR/${SHRUNK_NAME}.img.xz"
FINAL_IMG="$OUT_DIR/${SHRUNK_NAME}.img"
# Uncompressed .img kept under WORK by default; copied to OUT_DIR only when safe.
FINAL_IMG_KEEP="$WORK/${SHRUNK_NAME}.img"

# True when OUT_DIR is a Windows/drvfs mount (high risk of ENOSPC for large copies).
out_dir_is_drvfs() {
    case "$OUT_DIR" in
        /mnt/[a-zA-Z]/*|/mnt/[a-zA-Z]) return 0 ;;
    esac
    local fst
    fst=$(df -T "$OUT_DIR" 2>/dev/null | awk 'NR==2 {print $2}')
    case "$fst" in
        9p|drvfs|fuseblk|cifs) return 0 ;;
    esac
    return 1
}

# need_bytes: return 0 if OUT_DIR has at least that many free bytes (+64MiB slack).
out_dir_has_space() {
    local need=$1
    local avail
    avail=$(df -B1 --output=avail "$OUT_DIR" 2>/dev/null | tail -n1 | tr -d " ")
    [ -n "$avail" ] || return 1
    [ "$avail" -gt $((need + 64*1024*1024)) ]
}

# Copy src->dst only if OUT_DIR can hold it; else leave under WORK and echo path.
safe_publish_to_out() {
    local src=$1
    local dst=$2
    local sz
    sz=$(stat -c%s "$src")
    if out_dir_has_space "$sz"; then
        cp -f "$src" "$dst"
        return 0
    fi
    log "    WARN: OUT_DIR has insufficient space for $(basename "$dst") ($(numfmt --to=iec --suffix=B "$sz")); keeping under WORK: $src"
    return 1
}

# If final artifacts remain under WORK (OUT_DIR full/drvfs), move them aside before rm -rf.
rescue_work_artifacts() {
    local keep_dir="$1"
    mkdir -p "$keep_dir"
    local f
    for f in "$SHRUNK_XZ" "${SHRUNK_XZ}.sha256" "${SHRUNK_XZ%.img.xz}.manifest.json"; do
        case "$f" in
            "$WORK"/*)
                if [ -e "$f" ]; then
                    mv -f "$f" "$keep_dir/$(basename "$f")"
                    log "    rescued: $keep_dir/$(basename "$f")"
                fi
                ;;
        esac
    done
    if [ "$KEEP_RAW_IMG" -eq 1 ]; then
        for f in "$FINAL_IMG" "$FINAL_IMG_KEEP"; do
            case "$f" in
                "$WORK"/*)
                    if [ -e "$f" ]; then
                        mv -f "$f" "$keep_dir/$(basename "$f")"
                        log "    rescued KEEP_RAW_IMG: $keep_dir/$(basename "$f")"
                        FINAL_IMG="$keep_dir/$(basename "$f")"
                    fi
                    ;;
            esac
        done
    fi
    # Update SHRUNK_XZ if it was under WORK
    if [ -f "$keep_dir/$(basename "$SHRUNK_XZ")" ]; then
        case "$SHRUNK_XZ" in
            "$WORK"/*) SHRUNK_XZ="$keep_dir/$(basename "$SHRUNK_XZ")" ;;
        esac
    fi
}

cleanup_work() {
    local need_rescue=0
    case "$SHRUNK_XZ" in "$WORK"/*) need_rescue=1 ;; esac
    if [ "$KEEP_RAW_IMG" -eq 1 ]; then
        case "$FINAL_IMG$FINAL_IMG_KEEP" in "$WORK"/*|*"$WORK"*) need_rescue=1 ;; esac
        [ -e "$FINAL_IMG_KEEP" ] && case "$FINAL_IMG_KEEP" in "$WORK"/*) need_rescue=1 ;; esac
        [ -e "$FINAL_IMG" ] && case "$FINAL_IMG" in "$WORK"/*) need_rescue=1 ;; esac
    fi
    if [ "$need_rescue" -eq 1 ]; then
        local keep_dir
        keep_dir="$(dirname "$WORK")/sa02m-image-rescue-$$"
        rescue_work_artifacts "$keep_dir"
        echo "    WORK artifacts kept under: $keep_dir"
    fi
    rm -rf "$WORK"
}
trap cleanup_work EXIT

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
        "version": "1.4",
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
[ -r "$CLEANUP_SCRIPT" ] || die "не найден $CLEANUP_SCRIPT"
[ -r "$STREAM_SCRIPT" ] || die "не найден $STREAM_SCRIPT"
log "    auth: $SSH_AUTH → ${SSH_USER}@${DEVICE_IP}"

log "    ssh → $DEVICE_IP (ожидание до 5 мин после reboot)"
WAIT_SCRIPT="$SCRIPT_DIR/wait-donor.sh"
if [ -x "$WAIT_SCRIPT" ] || [ -f "$WAIT_SCRIPT" ]; then
    IP="$DEVICE_IP" SA02M_PASS="$SSH_PASS" SA02M_USER="$SSH_USER" SSH_KEY="$SSH_KEY" bash "$WAIT_SCRIPT" || die "донор недоступен по ssh"
else
    "${SSH[@]}" "uname -nrm" || die "ssh до $DEVICE_IP не работает"
fi

if [ -r "$FIX_DONOR_SCRIPT" ]; then
    log "    preflight: orphan dd на доноре"
    "${SSH[@]}" 'bash -s -- --preflight' < "$FIX_DONOR_SCRIPT" || true
fi

deploy_userspace_watchdog() {
    [ -r "$WATCHDOG_SCRIPT" ] || return 0
    log "    deploy: userspace watchdog (imaging lock + FAIL_THRESHOLD=60)"
    "${SCP[@]}" "$WATCHDOG_SCRIPT" "root@${DEVICE_IP}:/usr/local/sbin/sa02m-userspace-watchdog"
    if [ -r "$WATCHDOG_CONF" ]; then
        "${SCP[@]}" "$WATCHDOG_CONF" "root@${DEVICE_IP}:/etc/sa02m_userspace_watchdog.conf"
    else
        "${SSH[@]}" 'grep -q "^FAIL_THRESHOLD=" /etc/sa02m_userspace_watchdog.conf 2>/dev/null && \
            sed -i "s/^FAIL_THRESHOLD=.*/FAIL_THRESHOLD=60/" /etc/sa02m_userspace_watchdog.conf || \
            echo "FAIL_THRESHOLD=60" >> /etc/sa02m_userspace_watchdog.conf' || true
    fi
    "${SSH[@]}" 'chmod 755 /usr/local/sbin/sa02m-userspace-watchdog'
}

prepare_donor_watchdogs_for_imaging() {
    log "    stop/mask watchdog + disable HW RuntimeWatchdogSec for imaging"
    "${SSH[@]}" 'bash -s' <<'REMOTE'
set -euo pipefail
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog sa02m-watchdog-feed; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl mask "$svc" 2>/dev/null || true
done
# Zero-fill/dd на eMMC может блокировать PID1 >10s — отключаем HW watchdog до reboot.
systemctl set-property --runtime Manager RuntimeWatchdogSec=0 2>/dev/null || \
    busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager RuntimeWatchdogUSec t 0 2>/dev/null || true
sync
REMOTE
}

deploy_userspace_watchdog
prepare_donor_watchdogs_for_imaging

log "    метаданные донора"
DONOR_META="$(collect_donor_metadata)"
export DONOR_META DEVICE_IP RELEASE_PROFILE RELEASE_VERSION
export PIPE_CLEANUP="$DO_CLEANUP" PIPE_ZEROFILL="$DO_ZEROFILL" PIPE_ID_RESET="$DO_ID_RESET" PIPE_XZ_LEVEL="$FINAL_XZ_LEVEL"

if [ "$DO_CLEANUP" -eq 1 ]; then
    log "[1/6] Cleanup на доноре (фазы 1–4, --apply, ssh остаётся рабочим)"
    # --apply required: cleanup-donor.sh is fail-safe (default dry-run without flag)
    "${SSH[@]}" 'bash -s -- --apply --report' < "$CLEANUP_SCRIPT"
else
    log "[1/6] Cleanup пропущен (--no-cleanup)"
fi

STREAM_ARGS=()
[ "$DO_ZEROFILL" -eq 0 ] && STREAM_ARGS+=(--no-zerofill)
[ "$DO_ID_RESET" -eq 0 ] && STREAM_ARGS+=(--no-id-reset)

log "[2/6] Zero-fill + id reset + dd (одна ssh-сессия, stdout → raw)"
log "    ⚠ после начала dd не прерывайте — новый ssh на доноре будет недоступен до reboot"
rm -f "$RAW_IMG"
# SSH may report "Broken Pipe" when the remote dd completes and closes the channel
# while the client is still sending a keepalive — this is harmless.  We treat
# any SSH exit as "try to recover": the authoritative check is the raw image size.
set +e
"${SSH[@]}" "bash -s -- ${STREAM_ARGS[*]}" < "$STREAM_SCRIPT" > "$RAW_IMG"
SSH_RC=$?
set -e

RAW_SIZE=$(stat -c%s "$RAW_IMG" 2>/dev/null || echo 0)
log "    raw: $(numfmt --to=iec --suffix=B "$RAW_SIZE") (ssh_rc=${SSH_RC})"
[ "$RAW_SIZE" -eq "$EMMC_BYTES" ] || die "размер raw $RAW_SIZE != $EMMC_BYTES (ssh_rc=${SSH_RC})"

log "[3/6] compress raw (optional archive under WORK) + PiShrink"
# Write intermediate raw.xz only into WORK — do not copy onto OUT_DIR.
xz "-T0" "-${STREAM_XZ_LEVEL}" -v -c "$RAW_IMG" > "$RAW_XZ"
log "    raw.xz (WORK only): $RAW_XZ ($(numfmt --to=iec --suffix=B "$(stat -c%s "$RAW_XZ")"))"
sudo pishrink.sh -a -v "$RAW_IMG"

# Патч образа: first-boot resize/network/watchdog (см. patch-firstboot-image.sh).
if [ -x "$SCRIPT_DIR/patch-firstboot-image.sh" ]; then
    log "    [patch] patch-firstboot-image.sh"
    sudo "$SCRIPT_DIR/patch-firstboot-image.sh" "$RAW_IMG" || \
        log "    [patch] WARN: patch-firstboot-image failed (не критично)"
else
patch_image() {
    local img="$1"
    local loop mnt rootpart
    log "    [patch] loop-mount образа для патча watchdog unit-файлов"
    loop=$(sudo losetup --partscan -f --show "$img" 2>/dev/null) || {
        log "    [patch] WARN: losetup не удался — патч пропущен (не критично)"
        return 0
    }
    mnt=$(mktemp -d /tmp/sa02m-img-patch-XXXXXX)
    rootpart="${loop}p2"
    local i
    for i in 1 2 3 4 5; do [ -b "$rootpart" ] && break; sleep 1; done
    if ! sudo mount "$rootpart" "$mnt" 2>/dev/null; then
        log "    [patch] WARN: mount образа не удался — патч пропущен"
        sudo losetup -d "$loop" 2>/dev/null || true
        rm -rf "$mnt"; return 0
    fi

    # 1. Нормальные watchdog unit-файлы из репозитория
    sudo cp -f "$REPO_ROOT/etc/systemd/sa02m-userspace-watchdog.service" \
        "$mnt/etc/systemd/system/sa02m-userspace-watchdog.service" 2>/dev/null || true
    sudo cp -f "$REPO_ROOT/etc/net-watchdog.service" \
        "$mnt/etc/systemd/system/net-watchdog.service" 2>/dev/null || true
    sudo cp -f "$REPO_ROOT/etc/sa02m-failure-monitor.service" \
        "$mnt/etc/systemd/system/sa02m-failure-monitor.service" 2>/dev/null || true

    # 2. Watchdog drop-in (RuntimeWatchdogSec=10s из репо)
    sudo install -d "$mnt/etc/systemd/system.conf.d"
    sudo cp -f "$REPO_ROOT/etc/systemd/sa02m-watchdog.conf" \
        "$mnt/etc/systemd/system.conf.d/sa02m-watchdog.conf" 2>/dev/null || true
    # Убрать прямую правку RuntimeWatchdogSec в system.conf (управляется drop-in'ом)
    sudo sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' \
        "$mnt/etc/systemd/system.conf" 2>/dev/null || true

    # 3. Убрать DONE-флаг expand (на случай если stream не удалил)
    sudo rm -f "$mnt/var/lib/sa02m-rootfs-expand.done"

    log "    [patch] watchdog unit-файлы восстановлены, DONE-флаг удалён"
    sudo umount "$mnt"
    sudo losetup -d "$loop" 2>/dev/null || true
    rm -rf "$mnt"
}
patch_image "$RAW_IMG"
fi

log "[4/6] final xz (-T0 -${FINAL_XZ_LEVEL})"
rm -f "$SHRUNK_XZ" "$FINAL_IMG"
FINAL_IMG_WORK="$WORK/$(basename "$FINAL_IMG")"
cp -f "$RAW_IMG" "$FINAL_IMG_WORK"
FINAL_XZ_TMP="$WORK/$(basename "$SHRUNK_XZ")"
xz "-T0" "-${FINAL_XZ_LEVEL}" -v -c "$FINAL_IMG_WORK" > "$FINAL_XZ_TMP"
# Publish only the final shrunk .img.xz to OUT_DIR (skip if OUT_DIR cannot hold it).
if ! safe_publish_to_out "$FINAL_XZ_TMP" "$SHRUNK_XZ"; then
    SHRUNK_XZ="$FINAL_XZ_TMP"
    log "    using WORK path for final xz: $SHRUNK_XZ"
fi
if [ "$KEEP_RAW_IMG" -eq 1 ]; then
    cp -f "$FINAL_IMG_WORK" "$FINAL_IMG_KEEP"
    # Prefer native WORK for large .img when OUT_DIR is drvfs or low on space.
    if out_dir_is_drvfs || ! out_dir_has_space "$(stat -c%s "$FINAL_IMG_KEEP")"; then
        FINAL_IMG="$FINAL_IMG_KEEP"
        log "    KEEP_RAW_IMG: keeping uncompressed .img under WORK (not copying to OUT_DIR): $FINAL_IMG"
    else
        if safe_publish_to_out "$FINAL_IMG_KEEP" "$FINAL_IMG"; then
            : # published
        else
            FINAL_IMG="$FINAL_IMG_KEEP"
        fi
    fi
else
    rm -f "$FINAL_IMG" "$FINAL_IMG_KEEP"
fi
rm -f "$FINAL_IMG_WORK"

log "[5/6] sha256"
SHA_DIR="$(dirname "$SHRUNK_XZ")"
( cd "$SHA_DIR" && sha256sum "$(basename "$SHRUNK_XZ")" > "$(basename "$SHRUNK_XZ").sha256" )
# If final xz landed in WORK but OUT_DIR is usable for small sidecar files, mirror sha256 there.
if [ "$SHA_DIR" != "$OUT_DIR" ] && [ -f "${SHRUNK_XZ}.sha256" ]; then
    if out_dir_has_space "$(stat -c%s "${SHRUNK_XZ}.sha256")"; then
        cp -f "${SHRUNK_XZ}.sha256" "$OUT_DIR/$(basename "$SHRUNK_XZ").sha256" || true
    fi
fi

if [ "$DO_MANIFEST" -eq 1 ]; then
    log "[6/6] manifest.json"
    write_manifest "$SHRUNK_XZ"
else
    log "[6/6] manifest пропущен"
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
