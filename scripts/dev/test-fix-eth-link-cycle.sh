#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-fix-eth-link-cycle.sh — regression test for the no-carrier branch of
# etc/fix-eth.sh `recover_iface` (the static-IP recovery ladder, run by udev
# on add/change and by net-watchdog every 30 s).
#
# Why this exists: a board booted WITHOUT its eth0 cable spends the five
# link cycles (MAX_LINK_CYCLES) inside ~4.5 min of boot. The eth0 PHY (IP101A
# under the kernel's generic PHY driver) does not restart autonegotiation by
# itself when a partner appears later, so a cable plugged after cycle 5 left
# the board off the LAN until a reboot — the ladder logged «cycles=5, пропуск»
# every 30 s and did nothing (bench 1.135, 2026-10-28; docs/bugs/BUGLOG.md).
# Since 1.0.7.12 the exhausted branch restarts autonegotiation on every pass
# while the port is admin UP (one MDIO restart, no down/up, no sleeps). That
# is a BEHAVIOUR of a script no syntax gate can see, so it is pinned here.
#
# Method, in the idiom of test-iface-dns-ensure.sh: copy the SHIPPED script
# into a scratch tree and sed-retarget its roots (/sys/class/net,
# /etc/network/interfaces.d, /etc/sa02m_network.conf, LOG_FILE, LOCK_DIR,
# STATE_DIR) into it; `ip`, `mii-tool`, `ethtool`, `ping`, `systemctl` and
# `sleep` are recording PATH shims (sleep is a no-op, so no budget is really
# waited). No root, no device, no systemd, no python — bash + coreutils only,
# so it runs in Windows git-bash as well as on Linux CI. A host `mii-tool` / `python3` /
# `ethtool` is hidden through scripts/dev/lib_path_mask.sh so the «not
# installed» case is the harness's call on every host. There is deliberately
# NO env seam in the shipped script — the retarget is the test's job.
# (No `logger` shim: fix-eth.sh never calls logger; it echoes to its own log.)
#
# Cases (each pass = one `fix-eth.sh eth0` run, as udev / the watchdog do):
#   C1  carrier=0, admin UP, operstate=down, passes 1-5: each pass does the
#       full link cycle (ip link down, ip link up, mii-tool -r) and bumps the
#       counter to 5.
#   C2  passes 6 and 7 (budget spent): NO ip link down/up, NO sleep, exactly
#       one `mii-tool -r eth0` per pass, its stdout silenced, counter stays 5,
#       log says «перезапуск автосогласования».   <- RED on the pre-1.0.7.12
#       script (0 mii-tool calls, «пропуск»), GREEN after.
#   C3  carrier=1 + IP + default route: counter file removed, no restart,
#       no link set; the carrier-up path ran (systemctl + ping consulted).
#   C4  admin DOWN (flags=0x1002), budget spent — and budget unspent: nothing
#       is touched (no ip link set, no mii-tool, no ethtool), «пропуск».
#   C5  mii-tool not installed, budget spent: `ethtool -r eth0` instead.
#   C6  carrier=0 at entry, admin UP, budget unspent, address present, no
#       default route; the link comes up DURING the link cycle (the `ip`
#       shim flips the sandbox carrier on `ip link set eth0 up`). The SAME
#       pass must re-read carrier, log «линк появился после link cycle 1»,
#       remove the counter and run the carrier-up path — `ip route add
#       default via 192.168.1.1 dev eth0`. On a board nothing else reacts
#       to link-up: a carrier change emits no uevent, so the carrier=1 udev
#       rule never starts fix-eth@, and the routes the cycle's
#       `ip link set down` flushed waited 65 s for net-watchdog (bench 1.135,
#       2026-10-30; docs/bugs/BUGLOG.md).   <- RED on the 1.0.7.19 script
#       (no route add, counter=1, no log line), GREEN after.
#
# Non-vacuous: a failed or over-wide retarget, a leftover real root on an
# executable line, a missing script, or a shim that was never invoked across
# the whole run FAILS rather than passing on zero matches.
#
# Drive-to-failure recipe (how this harness was proven to be a real ratchet):
#   git show 9bf0ab60:etc/fix-eth.sh > /tmp/fix-eth.orig      # pre-fix script
#   FIX_ETH_SRC=/tmp/fix-eth.orig bash scripts/dev/test-fix-eth-link-cycle.sh
#   -> 4 FAIL: C2 pass 6, C2 pass 7 (0 mii-tool calls, log says «пропуск»),
#   C5 (no ethtool fallback either) and the «ethtool shim was never invoked»
#   witness; C1, C3, C4 pass. Commenting the exhausted-branch `mii-tool -r`
#   line out of a copy gives the same 4 FAIL (registered in
#   comment-mutation-proof). The override never edits the real tree.
#   C6, against the 1.0.7.19 script (`git show b68f8aca:etc/fix-eth.sh`):
#   1 FAIL — «C6: linkset=2 route_add=0 counter=1 log_line=0» (the pass
#   returns after the cycle; C1-C5 pass). Commenting the
#   `carrier_up "$iface" && _link_after_cycle=1` line out of a copy gives the
#   same single C6 FAIL (registered in comment-mutation-proof).
#
# Off-board only: /usr/local/{bin,sbin} helpers the script probes with -x / -f
# (sa02m-eth0-led.sh, sa02m-dns-ensure.sh, sa02m-routes-ensure.sh,
# sa02m-grat-arp.py) are NOT retargeted — absent on CI and dev hosts they are
# skipped, but on a board the harness would drive the real LED/DNS/route
# helpers. Do not run it on a board.
#
# Run: bash scripts/dev/test-fix-eth-link-cycle.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${FIX_ETH_SRC:-etc/fix-eth.sh}

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }
# shellcheck source=scripts/dev/lib_path_mask.sh
. scripts/dev/lib_path_mask.sh || { echo "FAIL  scripts/dev/lib_path_mask.sh not loadable"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

CD="$T/ifd"                       # /etc/network/interfaces.d
SYS="$T/sysnet"                  # /sys/class/net (a name without that substring, so the leftover scan below cannot match the sandbox itself)
NETCONF="$T/sa02m_network.conf"
LOGF="$T/fix-eth.log"
LOCK="$T/lockdir"                 # LOCK_DIR (/run/fix-eth): the link_cycle_count file lives here
STATE="$T/statedir"               # STATE_DIR (/run/fix-eth-state)
BIN="$T/bin"
OUT="$T/run.out"                  # stdout+stderr of the last pass
CALLS="$T/calls.log"              # reset per pass
CALLS_ALL="$T/calls-all.log"      # never reset — the harness non-vacuity witness
mkdir -p "$CD" "$SYS" "$BIN"

# ── retarget the SHIPPED script into the sandbox ───────────────────────────
FIX="$T/fix-eth.sh"
sed -e "s#^LOG_FILE=\"/var/log/fix-eth\.log\"#LOG_FILE=\"$LOGF\"#" \
    -e "s#^LOCK_DIR=\"/run/fix-eth\"#LOCK_DIR=\"$LOCK\"#" \
    -e "s#^STATE_DIR=\"/run/fix-eth-state\"#STATE_DIR=\"$STATE\"#" \
    -e "s#/etc/network/interfaces\.d#$CD#g" \
    -e "s#/etc/sa02m_network\.conf#$NETCONF#g" \
    -e "s#/sys/class/net#$SYS#g" \
    "$SRC" > "$FIX"
chmod +x "$FIX"

# Non-vacuity: every root MUST have been replaced. An un-retargeted script
# would read the real /sys and /etc (and write /run, /var/log) — that is the
# one failure this harness must never pass through. The leftover scan reads
# only the part of a line before any `#`: the shipped comments mention
# /etc/resolv.conf and /run/, which is text, not a path the script opens.
for pat in "LOG_FILE=\"$LOGF\"" "LOCK_DIR=\"$LOCK\"" "STATE_DIR=\"$STATE\"" \
           "$CD/\${iface}.conf" "$NETCONF" "$SYS/\${iface}/carrier"; do
    grep -Fq "$pat" "$FIX" || { echo "FAIL  retarget missed in fix-eth: $pat"; exit 1; }
done
if grep -Eq '^[^#]*(/sys/class/net|/etc/network|/etc/sa02m_|/var/log/|/run/fix-eth)' "$FIX"; then
    echo "FAIL  fix-eth copy still holds a real root on an executable line"; exit 1
fi

# ── PATH shims ─────────────────────────────────────────────────────────────
# `ip` answers `addr show` / `route show` from per-interface state files
# (.addr / .routes next to the sysfs stand-ins); every other form is recorded
# and succeeds; `ip link set <iface> up` also brings that iface's sandbox
# carrier to 1 while $FLIP names it (C6 only — empty otherwise, so C1 keeps
# carrier 0 through its cycles). `mii-tool -r` prints what the real one prints on stdout, so
# the «silenced on the exhausted path» rule is observable. `ping` always
# fails (a gateway that never answered is the isolated-LAN case, logged once
# and not an outage). `systemctl` reports every unit inactive/dead, so the
# carrier-up path does not believe ifupdown is still running. `sleep` is a
# recorded no-op: C1 would otherwise wait 25 real seconds.
FLIP="$T/carrier-flip"             # C6: holds an iface name; its carrier goes 1 on `ip link set <iface> up`
cat > "$BIN/ip" <<SHIM
#!/bin/bash
echo "ip \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
if [ "\$1 \$2 \$4" = "link set up" ] && [ -s "$FLIP" ] && [ "\$3" = "\$(cat "$FLIP")" ]; then
    printf '1\n' > "$SYS/\$3/carrier"
fi
for _a; do _last=\$_a; done
case "\$*" in
  "-4 addr show dev "*) [ -f "$SYS/\$_last/.addr" ] && cat "$SYS/\$_last/.addr" ;;
  "route show "*)       [ -f "$SYS/\$_last/.routes" ] && cat "$SYS/\$_last/.routes" ;;
