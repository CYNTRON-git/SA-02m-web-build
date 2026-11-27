#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-iface-dns-ensure.sh — regression test for the boot-time DNS guarantee
# (usr/local/sbin/sa02m-dns-ensure.sh + usr/local/sbin/sa02m-wait-carrier.sh;
# contract docs/contracts/boot-network-dns.md).
#
# Why this exists: the defect it pins is SILENT. A board whose `ifup` died on
# the gateway step (late PHY carrier) never runs its if-up.d hooks, so
# /etc/resolv.conf stays EMPTY while the address and the default route look
# healthy — the board pings, serves its panel, and nothing fails until
# something resolves a name; then the OTA web update dies with
# «Could not resolve host: github.com». No syntax or static gate can see that,
# and the failure frequency is boot-timing dependent, so the mechanism is
# pinned functionally here instead.
#
# Method, in the idiom of test-iface-gw-repair.sh / test-update-recover-rollback.sh:
# copy the SHIPPED helpers into a scratch tree and sed-retarget their four
# absolute roots (interfaces.d, /run/resolvconf/interface, /etc/resolv.conf,
# /etc/sa02m_network.conf, /sys/class/net) into it; `resolvconf`, `logger` and
# `ip` are recording PATH shims (plus a scripted `systemctl` shim for the
# coldboot bootstrap, kept on PATH through the last run of shipped code — the
# POSIX-shell dialect run, which REFUSES to start if `systemctl` does not
# resolve to it). No root, no device, no systemd, nothing touches the real
# system. There is deliberately NO env seam in the shipped
# helpers — the retarget is the test's job.
#
# Non-vacuous: a failed or over-wide retarget, a missing helper, a shim that
# was never called, or a fixture that stopped matching FAILS rather than
# passing on zero matches.
#
# Drive-to-failure recipe (how this harness was proven to be a real ratchet):
#   cp usr/local/sbin/sa02m-dns-ensure.sh /tmp/broken.sh
#   # neuter one guard, e.g. delete the `[ -e "$RESOLVCONF_RUN_DIR/... ]` early
#   # return (rung 1 then overwrites a live record), or delete the whole rung-2
#   # block (the outage this shipped to fix), or drop `valid_ns "$tok" ||
#   # continue` (unvalidated operator data reaches resolvconf)
#   DNS_ENSURE_SRC=/tmp/broken.sh bash scripts/dev/test-iface-dns-ensure.sh
# WAIT_CARRIER_SRC does the same for the carrier wait (e.g. remove the deadline
# check: the budget-expiry case FAILS — the simulated clock's cap stops the loop
# instead of hanging; drop the `ip link set up`: the admin-down case reads the
# whole budget). Neither override ever edits the real tree. The wait's timings
# are measured on a SIMULATED clock (its `date`/`sleep` are shims — see the
# wait section), so host load cannot fail them and no budget is really waited.
#
# Run: bash scripts/dev/test-iface-dns-ensure.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

DNS_SRC=${DNS_ENSURE_SRC:-usr/local/sbin/sa02m-dns-ensure.sh}
WAIT_SRC=${WAIT_CARRIER_SRC:-usr/local/sbin/sa02m-wait-carrier.sh}
UNIT=etc/systemd/system/sa02m-dns-ensure.service
DROPIN_IFUP='etc/systemd/system/ifup@.service.d/sa02m-carrier-wait.conf'
DROPIN_NET=etc/systemd/system/networking.service.d/sa02m-carrier-wait.conf
CONTRACT=docs/contracts/boot-network-dns.md

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$DNS_SRC" "$WAIT_SRC" "$UNIT" "$DROPIN_IFUP" "$DROPIN_NET" "$CONTRACT"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

CD="$T/ifd"                       # /etc/network/interfaces.d
RC_RUN="$T/run/resolvconf/interface"
RESOLV="$T/resolv.conf"
NETCONF="$T/sa02m_network.conf"
SYS="$T/sys/class/net"
UPTIME="$T/uptime"
BASE="$T/resolvconf-base"         # stands in for resolv.conf.d/base
BIN="$T/bin"
CALLS="$T/calls.log"
CALLS_ALL="$T/calls-all.log"   # never reset — the harness non-vacuity witness
LOGGED="$T/logger.log"
mkdir -p "$CD" "$RC_RUN" "$SYS" "$BIN"

# ── retarget the SHIPPED helpers into the sandbox ──────────────────────────
DNS="$T/sa02m-dns-ensure.sh"
WAIT="$T/sa02m-wait-carrier.sh"
sed -e "s#^IFACE_CONF_DIR=/etc/network/interfaces\.d\$#IFACE_CONF_DIR=$CD#" \
    -e "s#^RESOLVCONF_RUN_DIR=/run/resolvconf/interface\$#RESOLVCONF_RUN_DIR=$RC_RUN#" \
    -e "s#^RESOLV_CONF=/etc/resolv\.conf\$#RESOLV_CONF=$RESOLV#" \
    "$DNS_SRC" > "$DNS"
sed -e "s#^NET_CONF=/etc/sa02m_network\.conf\$#NET_CONF=$NETCONF#" \
    -e "s#^IFACE_CONF_DIR=/etc/network/interfaces\.d\$#IFACE_CONF_DIR=$CD#" \
    -e "s#^SYS_NET_DIR=/sys/class/net\$#SYS_NET_DIR=$SYS#" \
    -e "s#^UPTIME_FILE=/proc/uptime\$#UPTIME_FILE=$UPTIME#" \
    "$WAIT_SRC" > "$WAIT"
chmod +x "$DNS" "$WAIT"

# Non-vacuity: every root MUST have been replaced. An un-retargeted helper
# would silently read (or write!) the real /etc — that is the one failure this
# harness must never pass through.
for pat in "IFACE_CONF_DIR=$CD" "RESOLVCONF_RUN_DIR=$RC_RUN" "RESOLV_CONF=$RESOLV"; do
    grep -Fq "$pat" "$DNS" || { echo "FAIL  retarget missed in dns-ensure: $pat"; exit 1; }
done
for pat in "NET_CONF=$NETCONF" "IFACE_CONF_DIR=$CD" "SYS_NET_DIR=$SYS"; do
    grep -Fq "$pat" "$WAIT" || { echo "FAIL  retarget missed in wait-carrier: $pat"; exit 1; }
done
grep -Eq '^[A-Z_]+=/etc/' "$DNS" && { echo "FAIL  dns-ensure still holds an /etc root"; exit 1; }
grep -Eq '^[A-Z_]+=/(etc|sys|run)/' "$WAIT" && { echo "FAIL  wait-carrier still holds a real root"; exit 1; }
# Only constant assignments may differ; this also supports the original source
# override for RED reproduction before UPTIME_FILE existed.
sed -e "s#^NET_CONF=$NETCONF\$#NET_CONF=/etc/sa02m_network.conf#" \
    -e "s#^IFACE_CONF_DIR=$CD\$#IFACE_CONF_DIR=/etc/network/interfaces.d#" \
    -e "s#^SYS_NET_DIR=$SYS\$#SYS_NET_DIR=/sys/class/net#" \
    -e "s#^UPTIME_FILE=$UPTIME\$#UPTIME_FILE=/proc/uptime#" "$WAIT" > "$T/wait-restored"
cmp -s "$WAIT_SRC" "$T/wait-restored" || { echo 'FAIL  wait retarget changed more than constants'; exit 1; }

