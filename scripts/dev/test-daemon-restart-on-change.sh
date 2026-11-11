#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-daemon-restart-on-change.sh — a long-lived daemon whose script or unit an
# install/update replaced must be RESTARTED, or the old code runs until reboot.
#
# Why this exists (bench .135, D36-02): scripts/01-system.sh applied the userspace
# watchdog with `sa02m_svc_apply … infra start`. The script is replaced atomically
# (rename-over), the running bash keeps the old inode, and `start` on an active
# unit does nothing — so the 1.0.7.36 heal-only fix (Operator decisions J-1/J-2)
# did not reach an upgraded board until its next reboot: the old watchdog sat
# 110 s on a dead fcgiwrap and would still have rebooted the board for nginx.
# The same shape sat on sa02m-failure-monitor (03-webserver.sh) and net-watchdog
# (02-network.sh), and on the GitHub OTA, whose manifest restarted neither.
#
# Method:
#   A  (behavioural) the SHIPPED install block + unit loop of scripts/01-system.sh
#      (from the `_wd_sig0=` capture to the NetworkManager section) runs in a
#      sandbox: install helpers are stubs that copy files, `sa02m_svc_apply`
#      records its arguments, the system paths are sed-retargeted, sa02m_file_sig
#      is the real function extracted from scripts/lib.sh.
#        W1 first install                       -> `… infra start restart`
#        W2 installed copy identical            -> `… infra start` (no restart)
#        W3 installed script differs            -> `… infra start restart`
#        W4 installed unit differs              -> `… infra start restart`
#        W5 the other two units in the loop stay `infra` (their restart lives in
#           their own modules, case B)
#   B  (static, comment-stripped) 03-webserver.sh (failure monitor) and
#      02-network.sh (net-watchdog) capture the signature BEFORE their first
#      install line, read it again BEFORE the apply and pass `restart` only inside
#      the `!=` branch.
#   C  (static) the GitHub OTA manifest (etc/sa02m-update-runner.sh) and the
#      offline packer (scripts/pack-offline-update.py) both list the watchdog and
#      the failure monitor under restart_if_active (restarted only when already
#      running), in step with each other.
#
# RED on bb42f86f: W1/W3/W4 print `infra start` without restart, B and C find no
# signature / no entries. Run: bash scripts/dev/test-daemon-restart-on-change.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  lib_check.sh not loadable"; exit 1; }

SYS=scripts/01-system.sh
LIB=scripts/lib.sh
WEB=scripts/03-webserver.sh
NET=scripts/02-network.sh
RUNNER=etc/sa02m-update-runner.sh
PACK=scripts/pack-offline-update.py
for f in "$SYS" "$LIB" "$WEB" "$NET" "$RUNNER" "$PACK" etc/sa02m-userspace-watchdog.sh etc/systemd/sa02m-userspace-watchdog.service; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

# ── A: the shipped watchdog block of 01-system.sh ─────────────────────────
sig_fn=$(awk 'index($0,"sa02m_file_sig() {")==1{f=1} f{print} f&&/^\}/{exit}' "$LIB")
start=$(grep -n '^_wd_sig0=\$(' "$SYS" | head -n 1 | cut -d: -f1)
stop=$(grep -n '^# ── Маскировка NetworkManager' "$SYS" | head -n 1 | cut -d: -f1)
if [ -z "$sig_fn" ] || [ -z "$start" ] || [ -z "$stop" ] || [ "$start" -ge "$stop" ]; then
    bad "A: cannot extract the watchdog block (sa02m_file_sig=${#sig_fn}B start=${start:-none} stop=${stop:-none}) — the install does not capture a signature before it writes the daemon"
