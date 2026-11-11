#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-apply-time.sh — behavioural regression for the time / time-zone half of
# www/network_config/cgi-bin/apply.cgi («Сеть / Время» → «Применить вручную»).
#
# Why this exists (audit 2026-11-11 A4-05, A8-02):
#   * A4-05 — the form sent «2026-11-11 12:00:00» meant in the zone the Operator
#     was LOOKING at (the device's zone before the save). apply.cgi switched the
#     zone first and then set that same wall-clock text in the NEW zone: a
#     Moscow -> Samara save with a time moved the clock by one hour. And a
#     zone-only save re-ran `timedatectl set-timezone` even for the zone already
#     in force;
#   * A8-02 — `timeout_run 12 sync_rtc_from_system`: `timeout` cannot run a
#     shell function (127, swallowed by `|| true`), so after a manual time set
#     the battery RTC was NEVER written, and the next cold boot restored the old
#     time from it.
#
# Method (idiom of test-iface-conf-write.sh W13–W18): the SHIPPED apply.cgi runs
# in a sandbox with real lib_web_validate.sh, a retargeted lib_net_iface.sh and
# the REAL lib_rtc.sh (retargeted: /sys/bus/i2c/devices/ -> a sandbox tree with
# one ds3231 at 1-0068, /dev/rtc1 -> absent), so the RTC write goes the i2c way
# a board without /dev/rtc1 takes; auth/CSRF are stubbed (their own rows gate
# them). Shims on PATH: `sudo` (records the clock act, applies a zone change to
# a fake «/etc/localtime» file), `timedatectl` (answers `show -p Timezone`),
# `date` (the real date under TZ=<fake zone>), `i2cset` (records). The sudo shim
# normalises both clock call forms — the bare `timedatectl`/`date -s` grants and
# the pinned helper `sa02m-clock-set.sh tz|ntp-off|time` (B9) — to one record
# (SET-TZ / NTP-OFF / SET-TIME), so the cases test the behaviour, not the form.
#
# Cases:
#   T1  zone-only change, empty time -> SET-TZ, no SET-TIME, no NTP-OFF, no RTC
#       write (the clock and NTP are left alone).
#   T2  Moscow -> Samara + «2026-11-11 12:00:00» -> SET-TIME «2026-11-11
#       13:00:00» (the same instant in the new zone).     RED: 12:00:00
#   T3  the zone already in force -> no SET-TZ.            RED: SET-TZ issued
#   T4  a time that `date -d` cannot read -> status error_time, no SET-TIME, no
#       NTP-OFF (the zone is still applied, as before).    RED: set-time issued
#   T5  after a time set the RTC is written (i2cset to 1-0068 regs 0x00..0x06).
#                                                         RED: never written
# Needs Linux (GNU date + tzdata for Europe/Moscow and Europe/Samara); anything
# else is SKIP via lib_skip.sh. Run: bash scripts/dev/test-apply-time.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

APPLY_SRC=${APPLY_CGI_SRC:-www/network_config/cgi-bin/apply.cgi}
CGI=www/network_config/cgi-bin
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  lib_skip.sh not loadable"; exit 1; }

for f in "$APPLY_SRC" "$CGI/lib_web_validate.sh" "$CGI/lib_net_iface.sh" "$CGI/lib_rtc.sh"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done
if [ "$(uname -s)" != Linux ] || [ ! -e /usr/share/zoneinfo/Europe/Samara ] \
   || ! TZ=Europe/Samara date -d '2026-11-11 12:00:00' +%s >/dev/null 2>&1; then
    quality_skip "T1-T5 apply.cgi time sandbox: needs Linux + GNU date + tzdata ($(uname -s)) — CI is the authoritative run"
    echo; echo "test-apply-time: skipped"; exit 77
fi
REAL_DATE=$(command -v date)

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
E="$T/cgi"; B="$T/bin"; I2C="$T/i2c"
mkdir -p "$E" "$B" "$I2C/1-0068"
echo ds3231 > "$I2C/1-0068/name"
LOGF="$T/install.log"

sed -e "s#/var/log/sa02m_install\.log#$LOGF#g" "$APPLY_SRC" > "$E/apply.cgi"
grep -q '/var/log/sa02m_install\.log' "$E/apply.cgi" && { echo "FAIL  apply.cgi retarget left the real log"; exit 1; }
sed -e "s#/sys/class/net/#$T/sys/#g" -e "s#/etc/network/interfaces\.d/#$T/ifd/#g" \
    "$CGI/lib_net_iface.sh" > "$E/lib_net_iface.sh"
sed -e "s#/sys/bus/i2c/devices/#$I2C/#g" -e "s#/dev/rtc1#$T/no-rtc1#g" -e "s#/dev/rtc\\b#$T/no-rtc#g" \
    "$CGI/lib_rtc.sh" > "$E/lib_rtc.sh"
grep -qF "$I2C/" "$E/lib_rtc.sh" || { echo "FAIL  lib_rtc.sh retarget missed /sys/bus/i2c/devices/"; exit 1; }
grep -q '/dev/rtc1' "$E/lib_rtc.sh" && { echo "FAIL  lib_rtc.sh retarget left /dev/rtc1"; exit 1; }
cp "$CGI/lib_web_validate.sh" "$E/"
printf '%s\n' 'web_session_check_cookie() { return 0; }' 'web_csrf_require() { :; }' > "$E/lib_web_auth.sh"

