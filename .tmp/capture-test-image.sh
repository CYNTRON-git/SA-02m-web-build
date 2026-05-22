#!/bin/bash
set -euo pipefail
LC_ALL=C

OUT_DIR="/mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging/out"
KEY='C:/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02'
IP="192.168.1.136"
BASE="SA-02m_test_210526"
RAW="${OUT_DIR}/${BASE}.img"
OUT="${OUT_DIR}/${BASE}.img.xz"
LOG="${OUT_DIR}/${BASE}.log"
EXPECTED=7818182656
BS=4M
BS_BYTES=$((4 * 1024 * 1024))
CHUNK_BLOCKS=64
CHUNK_BYTES=$((CHUNK_BLOCKS * BS_BYTES))

SSH=(/mnt/c/Windows/System32/OpenSSH/ssh.exe -i "$KEY"
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
     -o ServerAliveInterval=15 -o ServerAliveCountMax=9999
     -o TCPKeepAlive=yes
     "root@${IP}")

mkdir -p "$OUT_DIR"
exec >>"$LOG" 2>&1

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; echo "[$(date +%H:%M:%S)] $*" >&2; }

log "=== chunked capture (no cleanup) ==="

current=0
if [ -f "$RAW" ]; then
    current=$(stat -c%s "$RAW")
    aligned=$(( current / CHUNK_BYTES * CHUNK_BYTES ))
    if [ "$aligned" -ne "$current" ]; then
        truncate -s "$aligned" "$RAW"
        current=$aligned
        log "aligned partial raw to $(numfmt --to=iec --suffix=B "$current")"
    else
        log "resume from $(numfmt --to=iec --suffix=B "$current")"
    fi
fi

if [ "$current" -ge "$EXPECTED" ]; then
    log "raw already complete"
else
    log "SSH check"
    "${SSH[@]}" "uname -nrm; sync"

    if [ "$current" -eq 0 ]; then
        log "stop services"
        "${SSH[@]}" "systemctl stop nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm 2>/dev/null || true; rmmod -f g_mass_storage 2>/dev/null || true; sync; sync" || true
    fi

    while [ "$current" -lt "$EXPECTED" ]; do
        skip=$((current / BS_BYTES))
        remaining=$((EXPECTED - current))
        blocks=$(( (remaining + BS_BYTES - 1) / BS_BYTES ))
        [ "$blocks" -gt "$CHUNK_BLOCKS" ] && blocks=$CHUNK_BLOCKS

        log "chunk skip=${skip} count=${blocks} ($(numfmt --to=iec --suffix=B "$current") / $(numfmt --to=iec --suffix=B "$EXPECTED"))"

        n=0
        until "${SSH[@]}" "dd if=/dev/mmcblk2 bs=${BS} skip=${skip} count=${blocks} status=none" >>"$RAW"; do
            n=$((n + 1))
            [ "$n" -ge 5 ] && { log "ERROR: chunk failed after 5 retries"; exit 1; }
            aligned=$(( $(stat -c%s "$RAW") / CHUNK_BYTES * CHUNK_BYTES ))
            truncate -s "$aligned" "$RAW"
            log "retry chunk ($n), truncated to $(numfmt --to=iec --suffix=B "$aligned")"
            sleep 3
            current=$(stat -c%s "$RAW")
            skip=$((current / BS_BYTES))
            remaining=$((EXPECTED - current))
            blocks=$(( (remaining + BS_BYTES - 1) / BS_BYTES ))
            [ "$blocks" -gt "$CHUNK_BLOCKS" ] && blocks=$CHUNK_BLOCKS
        done

        current=$(stat -c%s "$RAW")
        [ "$current" -gt "$EXPECTED" ] && truncate -s "$EXPECTED" "$RAW"
        current=$(stat -c%s "$RAW")
    done
fi

log "raw complete: $(numfmt --to=iec --suffix=B "$current")"

log "xz -> ${BASE}.img.xz"
xz -T0 -1 -v -c "$RAW" > "$OUT"
rm -f "$RAW"

log "sha256"
( cd "$OUT_DIR" && sha256sum "${BASE}.img.xz" > "${BASE}.img.xz.sha256" )

log "DONE"
ls -lh "$OUT" "${OUT}.sha256"
cat "${OUT}.sha256"