# ── PATH shims ─────────────────────────────────────────────────────────────
# resolvconf models the real thing closely enough for the assertions that
# matter: -a files a record, -u regenerates, and BOTH regenerate resolv.conf
# from base + records (that is how resolv.conf.d/base materialises on a board).
cat > "$BIN/resolvconf" <<SHIM
#!/bin/bash
echo "resolvconf \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
regen() {
    { [ -f "$BASE" ] && cat "$BASE"
      for r in "$RC_RUN"/*; do [ -f "\$r" ] && cat "\$r"; done
    } > "$RESOLV" 2>/dev/null
    return 0
}
case "\${1:-}" in
  -a) [ -n "\${RC_FAIL:-}" ] && exit 1
      cat > "$RC_RUN/\$2"; regen ;;
  -d) rm -f "$RC_RUN/\$2"; regen ;;
  -u) [ -n "\${RC_FAIL:-}" ] && exit 1
      regen ;;
esac
exit 0
SHIM
cat > "$BIN/logger" <<SHIM
#!/bin/bash
echo "\$*" >> "$LOGGED"
exit 0
SHIM
# `ip link set X up` is what makes an admin-DOWN device's carrier readable at
# all (the kernel returns -EINVAL otherwise). The shim models exactly that:
# raising a link publishes whatever carrier value the case seeded.
cat > "$BIN/ip" <<SHIM
#!/bin/bash
echo "ip \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
if [ "\${1:-}" = "link" ] && [ "\${2:-}" = "set" ] && [ "\${4:-}" = "up" ]; then
    _i="\$3"
    touch "$SYS/\$_i/.up"
    [ -f "$SYS/\$_i/.pending" ] && cp "$SYS/\$_i/.pending" "$SYS/\$_i/carrier"
fi
exit 0
SHIM
chmod +x "$BIN/resolvconf" "$BIN/logger" "$BIN/ip"
cat > "$BIN/ifquery" <<SHIM
#!/bin/bash
echo "ifquery \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
[ "\$#" = 2 ] && [ "\$1" = --no-mappings ] || exit 2
[ "\${QUERY_FAIL:-0}" = 0 ] || exit 1
if [ -f "$T/query-\$2" ]; then cat "$T/query-\$2"; exit 0; fi
printf 'address: 192.168.1.136\n'
if [ -f "$CD/\$2.conf" ]; then
  awk -v iface="\$2" '\$1=="iface" { own=(\$2==iface); next } own && \$1=="hwaddress" { \$1=""; sub(/^[ \\t]+/, ""); print "hwaddress: " \$0 }' "$CD/\$2.conf"
fi
SHIM
cat > "$BIN/timeout" <<SHIM
#!/bin/bash
echo "timeout \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
case "\${1:-}" in 1|2) ;; *) exit 2 ;; esac
[ "\${2:-}" = ifquery ] || exit 2
[ "\${QUERY_TIMEOUT:-0}" = 0 ] || exit 124
if [ "\${QUERY_DELAY:-0}" != 0 ]; then
    if [ "\$QUERY_DELAY" -ge "\$1" ]; then sleep "\$1"; exit 124; fi
    sleep "\$QUERY_DELAY"
fi
shift; exec "\$@"
SHIM
chmod +x "$BIN/ifquery" "$BIN/timeout"
PATH="$BIN:$PATH"; export PATH

reset() {
    rm -rf "$CD" "$RC_RUN" "$SYS"; mkdir -p "$CD" "$RC_RUN" "$SYS"
    : > "$CALLS"; : > "$LOGGED"; : > "$RESOLV"; : > "$NETCONF"
    rm -f "$T"/query-*
    printf '# SA-02m fallback DNS\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n' > "$BASE"
}
# `grep -c` prints 0 AND exits 1 on no match, so a `|| echo 0` fallback would
# print TWO zeroes on separate lines and every zero-comparison would silently
# pass. `|| true` keeps the single "0" grep has already printed.
add_calls()   { grep -c '^resolvconf -a' "$CALLS" 2>/dev/null || true; }
update_calls(){ grep -c '^resolvconf -u' "$CALLS" 2>/dev/null || true; }
record()      { cat "$RC_RUN/$1.inet" 2>/dev/null; }

conf_static() {   # $1 iface, $2 dns line (may be empty)
    { printf 'auto %s\niface %s inet static\n    address 192.168.1.136\n' "$1" "$1"
      printf '    netmask 255.255.255.0\n    gateway 192.168.1.1\n'
      [ -n "${2:-}" ] && printf '    %s\n' "$2"
    } > "$CD/$1.conf"
}

# ═══ rung 1 ════════════════════════════════════════════════════════════════

reset
conf_static eth0 "dns-nameservers 77.88.8.8 77.88.8.1"
printf 'nameserver 10.0.0.1\n' > "$RC_RUN/eth0.inet"
printf 'nameserver 10.0.0.1\n' > "$RESOLV"
"$DNS" eth0 >/dev/null 2>&1
rc=$?
if [ "$rc" = 0 ] && [ "$(add_calls)" = 0 ] && [ "$(record eth0)" = "nameserver 10.0.0.1" ]; then
    ok "rung1: an existing interface record is left untouched (a real ifup always wins)"
else
    bad "rung1: existing record was touched (rc=$rc adds=$(add_calls))"
fi

reset
conf_static eth0 ""
printf 'nameserver 10.0.0.1\n' > "$RESOLV"
"$DNS" eth0 >/dev/null 2>&1
if [ "$(add_calls)" = 0 ] && [ ! -e "$RC_RUN/eth0.inet" ]; then
    ok "rung1: a conf with no dns-nameservers writes nothing (the dhcp/2-eth case)"
else
    bad "rung1: wrote a record for a conf declaring no nameservers"
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8 77.88.8.1 1.1.1.1"
grep -q 'dns-nameservers 77.88.8.8 77.88.8.1 1.1.1.1' "$CD/eth0.conf" \
  || bad "fixture: multi-nameserver line not present"
"$DNS" eth0 >/dev/null 2>&1
exp=$'nameserver 77.88.8.8\nnameserver 77.88.8.1\nnameserver 1.1.1.1'
if [ "$(record eth0)" = "$exp" ]; then
    ok "rung1: every nameserver is filed, in the conf's own order"
else
    bad "rung1: multi-nameserver order/content wrong: $(record eth0 | tr '\n' '|')"
fi

# The bench board's real shape: a foreign `no-auto-down`, a KLogic post-up,
# interleaved comments and a TAB indent — the formatting variance that has
# broken every previous parser in this tree.
reset
{ printf 'no-auto-down eth0\n# operator note\nauto eth0\n'
  printf 'iface eth0 inet static\n\taddress 192.168.1.136\n'
  printf '\tnetmask 255.255.255.0\n# gateway comment\n\tgateway 192.168.1.1\n'
  printf '\tdns-nameservers 77.88.8.8  77.88.8.1\n'
  printf '\tdns-search example.lan\n'
  printf '\tpost-up /home/klogic/adjust-eth0\n'
} > "$CD/eth0.conf"
"$DNS" eth0 >/dev/null 2>&1
exp=$'search example.lan\nnameserver 77.88.8.8\nnameserver 77.88.8.1'
if [ "$(record eth0)" = "$exp" ]; then
    ok "rung1: a KLogic-shaped conf (no-auto-down, TAB indent, comments, post-up) parses"
else
    bad "rung1: KLogic-shaped conf mis-parsed: $(record eth0 | tr '\n' '|')"
fi

reset
conf_static eth0 "$(printf 'dns-nameservers \001evil')"
"$DNS" eth0 >/dev/null 2>&1
if [ "$(add_calls)" = 0 ] && [ ! -e "$RC_RUN/eth0.inet" ]; then
    ok "rung1: a control-character token never reaches resolvconf"
else
    bad "rung1: control-character token was filed"
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8 999.1.1.1 8.8.8.8.8 1.1.1.1 010.1.1.1"
"$DNS" eth0 >/dev/null 2>&1
exp=$'nameserver 77.88.8.8\nnameserver 1.1.1.1'
if [ "$(record eth0)" = "$exp" ]; then
    ok "rung1: invalid tokens are dropped (incl. the leading-zero octal footgun), valid ones kept"
else
    bad "rung1: token validation wrong: $(record eth0 | tr '\n' '|')"
fi

reset
long=$(printf 'a%.0s' $(seq 1 1100))
conf_static eth0 "dns-nameservers 77.88.8.8 $long"
"$DNS" eth0 >/dev/null 2>&1
if [ "$(add_calls)" = 0 ] && [ ! -e "$RC_RUN/eth0.inet" ]; then
    ok "rung1: an oversized dns-nameservers line is dropped whole, nothing written"
else
    bad "rung1: oversized line reached resolvconf"
fi

reset
printf 'this is not an interfaces stanza at all\n\x00\n' > "$CD/eth0.conf"
"$DNS" eth0 >/dev/null 2>&1
rc=$?
if [ "$rc" = 0 ] && [ "$(add_calls)" = 0 ]; then
    ok "rung1: an unparseable conf writes nothing and still exits 0"
else
    bad "rung1: unparseable conf: rc=$rc adds=$(add_calls)"
fi

reset
{ printf 'auto eth0\r\niface eth0 inet static\r\n'
  printf '    gateway 192.168.1.1\r\n    dns-nameservers 77.88.8.8\r\n'
} > "$CD/eth0.conf"
"$DNS" eth0 >/dev/null 2>&1
if [ "$(record eth0)" = "nameserver 77.88.8.8" ]; then
    ok "rung1: a CRLF-delivered conf still parses (the recurring CRLF class)"
else
    bad "rung1: CRLF conf mis-parsed: $(record eth0 | tr '\n' '|')"
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8"
{ printf '    dns-search  good.lan bad_domain!\n    dns-domain good.lan\n'; } >> "$CD/eth0.conf"
"$DNS" eth0 >/dev/null 2>&1
exp=$'search good.lan\ndomain good.lan\nnameserver 77.88.8.8'
if [ "$(record eth0)" = "$exp" ]; then
    ok "rung1: dns-search/dns-domain are filed, an invalid domain token is dropped"
else
    bad "rung1: search/domain handling wrong: $(record eth0 | tr '\n' '|')"
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8"
conf_static eth1 "dns-nameservers 1.1.1.1"
"$DNS" --all >/dev/null 2>&1
if [ "$(record eth0)" = "nameserver 77.88.8.8" ] && [ "$(record eth1)" = "nameserver 1.1.1.1" ]; then
    ok "rung1: --all covers every conf in interfaces.d"
else
    bad "rung1: --all missed an interface"
fi

# ═══ rung 2 — the fallback sweep (the rung that would have prevented the outage)

reset
conf_static eth0 ""
: > "$RESOLV"
"$DNS" --all >/dev/null 2>&1
if [ "$(update_calls)" -ge 1 ] && grep -q '^nameserver 8.8.8.8' "$RESOLV"; then
    ok "rung2: an empty resolv.conf triggers 'resolvconf -u' and base materialises"
else
    bad "rung2: empty resolv.conf did not regenerate (updates=$(update_calls))"
fi

reset
conf_static eth0 ""
printf 'nameserver 192.168.1.1\n' > "$RESOLV"
"$DNS" --all >/dev/null 2>&1
if [ "$(update_calls)" = 0 ] && [ "$(cat "$RESOLV")" = "nameserver 192.168.1.1" ]; then
    ok "rung2: a populated resolv.conf is left alone (never removes a working resolver)"
else
    bad "rung2: fired against a populated resolv.conf (updates=$(update_calls))"
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8"
: > "$RESOLV"
"$DNS" --all >/dev/null 2>&1
if [ "$(update_calls)" = 0 ] && grep -q '^nameserver 77.88.8.8' "$RESOLV"; then
    ok "rung2: does not fire when rung 1 already produced a resolver"
else
    bad "rung2: fired needlessly after a rung-1 write (updates=$(update_calls))"
fi

# ═══ idempotency / re-entrancy (net-watchdog drives this every 30 s) ════════

reset
conf_static eth0 "dns-nameservers 77.88.8.8"
: > "$RESOLV"
"$DNS" eth0 >/dev/null 2>&1
after1=$(cat "$RESOLV"); logs1=$(wc -l < "$LOGGED")
"$DNS" eth0 >/dev/null 2>&1
"$DNS" eth0 >/dev/null 2>&1
after3=$(cat "$RESOLV")
if [ "$(add_calls)" = 1 ] && [ "$after1" = "$after3" ] && [ "$logs1" = "$(wc -l < "$LOGGED")" ]; then
    ok "idempotent: three runs = one write, one log line, byte-identical resolv.conf"
else
    bad "idempotency broken: adds=$(add_calls) logs=$logs1->$(wc -l < "$LOGGED")"
fi

# ═══ degradation ═══════════════════════════════════════════════════════════

# The `command -v resolvconf || exit 0` guard is only PINNED if `logger` stays
# reachable: with logger absent too, a helper missing the guard would fall into
# rung 2, fail `resolvconf -u`, and log nothing anyway — the case would pass on
# a broken helper. So this PATH keeps the logger shim and drops only resolvconf.
# A host resolvconf in /usr/bin or /bin is hidden (lib_path_mask.sh), so whether
# it «is installed» is this harness's call on every host, not a host-dependent
# FAIL below.
BIN2="$T/bin-nores"; mkdir -p "$BIN2"
command cp "$BIN/logger" "$BIN/ip" "$BIN2/"
# shellcheck source=scripts/dev/lib_path_mask.sh
. scripts/dev/lib_path_mask.sh || { echo "FAIL  scripts/dev/lib_path_mask.sh not loadable"; exit 1; }
NORES_PATH="$BIN2:$(PATH=/usr/bin:/bin path_without "$T/hostmask" resolvconf)"
reset
conf_static eth0 "dns-nameservers 77.88.8.8"
: > "$RESOLV"
if PATH="$NORES_PATH" command -v resolvconf >/dev/null 2>&1; then
    bad "harness: resolvconf is reachable in the 'absent' PATH — the case cannot test anything"
else
    out=$(PATH="$NORES_PATH" "$DNS" eth0 2>&1); rc=$?
    if [ "$rc" = 0 ] && [ -z "$out" ] && [ ! -s "$LOGGED" ] && [ ! -e "$RC_RUN/eth0.inet" ]; then
        ok "degradation: resolvconf absent = clean SILENT no-op, exit 0 (guard pinned via a live logger)"
    else
        bad "degradation: resolvconf-absent path rc=$rc out='$out' log='$(cat "$LOGGED")'"
    fi
fi

reset
conf_static eth0 "dns-nameservers 77.88.8.8"
: > "$RESOLV"
RC_FAIL=1 "$DNS" eth0 >/dev/null 2>&1
rc=$?
if [ "$rc" = 0 ] && grep -q 'resolvconf -a failed' "$LOGGED"; then
    ok "degradation: a failing 'resolvconf -a' is logged and still exits 0"
else
    bad "degradation: resolvconf failure path rc=$rc"
fi

# ═══ sa02m-wait-carrier.sh ═════════════════════════════════════════════════

seed_iface() {   # $1 iface, $2 carrier now ("" = unreadable/admin-down), $3 carrier after `ip link set up`
    mkdir -p "$SYS/$1"
    printf '%s' "${2:-}" > "$SYS/$1/carrier"
    [ -n "${3:-}" ] && printf '%s\n' "$3" > "$SYS/$1/.pending"
    return 0
}
# ── the wait's clock is SIMULATED, not the host's ─────────────────────────
# The cases below assert how long the wait waits. Measured in wall seconds,
# that was the host's fork latency as much as the wait's logic: with many
# agents in parallel a zero-sleep path took 3–5 s of `date`/`sed`/`grep`/shim
# forks and the cases went RED with the shipped loop unchanged, while an idle
# host and WSL passed (every review of 2026-09-29/30). The wait reads time
# through a retargeted /proc/uptime and waits through `sleep 1`; the original
# wall-clock implementation remains reproducible with WAIT_CARRIER_SRC.
# Both date/sleep are PATH shims for its invocations (never for the harness
# or dns-ensure): `sleep N` advances a clock file by N and returns at once,
# `date +%s` reads it. Elapsed is then exactly the loop's own sleeps — a burned
# budget reads as the budget, a zero-sleep path reads 0 — whatever the host is
# doing, and no wall second is spent waiting a budget out. A loop that never
# reaches its deadline (the check deleted) would sleep forever: past CLOCK_CAP
# simulated seconds the sleep shim stops the wait (SIGTERM to its shell), so
# that defect FAILS its case instead of hanging the beat.
CLK="$T/bin-clock"; mkdir -p "$CLK"
CLOCK="$T/clock"; CLOCK_CAP=600
cat > "$CLK/date" <<SHIM
#!/bin/bash
if [ "\$#" = 1 ] && [ "\$1" = "+%s" ]; then
    read -r _t < "$CLOCK" 2>/dev/null || _t=0
    _jump=0; [ "\$_t" -lt 1 ] || _jump=\${WALL_JUMP:-0}
    echo \$((1700000000 + _t + _jump)); exit 0
fi
PATH=/usr/bin:/bin exec date "\$@"
SHIM
cat > "$CLK/sleep" <<SHIM
#!/bin/bash
echo "sleep \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
_n=\${1%%.*}; case "\$_n" in ''|*[!0-9]*) _n=1 ;; esac
read -r _t < "$CLOCK" 2>/dev/null || _t=0
_t=\$((_t + _n)); echo "\$_t" > "$CLOCK"
printf '%s.00 0.00\n' "\$_t" > "$UPTIME"
case "\${UPTIME_BREAK:-}" in
  malformed) echo invalid > "$UPTIME" ;;
  missing) rm -f "$UPTIME" ;;
  backward) echo '0.00 0.00' > "$UPTIME" ;;
esac
if [ "\$_t" -gt $CLOCK_CAP ]; then
    echo "harness: the wait slept past $CLOCK_CAP simulated seconds without reaching a deadline — stopped" >> "$LOGGED"
    kill -TERM "\$PPID" 2>/dev/null
fi
exit 0
SHIM
chmod +x "$CLK/date" "$CLK/sleep"
clock_now() { read -r _t < "$CLOCK" 2>/dev/null || _t=0; echo "$_t"; }
run_wait() {   # runs the wait from simulated 0; leaves rc, el (simulated s), sleeps (count)
    echo 0 > "$CLOCK"
    printf '%s\n' "${UPTIME_INITIAL:-0.00 0.00}" > "$UPTIME"
    [ "${UPTIME_MISSING:-0}" = 0 ] || rm -f "$UPTIME"
    PATH="$CLK:$PATH" dash "$WAIT" "$@" >/dev/null 2>&1; rc=$?
    el=$(clock_now); sleeps=$(grep -c '^sleep ' "$CALLS" 2>/dev/null || true)
}
# Precondition: the clock is live and advances ONLY through `sleep` — a shim
# that fell off PATH would make every zero-elapsed assertion below vacuous.
echo 0 > "$CLOCK"
probe=$(PATH="$CLK:$PATH" sh -c 's=$(date +%s); sleep 3; sleep 2; e=$(date +%s); echo $((e - s))' 2>&1)
if [ "$probe" = 5 ] && [ "$(clock_now)" = 5 ]; then
    ok "harness: the wait's clock is simulated — 'sleep 3; sleep 2' advance date +%s by exactly 5, no wall time"
else
    bad "harness: simulated clock broken (probe='$probe' clock=$(clock_now)) — the wait timings below would be vacuous"
fi

reset
seed_iface eth0 1
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'IFUP_CARRIER_WAIT_SECS=5\n' > "$NETCONF"
run_wait eth0
if [ "$rc" = 0 ] && [ "$el" = 0 ] && grep -q 'carrier up for \[eth0\]' "$LOGGED"; then
    ok "wait: returns immediately when carrier is already up (${el}s simulated, ${sleeps} sleeps)"
else
    bad "wait: carrier-up path rc=$rc took ${el}s simulated (${sleeps} sleeps) / did not log"
fi

reset
seed_iface eth0 0
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'IFUP_CARRIER_WAIT_SECS=2\n' > "$NETCONF"
run_wait eth0
if [ "$rc" = 0 ] && [ "$el" = 2 ] && [ "$sleeps" = 2 ] && grep -q 'budget 2s expired' "$LOGGED"; then
    ok "wait: TERMINATES at the budget on a carrier-less port, rc=0 (exactly ${el}s simulated = ${sleeps} sleeps, not one more)"
else
    bad "wait: budget expiry rc=$rc elapsed=${el}s simulated sleeps=${sleeps}"
fi

reset
seed_iface eth0 0
printf 'IFUP_CARRIER_WAIT_SECS=0\n' > "$NETCONF"
run_wait eth0
if [ "$rc" = 0 ] && [ "$el" = 0 ] && [ ! -s "$LOGGED" ]; then
    ok "wait: IFUP_CARRIER_WAIT_SECS=0 disables it completely (no wait, no side effect)"
else
    bad "wait: the 0 escape hatch did not disable (rc=$rc elapsed=${el}s simulated)"
fi

# An admin-DOWN device answers -EINVAL to a carrier read, so without raising the
# link first the wait could never observe a live link and would burn the whole
# budget on EVERY boot, cable or no cable.
reset
seed_iface eth0 "" 1
printf 'IFUP_CARRIER_WAIT_SECS=6\n' > "$NETCONF"
run_wait eth0
if [ "$rc" = 0 ] && [ "$el" = 0 ] && grep -q '^ip link set eth0 up$' "$CALLS" && grep -q 'carrier up' "$LOGGED"; then
    ok "wait: an admin-down device is raised first, then carrier is observed (${el}s simulated)"
else
    bad "wait: admin-down path rc=$rc took ${el}s simulated (${sleeps} sleeps) / no 'ip link set up'"
fi

reset
seed_iface eth0 1; seed_iface eth1 0; seed_iface eth2 0
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'auto eth1\niface eth1 inet dhcp\n' > "$CD/eth1.conf"
printf 'auto eth2\niface eth2 inet static\n    address 10.0.0.2\n    netmask 255.255.255.0\n' > "$CD/eth2.conf"
printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
run_wait --auto
line=$(grep 'carrier' "$LOGGED" | tail -n 1)
if [ "$rc" = 0 ] && [ "$el" = 0 ] && [ "${line#*eth0}" != "$line" ] \
   && [ "${line#*eth1}" = "$line" ] && [ "${line#*eth2}" = "$line" ]; then
    ok "wait: --auto selects only auto+static+gateway interfaces (dhcp and gateway-less skipped)"
else
    bad "wait: --auto selection wrong (rc=$rc ${el}s simulated): $line"
fi

reset
seed_iface eth0 1
printf 'IFUP_CARRIER_WAIT_SECS=9999\n' > "$NETCONF"
run_wait eth0
if grep -q 'exceeds the 120s ceiling' "$LOGGED"; then
    ok "wait: an over-large budget is clamped AND logged, never silently honoured"
else
    bad "wait: over-large budget was not clamped/logged"
fi

# A value wide enough to overflow `[ -gt ]` in some shells would SKIP the clamp
# and produce an unbounded deadline — the one way this script could hang a boot.
reset
seed_iface eth0 1
printf 'IFUP_CARRIER_WAIT_SECS=99999999999999999999\n' > "$NETCONF"
run_wait eth0
if [ "$rc" = 0 ] && [ "$el" = 0 ] && grep -q 'exceeds the 120s ceiling' "$LOGGED"; then
    ok "wait: an arithmetic-overflow-wide budget is still clamped, never unbounded (${el}s simulated)"
else
    bad "wait: overflow-wide budget was not clamped (rc=$rc elapsed=${el}s simulated)"
fi

reset
seed_iface eth0 0
printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
run_wait 'eth0;reboot'
if [ "$rc" = 0 ] && [ "$el" = 0 ] && grep -q 'malformed interface name' "$LOGGED" \
   && ! grep -q 'eth0;reboot' "$LOGGED"; then
    ok "wait: a malformed interface name is refused before it reaches a path or ip(8)"
else
    bad "wait: malformed name not refused (rc=$rc ${el}s simulated)"
fi

reset
seed_iface lo 0
printf 'IFUP_CARRIER_WAIT_SECS=5\n' > "$NETCONF"
run_wait lo
if [ "$rc" = 0 ] && [ "$el" = 0 ]; then
    ok "wait: lo is never waited on (ifup@lo pays nothing)"
else
    bad "wait: lo waited ${el}s simulated (rc=$rc)"
fi

# Both clamp cases above seed carrier-up, so they prove the LOG fires — not that
# the returned budget actually shrank. Extract the SHIPPED read_budget and call
# it directly: that asserts the clamp's EFFECT without waiting out a 120 s run.
{ grep -E '^(NET_CONF|DEFAULT_WAIT_SECS|MAX_WAIT_SECS)=' "$WAIT"
  sed -n '/^read_budget() {/,/^}$/p' "$WAIT"
} > "$T/read_budget.sh"
budget_of() {  # $1 = conf body ("" = no conf file at all)
    if [ -n "${1:-}" ]; then printf '%s\n' "$1" > "$NETCONF"; else rm -f "$NETCONF"; fi
    ( log() { :; }
      # shellcheck source=/dev/null
      . "$T/read_budget.sh"
      read_budget )
}
if ! grep -q 'IFUP_CARRIER_WAIT_SECS' "$T/read_budget.sh" \
   || ! grep -q '^read_budget() {' "$T/read_budget.sh" \
   || ! grep -Fq "NET_CONF=$NETCONF" "$T/read_budget.sh"; then
    bad "harness: read_budget extraction is empty, over-wide, or un-retargeted"
else
    b_def=$(budget_of "")
    b_set=$(budget_of 'IFUP_CARRIER_WAIT_SECS=45')
    b_big=$(budget_of 'IFUP_CARRIER_WAIT_SECS=9999')
    b_ovf=$(budget_of 'IFUP_CARRIER_WAIT_SECS=99999999999999999999')
    b_off=$(budget_of 'IFUP_CARRIER_WAIT_SECS=0')
    if [ "$b_def" = 10 ] && [ "$b_set" = 45 ] && [ "$b_big" = 120 ] \
       && [ "$b_ovf" = 120 ] && [ "$b_off" = 0 ]; then
        ok "wait: the clamp's EFFECT holds — 9999 and an overflow-wide value both return 120; 45 and 0 pass through"
    else
        bad "wait: read_budget returned def=$b_def set=$b_set big=$b_big ovf=$b_ovf off=$b_off"
    fi

    # `[ -gt ]` is base 10 but `$(( ))` reads a leading zero as OCTAL: before
    # the strip, `08`/`09` aborted the shell with "value too great for base"
    # (so the helper never reached its unconditional exit 0 — the one input
    # that could break that promise) and `010` silently meant 8 s, not 10.
    z_08=$(budget_of 'IFUP_CARRIER_WAIT_SECS=08' 2>&1)
    z_09=$(budget_of 'IFUP_CARRIER_WAIT_SECS=09' 2>&1)
    z_010=$(budget_of 'IFUP_CARRIER_WAIT_SECS=010' 2>&1)
    z_0120=$(budget_of 'IFUP_CARRIER_WAIT_SECS=0120' 2>&1)
    z_00=$(budget_of 'IFUP_CARRIER_WAIT_SECS=00' 2>&1)
    # The value must also survive the arithmetic the caller actually performs.
    arith_ok=yes
    for _z in "$z_08" "$z_09" "$z_010" "$z_0120" "$z_00"; do
        # In a SUBSHELL: an invalid octal is a FATAL expansion error, and run
        # inline it would kill this harness mid-case — no ok, no bad, `fails`
        # untouched, suite still green. Containing it turns the exact defect
        # under test into a recorded failure instead of a silent skip.
        ( _d=$(( 1000 + _z )); : "$_d" ) 2>/dev/null || arith_ok=no
    done
    if [ "$z_08" = 8 ] && [ "$z_09" = 9 ] && [ "$z_010" = 10 ] \
       && [ "$z_0120" = 120 ] && [ "$z_00" = 0 ] && [ "$arith_ok" = yes ]; then
        ok "wait: a leading-zero budget is read base-10 and never octal (08/09 no longer abort the shell)"
    else
        bad "wait: leading-zero budgets 08=$z_08 09=$z_09 010=$z_010 0120=$z_0120 00=$z_00 arith=$arith_ok"
    fi
fi

# The budget's ceiling is a CONTRACT number, not a preference: ifup@ and
# networking carry TimeoutStartUSec=5min, and a wait that approached it would
# turn a late PHY into a FAILED unit — the failure this whole change removes.
cap=$(sed -n 's/^MAX_WAIT_SECS=\([0-9][0-9]*\).*/\1/p' "$WAIT_SRC" | head -n 1)
if [ -n "$cap" ] && [ "$cap" -le 120 ] && [ "$cap" -lt 300 ]; then
    ok "wait: the hard ceiling (${cap}s) stays well under ifup@'s 5-min start timeout"
else
    bad "wait: MAX_WAIT_SECS='$cap' is missing or not safely under 300 s"
fi

# Clock corrections and MAC ownership: execute the whole shipped helper in dash.
for jump in 900 -900; do
    reset; seed_iface eth0 0; conf_static eth0 ''
    WALL_JUMP=$jump run_wait eth0
    if [ "$rc" = 0 ] && [ "$el" = 10 ] && grep -q 'after 10s' "$LOGGED"; then
        ok "wait: wall jump $jump leaves the 10-second uptime budget and elapsed intact"
    else bad "wait: wall jump $jump rc=$rc elapsed=$el log=$(cat "$LOGGED")"; fi
done
reset; seed_iface eth0 0; seed_iface eth1 0
conf_static eth0 ''; conf_static eth1 ''
run_wait --auto
if [ "$rc" = 0 ] && [ "$el" = 10 ] && [ "$sleeps" = 10 ]; then
    ok 'wait: two interfaces share one total deadline'
else bad "wait: shared budget rc=$rc elapsed=$el sleeps=$sleeps"; fi

for value in invalid '' '-1.0 0' '1x.0 0' '1.x 0' '999999999999999999999.00 0'; do
    reset; seed_iface eth0 '' 1
    # A single blank exercises empty input without the default expansion.
    UPTIME_INITIAL="${value:- }" run_wait eth0
    if [ "$rc" = 0 ] && [ "$el" = 0 ] && [ ! -s "$CALLS" ] \
       && grep -q 'uptime unavailable' "$LOGGED" && ! grep -q 'carrier up' "$LOGGED"; then
        ok 'wait: malformed uptime is an honest no-action success exit'
    else bad 'wait: malformed uptime caused a wait, action or false carrier success'; fi
done
reset; seed_iface eth0 '' 1
UPTIME_MISSING=1 run_wait eth0
if [ "$rc" = 0 ] && [ ! -s "$CALLS" ] && grep -q 'uptime unavailable' "$LOGGED"; then
    ok 'wait: unreadable uptime skips all network probes'
else bad 'wait: unreadable uptime was not bounded'; fi
for broken in malformed missing backward; do
    reset; seed_iface eth0 0
    UPTIME_INITIAL='1.00 0.00' UPTIME_BREAK=$broken run_wait eth0
    if [ "$rc" = 0 ] && [ "$el" -le 2 ] && grep -q 'uptime unavailable' "$LOGGED" \
       && ! grep -q 'carrier up\|after .*s' "$LOGGED"; then
        ok "wait: $broken mid-wait clock ends without fabricated elapsed"
    else bad "wait: $broken clock did not stop safely (elapsed=$el)"; fi
done
reset; seed_iface eth0 0; printf 'IFUP_CARRIER_WAIT_SECS=010\n' > "$NETCONF"
UPTIME_INITIAL='0000.00 0.00' run_wait eth0
[ "$rc:$el" = 0:10 ] && ok 'wait: leading zero uptime/budget remain decimal' || bad 'wait: decimal uptime failed'
reset; seed_iface eth0 '' 1; printf 'IFUP_CARRIER_WAIT_SECS=0\n' > "$NETCONF"
UPTIME_MISSING=1 run_wait eth0
[ "$rc" = 0 ] && [ ! -s "$CALLS" ] && [ ! -s "$LOGGED" ] \
    && ok 'wait: zero budget makes no query/ip/sleep, even with absent clock' || bad 'wait: zero is not inert'

for mode in direct auto sourced; do
  for mac in '02:53:f9:42:bc:1f' 'ether 02:53:b4:4c:14:bf'; do
    reset; seed_iface eth0 '' 1; conf_static eth0 ''
    if [ "$mode" = sourced ]; then printf 'address: 192.168.1.136\nhwaddress: %s\n' "$mac" > "$T/query-eth0"
    else printf '    hwaddress %s\n' "$mac" >> "$CD/eth0.conf"; fi
    if [ "$mode" = auto ]; then run_wait --auto; else run_wait eth0; fi
    if [ "$rc" = 0 ] && [ "$el" = 0 ] && ! grep -q '^ip ' "$CALLS" \
       && grep -q 'hwaddress.*deferred to ifupdown' "$LOGGED" && ! grep -q 'carrier up' "$LOGGED"; then
        ok "wait: $mode hwaddress defers MAC and initial link-up to ifupdown"
    else bad "wait: $mode hwaddress was pre-raised or misreported"; fi
  done
done
reset; seed_iface eth0 '' 1; conf_static eth0 ''
printf '# hwaddress ether 02:53:f9:42:bc:1f\niface eth1 inet static\n    hwaddress ether 02:53:f9:42:bc:1f\n' >> "$CD/eth0.conf"
run_wait eth0
[ "$rc" = 0 ] && [ "$(grep -c '^ip link set eth0 up$' "$CALLS")" = 1 ] \
    && ok 'wait: another stanza/comment does not defer a confirmed clean target' || bad 'wait: wrong stanza caused deferral'
for failure in failed timeout ambiguous empty; do
    reset; seed_iface eth0 '' 1
    case "$failure" in
      failed) QUERY_FAIL=1 run_wait eth0 ;;
      timeout) QUERY_TIMEOUT=1 run_wait eth0 ;;
      ambiguous) printf 'address: 1\naddress: 2\n' > "$T/query-eth0"; run_wait eth0 ;;
      empty) : > "$T/query-eth0"; run_wait eth0 ;;
    esac
    if [ "$rc" = 0 ] && [ "$el" = 0 ] && ! grep -q '^ip ' "$CALLS" && grep -q 'query.*deferred' "$LOGGED"; then
        ok "wait: $failure effective config defers without unsafe link-up"
    else bad "wait: $failure effective config was treated as clean"; fi
