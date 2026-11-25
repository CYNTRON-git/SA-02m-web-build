#!/bin/sh
# Standalone external-boot receiver; see docs/contracts/image-receiver-safety.md.
# IMG_DIR, IMG_NAME and TARGET_DEV keep the workshop interface. No guard bypass.
set -eu
umask 077

LOG="/var/log/flash-receiver.log"
PROC_MOUNTINFO="/proc/self/mountinfo"
PROC_SWAPS="/proc/swaps"
SYS_DEV_BLOCK="/sys/dev/block"
SYS_DEVICES="/sys/devices"
PROC_SELF_MNT="/proc/self/ns/mnt"
PROC_INIT_MNT="/proc/1/ns/mnt"
RUNTIME_DIR="/run"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$LOG" >&2; }
WRITE_STARTED=0
producer_pid=
writer_pid=
die() {
    log "FATAL: $*"
    if [ "$WRITE_STARTED" = 1 ]; then
        log 'Target may contain a partial image; do not boot it. Restore from verified external media.'
    fi
    exit 1
}
is_block() { [ -b "$1" ]; }
valid_id() {
    case "$1" in ''|*[!0-9:]*|:*|*:|*:*:*) return 1;; esac
    case "$1" in *:*) return 0;; *) return 1;; esac
}
block_id() {
    is_block "$1" || return 1
    _b_hex=$(stat -c '%t:%T' "$1") || return 1
    case "$_b_hex" in ''|*[!0-9a-fA-F:]*|:*|*:|*:*:*) return 1;; esac
    case "$_b_hex" in *:*) ;; *) return 1;; esac
    printf '%d:%d\n' "0x${_b_hex%:*}" "0x${_b_hex#*:}"
}
sys_node() {
    valid_id "$1" || return 1
    _n_path=$(readlink -f "$SYS_DEV_BLOCK/$1") || return 1
    case "$_n_path" in "$SYS_DEVICES"/*) ;; *) return 1;; esac
    [ -d "$_n_path" ] && [ -r "$_n_path/dev" ] || return 1
    [ "$(cat "$_n_path/dev")" = "$1" ] || return 1
    printf '%s\n' "$_n_path"
}
check_graph() {
    printf '%s 0\n' "$1" > "$LOCK/queue"
    _g_seen=' '
    _g_count=0
    while read -r _g_id _g_depth; do
        case "$_g_seen" in *" $_g_id "*) continue;; esac
        _g_count=$((_g_count + 1))
        [ "$_g_count" -le 128 ] && [ "$_g_depth" -le 32 ] || {
            log 'Block dependency traversal limit exceeded'; return 1;
        }
        _g_seen="$_g_seen$_g_id "
        _g_node=$(sys_node "$_g_id") || { log "Unresolved block identity $_g_id"; return 1; }
        _g_parent=$_g_node
        while [ "$_g_parent" != "$SYS_DEVICES" ]; do
            case "$_g_parent" in "$SYS_DEVICES"/*) ;; *) return 1;; esac
            if [ "$_g_parent" = "$TARGET_SYS" ]; then
                log "Target backs an active filesystem or swap: $_g_id"; return 1
            fi
            _g_parent=${_g_parent%/*}
        done
        for _g_slave in "$_g_node"/slaves/*; do
            [ -e "$_g_slave" ] || [ -L "$_g_slave" ] || continue
            _g_real=$(readlink -f "$_g_slave") || return 1
            case "$_g_real" in "$SYS_DEVICES"/*) ;; *) return 1;; esac
            [ -r "$_g_real/dev" ] || { log 'Missing slave identity'; return 1; }
            _g_child=$(cat "$_g_real/dev") || return 1
            valid_id "$_g_child" || return 1
            [ "$(sys_node "$_g_child")" = "$_g_real" ] || return 1
            printf '%s %s\n' "$_g_child" "$((_g_depth + 1))" >> "$LOCK/queue"
        done
    done < "$LOCK/queue"
}
guard_topology() {
    _t_canon=$(readlink -f "$TARGET_DEV") || return 1
    [ "$_t_canon" = "$CANON" ] || { log 'Target path changed'; return 1; }
    [ "$(block_id "$CANON")" = "$TARGET_ID" ] || { log 'Target identity changed'; return 1; }
    [ "$(sys_node "$TARGET_ID")" = "$TARGET_SYS" ] || return 1
    [ ! -e "$TARGET_SYS/partition" ] || { log 'Target must be a whole disk'; return 1; }
    [ -L "$PROC_SELF_MNT" ] && [ -L "$PROC_INIT_MNT" ] || { log 'Mount namespace metadata unavailable'; return 1; }
    _t_self=$(readlink "$PROC_SELF_MNT") || return 1
    _t_init=$(readlink "$PROC_INIT_MNT") || return 1
    [ -n "$_t_self" ] && [ "$_t_self" = "$_t_init" ] || { log 'Receiver requires the host mount namespace'; return 1; }
    [ -r "$PROC_MOUNTINFO" ] && [ -r "$PROC_SWAPS" ] || { log 'Mount/swap inventory unavailable'; return 1; }
    awk '
      NF < 10 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+:[0-9]+$/ || $4 !~ /^\// || $5 !~ /^\// { bad=1; exit }
      { sep=0; for(i=7;i<=NF;i++) if($i=="-"){sep=i;break}
        if(!sep || NF<sep+3){bad=1;exit}
        if($5=="/") root=1
        print $3, $(sep+1)
      }
      END { if(bad || !NR || !root) exit 1 }
    ' "$PROC_MOUNTINFO" > "$LOCK/mounts" || { log 'Malformed mount inventory'; return 1; }
    while read -r _m_id _m_type; do
        case "$_m_id" in 0:*)
            if [ ! -e "$SYS_DEV_BLOCK/$_m_id" ]; then
                case "$_m_type" in rootfs|tmpfs|ramfs|proc|sysfs|devtmpfs|devpts|cgroup|cgroup2|debugfs|tracefs|securityfs|pstore|configfs|mqueue|hugetlbfs|bpf|fusectl|binfmt_misc|rpc_pipefs|nfs|nfs4|cifs|9p) continue;; esac
                log "Unresolved filesystem backing: $_m_type"; return 1
            fi;;
        esac
        check_graph "$_m_id" || return 1
    done < "$LOCK/mounts"
    awk 'NR==1 {if($1!="Filename" || $2!="Type" || NF!=5) bad=1; next}
         NF!=5 || $3 !~ /^[0-9]+$/ || $4 !~ /^[0-9]+$/ {bad=1;exit}
         {print $1, $2}
         END {if(bad || !NR) exit 1}' "$PROC_SWAPS" > "$LOCK/swaps" || return 1
    while read -r _s_path _s_type; do
        [ "$_s_type" = partition ] || { log 'Active swapfile backing cannot be proved; disable swap first'; return 1; }
        _s_canon=$(readlink -f "$_s_path") || return 1
        _s_id=$(block_id "$_s_canon") || return 1
        check_graph "$_s_id" || return 1
    done < "$LOCK/swaps"
}
cleanup() {
    _c_rc=$?
    trap - 0 HUP INT TERM
    if [ -n "$writer_pid" ]; then kill "$writer_pid" 2>/dev/null || true; wait "$writer_pid" 2>/dev/null || true; fi
    if [ -n "$producer_pid" ]; then kill "$producer_pid" 2>/dev/null || true; wait "$producer_pid" 2>/dev/null || true; fi
    rm -f "$LOCK/stream" "$LOCK/queue" "$LOCK/mounts" "$LOCK/swaps"
    rmdir "$LOCK" || _c_rc=1
    exit "$_c_rc"
}

[ "$(id -u)" = 0 ] || { printf 'Receiver requires root\n' >&2; exit 1; }
for tool in readlink dirname stat awk cat sha256sum xz dd sync mkfifo mkdir rmdir rm date tee grep sleep reboot; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'Missing receiver tool: %s\n' "$tool" >&2; exit 1; }
done
mkdir -p "$(dirname "$LOG")"
LOCK="$RUNTIME_DIR/sa02m-flash-receiver.lock"
mkdir "$LOCK" 2>/dev/null || die 'Receiver operation lock exists or runtime directory is unavailable'
trap cleanup 0
trap 'die "Interrupted by HUP"' HUP
trap 'die "Interrupted by INT"' INT
trap 'die "Interrupted by TERM"' TERM

_SELF=$(readlink -f "$0") || die 'Cannot resolve receiver path'
IMG_DIR="${IMG_DIR:-$(dirname "$_SELF")}"
IMG_NAME="${IMG_NAME:-sa02m-shrunk.img.xz}"
TARGET_DEV="${TARGET_DEV:-/dev/mmcblk2}"
case "$IMG_NAME" in ''|*/*|*\\*|*'
'*) die 'Image name must be one ordinary basename';; esac
[ "${#IMG_NAME}" -le 255 ] || die 'Image name too long'
if printf '%s' "$IMG_NAME" | LC_ALL=C grep '[[:cntrl:]]' >/dev/null; then die 'Control character in image name'; fi
IMG="$IMG_DIR/$IMG_NAME"
SHA="$IMG.sha256"
CANON=$(readlink -f "$TARGET_DEV") || die 'Cannot resolve target'
TARGET_ID=$(block_id "$CANON") || die 'Target is not a readable block device'
TARGET_SYS=$(sys_node "$TARGET_ID") || die 'Target sysfs identity unavailable'
guard_topology || die 'Target preflight refused'
[ -f "$IMG" ] && [ -r "$IMG" ] || die 'Image missing or unreadable'
[ -f "$SHA" ] && [ -r "$SHA" ] || die 'Checksum sidecar is mandatory and must be readable'
IMG_CANON=$(readlink -f "$IMG") || die 'Cannot resolve image'
IMG_META=$(stat -Lc '%d:%i:%s:%Y:%Z' "$IMG_CANON") || die 'Cannot inspect image'
EXP=$(awk -v name="$IMG_NAME" '
    NR!=1 {bad=1;exit}
    { hash=substr($0,1,64); mark=substr($0,65,2)
      if(length(hash)!=64 || hash !~ /^[0-9a-fA-F]+$/ || (mark!="  " && mark!=" *") || substr($0,67)!=name){bad=1;exit}
    }
    END {if(bad || NR!=1) exit 1; print tolower(hash)}' "$SHA") || die 'Malformed checksum sidecar or wrong filename'
log '[1/4] Verifying image checksum and compressed stream'
GOT=$(sha256sum "$IMG_CANON") || die 'Checksum tool/read failed'
GOT=${GOT%% *}
[ "$EXP" = "$GOT" ] || die 'Image checksum mismatch'
xz -t "$IMG_CANON" 2>>"$LOG" || die 'Compressed image failed integrity test'

log '[2/4] Preparing external-boot receiver'
rmmod -f g_mass_storage 2>/dev/null || true
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog sa02m-watchdog-feed nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl mask "$svc" 2>/dev/null || true
done
guard_topology || die 'Target changed during image verification'
[ "$(readlink -f "$IMG")" = "$IMG_CANON" ] && [ "$(stat -Lc '%d:%i:%s:%Y:%Z' "$IMG_CANON")" = "$IMG_META" ] || die 'Image changed during verification'
mkfifo "$LOCK/stream" || die 'Cannot create image FIFO'
log "[3/4] Writing verified image to $CANON"
WRITE_STARTED=1
xz -dc "$IMG_CANON" > "$LOCK/stream" 2>>"$LOG" &
producer_pid=$!
dd if="$LOCK/stream" of="$CANON" bs=4M conv=fsync 2>>"$LOG" &
writer_pid=$!
if wait "$writer_pid"; then writer_pid=; else
    _write_rc=$?; writer_pid=; die "Image writer failed (status $_write_rc)"
fi
if wait "$producer_pid"; then producer_pid=; else
    _xz_rc=$?; producer_pid=; die "Image decompressor failed (status $_xz_rc)"
fi
sync || die 'Final sync failed'
log '[4/4] Image write completed; rebooting'
sleep 2
reboot || die 'Reboot command failed'
