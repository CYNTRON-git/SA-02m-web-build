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
OUT_LOCK=""
RESCUE_INCOMPLETE=0
PIPE_RUNTIME_MODE=legacy
CAPTURE_SHA=""
SSH_OPTS=(-o StrictHostKeyChecking=yes -o ConnectTimeout=15
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
        --zerofill)        DO_ZEROFILL=1;         shift ;;
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
PATCH_SCRIPT="$SCRIPT_DIR/patch-firstboot-image.sh"
SEAL_SCRIPT="$SCRIPT_DIR/seal-captured-runtime.sh"
FREE_BLOCK_SCRIPT="$SCRIPT_DIR/zero-free-ext4.py"
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
mkdir -p -- "$OUT_DIR"

if [ -n "$OUTPUT_NAME" ]; then
    SHRUNK_NAME="${OUTPUT_NAME}"
elif [ -n "$RELEASE_PROFILE" ] && [ -n "$RELEASE_VERSION" ]; then
    SHRUNK_NAME="${RELEASE_PROFILE}-v${RELEASE_VERSION}-shrunk"
else
    SHRUNK_NAME="sa02m-${STAMP}-shrunk"
fi
case "$SHRUNK_NAME" in
    */*|*\\*|*[[:cntrl:]]*) die "имя образа должно быть одним именем файла" ;;
esac
SHRUNK_XZ="$OUT_DIR/${SHRUNK_NAME}.img.xz"
FINAL_IMG="$OUT_DIR/${SHRUNK_NAME}.img"
WORK=$(mktemp -d -- "${TMPDIR:-/tmp}/sa02m-make-image-XXXXXXXX") || die "не удалось создать WORK"
RAW_IMG="$WORK/sa02m-${STAMP}-raw.img"
# Intermediate raw.xz stays under WORK, including when OUT_DIR is on DrvFS.
RAW_XZ="$WORK/sa02m-${STAMP}-raw.img.xz"
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

# Stage the image and its optional sidecars on the destination filesystem.
# Promote the image last so a failed copy cannot expose a partial image set.
safe_publish_to_out() {
    local src=$1
    local dst=$2
    shift 2
    local src_file size total=0 stage i j
    local sources=("$@" "$src") targets=()
    # src and dst can resolve to the same file (e.g. FINAL_IMG already == a WORK
    # path after a keep/rescue reassignment) — a plain `cp` would error
    # "are the same file". Nothing to copy: it is already at dst.
    if [ "$src" -ef "$dst" ]; then
        return 0
    fi
    for src_file in "${sources[@]}"; do
        size=$(stat -c%s -- "$src_file") || return 1
        total=$((total + size))
        targets+=("$(dirname -- "$dst")/$(basename -- "$src_file")")
    done
    targets[${#targets[@]}-1]="$dst"
    for src_file in "${targets[@]}"; do
        if [ -e "$src_file" ] || [ -L "$src_file" ]; then
            log "    WARN: output already exists: $src_file"
            return 1
        fi
    done
    if ! out_dir_has_space "$total"; then
        log "    WARN: OUT_DIR has insufficient space for $(basename "$dst"); keeping under WORK: $src"
        return 1
    fi
    stage=$(mktemp -d -- "$(dirname -- "$dst")/.sa02m-publish-XXXXXXXX") || return 1
    for i in "${!sources[@]}"; do
        src_file=${sources[i]}
        if ! cp -- "$src_file" "$stage/$i" || ! sync -d -- "$stage/$i" \
            || ! cmp -s -- "$src_file" "$stage/$i"; then
            log "    WARN: copy or verification failed for $(basename "$src_file"); keeping under WORK: $src"
            rm -rf -- "$stage"
            return 1
        fi
    done
    for i in "${!targets[@]}"; do
        # GNU mv -n can exit 0 on a collision and leave its source in place.
        if ! mv -nT -- "$stage/$i" "${targets[i]}" || [ -e "$stage/$i" ]; then
            log "    WARN: could not publish ${targets[i]}; keeping under WORK: $src"
            for ((j = 0; j < i; j++)); do
                rm -f -- "${targets[j]}" || log "    WARN: could not remove ${targets[j]}"
            done
            rm -rf -- "$stage"
            return 1
        fi
    done
    rmdir -- "$stage" || log "    WARN: could not remove $stage"
}

claim_output_name() {
    local lock="$OUT_DIR/.${SHRUNK_NAME}.capture-lock" path
    mkdir -- "$lock" || die "имя образа занято: $lock"
    OUT_LOCK="$lock"
    for path in "$SHRUNK_XZ" "${SHRUNK_XZ}.sha256" "${SHRUNK_XZ%.img.xz}.manifest.json" "$FINAL_IMG"; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            die "артефакт уже существует: $path"
        fi
    done
}

# If final artifacts remain under WORK (OUT_DIR full/drvfs), move them aside before rm -rf.
rescue_work_artifacts() {
    local keep_dir="$1"
    local f
    for f in "$SHRUNK_XZ" "${SHRUNK_XZ}.sha256" "${SHRUNK_XZ%.img.xz}.manifest.json"; do
        case "$f" in
            "$WORK"/*)
                if [ -e "$f" ]; then
                    mv -- "$f" "$keep_dir/$(basename "$f")" || return 1
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
                        mv -- "$f" "$keep_dir/$(basename "$f")" || return 1
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

rescue_remaining_artifacts() {
    if [[ "$SHRUNK_XZ" == "$WORK/"* && -f "$SHRUNK_XZ" ]] \
        || [[ "$KEEP_RAW_IMG" -eq 1 && -f "$FINAL_IMG_KEEP" ]]; then
        local keep_dir
        # A failed move can split the set across WORK and the rescue directory.
        # EXIT must preserve both locations instead of retrying or deleting WORK.
        RESCUE_INCOMPLETE=1
        if ! keep_dir=$(mktemp -d -- "$(dirname "$WORK")/sa02m-image-rescue-XXXXXXXX"); then
            log "    WARN: could not create rescue directory; files remain under $WORK"
            return 1
        fi
        if ! rescue_work_artifacts "$keep_dir"; then
            log "    WARN: rescue incomplete; inspect both $keep_dir and $WORK"
            return 1
        fi
        RESCUE_INCOMPLETE=0
        echo "    WORK artifacts kept under: $keep_dir"
    fi
}

cleanup_work() {
    [ -z "$OUT_LOCK" ] || rmdir -- "$OUT_LOCK" || true
    [ "$RESCUE_INCOMPLETE" -eq 0 ] || return 1
    rescue_remaining_artifacts || return 1
    rm -rf -- "$WORK"
}
trap cleanup_work EXIT
claim_output_name

collect_donor_metadata() {
    "${SSH[@]}" 'bash -s' <<'REMOTE'
set -euo pipefail
# key=value, exactly one line per field. Each value is captured in its OWN
# command substitution: a newline-less source (/proc/device-tree/model is
# NUL-terminated) can no longer merge with the next field's line.
emit() {
    local key=$1 val=$2
    val=${val%%$'\n'*}
    val=${val//$'\r'/}
    printf '%s=%s\n' "$key" "${val:-unknown}"
}
emit hostname       "$(hostname -s 2>/dev/null || true)"
emit board          "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
emit kernel         "$(uname -r 2>/dev/null || true)"
emit os             "$(grep '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
emit git_commit     "$(cat /var/lib/sa02m-web-build/deployed_commit 2>/dev/null || true)"
emit serial_profile "$(awk -F= '/^SA02M_SERIAL_PROFILE=/{print $2; exit}' /etc/sa02m_serial_profile.conf 2>/dev/null || true)"
emit boot_uuid      "$(blkid -s UUID -o value /dev/mmcblk2p1 2>/dev/null || true)"
emit root_uuid      "$(blkid -s UUID -o value /dev/mmcblk2p2 2>/dev/null || true)"
emit bsp            "$(dpkg-query -W -f='${Version}' armbian-bsp-cli-bananapim2ultra-current 2>/dev/null || true)"
REMOTE
}

write_manifest() {
    local img_path=$1
    local sha_file="${img_path}.sha256"
    local manifest_path="${img_path%.img.xz}.manifest.json"
    python3 - "$img_path" "$sha_file" "$manifest_path" <<'PY'
import datetime, json, os, re, sys
img, sha_file, out = sys.argv[1:4]
with open(sha_file, encoding="utf-8") as f:
    sha = f.read().split()[0]
meta = {}
for line in os.environ.get("DONOR_META", "").splitlines():
    key, sep, value = line.partition("=")
    if sep:
        meta[key.strip()] = value.strip()
def m(key, default="unknown"):
    return meta.get(key, "").strip() or default
hostname, board, kernel, os_name, git_commit, serial_profile, boot_uuid, root_uuid, bsp = (
    m("hostname"), m("board"), m("kernel"), m("os"), m("git_commit"),
    m("serial_profile"), m("boot_uuid"), m("root_uuid"), m("bsp"),
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
doc["runtime_capture"] = os.environ.get("PIPE_RUNTIME_MODE", "legacy")
if doc["runtime_capture"] == "managed":
    with open(os.environ["PIPE_RUNTIME_SEAL"], encoding="utf-8") as f:
        seal = json.load(f)
    hex_keys = {"source", "release_id", "manifest_sha256", "selector_sha256"}
    if (not isinstance(seal, dict) or set(seal) != hex_keys | {"schema"}
            or type(seal["schema"]) is not int or seal["schema"] != 1
            or any(not isinstance(seal[key], str) or not re.fullmatch(r"[0-9a-f]{64}", seal[key])
                   for key in hex_keys)):
        raise ValueError("invalid managed runtime image seal")
    doc["runtime_image_seal"] = seal
    if doc["pipeline"]["zerofill"]:
        with open(os.environ["PIPE_FINAL_FREE_BLOCKS"], encoding="utf-8") as f:
            final_copy = json.load(f)
        raw_sha = final_copy["copy_sha256"]
        if not isinstance(raw_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_sha):
            raise ValueError("invalid final free-block receipt")
        doc["uncompressed_sha256"] = raw_sha
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(out)
PY
    log "    manifest: ${img_path%.img.xz}.manifest.json"
}

log "[0/6] Проверка окружения"
for bin in ssh xz e2fsck resize2fs parted truncate sha256sum dd python3 cmp sync; do
    command -v "$bin" >/dev/null || die "не найден '$bin'"
done
command -v pishrink.sh >/dev/null || die "не найден pishrink.sh"
# mkimage — жёсткое требование: без него boot.scr собирает запасной writer, а
# образ без валидного boot.scr не грузится (docs/contracts/uboot-boot-script.md).
command -v mkimage >/dev/null || die "не найден 'mkimage' (apt install u-boot-tools)"
[ -r "$CLEANUP_SCRIPT" ] || die "не найден $CLEANUP_SCRIPT"
[ -r "$STREAM_SCRIPT" ] || die "не найден $STREAM_SCRIPT"
# -r, НЕ -x: файл лежит в git с режимом 100644 (как 110 из 115 *.sh репозитория),
# поэтому проверка по x-биту на свежем клоне была ЛОЖНОЙ и патч-шаг молча
# пропускался вместе со сборкой boot.scr.
[ -r "$PATCH_SCRIPT" ] || die "не найден $PATCH_SCRIPT — образ не получит валидный boot.scr"
log "    auth: $SSH_AUTH → ${SSH_USER}@${DEVICE_IP}"

donor_mode=$("${SSH[@]}" 'bash -s -- --probe-mode' < "$STREAM_SCRIPT") \
    || die "не удалось подтвердить версию и runtime донора по SSH"
[[ "$donor_mode" =~ ^(legacy|managed)\ ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)$ ]] \
    || die "неизвестный результат проверки донора"
PIPE_RUNTIME_MODE=${BASH_REMATCH[1]}
DONOR_VERSION=${BASH_REMATCH[2]}
if [ -n "$RELEASE_VERSION" ] && [ "$RELEASE_VERSION" != "$DONOR_VERSION" ]; then
    die "версия донора $DONOR_VERSION отличается от запрошенной $RELEASE_VERSION"
fi
if [ "$PIPE_RUNTIME_MODE" = managed ]; then
    [ "$EUID" -eq 0 ] && [ "$DO_ID_RESET" = 1 ] && [ "$DO_MANIFEST" = 1 ] && [ "$DO_CLEANUP" = 0 ] \
        || die "управляемый захват требует root, сброса идентичности, манифеста и подготовленного донора (--no-cleanup)"
    [ -r "$SEAL_SCRIPT" ] || die "не найден $SEAL_SCRIPT"
    if [ "$DO_ZEROFILL" = 1 ]; then
        [ -r "$FREE_BLOCK_SCRIPT" ] || die "не найден $FREE_BLOCK_SCRIPT"
        for bin in blkid dumpe2fs; do command -v "$bin" >/dev/null || die "не найден $bin"; done
    fi
    for bin in losetup mount umount; do command -v "$bin" >/dev/null || die "не найден '$bin'"; done
    RELEASE_VERSION="$DONOR_VERSION"
    CAPTURE_HOME=/var/lib/sa02m-image-captures
    if [ ! -e "$CAPTURE_HOME" ] && [ ! -L "$CAPTURE_HOME" ]; then mkdir -m 700 -- "$CAPTURE_HOME"; fi
    [ -d "$CAPTURE_HOME" ] && [ ! -L "$CAPTURE_HOME" ] \
        && [ "$(stat -c '%u:%g:%a' -- "$CAPTURE_HOME")" = 0:0:700 ] \
        || die "ненадёжный каталог захвата $CAPTURE_HOME"
    available=$(df -B1 --output=avail "$CAPTURE_HOME" | tail -n1 | tr -d ' ')
    [[ "$available" =~ ^[0-9]+$ ]] && [ "$available" -ge $((2 * EMMC_BYTES + 64*1024*1024)) ] \
        || die "нет места для исходного образа и рабочей копии в $CAPTURE_HOME"
    donor_bytes=$("${SSH[@]}" 'test -b /dev/mmcblk2 && blockdev --getsize64 /dev/mmcblk2') \
        || die "не удалось определить размер eMMC донора"
    [ "$donor_bytes" = "$EMMC_BYTES" ] || die "неожиданный размер eMMC донора"
    CAPTURE_DIR=$(mktemp -d -- "$CAPTURE_HOME/acquisition-XXXXXXXX")
    rmdir -- "$WORK"
    WORK="$CAPTURE_DIR/work"
    mkdir -m 700 -- "$WORK"
    FINAL_IMG_KEEP="$WORK/${SHRUNK_NAME}.img"
    CAPTURE_RAW="$CAPTURE_DIR/raw.img"
    CAPTURE_RECEIPT="$CAPTURE_DIR/preflight.json"
    CAPTURE_PASSPORT="$CAPTURE_DIR/passport.json"
    PIPE_RUNTIME_SEAL="$CAPTURE_DIR/seal.json"
    (umask 077; "${SSH[@]}" '/usr/local/libexec/sa02m-runtime-activate capture-receipt' > "$CAPTURE_RECEIPT") \
        || die "донор не выдал квитанцию захвата"
    chmod 400 "$CAPTURE_RECEIPT"
    CAPTURE_SHA=$(sha256sum -- "$CAPTURE_RECEIPT" | cut -d' ' -f1)
    RAW_IMG="$CAPTURE_RAW"
    log "    managed capture: $CAPTURE_DIR"
    log "    захват квитанции: $CAPTURE_SHA"
elif [ -r "$FIX_DONOR_SCRIPT" ]; then
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

if [ "$PIPE_RUNTIME_MODE" = legacy ]; then
    deploy_userspace_watchdog
    prepare_donor_watchdogs_for_imaging
fi

log "    метаданные донора"
DONOR_META="$(collect_donor_metadata)"
export DONOR_META DEVICE_IP RELEASE_PROFILE RELEASE_VERSION
export PIPE_CLEANUP="$DO_CLEANUP" PIPE_ZEROFILL="$DO_ZEROFILL" PIPE_ID_RESET="$DO_ID_RESET" PIPE_XZ_LEVEL="$FINAL_XZ_LEVEL"
export PIPE_RUNTIME_MODE PIPE_RUNTIME_SEAL

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
[ -z "$CAPTURE_SHA" ] || STREAM_ARGS+=(--capture-receipt-sha256 "$CAPTURE_SHA")

log "[2/6] Zero-fill + id reset + dd (одна ssh-сессия, stdout → raw)"
log "    ⚠ после начала dd не прерывайте — новый ssh на доноре будет недоступен до reboot"
# Legacy captures can salvage a full-size stream after SSH errors. Managed
# captures need both complete bytes and a successful remote dd exit.
set +e
(umask 077; "${SSH[@]}" "bash -s -- ${STREAM_ARGS[*]}" < "$STREAM_SCRIPT" > "$RAW_IMG")
SSH_RC=$?
set -e

RAW_SIZE=$(stat -c%s "$RAW_IMG" 2>/dev/null || echo 0)
log "    raw: $(numfmt --to=iec --suffix=B "$RAW_SIZE") (ssh_rc=${SSH_RC})"
[ "$RAW_SIZE" -eq "$EMMC_BYTES" ] || die "размер raw $RAW_SIZE != $EMMC_BYTES (ssh_rc=${SSH_RC})"

if [ "$PIPE_RUNTIME_MODE" = managed ]; then
    [ "$SSH_RC" -eq 0 ] || die "неуспешное чтение eMMC (ssh_rc=$SSH_RC), raw остаётся в $CAPTURE_DIR"
    chmod 400 "$CAPTURE_RAW"
    raw_hash=$(sha256sum -- "$CAPTURE_RAW" | cut -d' ' -f1)
    (umask 077; printf '{"schema":1,"raw_sha256":"%s","receipt_sha256":"%s"}\n' \
        "$raw_hash" "$CAPTURE_SHA" > "$CAPTURE_PASSPORT")
    sync -f "$CAPTURE_DIR"
    log "[3/6] сохранение исходного raw и PiShrink отдельной копии"
    RAW_IMG="$CAPTURE_DIR/derivative.img"
    cp --reflink=auto --sparse=always -- "$CAPTURE_RAW" "$RAW_IMG"
    chmod 600 "$RAW_IMG"
else
    log "[3/6] compress raw (optional archive under WORK) + PiShrink"
    # Keep intermediate raw.xz in WORK.
    xz "-T0" "-${STREAM_XZ_LEVEL}" -v -c "$RAW_IMG" > "$RAW_XZ"
    log "    raw.xz (WORK only): $RAW_XZ ($(numfmt --to=iec --suffix=B "$(stat -c%s "$RAW_XZ")"))"
fi
sudo pishrink.sh -a -v "$RAW_IMG"

# Патч образа: first-boot resize/network/watchdog (см. patch-firstboot-image.sh).
# Шаг БЕЗУСЛОВНЫЙ: наличие скрипта проверено на шаге 0. Прежняя обёртка
# `if [ -x … ]` на свежем клоне (режим 100644) уходила в ветку-заглушку, которая
# boot.scr вообще не собирала и глушила свои ошибки через `return 0` — образ
# уезжал с непроверенным boot.scr донора. Тот же fail-open, ради которого
# и затевалась эта правка.
run_firstboot_patch() {
    local img="$1"
    # Селектор payload передаётся АРГУМЕНТОМ, а не переменной окружения: sudo с
    # env_reset её срезает, а вернуть её через `sudo VAR=value cmd` нельзя —
    # sudoers `setenv` по умолчанию ВЫКЛЮЧЕН, и такая команда просто
    # отклоняется. Аргумент переживает sudo на любом хосте.
    # Через `bash`, а не по x-биту: режим файла в git не должен решать,
    # применён ли патч.
    if [ -n "${SA02M_BOOTCMD:-}" ]; then
        sudo bash "$PATCH_SCRIPT" --bootcmd "$SA02M_BOOTCMD" "$img"
    else
        sudo bash "$PATCH_SCRIPT" "$img"
    fi
}

log "    [patch] patch-firstboot-image.sh"
# НЕ "не критично": патч ставит boot.scr. Проглоченная ошибка здесь дважды
# выпускала образ с невалидным boot.scr (плата не грузится, нужен FEL).
run_firstboot_patch "$RAW_IMG" || \
    die "patch-firstboot-image.sh failed — образ непригоден (boot.scr/first-boot патч не применён)"

if [ "$PIPE_RUNTIME_MODE" = managed ]; then
    log "    подтверждение runtime по исходному raw"
    (umask 077; bash "$SEAL_SCRIPT" "$CAPTURE_RAW" "$RAW_IMG" "$CAPTURE_RECEIPT" "$CAPTURE_PASSPORT" > "$PIPE_RUNTIME_SEAL") \
        || die "не удалось подтвердить runtime образа; исходный raw остался в $CAPTURE_DIR"
fi

FINAL_IMG_WORK="$WORK/$(basename "$FINAL_IMG")"
if [ "$PIPE_RUNTIME_MODE" = managed ] && [ "$DO_ZEROFILL" = 1 ]; then
    log "    очистка свободных блоков запечатанной производной"
    export PIPE_FINAL_FREE_BLOCKS="$CAPTURE_DIR/final-free-blocks.json"
    (umask 077; python3 -I -B "$FREE_BLOCK_SCRIPT" "$RAW_IMG" "$FINAL_IMG_WORK" > "$PIPE_FINAL_FREE_BLOCKS") \
        || die "не удалось подтвердить очистку свободных блоков; исходный raw сохранён в $CAPTURE_DIR"
    rm -- "$RAW_IMG"
else
    mv -T -- "$RAW_IMG" "$FINAL_IMG_WORK"
fi
log "[4/6] final xz (-T0 -${FINAL_XZ_LEVEL})"
SHRUNK_OUT="$SHRUNK_XZ"
FINAL_XZ_TMP="$WORK/$(basename "$SHRUNK_OUT")"
xz "-T0" "-${FINAL_XZ_LEVEL}" -v -c "$FINAL_IMG_WORK" > "$FINAL_XZ_TMP"
SHRUNK_XZ="$FINAL_XZ_TMP"

log "[5/6] sha256"
( cd "$WORK" && sha256sum -- "$(basename "$SHRUNK_XZ")" > "$(basename "$SHRUNK_XZ").sha256" )
SIDECARS=("${SHRUNK_XZ}.sha256")
if [ "$DO_MANIFEST" -eq 1 ]; then
    log "[6/6] manifest.json"
    write_manifest "$SHRUNK_XZ"
    SIDECARS+=("${SHRUNK_XZ%.img.xz}.manifest.json")
else
    log "[6/6] manifest пропущен"
fi

if safe_publish_to_out "$SHRUNK_XZ" "$SHRUNK_OUT" "${SIDECARS[@]}"; then
    SHRUNK_XZ="$SHRUNK_OUT"
else
    log "    using WORK path for final xz: $SHRUNK_XZ"
fi
if [ "$KEEP_RAW_IMG" -eq 1 ]; then
    # FINAL_IMG_WORK ($WORK/<name>.img) and FINAL_IMG_KEEP ($WORK/<name>.img) are the
    # same path — do NOT cp a file onto itself (it errors and, under set -e, aborts).
    [ "$FINAL_IMG_WORK" -ef "$FINAL_IMG_KEEP" ] || cp -f "$FINAL_IMG_WORK" "$FINAL_IMG_KEEP"
    # Keep the raw image with a rescued bundle and avoid large copies to DrvFS.
    if [ "$SHRUNK_XZ" = "$SHRUNK_OUT" ] && ! out_dir_is_drvfs \
        && safe_publish_to_out "$FINAL_IMG_KEEP" "$FINAL_IMG"; then
        : # published
    else
        FINAL_IMG="$FINAL_IMG_KEEP"
        log "    KEEP_RAW_IMG: keeping uncompressed .img under WORK: $FINAL_IMG"
    fi
fi
# Keep the raw .img if FINAL_IMG_WORK ended up being the final kept image
# (drvfs/low-space/publish-failed set FINAL_IMG to this WORK path).
[ "$FINAL_IMG_WORK" -ef "$FINAL_IMG" ] || rm -f "$FINAL_IMG_WORK"

rescue_remaining_artifacts || die "не удалось перенести артефакты из WORK: $WORK"
FINAL_SIZE=$(stat -c%s "$SHRUNK_XZ")
echo
echo "    ════════════════════════════════════════════════════════════════"
echo "    READY: $SHRUNK_XZ"
echo "           размер: $(numfmt --to=iec --suffix=B "$FINAL_SIZE")"
echo "           sha256: $(awk '{print $1}' "${SHRUNK_XZ}.sha256")"
[ "$KEEP_RAW_IMG" -eq 1 ] && echo "           raw img: $FINAL_IMG"
[ "$DO_MANIFEST" -eq 1 ] && echo "           manifest: ${SHRUNK_XZ%.img.xz}.manifest.json"
echo "    ════════════════════════════════════════════════════════════════"
if [ "$PIPE_RUNTIME_MODE" = managed ]; then
    echo "    Квитанция захвата снята. Восстановите сервисную политику донора через заранее открытую сессию."
else
    echo "    После снятия образа донор без ssh host keys — выполните reboot."
fi
echo "    USB: ./prepare-flash-media.sh --image $SHRUNK_XZ [--dest /mnt/c/USB/SA02m]"