done
for absent in timeout ifquery; do
    reset; seed_iface eth0 '' 1
    limited="$T/no-$absent"; mkdir -p "$limited"
    for tool in dash cat grep sed tail tr awk basename logger ip sleep; do
        if [ "$tool" = sleep ]; then ln -sf "$CLK/sleep" "$limited/$tool"
        else ln -sf "$(command -v "$tool")" "$limited/$tool"; fi
    done
    if [ "$absent" = timeout ]; then ln -sf "$BIN/ifquery" "$limited/ifquery"
    else ln -sf "$BIN/timeout" "$limited/timeout"; fi
    echo '0.00 0.00' > "$UPTIME"
    PATH="$limited" dash "$WAIT" eth0 >/dev/null 2>&1; rc=$?
    if [ "$rc" = 0 ] && [ ! -s "$CALLS" ] && grep -q 'bounded query unavailable' "$LOGGED"; then
        ok "wait: missing $absent defers safely without unbounded execution"
    else bad "wait: missing $absent was not safe"; fi
done
reset; seed_iface eth0 1; seed_iface eth1 1; conf_static eth0 ''; conf_static eth1 ''
printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
QUERY_DELAY=1 run_wait --auto
if [ "$rc" = 0 ] && [ "$el" = 2 ] && [ "$(grep -c '^timeout 2 ifquery' "$CALLS")" = 2 ]; then
    ok 'wait: query cost is included in the shared uptime budget'