else
    SB="$T/sb"
    block=$(sed -n "${start},$((stop - 1))p" "$SYS" \
        | sed -e "s#/usr/local/sbin/#$SB/usr/local/sbin/#g" \
              -e "s#/etc/systemd/system/#$SB/etc/systemd/system/#g" \
              -e "s#/etc/sa02m_userspace_watchdog\.conf#$SB/etc/sa02m_userspace_watchdog.conf#g")
    n_re=$(printf '%s\n' "$block" | grep -cF "$SB/")
    if [ "$n_re" -lt 6 ]; then
        bad "A: the retarget hit only $n_re path(s) of the block — extraction guard"
    else
        mkdir -p "$T/repo/systemd"
        cp etc/sa02m-userspace-watchdog.sh "$T/repo/sa02m-userspace-watchdog.sh"
        cp etc/systemd/sa02m-userspace-watchdog.service "$T/repo/systemd/sa02m-userspace-watchdog.service"
        cp etc/sa02m_userspace_watchdog.conf "$T/repo/sa02m_userspace_watchdog.conf"
        run_block() {   # $1 = scenario dir name → recorded sa02m_svc_apply lines
            local sc="$T/$1" rec="$T/$1.rec"
            : > "$rec"
            (
                SB="$T/sb"; export SB
                ETC_REPO="$T/repo"; LOG_FILE=/dev/null
                REC="$rec"
                log() { :; }
                sa02m_systemctl() { :; }
                sa02m_atomic_install() {   # [-m MODE] src dst
                    local src dst
                    while [ "${1:-}" = -m ] || [ "${1:-}" = -o ] || [ "${1:-}" = -g ]; do shift 2; done
                    src=$1; dst=$2
                    mkdir -p "$(dirname "$dst")"; cp -f "$src" "$dst"
                }
                sa02m_svc_apply() { printf '%s\n' "$*" >> "$REC"; }
                eval "$sig_fn"
                eval "$block"
            ) >/dev/null 2>&1
            return 0
        }
        reset_sb() { rm -rf "$T/sb"; mkdir -p "$T/sb/usr/local/sbin" "$T/sb/etc/systemd/system"; }
        inst_same() {
            cp etc/sa02m-userspace-watchdog.sh "$T/sb/usr/local/sbin/sa02m-userspace-watchdog"
            cp etc/systemd/sa02m-userspace-watchdog.service "$T/sb/etc/systemd/system/sa02m-userspace-watchdog.service"
            : > "$T/sb/etc/sa02m_userspace_watchdog.conf"
        }
        wd_line() { grep -E '^sa02m-userspace-watchdog\.service infra' "$T/$1.rec" | tr '\n' '|'; }

        reset_sb; run_block w1
        [ "$(wd_line w1)" = "sa02m-userspace-watchdog.service infra start restart|" ] \
            && ok "A/W1: first install -> infra start restart (a no-op restart of an inactive unit is a start)" \
            || bad "A/W1: first install applied as '$(wd_line w1)'"
        reset_sb; inst_same; run_block w2
        [ "$(wd_line w2)" = "sa02m-userspace-watchdog.service infra start|" ] \
            && ok "A/W2: installed script and unit identical -> infra start only (a running watchdog is not bounced)" \
            || bad "A/W2: unchanged files applied as '$(wd_line w2)'"
        reset_sb; inst_same; printf '# old heal-and-reboot code\n' >> "$T/sb/usr/local/sbin/sa02m-userspace-watchdog"; run_block w3
        [ "$(wd_line w3)" = "sa02m-userspace-watchdog.service infra start restart|" ] \
            && ok "A/W3: an older script on the board -> infra start restart (the heal-only code reaches the running process)" \
            || bad "A/W3: an old script was applied as '$(wd_line w3)' — the running watchdog keeps the old code until reboot"
        reset_sb; inst_same; printf '# old unit\n' >> "$T/sb/etc/systemd/system/sa02m-userspace-watchdog.service"; run_block w4
        [ "$(wd_line w4)" = "sa02m-userspace-watchdog.service infra start restart|" ] \
            && ok "A/W4: an older unit file on the board -> infra start restart" \
            || bad "A/W4: an old unit was applied as '$(wd_line w4)'"
        others=$(grep -E '^sa02m-failure-monitor\.service|^net-watchdog\.service' "$T/w3.rec" | tr '\n' '|')
        [ "$others" = "sa02m-failure-monitor.service infra|net-watchdog.service infra|" ] \
            && ok "A/W5: the other two units of the loop stay enable-only here (their own modules restart them, case B)" \
            || bad "A/W5: the loop's other units are applied as '$others'"
    fi
fi

# ── B: failure monitor (03-webserver.sh) and net-watchdog (02-network.sh) ──
check_site() {   # <label> <file> <sigvar-prefix> <first-install-ERE> <apply-unit>
    local label=$1 f=$2 p=$3 first=$4 unit=$5 txt l0 lfirst l1 lapply lrestart
    txt=$(stripped_text "$f")
    l0=$(grep -nE "^${p}_sig0=\\$\\(" <<<"$txt" | head -n 1 | cut -d: -f1)
    lfirst=$(grep -nE "$first" <<<"$txt" | head -n 1 | cut -d: -f1)
    l1=$(grep -nE "^${p}_sig1=\\$\\(" <<<"$txt" | head -n 1 | cut -d: -f1)
    lapply=$(grep -nE "sa02m_svc_apply ${unit} infra start\$" <<<"$txt" | tail -n 1 | cut -d: -f1)
    lrestart=$(grep -nE "sa02m_svc_apply ${unit} infra start restart\$" <<<"$txt" | head -n 1 | cut -d: -f1)
    if [ -n "$l0" ] && [ -n "$lfirst" ] && [ "$l0" -lt "$lfirst" ] && [ -n "$l1" ] && [ -n "$lrestart" ] \
       && [ "$l1" -lt "$lrestart" ] && [ -n "$lapply" ] \
       && text_has "$txt" "if [ \"\$${p}_sig0\" != \"\$${p}_sig1\" ]; then"; then
        ok "B/$label: signature captured (line $l0) before the first install line ($lfirst), re-read (line $l1), '$unit … infra start restart' only in the != branch"
    else
        bad "B/$label: $f does not restart $unit when its script/unit changed (sig0=${l0:-none} first-install=${lfirst:-none} sig1=${l1:-none} restart-line=${lrestart:-none} plain-apply=${lapply:-none})"
    fi
}
check_site "failure-monitor" "$WEB" _fm 'sa02m_atomic_install -m 755 "\$ETC_DIR/sa02m-failure-monitor\.sh"' sa02m-failure-monitor.service
check_site "net-watchdog" "$NET" _nw 'sa02m_atomic_install -m 755 "\$ETC_DIR/net-watchdog\.sh"' net-watchdog.service

# ── C: the GitHub OTA manifest and the offline packer, in step ─────────────
# restart_if_active (not restart_if_changed: its validator takes directory
# prefixes ending in '/', not a file) — the units are always on, an inactive
# one is never started by it.
for u in sa02m-userspace-watchdog sa02m-failure-monitor; do
    for f in "$RUNNER" "$PACK"; do
        region=$(stripped_text "$f" | awk '/restart_if_active.*\[/{f=1; print; next} f{print} f&&/^[[:space:]]*\]/{exit}')
        if text_has "$region" "\"$u\","; then
            ok "C: $f lists $u under restart_if_active"
        else
            bad "C: $f has no restart_if_active entry '$u' — an update replacing it leaves the old process running"
        fi
    done
done

echo
if [ "$fails" = 0 ]; then
    echo "test-daemon-restart-on-change: all cases passed"
    exit 0
fi
echo "test-daemon-restart-on-change: $fails case(s) FAILED"
exit 1
