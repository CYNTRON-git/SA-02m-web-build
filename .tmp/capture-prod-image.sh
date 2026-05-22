#!/bin/bash
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="/mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging"
OUT_DIR="${SCRIPT_DIR}/out"
IP="192.168.1.136"
BASE="SA-02m_210526"
SSH_KEY="$HOME/.ssh/sa02m_sa02"
WORK="$HOME/capture_${BASE}"
RAW="${WORK}/${BASE}-raw.img"
FINAL_IMG="${OUT_DIR}/${BASE}.img"
FINAL_XZ="${OUT_DIR}/${BASE}.img.xz"
LOG="${OUT_DIR}/${BASE}.log"
EXPECTED=7818182656
BS=4M
BS_BYTES=$((4 * 1024 * 1024))
CHUNK_BLOCKS=64
CHUNK_BYTES=$((CHUNK_BLOCKS * BS_BYTES))
SKIP_CLEANUP="${SKIP_CLEANUP:-0}"
ROOT_PASS="${ROOT_PASS:-cyntron}"

mkdir -p "$OUT_DIR" "$WORK" ~/.ssh
chmod 700 ~/.ssh
cp /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 "$SSH_KEY"
chmod 600 "$SSH_KEY"
exec >>"$LOG" 2>&1

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

ssh_cmd() {
    if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15 \
        "root@${IP}" "$@" 2>/dev/null; then
        return 0
    fi
    sshpass -p "$ROOT_PASS" ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
        -o ServerAliveInterval=15 "root@${IP}" "$@"
}

wait_ssh() {
    local n=0
    until ssh_cmd "echo ok" >/dev/null 2>&1; do
        n=$((n + 1))
        [ "$n" -ge 120 ] && die "SSH unavailable — reboot donor"
        log "  waiting SSH ($n)..."
        sleep 15
    done
}

log "=== ${BASE} production (WSL) ==="
for bin in ssh sshpass xz pishrink.sh sha256sum python3; do
    command -v "$bin" >/dev/null || die "missing $bin"
done

wait_ssh
ssh_cmd "uname -nrm; df -h /" || true

if [ "$SKIP_CLEANUP" != "1" ]; then
    log "[1] cleanup 1-7"
    ssh_cmd "bash -s" < "${SCRIPT_DIR}/cleanup-donor.sh"
else
    log "[1] cleanup skipped (already done)"
fi

log "[2-4] zerofill + id reset + dd (single SSH stream)"
rm -f "$RAW"
ssh_cmd "bash -s" < "${SCRIPT_DIR}/stream-after-cleanup.sh" > "$RAW"
[ "$(stat -c%s "$RAW")" -eq "$EXPECTED" ] || die "raw size mismatch: $(stat -c%s "$RAW") vs $EXPECTED"

log "[5] PiShrink"
sudo pishrink.sh -a -v "$RAW"

log "[6] -> $FINAL_IMG"
cp -f "$RAW" "$FINAL_IMG"
rm -f "$RAW"; rmdir "$WORK" 2>/dev/null || true

log "[7] xz -9e"
rm -f "$FINAL_XZ"
xz -T0 -9e -k -v -f "$FINAL_IMG" || true

log "[8] sha256"
( cd "$OUT_DIR" && sha256sum "${BASE}.img.xz" > "${BASE}.img.xz.sha256" )

log "DONE"
ls -lh "$FINAL_IMG" "$FINAL_XZ" "${FINAL_XZ}.sha256"
cat "${FINAL_XZ}.sha256"
