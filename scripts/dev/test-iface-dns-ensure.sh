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
# `ip` are recording PATH shims. No root, no device, no systemd, nothing
# touches the real system. There is deliberately NO env seam in the shipped
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
# check and the budget-expiry case hangs / fails). Neither override ever edits
# the real tree.
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
    [ -f "$SYS/\$_i/.pending" ] && cp "$SYS/\$_i/.pending" "$SYS/\$_i/carrier"
fi
exit 0
SHIM
chmod +x "$BIN/resolvconf" "$BIN/logger" "$BIN/ip"
PATH="$BIN:$PATH"; export PATH

reset() {
    rm -rf "$CD" "$RC_RUN" "$SYS"; mkdir -p "$CD" "$RC_RUN" "$SYS"
    : > "$CALLS"; : > "$LOGGED"; : > "$RESOLV"; : > "$NETCONF"
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
BIN2="$T/bin-nores"; mkdir -p "$BIN2"
command cp "$BIN/logger" "$BIN/ip" "$BIN2/"
reset
conf_static eth0 "dns-nameservers 77.88.8.8"
: > "$RESOLV"
if PATH="$BIN2:/usr/bin:/bin" command -v resolvconf >/dev/null 2>&1; then
    bad "harness: resolvconf is reachable in the 'absent' PATH — the case cannot test anything"
else
    out=$(PATH="$BIN2:/usr/bin:/bin" "$DNS" eth0 2>&1); rc=$?
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
timed() {  # prints elapsed seconds of "$@"
    local s e; s=$(date +%s); "$@" >/dev/null 2>&1; e=$(date +%s); echo $((e - s))
}

reset
seed_iface eth0 1
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'IFUP_CARRIER_WAIT_SECS=5\n' > "$NETCONF"
el=$(timed "$WAIT" eth0)
if [ "$el" -le 2 ] && grep -q 'carrier up for \[eth0\]' "$LOGGED"; then
    ok "wait: returns immediately when carrier is already up (${el}s)"
else
    bad "wait: carrier-up path took ${el}s / did not log"
fi

reset
seed_iface eth0 0
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'IFUP_CARRIER_WAIT_SECS=2\n' > "$NETCONF"
s=$(date +%s); "$WAIT" eth0 >/dev/null 2>&1; rc=$?; el=$(( $(date +%s) - s ))
if [ "$rc" = 0 ] && [ "$el" -ge 2 ] && [ "$el" -le 6 ] && grep -q 'budget 2s expired' "$LOGGED"; then
    ok "wait: TERMINATES at the budget on a carrier-less port, rc=0 (${el}s)"
else
    bad "wait: budget expiry rc=$rc elapsed=${el}s"
fi

reset
seed_iface eth0 0
printf 'IFUP_CARRIER_WAIT_SECS=0\n' > "$NETCONF"
s=$(date +%s); "$WAIT" eth0 >/dev/null 2>&1; rc=$?; el=$(( $(date +%s) - s ))
if [ "$rc" = 0 ] && [ "$el" -le 1 ] && [ ! -s "$LOGGED" ]; then
    ok "wait: IFUP_CARRIER_WAIT_SECS=0 disables it completely (no wait, no side effect)"
else
    bad "wait: the 0 escape hatch did not disable (rc=$rc elapsed=${el}s)"
fi

# An admin-DOWN device answers -EINVAL to a carrier read, so without raising the
# link first the wait could never observe a live link and would burn the whole
# budget on EVERY boot, cable or no cable.
reset
seed_iface eth0 "" 1
printf 'IFUP_CARRIER_WAIT_SECS=6\n' > "$NETCONF"
el=$(timed "$WAIT" eth0)
if [ "$el" -le 2 ] && grep -q '^ip link set eth0 up$' "$CALLS" && grep -q 'carrier up' "$LOGGED"; then
    ok "wait: an admin-down device is raised first, then carrier is observed (${el}s)"
else
    bad "wait: admin-down path took ${el}s / no 'ip link set up'"
fi

reset
seed_iface eth0 1; seed_iface eth1 0; seed_iface eth2 0
conf_static eth0 "dns-nameservers 77.88.8.8"
printf 'auto eth1\niface eth1 inet dhcp\n' > "$CD/eth1.conf"
printf 'auto eth2\niface eth2 inet static\n    address 10.0.0.2\n    netmask 255.255.255.0\n' > "$CD/eth2.conf"
printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
el=$(timed "$WAIT" --auto)
line=$(grep 'carrier' "$LOGGED" | tail -n 1)
if [ "$el" -le 2 ] && [ "${line#*eth0}" != "$line" ] \
   && [ "${line#*eth1}" = "$line" ] && [ "${line#*eth2}" = "$line" ]; then
    ok "wait: --auto selects only auto+static+gateway interfaces (dhcp and gateway-less skipped)"
else
    bad "wait: --auto selection wrong (${el}s): $line"
fi

reset
seed_iface eth0 1
printf 'IFUP_CARRIER_WAIT_SECS=9999\n' > "$NETCONF"
"$WAIT" eth0 >/dev/null 2>&1
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
s=$(date +%s); "$WAIT" eth0 >/dev/null 2>&1; rc=$?; el=$(( $(date +%s) - s ))
if [ "$rc" = 0 ] && [ "$el" -le 5 ] && grep -q 'exceeds the 120s ceiling' "$LOGGED"; then
    ok "wait: an arithmetic-overflow-wide budget is still clamped, never unbounded (${el}s)"
else
    bad "wait: overflow-wide budget was not clamped (rc=$rc elapsed=${el}s)"
fi

reset
seed_iface eth0 0
printf 'IFUP_CARRIER_WAIT_SECS=3\n' > "$NETCONF"
el=$(timed "$WAIT" 'eth0;reboot')
if [ "$el" -le 1 ] && grep -q 'malformed interface name' "$LOGGED" \
   && ! grep -q 'eth0;reboot' "$LOGGED"; then
    ok "wait: a malformed interface name is refused before it reaches a path or ip(8)"
else
    bad "wait: malformed name not refused (${el}s)"
fi

reset
seed_iface lo 0
printf 'IFUP_CARRIER_WAIT_SECS=5\n' > "$NETCONF"
el=$(timed "$WAIT" lo)
if [ "$el" -le 1 ]; then
    ok "wait: lo is never waited on (ifup@lo pays nothing)"
else
    bad "wait: lo waited ${el}s"
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
if ! grep -q '^dns_ensure_bootstrap() {' "$T/bootstrap.sh" \
   || ! grep -q 'systemctl enable' "$T/bootstrap.sh" \
   || ! grep -Fq "DNS_ENSURE_UNIT_FILE=$SHIM_UNIT" "$T/bootstrap.sh" \
   || ! grep -q '^dns_ensure_bootstrap$' "$COLD_SRC"; then
    bad "harness: bootstrap extraction empty/un-retargeted, or the shim is never CALLED in coldboot"
else
    SC="$T/systemctl.log"
    cat > "$BIN/systemctl" <<SHIM
#!/bin/bash
echo "systemctl \$*" >> "$SC"
if [ "\${1:-}" = "is-enabled" ]; then printf '%s\n' "\$(cat "$T/is-enabled" 2>/dev/null)"; exit 0; fi
if [ "\${1:-}" = "enable" ]; then [ -n "\${SC_ENABLE_FAIL:-}" ] && exit 1; printf 'enabled\n' > "$T/is-enabled"; fi
exit 0
SHIM
    chmod +x "$BIN/systemctl"
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
    command rm -f "$BIN/systemctl"
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
POSIX_SCRIPTS="usr/local/sbin/sa02m-dns-ensure.sh usr/local/sbin/sa02m-wait-carrier.sh usr/local/sbin/sa02m-eth-coldboot.sh"
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
# Non-vacuous: the scan must have seen all three files AND must actually detect
# a known-bad line, else a broken regex would "pass" everything forever.
printf ': > "$x" 2>/dev/null || true\n' > "$T/badfixture.sh"
if [ "$scanned" != 3 ]; then
    bad "dialect: scanned $scanned/3 shipped POSIX scripts"
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
    dout=$($POSIX_SH "$T/dialect_driver.sh" 2>&1); drc=$?
    if [ "$drc" = 0 ] && [ "${dout#*REACHED_END}" != "$dout" ]; then
        ok "dialect: under $POSIX_SH an unwritable marker does NOT abort the shim — it runs on to completion"
    else
        bad "dialect: under $POSIX_SH the shim died on an unwritable marker (rc=$drc out='$dout')"
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

# Deploying the unit file is NOT the guarantee: the runner only enables what the
# manifest's services.enable lists, and that list is hardcoded in BOTH manifest
# generators. Missing from either one = the unit lands on a field device and
# stays inert forever (the defect this case now pins).
runner=etc/sa02m-update-runner.sh
packer=scripts/pack-offline-update.py
enable_has() {  # is sa02m-dns-ensure.service inside $1's "enable": [ ... ] block?
    # Range end is the block terminator on its OWN line, not any `]`: a comment
    # inside the block mentioning `restart[]` would otherwise close it early and
    # this case would fail on correct code.
    sed -n '/"enable": \[/,/^[[:space:]]*\],\{0,1\}[[:space:]]*$/p' "$1" \
      | grep -q '"sa02m-dns-ensure.service"'
}
if enable_has "$runner" && enable_has "$packer" \
   && grep -Eq '_systemctl_bounded [0-9]+ enable "\$u" \|\| true|systemctl enable "\$u" 2>/dev/null \|\| true' "$runner"; then
    ok "wiring: the belt unit is in services.enable in BOTH generators, and the enable loop cannot fail the apply"
else
    bad "wiring: sa02m-dns-ensure.service missing from a manifest enable list or the enable loop lost its || true"
fi

# The recovery ladder's two call sites — a belt that only lives in the unit
# inherits none of the "cable arrived later" coverage.
if grep -q 'dns_ensure "\$iface"' etc/fix-eth.sh \
   && [ "$(grep -c 'dns_ensure "\$iface"' etc/fix-eth.sh)" -ge 2 ] \
   && [ "$(grep -c 'dns_ensure "\$IFACE"' usr/local/sbin/sa02m-eth-coldboot.sh)" -ge 2 ]; then
    ok "wiring: the ladder calls the belt on both fix-eth and both coldboot paths"
else
    bad "wiring: a recovery-ladder call site is missing"
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
