#!/bin/bash
# sa02m-rtc-sync.sh — sync system clock → DS3231 (rtc1) + fake-hwclock.
#
# Called by:
#   sa02m-rtc-sync.timer  — every 30 min (first at 5 min after boot)
#   sa02m-pre-start.service ExecStop — at shutdown/reboot
#
# Algorithm:
#   1. Write current system time to external RTC via hwclock (/dev/rtc1) or
#      I2C fallback (DS3231/PCF8563) when kernel driver is absent.
#   2. Save fake-hwclock as secondary backup.
#   3. Log NTP sync status (stratum) for diagnostics.
set -u

log() { logger -t sa02m-rtc-sync -- "$*" 2>/dev/null || true; }

sa02m_rtc_load_lib() {
    local f
    for f in /usr/local/lib/sa02m-lib-rtc.sh /var/www/network_config/cgi-bin/lib_rtc.sh; do
        if [ -f "$f" ]; then
            # shellcheck source=/dev/null
            . "$f"
            return 0
        fi
    done
    return 1
}

# ── NTP sync status ───────────────────────────────────────────────────────
STRATUM=$(chronyc tracking 2>/dev/null | awk '/^Stratum[[:space:]]*:/{print $3; exit}')
if [ -n "$STRATUM" ] && [ "$STRATUM" -gt 0 ] && [ "$STRATUM" -lt 15 ] 2>/dev/null; then
    SYNC_INFO="NTP synced stratum=${STRATUM}"
else
    SYNC_INFO="NTP not synced"
fi

# ── Write system time → external RTC ─────────────────────────────────────
if sa02m_rtc_load_lib && sync_rtc_from_system; then
    log "RTC updated via I2C/hwclock (${SYNC_INFO}) — $(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    log "RTC update FAILED or lib missing (${SYNC_INFO})"
fi

# ── fake-hwclock backup ───────────────────────────────────────────────────
fake-hwclock save 2>/dev/null \
    && log "fake-hwclock saved" \
    || log "fake-hwclock save failed"
