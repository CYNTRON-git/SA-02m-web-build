#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-clock-set.sh — behavioural regression for the pinned root clock helper
# usr/local/sbin/sa02m-clock-set.sh (sudoers: etc/sudoers.d/sa02m-www).
#
# Why this exists (audit 2026-11-11 A8-01): www-data held `date`, `hwclock`
# and `timedatectl` as root with NO argument pin. The grant's comment called it
# «set-only; clock DoS at worst», but `date -f <file>` reads any root-readable
# file line by line into its error output and `hwclock --adjfile=<path>` writes
# one — a web session had root file read and a root file write. The helper is
# the only clock capability left: exactly three forms, argv validated INSIDE the
# helper (it must hold when invoked directly with any argv).
#
# Method: the SHIPPED helper is copied into a scratch tree with its two
# single-assignment constants retargeted — PATH (so `timedatectl` / `date -s`
# resolve to recording shims; `date -d` passes through to the real date for the
# range check) and ZONEINFO (a sandbox zoneinfo with Europe/Moscow and
# Asia/Yekaterinburg) — and proven to differ from the original in nothing else.
#
# Cases:
#   C1  `tz Europe/Moscow` -> exactly `timedatectl set-timezone Europe/Moscow`.
#   C2  `ntp-off` -> exactly `timedatectl set-ntp false`.
#   C3  `time "2026-11-11 12:00:00"` -> exactly `timedatectl set-time 2026-11-11 12:00:00`.
#   C4  set-time failing -> falls back to `date -s 2026-11-11 12:00:00`, exit 0.
#   C5  refused with exit 2 and NO clock call: `-f /etc/shadow`, `tz ../../x`,
#       `tz -H`, `tz Europe/Nowhere`, `tz` with an extra argument, `tz` alone,
#       `time 2026-13-01 00:00:00`, `time 2026-02-30 10:00:00`, the date and
#       the time as two arguments, `time -s 2026-…`, `ntp-off x`, `hwclock`,
#       no argument at all, an unknown verb.
#
# RED: on the tree before the helper existed the helper file is missing — the
# harness FAILS (non-vacuity), it never passes on nothing. Runs on any host with
# bash + GNU date (Linux CI, WSL, git-bash).
# Run: bash scripts/dev/test-clock-set.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${CLOCK_SET_SRC:-usr/local/sbin/sa02m-clock-set.sh}
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC — the pinned clock helper does not exist"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
SH="$T/shim"; ZI="$T/zoneinfo"
mkdir -p "$SH" "$ZI/Europe" "$ZI/Asia"
: > "$ZI/Europe/Moscow"; : > "$ZI/Asia/Yekaterinburg"
REAL_DATE=$(command -v date)
export SHIM_LOG="$T/calls.log" REAL_DATE

H="$T/clock-set.sh"
sed -e "s#^PATH=/usr/sbin:/usr/bin:/sbin:/bin\$#PATH=$SH:/usr/sbin:/usr/bin:/sbin:/bin#" \
    -e "s#^ZONEINFO=/usr/share/zoneinfo\$#ZONEINFO=$ZI#" "$SRC" > "$H"
grep -q "^PATH=$SH:" "$H" || { echo "FAIL  retarget missed the PATH line"; exit 1; }
grep -q "^ZONEINFO=$ZI\$" "$H" || { echo "FAIL  retarget missed the ZONEINFO line"; exit 1; }
if ! sed -e "s#^PATH=$SH:#PATH=#" -e "s#^ZONEINFO=$ZI\$#ZONEINFO=/usr/share/zoneinfo#" "$H" | cmp -s - "$SRC"; then
    echo "FAIL  the helper copy differs from $SRC beyond the retargeted lines"; exit 1
fi

cat > "$SH/timedatectl" <<'EOF'
#!/bin/bash
printf 'timedatectl %s\n' "$*" >> "$SHIM_LOG"
[ "${1:-}" = set-time ] && [ -n "${SHIM_SETTIME_FAIL:-}" ] && exit 1
exit 0
EOF
cat > "$SH/date" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "-s" ]; then printf 'date %s\n' "$*" >> "$SHIM_LOG"; exit 0; fi
exec "$REAL_DATE" "$@"
EOF
chmod +x "$SH"/*

run() { : > "$SHIM_LOG"; bash "$H" "$@" > "$T/out" 2> "$T/err"; rc=$?; }
calls() { tr '\n' '|' < "$SHIM_LOG"; }

run tz Europe/Moscow
[ "$rc" = 0 ] && [ "$(calls)" = "timedatectl set-timezone Europe/Moscow|" ] \
    && ok "C1: tz Europe/Moscow -> timedatectl set-timezone Europe/Moscow" \
    || bad "C1: rc=$rc calls=$(calls)"
run ntp-off
[ "$rc" = 0 ] && [ "$(calls)" = "timedatectl set-ntp false|" ] \
    && ok "C2: ntp-off -> timedatectl set-ntp false" || bad "C2: rc=$rc calls=$(calls)"
run time "2026-11-11 12:00:00"
[ "$rc" = 0 ] && [ "$(calls)" = "timedatectl set-time 2026-11-11 12:00:00|" ] \
    && ok "C3: time '2026-11-11 12:00:00' -> timedatectl set-time 2026-11-11 12:00:00" || bad "C3: rc=$rc calls=$(calls)"
SHIM_SETTIME_FAIL=1 run time "2026-11-11 12:00:00"
[ "$rc" = 0 ] && [ "$(calls)" = "timedatectl set-time 2026-11-11 12:00:00|date -s 2026-11-11 12:00:00|" ] \
    && ok "C4: set-time failing -> date -s fallback with the same validated value" || bad "C4: rc=$rc calls=$(calls)"

c5_bad=0; n_ref=0
refuse_case() {
    n_ref=$((n_ref + 1))
    run "$@"
    if [ "$rc" != 2 ] || [ -s "$SHIM_LOG" ]; then
        bad "C5: not refused (rc=$rc, calls=$(calls)): $*"; c5_bad=1
    fi
}
refuse_case -f /etc/shadow
refuse_case tz ../../x
refuse_case tz -H
refuse_case tz Europe/Nowhere
refuse_case tz Europe/Moscow extra
refuse_case tz
refuse_case tz '/etc/shadow'
refuse_case time "2026-13-01 00:00:00"
refuse_case time "2026-02-30 10:00:00"
refuse_case time 2026-11-11 12:00:00
refuse_case time -s "2026-11-11 12:00:00"
refuse_case time "2026-11-11 12:00:00 +1 day"
refuse_case ntp-off x
refuse_case hwclock --adjfile=/etc/shadow
refuse_case
refuse_case set-timezone Europe/Moscow
[ "$c5_bad" = 0 ] && ok "C5: $n_ref malformed/foreign forms refused with exit 2 and no clock call"

echo
if [ "$fails" = 0 ]; then
    echo "test-clock-set: all cases passed"
    exit 0
fi
echo "test-clock-set: $fails case(s) FAILED"
exit 1