else bad 'wait: effective query time escaped the budget'; fi
reset; seed_iface eth0 1; seed_iface eth1 1; conf_static eth0 ''; conf_static eth1 ''
printf 'IFUP_CARRIER_WAIT_SECS=2\n' > "$NETCONF"
QUERY_DELAY=1 run_wait --auto
if [ "$rc" = 0 ] && [ "$el" = 2 ] && grep -q '^timeout 1 ifquery' "$CALLS" && ! grep -q '^ip ' "$CALLS"; then
    ok 'wait: query timeout shrinks to the single remaining second'
else bad 'wait: query timeout exceeded remaining budget'; fi

# Model ifupdown's observed address -> MAC/up -> route -> ifstate sequence.
# sun4i rejects a MAC assignment on an already-UP link, even the same address.
model_ifup() {
    [ ! -e "$T/ifstate" ] || return 0
    [ ! -e "$T/address" ] || return 17
    touch "$T/address"
    [ ! -e "$SYS/eth0/.up" ] || return 16
    touch "$SYS/eth0/.up" "$T/route" "$T/ifstate"
}

# Exercise the shipped MAC function without sourcing the boot script or using
# the host's sysfs/config/IP command. Only the extracted function runs.
MAC_SRC=${MAC_PRE_START_SRC:-etc/sa02m-pre-start.sh}
MAC_APPLY="$T/mac-apply.sh"
sed -n '/^sa02m_mac_apply() {/,/^}$/p' "$MAC_SRC" |
    sed -e "s#/sys/class/net/#$SYS/#g" -e "s#/etc/sa02m-mac\.conf#$T/mac.conf#g" > "$MAC_APPLY"