esac
exit 0
SHIM
cat > "$BIN/mii-tool" <<SHIM
#!/bin/bash
echo "mii-tool \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
echo "restarting autonegotiation..."
exit 0
SHIM
cat > "$BIN/ethtool" <<SHIM
#!/bin/bash
echo "ethtool \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
exit 0
SHIM
cat > "$BIN/ping" <<SHIM
#!/bin/bash
echo "ping \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
exit 1
SHIM
cat > "$BIN/systemctl" <<SHIM
#!/bin/bash
echo "systemctl \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
case "\$*" in
  *ActiveState*) echo inactive ;;
  *SubState*)    echo dead ;;
esac
exit 0
SHIM
cat > "$BIN/sleep" <<SHIM
#!/bin/bash
echo "sleep \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
exit 0
SHIM
chmod +x "$BIN"/*
# Hide a host mii-tool/ethtool (net-tools ships on CI runners): once the
# mii-tool shim is removed for C5, `command -v` must find NOTHING, not the
# host's binary — which, run unprivileged, fails and falls through to the
# ethtool shim anyway, passing C5 for the wrong reason. python3 is hidden too
# (the plan's «python3 absent»): the carrier-up path's grat-ARP burst is
# guarded by `command -v python3` AND the helper file, so no host interpreter
# can ever be reached from here, whatever the host carries in /usr/local/bin.
MASKED=$(path_without "$T/mask" mii-tool ethtool python3) || { echo "FAIL  path_without could not mask the host mii-tool/ethtool/python3"; exit 1; }
PATH="$BIN:$MASKED"; export PATH
command -v mii-tool | grep -Fq "$BIN/mii-tool" || { echo "FAIL  the mii-tool shim is not first on PATH"; exit 1; }
command -v python3 >/dev/null 2>&1 && { echo "FAIL  a python3 is still on PATH after the mask ($(command -v python3)) — the mask failed"; exit 1; }

# ── fixtures ───────────────────────────────────────────────────────────────
COUNTER="$LOCK/eth0.link_cycle_count"
seed() {   # $1 iface, $2 carrier, $3 flags (hex, as sysfs prints it), $4 operstate
    mkdir -p "$SYS/$1"
    printf '%s\n' "$2" > "$SYS/$1/carrier"
    printf '%s\n' "$3" > "$SYS/$1/flags"
    printf '%s\n' "$4" > "$SYS/$1/operstate"
    rm -f "$SYS/$1/.addr" "$SYS/$1/.routes"
}
conf_static() {   # $1 iface
    { printf 'auto %s\niface %s inet static\n    address 192.168.1.135\n' "$1" "$1"
      printf '    netmask 255.255.255.0\n    gateway 192.168.1.1\n'
    } > "$CD/$1.conf"
}
run_pass() {   # one recovery pass for $1, as udev / net-watchdog invoke it
    : > "$CALLS"
    "$FIX" "$1" > "$OUT" 2>&1
}
# `grep -c` prints 0 AND exits 1 on no match; `|| true` keeps that single 0.
n_calls()  { grep -c -E "$1" "$CALLS" 2>/dev/null || true; }
n_linkset(){ n_calls '^ip link set eth0 (down|up)$'; }
n_mii()    { n_calls '^mii-tool -r eth0$'; }
n_ethtool(){ n_calls '^ethtool -r eth0$'; }
n_sleep()  { n_calls '^sleep '; }
counter()  { cat "$COUNTER" 2>/dev/null || echo none; }

conf_static eth0

# ═══ C1: five full link cycles on a cable-less admin-UP port ═══════════════
seed eth0 0 0x1003 down
rm -f "$COUNTER"
for pass in 1 2 3 4 5; do
    run_pass eth0
    if [ "$(n_calls '^ip link set eth0 down$')" = 1 ] && [ "$(n_calls '^ip link set eth0 up$')" = 1 ] \
       && [ "$(n_mii)" = 1 ] && [ "$(counter)" = "$pass" ] \
       && grep -Fq "link cycle ${pass}/5" "$OUT"; then
        ok "C1: pass $pass does the full link cycle (down, up, mii-tool -r), counter=$pass"
    else
        bad "C1: pass $pass — linkset=$(n_linkset) mii=$(n_mii) counter=$(counter): $(grep -F 'eth0:' "$OUT" | head -1)"
    fi
done
[ "$(counter)" = 5 ] || bad "C1: counter is $(counter) after five passes, expected 5"

# ═══ C2: budget spent — restart autonegotiation only, every pass ═══════════
for pass in 6 7; do
    run_pass eth0
    if [ "$(n_linkset)" = 0 ] && [ "$(n_mii)" = 1 ] && [ "$(n_ethtool)" = 0 ] \
       && [ "$(n_sleep)" = 0 ] && [ "$(counter)" = 5 ] \
       && grep -Fq 'cycles=5), перезапуск автосогласования' "$OUT" \
       && ! grep -Fq 'restarting autonegotiation' "$OUT"; then
        ok "C2: pass $pass restarts autoneg only (no down/up, no sleep, mii-tool stdout silenced), counter stays 5"
    else
        bad "C2: pass $pass — linkset=$(n_linkset) mii=$(n_mii) ethtool=$(n_ethtool) sleep=$(n_sleep) counter=$(counter): $(grep -F 'eth0:' "$OUT" | head -1)"
    fi
done

# ═══ C3: carrier back — counter reset, nothing restarted ═══════════════════
seed eth0 1 0x1003 up
printf '    inet 192.168.1.135/24 brd 192.168.1.255 scope global eth0\n' > "$SYS/eth0/.addr"
printf 'default via 192.168.1.1 dev eth0\n' > "$SYS/eth0/.routes"
[ "$(counter)" = 5 ] || bad "C3: fixture — counter should still be 5 before the carrier-up pass"
run_pass eth0
if [ "$(counter)" = none ] && [ "$(n_mii)" = 0 ] && [ "$(n_ethtool)" = 0 ] && [ "$(n_linkset)" = 0 ] \
   && [ "$(n_calls '^systemctl ')" -ge 1 ] && [ "$(n_calls '^ping ')" -ge 1 ] \
   && ! grep -Fq 'перезапуск автосогласования' "$OUT"; then
    ok "C3: carrier=1 removes the counter and restarts nothing (carrier-up path consulted systemctl + ping)"
else
    bad "C3: counter=$(counter) mii=$(n_mii) ethtool=$(n_ethtool) linkset=$(n_linkset) systemctl=$(n_calls '^systemctl ') ping=$(n_calls '^ping ')"
fi

# ═══ C4: admin DOWN is left alone, budget spent or not ═════════════════════
seed eth0 0 0x1002 down
mkdir -p "$LOCK"; printf '5\n' > "$COUNTER"
run_pass eth0
if [ "$(n_linkset)" = 0 ] && [ "$(n_mii)" = 0 ] && [ "$(n_ethtool)" = 0 ] && [ "$(counter)" = 5 ] \
   && grep -Fq 'cycles=5), пропуск' "$OUT"; then
    ok "C4: admin-DOWN port with the budget spent: no link set, no restart, «пропуск»"
else
    bad "C4: budget spent — linkset=$(n_linkset) mii=$(n_mii) ethtool=$(n_ethtool) counter=$(counter): $(grep -F 'eth0:' "$OUT" | head -1)"
fi
rm -f "$COUNTER"
run_pass eth0
if [ "$(n_linkset)" = 0 ] && [ "$(n_mii)" = 0 ] && [ "$(n_ethtool)" = 0 ] && [ "$(counter)" = none ] \
   && grep -Fq 'cycles=0), пропуск' "$OUT"; then
    ok "C4: admin-DOWN port with the budget unspent: no link cycle either, counter untouched"
else
    bad "C4: budget unspent — linkset=$(n_linkset) mii=$(n_mii) ethtool=$(n_ethtool) counter=$(counter): $(grep -F 'eth0:' "$OUT" | head -1)"
fi

# ═══ C5: mii-tool not installed — ethtool -r is the fallback ═══════════════
seed eth0 0 0x1003 down
mkdir -p "$LOCK"; printf '5\n' > "$COUNTER"
rm -f "$BIN/mii-tool"
command -v mii-tool >/dev/null 2>&1 && { echo "FAIL  C5: a mii-tool is still on PATH after the shim was removed ($(command -v mii-tool)) — the mask failed"; exit 1; }
run_pass eth0
if [ "$(n_mii)" = 0 ] && [ "$(n_ethtool)" = 1 ] && [ "$(n_linkset)" = 0 ] \
   && grep -Fq 'перезапуск автосогласования' "$OUT"; then
    ok "C5: with mii-tool absent the exhausted pass falls back to ethtool -r"
else
    bad "C5: mii=$(n_mii) ethtool=$(n_ethtool) linkset=$(n_linkset): $(grep -F 'eth0:' "$OUT" | head -1)"
fi

# ═══ C6: link comes up during the cycle — routes restored in the same pass ══
# (mii-tool is absent since C5, so this cycle restarts autoneg via ethtool -r;
#  the flip rides `ip link set eth0 up`, which runs either way.)
seed eth0 0 0x1003 down
printf '    inet 192.168.1.135/24 brd 192.168.1.255 scope global eth0\n' > "$SYS/eth0/.addr"
rm -f "$COUNTER"
printf 'eth0\n' > "$FLIP"
run_pass eth0
c6_carrier=$(cat "$SYS/eth0/carrier")
: > "$FLIP"
[ "$c6_carrier" = 1 ] || bad "C6: fixture — the ip shim never flipped carrier to 1 (no \`ip link set eth0 up\`?), the case tested nothing"
if [ "$(n_calls '^ip link set eth0 down$')" = 1 ] && [ "$(n_calls '^ip link set eth0 up$')" = 1 ] \
   && [ "$(n_calls '^ip route add default via 192\.168\.1\.1 dev eth0$')" = 1 ] \
   && [ "$(counter)" = none ] \
   && grep -Fq 'линк появился после link cycle 1' "$OUT"; then
    ok "C6: link up during cycle 1 — same pass logs it, removes the counter and restores the default route"
else
    bad "C6: linkset=$(n_linkset) route_add=$(n_calls '^ip route add default via 192\.168\.1\.1 dev eth0$') counter=$(counter) log_line=$(grep -c 'линк появился после link cycle' "$OUT"): $(grep -F 'eth0:' "$OUT" | tail -1)"
fi

# ── harness non-vacuity ────────────────────────────────────────────────────
for s in ip mii-tool ethtool ping systemctl sleep; do
    grep -q "^$s " "$CALLS_ALL" || bad "harness: the $s shim was never invoked — the cases are not exercising the script"
done

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-fix-eth-link-cycle.sh"
    exit 0
fi
echo "FAIL  test-fix-eth-link-cycle.sh: $fails failing case(s)"
exit 1
