#!/bin/bash
# sa02m-clock-set.sh — set the system time zone / clock as root, three forms only.
# Usage: sa02m-clock-set.sh tz <Area/Zone>
#        sa02m-clock-set.sh ntp-off
#        sa02m-clock-set.sh time "<YYYY-MM-DD HH:MM:SS>"
# Called via sudo from apply.cgi (www-data) — the panel's «Сеть / Время» save.
#
# SECURITY (audit 2026-11-11 A8-01): www-data used to hold `date`, `hwclock` and
# `timedatectl` as root with no argument pin. That was not «clock DoS at
# worst»: `date -f <file>` reads any root-readable file into its error output
# and `hwclock --adjfile=<path>` writes one. This helper is now the only clock
# capability in sudoers, and it validates its OWN argv — the sudoers line
# carries no arguments, so www-data chooses them; any form but the three above,
# any extra argument, an argument starting with `-`, a zone outside the
# zoneinfo tree or a time that does not round-trip through `date -d` is refused
# with exit 2 before anything runs. No eval, no heredoc, no argument reaches a
# shell word unquoted. hwclock is not offered: no caller needs it as root
# (lib_rtc.sh writes the RTC without sudo).
#
# Home: usr/local/sbin/ so the file deploys path-identically on every delivery
# path. Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh; behaviour:
# scripts/dev/test-clock-set.sh.
set -u

PATH=/usr/sbin:/usr/bin:/sbin:/bin
ZONEINFO=/usr/share/zoneinfo

TZ_RE='^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$'
TIME_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$'

refuse() {
    printf 'sa02m-clock-set: refused: %s\n' "$1" >&2
}

# A zone name in the allow-listed alphabet (no `.`, so no `..`; no leading `-`)
# that names a file in the zoneinfo tree.
tz_ok() {
    [[ "$1" =~ $TZ_RE ]] || return 1
    [ -f "$ZONEINFO/$1" ]
}

# Strict text form AND a real calendar time: `date -d` must read it back to the
# same text (2026-13-01 and 2026-02-30 do not survive the round trip).
time_ok() {
    local back
    [[ "$1" =~ $TIME_RE ]] || return 1
    back=$(date -d "$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null) || return 1
    [ "$back" = "$1" ]
}

verb=${1:-}
case "$verb" in
    tz)
        if [ "$#" -ne 2 ]; then refuse "tz takes exactly one zone"; exit 2; fi
        if ! tz_ok "$2"; then refuse "not a zone under $ZONEINFO"; exit 2; fi
        timeout 8 timedatectl set-timezone "$2"
        exit $?
        ;;
    ntp-off)
        if [ "$#" -ne 1 ]; then refuse "ntp-off takes no argument"; exit 2; fi
        timeout 6 timedatectl set-ntp false
        exit $?
        ;;
    time)
        if [ "$#" -ne 2 ]; then refuse "time takes exactly one 'YYYY-MM-DD HH:MM:SS'"; exit 2; fi
        if ! time_ok "$2"; then refuse "not a valid 'YYYY-MM-DD HH:MM:SS'"; exit 2; fi
        # timedatectl first (keeps systemd-timesyncd's view consistent); date -s
        # when timedatectl is missing or wedged — same order apply.cgi used.
        if timeout 18 timedatectl set-time "$2" >/dev/null 2>&1; then exit 0; fi
        if timeout 10 date -s "$2" >/dev/null 2>&1; then exit 0; fi
        exit 1
        ;;
    *)
        refuse "unknown form (allowed: tz <zone> | ntp-off | time 'YYYY-MM-DD HH:MM:SS')"
        exit 2
        ;;
esac
