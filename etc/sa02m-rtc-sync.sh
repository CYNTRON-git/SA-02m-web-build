#!/bin/bash
# sa02m-rtc-sync.sh — sync system clock → DS3231 (rtc1) + fake-hwclock.
#
# Called by:
#   sa02m-rtc-sync.timer  — every 30 min (first at 5 min after boot)
#   sa02m-pre-start.service ExecStop — at shutdown/reboot
#
# Algorithm:
#   1. Write current system time to DS3231 via hwclock --systohc (verbose
#      mode ensures proper clock-tick alignment; retry once on failure).
#   2. Save fake-hwclock as secondary backup.
#   3. Log NTP sync status (stratum) for diagnostics.
set -u

log() { logger -t sa02m-rtc-sync -- "$*" 2>/dev/null || true; }

HWC=$(command -v hwclock 2>/dev/null || true)
[ -n "$HWC" ] || { log "hwclock not found — skip"; exit 0; }

# ── NTP sync status ───────────────────────────────────────────────────────
STRATUM=$(chronyc tracking 2>/dev/null | awk '/^Stratum[[:space:]]*:/{print $3; exit}')
if [ -n "$STRATUM" ] && [ "$STRATUM" -gt 0 ] && [ "$STRATUM" -lt 15 ] 2>/dev/null; then
    SYNC_INFO="NTP synced stratum=${STRATUM}"
else
    SYNC_INFO="NTP not synced"
fi

# ── Write system time → DS3231 ───────────────────────────────────────────
if [ -c /dev/rtc1 ]; then
    # --verbose ensures hwclock waits properly for the RTC tick boundary;
    # without it the ioctl can silently fail on a loaded system.
    HWC_OUT=$("$HWC" --systohc --rtc /dev/rtc1 --verbose 2>&1)
    HWC_RC=$?
    if [ "$HWC_RC" -ne 0 ]; then
        # One retry after a short pause (tick-alignment miss on busy system)
        sleep 1
        HWC_OUT=$("$HWC" --systohc --rtc /dev/rtc1 --verbose 2>&1)
        HWC_RC=$?
    fi
    echo "$HWC_OUT" | logger -t sa02m-rtc-sync || true
    if [ "$HWC_RC" -eq 0 ]; then
        log "DS3231 updated (${SYNC_INFO}) — $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    else
        log "DS3231 update FAILED rc=${HWC_RC} (${SYNC_INFO})"
    fi
else
    log "/dev/rtc1 not available — DS3231 skip (${SYNC_INFO})"
fi

# ── fake-hwclock backup ───────────────────────────────────────────────────
fake-hwclock save 2>/dev/null \
    && log "fake-hwclock saved" \
    || log "fake-hwclock save failed"
