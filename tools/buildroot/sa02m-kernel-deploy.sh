#!/bin/bash
# SA-02m: деплой эталонных zImage SMP/RT и модулей на устройство
# Usage:
#   sa02m-kernel-deploy.sh install-rt  /path/to/zImage [/path/to/modules-6.1.0-rc6-rt4.tgz]
#   sa02m-kernel-deploy.sh install-smp /path/to/zImage [/path/to/modules-6.1.0-rc6.tgz]
#   sa02m-kernel-deploy.sh init
#   sa02m-kernel-deploy.sh status
set -euo pipefail

CANON_DIR=/usr/local/share/sa02m/kernel
FAT_DEV=/dev/mmcblk2p1
FAT_MNT=/mnt/fat
LOG=/var/log/sa02m_install.log
SELECT=/usr/local/sbin/sa02m-kernel-select.sh

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-kernel-deploy: $*" | tee -a "$LOG" 2>/dev/null || echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-kernel-deploy: $*"; }

zimage_ok() {
    local f=$1 sz
    [ -f "$f" ] || return 1
    sz=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
    [ "$sz" -ge 5000000 ] && [ "$sz" -le 12000000 ]
}

install_zimage() {
    local profile=$1 src=$2
    local dst="$CANON_DIR/zImage.$profile"
    mkdir -p "$CANON_DIR"
    if ! zimage_ok "$src"; then
        log "ERROR: invalid zImage size: $src"
        return 1
    fi
    cp -f "$src" "$dst"
    chmod 644 "$dst"
    local md5
    md5=$(md5sum "$dst" | awk '{print $1}')
    log "installed $dst md5=$md5 size=$(stat -c '%s' "$dst")"
}

install_modules_tgz() {
    local tgz=$1 ver=$2
    [ -f "$tgz" ] || { log "WARN: modules archive missing: $tgz"; return 0; }
    tar -xzf "$tgz" -C /lib/modules
    log "extracted modules to /lib/modules/$ver from $tgz"
}

mirror_fat() {
    local profile=$1
    local src="$CANON_DIR/zImage.$profile"
    [ -f "$src" ] || return 0
    mkdir -p "$FAT_MNT"
    if ! mountpoint -q "$FAT_MNT" 2>/dev/null; then
        mount "$FAT_DEV" "$FAT_MNT" || return 1
        FAT_MOUNTED=1
    fi
    cp -f "$src" "$FAT_MNT/zImage.$profile"
    sync
    log "FAT mirror: $FAT_MNT/zImage.$profile"
    [ "${FAT_MOUNTED:-0}" = "1" ] && umount "$FAT_MNT" 2>/dev/null || true
}

write_manifest() {
    local f="$CANON_DIR/manifest.json"
    python3 - <<'PY' >"$f" 2>/dev/null || true
import json, os, hashlib, datetime
base = "/usr/local/share/sa02m/kernel"
out = {"updated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "images": {}}
for prof in ("smp", "rt"):
    p = os.path.join(base, f"zImage.{prof}")
    if os.path.isfile(p):
        h = hashlib.md5()
        with open(p, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
        out["images"][prof] = {"path": p, "size": os.path.getsize(p), "md5": h.hexdigest()}
print(json.dumps(out, indent=2))
PY
    chmod 644 "$f" 2>/dev/null || true
}

cmd_install_rt() {
    local zimg=${1:-} mod=${2:-}
    [ -n "$zimg" ] || { log "usage: install-rt ZIMAGE [modules.tgz]"; return 1; }
    install_zimage rt "$zimg"
    [ -n "$mod" ] && install_modules_tgz "$mod" "6.1.0-rc6-rt4"
    mirror_fat rt
    write_manifest
}

cmd_install_smp() {
    local zimg=${1:-} mod=${2:-}
    [ -n "$zimg" ] || { log "usage: install-smp ZIMAGE [modules.tgz]"; return 1; }
    install_zimage smp "$zimg"
    [ -n "$mod" ] && install_modules_tgz "$mod" "6.1.0-rc6"
    mirror_fat smp
    write_manifest
}

cmd_init() {
    if [ -x "$SELECT" ]; then
        "$SELECT" init
    fi
    write_manifest
}

cmd_status() {
    ls -la "$CANON_DIR" 2>/dev/null || true
    [ -x "$SELECT" ] && "$SELECT" status
}

main() {
    case "${1:-status}" in
        install-rt) shift; cmd_install_rt "$@" ;;
        install-smp) shift; cmd_install_smp "$@" ;;
        init) cmd_init ;;
        status) cmd_status ;;
        *)
            echo "Usage: $0 {install-rt|install-smp|init|status}"
            return 1
            ;;
    esac
}

main "$@"