if ! grep -q '^sa02m_mac_apply() {' "$MAC_APPLY" || ! grep -q '^}$' "$MAC_APPLY" \
    || grep -Eq '"/sys/|/etc/sa02m-mac\.conf' "$MAC_APPLY"; then
    echo "FAIL  missing or unretargeted MAC function"; exit 1
fi
OLD_MAC=02:53:b4:4c:14:bf
STABLE_MAC=02:53:f9:42:bc:1f
seed_mac() {
    seed_iface eth0 '' 1
    printf '%s\n' "$OLD_MAC" > "$SYS/eth0/address"
    case "$1" in
        missing) ;;
        0x1003) printf '%s\n' "$1" > "$SYS/eth0/flags"; touch "$SYS/eth0/.up" ;;
        *) printf '%s\n' "$1" > "$SYS/eth0/flags" ;;
    esac
}
run_mac_apply() (
    SA02M_MAC_ETH0=$STABLE_MAC
    SA02M_MAC_ETH1=02:53:f9:42:bc:20
    logp() { printf '%s\n' "$*" >> "$LOGGED"; }
    ip() {
        printf '%s\n' "$*" >> "$CALLS"
        [ "$#" -ge 5 ] && [ "$1 $2 $3" = "link set dev" ] && [ "$4" = eth0 ] || return 2
        [ "${MAC_FAIL_ACTION:-}" != "$5" ] || return 1
        case "$5" in
            down) rm -f "$SYS/eth0/.up" ;;
            address) [ ! -e "$SYS/eth0/.up" ] && printf '%s\n' "$6" > "$SYS/eth0/address" ;;
            up) touch "$SYS/eth0/.up" ;;
            *) return 2 ;;
        esac
    }
    . "$MAC_APPLY"
    sa02m_mac_apply
)