export SHIM_LOG="$T/acts.log" FAKE_TZ="$T/localtime" REAL_DATE
cat > "$B/sudo" <<'EOF'
#!/bin/bash
[ "${1:-}" = "-n" ] && shift
cmd=${1:-}; shift || true
case "${cmd##*/}" in
    timedatectl)
        case "${1:-}" in
            set-timezone) echo "SET-TZ $2" >> "$SHIM_LOG"; printf '%s\n' "$2" > "$FAKE_TZ" ;;
            set-ntp) echo "NTP-OFF $2" >> "$SHIM_LOG" ;;
            set-time) echo "SET-TIME $2" >> "$SHIM_LOG" ;;
            *) echo "SUDO-OTHER timedatectl $*" >> "$SHIM_LOG"; exit 1 ;;
        esac ;;
    date) [ "${1:-}" = "-s" ] && echo "SET-TIME $2" >> "$SHIM_LOG" ;;
    sa02m-clock-set.sh)
        case "${1:-}" in
            tz) echo "SET-TZ $2" >> "$SHIM_LOG"; printf '%s\n' "$2" > "$FAKE_TZ" ;;
            ntp-off) echo "NTP-OFF false" >> "$SHIM_LOG" ;;
            time) echo "SET-TIME $2" >> "$SHIM_LOG" ;;
            *) echo "SUDO-OTHER clock-set $*" >> "$SHIM_LOG"; exit 2 ;;
        esac ;;
    *) echo "SUDO-OTHER $cmd $*" >> "$SHIM_LOG"; exit 1 ;;
esac
exit 0
EOF
cat > "$B/timedatectl" <<'EOF'
#!/bin/bash
if [ "$*" = "show -p Timezone --value" ]; then cat "$FAKE_TZ"; exit 0; fi
echo "TIMEDATECTL-NOSUDO $*" >> "$SHIM_LOG"
exit 1
EOF
cat > "$B/date" <<'EOF'
#!/bin/bash
TZ=$(cat "$FAKE_TZ") exec "$REAL_DATE" "$@"
EOF
cat > "$B/i2cset" <<'EOF'
#!/bin/bash
echo "I2CSET $*" >> "$SHIM_LOG"
exit 0
EOF
cat > "$B/nohup" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$B"/*
for c in sudo timedatectl date i2cset; do
    [ "$(PATH="$B:$PATH" command -v "$c")" = "$B/$c" ] || { echo "FAIL  shim $c does not resolve first"; exit 1; }
done

post() {   # $1 = current zone, $2 = urlencoded body
    printf '%s\n' "$1" > "$FAKE_TZ"; : > "$SHIM_LOG"; : > "$LOGF"
    printf '%s' "$2" > "$T/body"
    REQUEST_METHOD=POST CONTENT_LENGTH=${#2} PATH="$B:$PATH" \
        bash "$E/apply.cgi" < "$T/body" > "$T/out" 2> "$T/err"; rc=$?
}
acts() { tr '\n' '|' < "$SHIM_LOG"; }

# ── T1: zone only ─────────────────────────────────────────────────────────
post Europe/Moscow 'skip_network=1&timezone=Europe%2FSamara&datetime='
if [ "$rc" = 0 ] && grep -qx 'SET-TZ Europe/Samara' "$SHIM_LOG" && ! grep -qE '^(SET-TIME|NTP-OFF)' "$SHIM_LOG" \
   && ! grep -q '^I2CSET' "$SHIM_LOG"; then
    ok "T1: zone-only save -> SET-TZ only; clock, NTP and RTC untouched"
else
    bad "T1: zone-only save -> $(acts)"
fi

# ── T2: zone change + time = the same instant in the new zone ─────────────
post Europe/Moscow 'skip_network=1&timezone=Europe%2FSamara&datetime=2026-11-11+12%3A00%3A00'
if grep -qx 'SET-TIME 2026-11-11 13:00:00' "$SHIM_LOG" && grep -qx 'SET-TZ Europe/Samara' "$SHIM_LOG"; then
    ok "T2: Moscow -> Samara + 12:00:00 (meant in Moscow) -> set-time 13:00:00 Samara (same instant)"
else
    bad "T2: zone + time -> $(acts)"
fi

# ── T3: zone unchanged -> no set-timezone ─────────────────────────────────
post Europe/Moscow 'skip_network=1&timezone=Europe%2FMoscow&datetime=2026-11-11+12%3A00%3A00'
if ! grep -q '^SET-TZ' "$SHIM_LOG" && grep -qx 'SET-TIME 2026-11-11 12:00:00' "$SHIM_LOG"; then
    ok "T3: the zone already in force -> no set-timezone; the time is set as typed"
else
    bad "T3: same zone -> $(acts)"
fi

# ── T4: unreadable time -> error_time, nothing set ────────────────────────
post Europe/Moscow 'skip_network=1&timezone=Europe%2FSamara&datetime=not-a-time'
if grep -q 'error_time' "$T/out" && ! grep -qE '^(SET-TIME|NTP-OFF)' "$SHIM_LOG"; then
    ok "T4: a time date -d cannot read -> error_time, neither clock nor NTP touched"
else
    bad "T4: unreadable time -> out='$(tr '\n' '|' < "$T/out")' acts=$(acts)"
fi

# ── T5: the battery RTC is written after a manual time set ────────────────
post Europe/Moscow 'skip_network=1&timezone=Europe%2FMoscow&datetime=2026-11-11+12%3A00%3A00'
n_regs=$(grep -cE '^I2CSET -y 1 0x0068 0x0[0-6] ' "$SHIM_LOG")
if [ "$n_regs" -eq 7 ] && ! grep -q 'RTC write after manual time set FAILED' "$LOGF"; then
    ok "T5: after the time set the DS3231 is written (7 registers on bus 1 @0x68, through the real lib_rtc.sh)"
else
    bad "T5: RTC write after a time set — $n_regs register write(s); acts=$(acts)"
fi

echo
if [ "$fails" = 0 ]; then
    echo "test-apply-time: all cases passed"
    exit 0
fi
echo "test-apply-time: $fails case(s) FAILED"
exit 1
