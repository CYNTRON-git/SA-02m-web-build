#!/usr/bin/env bash
# journal-on-disk-policy — the repo still carries the journal-on-disk policy and
# the installer still delivers it.
#
# WHY. Since 1.0.6.51 the system journal survives a reboot and a power cut: the
# journald drop-in keeps it on the eMMC and syncs it every minute, and the
# Armbian RAM-log hooks that kept moving it back to RAM are switched off (the
# reasons and the bench measurements live in the headers of the two files
# below — their one home). Until this row the policy was checked only ON A
# BOARD (scripts/dev/verify-release-on-board.sh, check 14), so a commented-out
# `Storage=persistent` or `ENABLED=false` stayed green in the repo and reached
# the next full install (audit 2026-09-24 L3).
#
# WHAT IS READ, and how:
#   etc/systemd/sa02m-journald.conf — as journald reads it: the [Journal]
#     section, `#`/`;` lines ignored, the LAST assignment of a key wins. The
#     effective Storage must be `persistent` and SyncIntervalSec `1m`.
#   etc/default/armbian-ramlog — as the Armbian scripts source it: the last
#     non-comment `ENABLED=` wins, quotes stripped; it must be `false`.
#   scripts/01-system.sh — the one module installing both (install.sh, refresh,
#     offline full update, the golden image): both install lines present in
#     comment-stripped text, the Armbian one FIRST (the module's own comment:
#     the journal stays persistent only with the hooks already off).
# A later override (`Storage=volatile` below the pinned line) is caught because
# the EFFECTIVE value is compared, not the presence of a line.
# NON-VACUOUS: a missing file, a missing [Journal] section or an unset key FAILS.
# Mutation cases (comment-mutation-proof): each value line and each install line
# commented out turns this row RED.
#
# Run: bash .ai-dev/quality/checks/journal-on-disk-policy.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "journal-on-disk-policy: cannot source lib_check.sh"; exit 1; }

fails=0
fail() { printf 'journal-on-disk-policy: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'journal-on-disk-policy: ok    %s\n' "$*"; }

JCONF=etc/systemd/sa02m-journald.conf
RAMLOG=etc/default/armbian-ramlog
INSTALLER=scripts/01-system.sh

# The effective value of <key> in section [<section>] of a systemd INI file:
# the last assignment wins; prints nothing when the key is unset there.
ini_effective() {  # $1=file $2=section $3=key
    awk -v sec="$2" -v key="$3" '
        { sub(/\r$/, "") }
        /^[[:space:]]*[#;]/ || /^[[:space:]]*$/ { next }
        /^[[:space:]]*\[.*\][[:space:]]*$/ {
            s = $0; gsub(/^[[:space:]]*\[|\][[:space:]]*$/, "", s); insec = (s == sec); next
        }
        insec {
            line = $0; eq = index(line, "="); if (!eq) next
            k = substr(line, 1, eq - 1); v = substr(line, eq + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", k); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            if (k == key) { val = v; seen = 1 }
        }
        END { if (seen) print val }
    ' "$1"
}

if [ ! -f "$JCONF" ]; then
    fail "$JCONF missing — the journal policy has no home"
else
    if ! grep -qE '^[[:space:]]*\[Journal\][[:space:]]*$' "$JCONF"; then
        fail "$JCONF has no [Journal] section — journald would ignore every key"
    fi
    v=$(ini_effective "$JCONF" Journal Storage)
    [ "$v" = persistent ] && pass "$JCONF: effective Storage=persistent" \
        || fail "$JCONF: effective Storage is '${v:-unset}', not persistent — the journal would not survive a reboot"
    v=$(ini_effective "$JCONF" Journal SyncIntervalSec)
    [ "$v" = 1m ] && pass "$JCONF: effective SyncIntervalSec=1m" \
        || fail "$JCONF: effective SyncIntervalSec is '${v:-unset}', not 1m — a power cut loses more than a minute"
fi

if [ ! -f "$RAMLOG" ]; then
    fail "$RAMLOG missing — the Armbian RAM-log hooks would stay on"
else
    v=$(awk '
        { sub(/\r$/, "") }
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*ENABLED=/ { v = $0; sub(/^[[:space:]]*ENABLED=/, "", v); gsub(/["'"'"'[:space:]]/, "", v); val = v; seen = 1 }
        END { if (seen) print val }
    ' "$RAMLOG")
    [ "$v" = false ] && pass "$RAMLOG: effective ENABLED=false" \
        || fail "$RAMLOG: effective ENABLED is '${v:-unset}', not false — the hooks would move the journal back to RAM"
fi

if [ ! -f "$INSTALLER" ]; then
    fail "$INSTALLER missing — nothing installs the policy"
else
    RL_NEEDLE='sa02m_atomic_install -m 644 "$ETC_REPO/default/armbian-ramlog" /etc/default/armbian-ramlog'
    JD_NEEDLE='sa02m_atomic_install -m 644 "$ETC_REPO/systemd/sa02m-journald.conf"'
    rl_line=$(stripped_first_line "$INSTALLER" 'sa02m_atomic_install -m 644 "\$ETC_REPO/default/armbian-ramlog" /etc/default/armbian-ramlog')
    jd_line=$(stripped_first_line "$INSTALLER" 'sa02m_atomic_install -m 644 "\$ETC_REPO/systemd/sa02m-journald\.conf"')
    [ -n "$rl_line" ] && pass "$INSTALLER installs $RAMLOG (line $rl_line)" \
        || fail "$INSTALLER no longer runs: $RL_NEEDLE"
    [ -n "$jd_line" ] && pass "$INSTALLER installs $JCONF (line $jd_line)" \
        || fail "$INSTALLER no longer runs: $JD_NEEDLE"
    if [ -n "$rl_line" ] && [ -n "$jd_line" ]; then
        [ "$rl_line" -lt "$jd_line" ] && pass "$INSTALLER turns the Armbian hooks off BEFORE installing the journald drop-in" \
            || fail "$INSTALLER installs the journald drop-in (line $jd_line) before turning the Armbian hooks off (line $rl_line)"
    fi
fi

if [ "$fails" -eq 0 ]; then
    echo "journal-on-disk-policy: ALL OK"
    exit 0
fi
echo "journal-on-disk-policy: $fails check(s) failed"
exit 1