reset; conf_static eth0 "hwaddress ether $STABLE_MAC"
seed_mac 0x1002
run_mac_apply
if [ "$(cat "$SYS/eth0/address")" = "$STABLE_MAC" ] && [ ! -e "$SYS/eth0/.up" ] \
    && [ "$(cat "$CALLS")" = "link set dev eth0 address $STABLE_MAC" ]; then
    ok 'pre-start: stable MAC is applied without raising an initially DOWN interface'
else bad 'pre-start: stable MAC assignment disturbed an initially DOWN interface'; fi
run_wait eth0
model_ifup; first=$?; model_ifup; second=$?
if [ "$rc:$first:$second" = 0:0:0 ] && [ -e "$T/route" ] && [ -e "$T/ifstate" ]; then
    ok 'wait: pre-start and ifup complete once and ifstate avoids EEXIST'
else bad "wait: pre-start/ifup failed first=$first second=$second (16=EBUSY 17=EEXIST)"; fi

reset; seed_mac 0x1003; run_mac_apply
expected=$(printf 'link set dev eth0 down\nlink set dev eth0 address %s\nlink set dev eth0 up\n' "$STABLE_MAC")
if [ "$(cat "$SYS/eth0/address")" = "$STABLE_MAC" ] && [ -e "$SYS/eth0/.up" ] \
    && [ "$(cat "$CALLS")" = "$expected" ]; then
    ok 'pre-start: initially UP interface is restored after MAC assignment'
else bad 'pre-start: initially UP interface was not restored'; fi

reset; seed_mac 0x1003; MAC_FAIL_ACTION=down run_mac_apply
if [ "$(cat "$CALLS")" = 'link set dev eth0 down' ] && [ -e "$SYS/eth0/.up" ] \
    && [ "$(cat "$SYS/eth0/address")" = "$OLD_MAC" ]; then
    ok 'pre-start: failed down stops MAC assignment'
else bad 'pre-start: attempted to assign MAC after a failed down'; fi

reset; seed_mac 0x1003; MAC_FAIL_ACTION=address run_mac_apply
if [ "$(cat "$CALLS")" = "$expected" ] && [ -e "$SYS/eth0/.up" ] \
    && [ "$(cat "$SYS/eth0/address")" = "$OLD_MAC" ]; then
    ok 'pre-start: failed MAC assignment still restores an initially UP interface'
else bad 'pre-start: failed MAC assignment lost the original UP state'; fi

reset; seed_mac 0x1003; MAC_FAIL_ACTION=up run_mac_apply
if [ "$(cat "$CALLS")" = "$expected" ] && [ ! -e "$SYS/eth0/.up" ] \
    && grep -q 'восстановить' "$LOGGED"; then
    ok 'pre-start: failure to restore UP is logged'
else bad 'pre-start: failure to restore UP was not diagnosed'; fi

for flags in missing '' malformed 0x 0x1002x; do
    reset; seed_mac "$flags"; run_mac_apply
    if [ ! -s "$CALLS" ] && [ "$(cat "$SYS/eth0/address")" = "$OLD_MAC" ]; then
        ok "pre-start: unreadable or malformed flags [$flags] leave MAC and link untouched"
    else bad "pre-start: changed link despite unknown flags [$flags]"; fi
done

reset; seed_mac 0x1003; printf '%s\n' "$STABLE_MAC" > "$SYS/eth0/address"; run_mac_apply
if [ ! -s "$CALLS" ] && [ -e "$SYS/eth0/.up" ]; then
    ok 'pre-start: matching MAC leaves an initially UP interface untouched'
else bad 'pre-start: matching MAC caused link changes'; fi

# ═══ wiring — the units and drop-ins the mechanism depends on ══════════════

if grep -q '^After=networking.service' "$UNIT" \
   && grep -q '^ExecStart=/usr/local/sbin/sa02m-dns-ensure.sh --all' "$UNIT" \
   && grep -q '^Type=oneshot' "$UNIT" \
   && grep -q '^WantedBy=multi-user.target' "$UNIT" \
   && ! grep -q '^Before=' "$UNIT"; then
    ok "wiring: the belt unit is ordering-only (After=, never Before=) and boot-scheduled"
else
    bad "wiring: sa02m-dns-ensure.service ordering/exec pins broken"
fi

if grep -q '^ExecStartPre=-/usr/local/sbin/sa02m-wait-carrier.sh %I$' "$DROPIN_IFUP" \
   && grep -q '^ExecStartPre=-/usr/local/sbin/sa02m-wait-carrier.sh --auto$' "$DROPIN_NET"; then
    ok "wiring: both bring-up paths carry the wait, both with the '-' (never-fail) prefix"
else
    bad "wiring: a carrier-wait drop-in is missing, mis-targeted, or lost its '-' prefix"
fi

# The installer is the only path the drop-ins reach a board by (they are NOT on
# the OTA allowlist — /etc/systemd/system/sa02m-* does not match a drop-in dir).
inst=scripts/02-network.sh
if grep -q 'usr/local/sbin/sa02m-dns-ensure.sh' "$inst" \
   && grep -q 'usr/local/sbin/sa02m-wait-carrier.sh' "$inst" \
   && grep -q 'ifup@.service.d' "$inst" \
   && grep -q 'networking.service.d' "$inst" \
   && grep -q 'sa02m_svc_apply sa02m-dns-ensure.service infra$' "$inst"; then
    ok "wiring: the installer provisions both helpers, both drop-ins, and enables the belt without start"
else
    bad "wiring: scripts/02-network.sh does not provision the full set"
fi

# ═══ the OTA bootstrap shim (usr/local/sbin/sa02m-eth-coldboot.sh) ═════════
# The online update generates its manifest from the runner ALREADY INSTALLED on
# the device and re-execs that same old binary before deploying, so a board
# coming from a pre-1.0.6.6 runner gets the belt unit DEPLOYED BUT DISABLED —
# the new services.enable entry only bites on the NEXT update. The shim closes
# that one-release gap from a unit already enabled on every board. Extract and
# run the SHIPPED function against a scripted systemctl shim.
COLD_SRC=${COLDBOOT_SRC:-usr/local/sbin/sa02m-eth-coldboot.sh}
[ -s "$COLD_SRC" ] || { echo "FAIL  missing or empty: $COLD_SRC"; exit 1; }
SHIM_UNIT="$T/sa02m-dns-ensure.service"
SHIM_MARK="$T/state/dns-ensure-enabled.once"
{ grep -E '^DNS_ENSURE_UNIT=' "$COLD_SRC"
  printf 'DNS_ENSURE_UNIT_FILE=%s\n' "$SHIM_UNIT"
  printf 'DNS_BOOTSTRAP_MARKER=%s\n' "$SHIM_MARK"
  sed -n '/^dns_ensure_bootstrap() {/,/^}$/p' "$COLD_SRC"
} > "$T/bootstrap.sh"
# The scripted systemctl shim is installed UNCONDITIONALLY and stays on PATH
# until the last run of shipped bootstrap code (the POSIX-shell dialect run
# below). dns_ensure_bootstrap calls `systemctl is-enabled` / `systemctl
# enable` by name, so any run of it without this shim reaches the host's real
# systemctl — which, on a host whose answer is `disabled`, ENABLES a unit on
# the machine running the suite. Until 2026-09-27 the shim was removed at the
# end of this block while the dialect run still executed the shipped function.
SC="$T/systemctl.log"
cat > "$BIN/systemctl" <<SHIM
#!/bin/bash
echo "systemctl \$*" >> "$SC"
if [ "\${1:-}" = "is-enabled" ]; then printf '%s\n' "\$(cat "$T/is-enabled" 2>/dev/null)"; exit 0; fi
if [ "\${1:-}" = "enable" ]; then [ -n "\${SC_ENABLE_FAIL:-}" ] && exit 1; printf 'enabled\n' > "$T/is-enabled"; fi
exit 0
SHIM
chmod +x "$BIN/systemctl"
if ! grep -q '^dns_ensure_bootstrap() {' "$T/bootstrap.sh" \
   || ! grep -q 'systemctl enable' "$T/bootstrap.sh" \
   || ! grep -Fq "DNS_ENSURE_UNIT_FILE=$SHIM_UNIT" "$T/bootstrap.sh" \
   || ! grep -q '^dns_ensure_bootstrap$' "$COLD_SRC"; then
    bad "harness: bootstrap extraction empty/un-retargeted, or the shim is never CALLED in coldboot"
else
    # `: >` not `rm -f`: grep -c on a MISSING file prints nothing, so enables()
    # would return the empty string and every =0 comparison would misfire.
    boot() { : > "$SC"; ( log() { :; }; . "$T/bootstrap.sh"; dns_ensure_bootstrap ); return $?; }
    enables() { grep -c '^systemctl enable' "$SC" 2>/dev/null || true; }

    rm -rf "$T/state" "$SHIM_UNIT"; printf 'disabled\n' > "$T/is-enabled"
    boot; rc=$?
    if [ "$rc" = 0 ] && [ "$(enables)" = 0 ] && [ ! -e "$SHIM_MARK" ]; then
        ok "bootstrap: no unit file on this board = pure no-op, no marker, rc=0"
    else
        bad "bootstrap: acted without a unit file (rc=$rc enables=$(enables))"
    fi

    rm -rf "$T/state"; : > "$SHIM_UNIT"; printf 'disabled\n' > "$T/is-enabled"
    boot
    if [ "$(enables)" = 1 ] && [ -e "$SHIM_MARK" ]; then
        ok "bootstrap: a deployed-but-disabled belt unit is enabled once, and the marker is written"
    else
        bad "bootstrap: first pass did not enable (enables=$(enables) marker=$([ -e "$SHIM_MARK" ] && echo y || echo n))"
    fi

    printf 'disabled\n' > "$T/is-enabled"   # pretend the operator disabled it again
    boot; e2=$(enables); boot; e3=$(enables)
    if [ "$e2" = 0 ] && [ "$e3" = 0 ]; then
        ok "bootstrap: ONE-TIME — later boots never re-enable, so an operator's disable stays («removed stays removed»)"
    else
        bad "bootstrap: re-enabled on a later boot (e2=$e2 e3=$e3)"
    fi

    for st in enabled static masked masked-runtime ""; do
        rm -rf "$T/state"; : > "$SHIM_UNIT"; printf '%s\n' "$st" > "$T/is-enabled"
        boot
        if [ "$(enables)" != 0 ]; then
            bad "bootstrap: touched a unit already in state '${st:-unknown}'"
            st_bad=1
        fi
    done
    [ -n "${st_bad:-}" ] || ok "bootstrap: enabled/static/masked/unknown are all left exactly alone"

    rm -rf "$T/state"; : > "$SHIM_UNIT"; printf 'disabled\n' > "$T/is-enabled"
    : > "$SC"; ( log() { :; }; SC_ENABLE_FAIL=1; export SC_ENABLE_FAIL; . "$T/bootstrap.sh"; dns_ensure_bootstrap ); rc=$?
    if [ "$rc" = 0 ] && [ -e "$SHIM_MARK" ]; then
        ok "bootstrap: a FAILING systemctl enable still returns 0 and still marks (no retry storm on the boot path)"
    else
        bad "bootstrap: enable-failure path rc=$rc marker=$([ -e "$SHIM_MARK" ] && echo y || echo n)"
    fi
fi

# ═══ shell-dialect safety of the SHIPPED #!/bin/sh scripts ════════════════
# WHY THIS EXISTS: a `: > "$file"` marker write shipped here and survived every
# earlier neuter, because the whole harness runs under bash. `:` is a POSIX
# SPECIAL built-in — under dash (what the board's /bin/sh is) a redirection
# error on one ABORTS the script outright, and neither `2>/dev/null` nor
# `|| true` catches it. bash just continues, so a bash-only ratchet is blind to
# the entire class. Two layers below: a static scan that runs everywhere, and a
# real execution under a POSIX shell where one exists.

# Layer 1 — static, runs on every host.
POSIX_SCRIPTS="usr/local/sbin/sa02m-dns-ensure.sh usr/local/sbin/sa02m-wait-carrier.sh usr/local/sbin/sa02m-eth-coldboot.sh usr/local/sbin/sa02m-routes-ensure.sh"
SPECIAL_RE='^[[:space:]]*(:|\.|eval|exec|export|readonly|set|shift|times|trap|unset)([[:space:]]+[^;&|]*)?[0-9]*[<>]'
scanned=0; offenders=""
for f in $POSIX_SCRIPTS; do
    [ -s "$f" ] || { bad "dialect: $f missing or empty"; continue; }
    head -1 "$f" | grep -q '^#!/bin/sh' || { bad "dialect: $f is no longer #!/bin/sh — re-check this scan's premise"; continue; }
    scanned=$((scanned + 1))
    if grep -Eq "$SPECIAL_RE" "$f"; then
        offenders="$offenders $f:$(grep -En "$SPECIAL_RE" "$f" | head -1 | cut -d: -f1)"
    fi
done
# Non-vacuous: the scan must have seen all four files AND must actually detect
# a known-bad line, else a broken regex would "pass" everything forever.
printf ': > "$x" 2>/dev/null || true\n' > "$T/badfixture.sh"
if [ "$scanned" != 4 ]; then
    bad "dialect: scanned $scanned/4 shipped POSIX scripts"
elif ! grep -Eq "$SPECIAL_RE" "$T/badfixture.sh"; then
    bad "dialect: the scan's own regex no longer detects a known-bad line"
elif [ -n "$offenders" ]; then
    bad "dialect: special built-in takes a redirection (aborts dash, uncatchable):$offenders"
else
    ok "dialect: no shipped #!/bin/sh script gives a redirection to a POSIX special built-in"
fi

# Layer 2 — real execution under a POSIX shell. `sh` is NOT assumed to be one:
# on this dev host /usr/bin/sh IS bash, which cannot see the class at all.
POSIX_SH=""
for cand in dash busybox_sh sh; do
    case "$cand" in
        busybox_sh) command -v busybox >/dev/null 2>&1 && POSIX_SH="busybox sh" ;;
        *) command -v "$cand" >/dev/null 2>&1 && POSIX_SH="$cand" ;;
    esac
    [ -n "$POSIX_SH" ] || continue
    # Reject a `sh` that is really bash — it would make this case vacuous.
    if [ -z "$($POSIX_SH -c 'echo ${BASH_VERSION:-}' 2>/dev/null)" ]; then break; fi
    POSIX_SH=""
done
if [ -z "$POSIX_SH" ]; then
    printf 'skip  dialect: no strict POSIX shell here (sh is bash) — layer 1 static scan is the enforcing check; CI/Linux runs this\n'
else
    # Make the marker's parent IMPOSSIBLE to create: put it under a regular file
    # (mkdir -p fails ENOTDIR), which is the RO-rootfs / ENOSPC case in miniature.
    : > "$T/notadir"
    : > "$T/unitfile"
    { grep -E '^DNS_ENSURE_UNIT=' "$COLD_SRC"
      printf 'DNS_ENSURE_UNIT_FILE=%s\n' "$T/unitfile"
      printf 'DNS_BOOTSTRAP_MARKER=%s\n' "$T/notadir/sub/x.once"
      sed -n '/^dns_ensure_bootstrap() {/,/^}$/p' "$COLD_SRC"
    } > "$T/bootstrap_ro.sh"
    { printf '#!/bin/sh\nlog() { :; }\n. "%s"\ndns_ensure_bootstrap\necho REACHED_END\nexit 0\n' "$T/bootstrap_ro.sh"
    } > "$T/dialect_driver.sh"
    printf 'disabled\n' > "$T/is-enabled"
    : > "$SC"
    dsc=$(command -v systemctl 2>/dev/null)
    if [ "$dsc" != "$BIN/systemctl" ]; then
        # Host-safety precondition: never run the shipped function against a
        # systemctl that is not the shim (it would act on this host's units).
        bad "dialect: systemctl resolves to '${dsc:-<not found>}', not the shim — the POSIX-shell run is REFUSED (it would reach the host)"
    else
        dout=$($POSIX_SH "$T/dialect_driver.sh" 2>&1); drc=$?
        if [ "$drc" = 0 ] && [ "${dout#*REACHED_END}" != "$dout" ] && grep -q '^systemctl is-enabled ' "$SC"; then
            ok "dialect: under $POSIX_SH an unwritable marker does NOT abort the shim — it runs on to completion (systemctl answered by the shim)"
        else
            bad "dialect: under $POSIX_SH the shim died on an unwritable marker, or never asked the systemctl shim (rc=$drc out='$dout' shim-calls='$(tr '\n' ';' < "$SC")')"
        fi
    fi

    # Both helpers are #!/bin/sh but every case above ran them through the
    # shebang, which on this host resolves to bash — so until now NOTHING had
    # ever executed them under the interpreter the board actually uses. Run a
    # representative set under the real POSIX shell.
    reset
    conf_static eth0 "dns-nameservers 77.88.8.8 77.88.8.1"
    : > "$RESOLV"
    d1=$($POSIX_SH "$DNS" eth0 2>&1); r1=$?
    seed_iface eth0 1
    printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
    d2=$($POSIX_SH "$WAIT" eth0 2>&1); r2=$?
    d3=$($POSIX_SH "$WAIT" --auto 2>&1); r3=$?
    # 08 is the A4 input: under a POSIX shell an un-stripped leading zero is a
    # FATAL octal error, so this is the one that would really have caught it.
    printf 'IFUP_CARRIER_WAIT_SECS=08\n' > "$NETCONF"
    d4=$($POSIX_SH "$WAIT" eth0 2>&1); r4=$?
    printf 'IFUP_CARRIER_WAIT_SECS=0\n' > "$NETCONF"
    d5=$($POSIX_SH "$WAIT" eth0 2>&1); r5=$?
    if [ "$r1" = 0 ] && [ "$r2" = 0 ] && [ "$r3" = 0 ] && [ "$r4" = 0 ] && [ "$r5" = 0 ] \
       && [ -z "$d1$d2$d3$d4$d5" ] \
       && [ "$(record eth0)" = "$(printf 'nameserver 77.88.8.8\nnameserver 77.88.8.1')" ]; then
        ok "dialect: both shipped helpers run clean under $POSIX_SH (incl. the octal budget 08), rc=0, no stderr"
    else
        bad "dialect: a helper misbehaved under $POSIX_SH (rc=$r1/$r2/$r3/$r4/$r5 out='$d1$d2$d3$d4$d5')"
    fi
fi
command rm -f "$BIN/systemctl"   # no shipped code runs past this point

# Deploying the unit file is NOT the guarantee: the runner only enables what the
# manifest's services.enable lists. ONLINE half: the runner's own generator
# carries the entry and its enable loop cannot fail the apply. OFFLINE half:
# since the frozen-v1 freeze (1.0.6.37, e1f1b6a) the packer emits enable[] ONLY
# when its advertised MIN_UPDATER is >= SERVICES_OPTIONAL_SINCE — while it is
# below, an offline pack enables NOTHING and the belt unit reaches an
# offline-updated board through the one-time dns_ensure_bootstrap shim in
# sa02m-eth-coldboot.sh (run behaviourally above). So the offline pin is
# three-fold: (a) the entry is still in the packer's optional-tier enable[], so
# it returns the moment the tier turns on — the release that raises MIN_UPDATER
# to >= 1.0.6.37, earliest 1.0.6.40, once the fleet reports a derived
# UPDATER_VERSION (runner >= 1.0.6.39; audit 2026-09-08, decision O1); (b) the
# freeze is the TIER gate, not a deleted block; (c) the shim is CALLED from the
# coldboot script body — the mechanism carrying the guarantee meanwhile.
# Removing the entry from EITHER generator FAILS; so does deleting the shim call.
runner=etc/sa02m-update-runner.sh
packer=scripts/pack-offline-update.py
enable_has() {  # is sa02m-dns-ensure.service inside $1's enable block opened by $2?
    # Range end is the block terminator on its OWN line, not any `]`: a comment
    # inside the block mentioning `restart[]` would otherwise close it early and
    # this case would fail on correct code.
    sed -n "/$2/,/^[[:space:]]*\],\{0,1\}[[:space:]]*\$/p" "$1" \
      | grep -q '"sa02m-dns-ensure.service"'
}
if enable_has "$runner" '"enable": \[' \
   && grep -Eq '_systemctl_bounded [0-9]+ enable "\$u" \|\| true|systemctl enable "\$u" 2>/dev/null \|\| true' "$runner"; then
    ok "wiring (online): the belt unit is in the runner's services.enable, and the enable loop cannot fail the apply"
else
    bad "wiring (online): sa02m-dns-ensure.service missing from the runner's enable list or the enable loop lost its || true"
fi
if enable_has "$packer" 'services\["enable"\] = \[' \
   && grep -q 'if not emit_optional_service_keys(min_updater):' "$packer" \
   && grep -q '^dns_ensure_bootstrap$' "$COLD_SRC"; then
    ok "wiring (offline): the belt unit is in the packer's tier enable[], the freeze is the tier gate, and the coldboot shim carries it meanwhile"
else
    bad "wiring (offline): the belt unit left the packer's tier enable[], the tier gate is gone, or the coldboot shim is no longer called — an offline-updated board never enables the belt"
fi

# The recovery ladder's two call sites — a belt that only lives in the unit
# inherits none of the "cable arrived later" coverage.
#
# COMMENT-STRIPPED, not raw grep: a `#` in front of either call leaves the text
# in the file, so a raw `grep -c` counted a DISABLED call site as a live one and
# this assertion could not go RED (shape (a), found by the coverage sweep of
# review Q6, 1.0.6.24). lib_check.sh is the one home for the stripping; an
# unavailable lib FAILS the case rather than skipping it.
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_count >/dev/null; then
    if [ "$(stripped_count etc/fix-eth.sh 'dns_ensure "\$iface"')" -ge 2 ] \
       && [ "$(stripped_count usr/local/sbin/sa02m-eth-coldboot.sh 'dns_ensure "\$IFACE"')" -ge 2 ]; then
        ok "wiring: the ladder calls the belt on both fix-eth and both coldboot paths"
    else
        bad "wiring: a recovery-ladder call site is missing (or commented out)"
    fi
else
    bad "wiring: .ai-dev/quality/checks/lib_check.sh could not be sourced — the recovery-ladder call sites were NOT verified (a skip is not a pass)"
fi

# ── harness non-vacuity ────────────────────────────────────────────────────
[ -s "$CALLS_ALL" ] || bad "harness: no shim was ever invoked — the cases are not exercising the helpers"

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-iface-dns-ensure.sh"
    exit 0
fi
echo "FAIL  test-iface-dns-ensure.sh: $fails failing case(s)"
exit 1
