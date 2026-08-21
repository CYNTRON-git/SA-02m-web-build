#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-wait-carrier.sh — bounded PHY-carrier wait before an ifupdown bring-up
#
# Why this exists: on a cold boot the canonical rename emits a udev uevent,
# /lib/udev/ifupdown-hotplug turns it into `ifup@eth0` (eth0 qualifies through
# the script's `auto` fallback), and that early run reaches the static method's
# `gateway` step while the PHY has not negotiated yet. `ip route add default`
# fails with "Nexthop device is not up", ifup aborts BEFORE its if-up.d stage,
# and /etc/network/if-up.d/000resolvconf therefore never runs — the board ends
# up with an EMPTY /etc/resolv.conf while address and default route look
# healthy. Waiting for carrier==1 (which implies IFF_UP) makes that first
# bring-up succeed instead.
# Contract: docs/contracts/boot-network-dns.md
#
# Wired as `ExecStartPre=-` through two drop-ins:
#   /etc/systemd/system/ifup@.service.d/sa02m-carrier-wait.conf       -> %I
#   /etc/systemd/system/networking.service.d/sa02m-carrier-wait.conf  -> --auto
# Both are needed: with only the ifup@ drop-in, networking.service can start
# its own `ifup -a` while our pre-step is still waiting, configure eth0 too
# early itself and hand the duplicate failure right back.
# ExecStartPre runs BEFORE ifup, so this holds no ifupdown lock while waiting,
# and the panel's live apply (which calls /sbin/ifup directly, not the unit)
# pays no added delay.
#
# NEVER blocks and NEVER fails a unit: unconditional `exit 0` plus the
# drop-ins' `-` prefix. Worst case on a port with no cable is
# IFUP_CARRIER_WAIT_SECS of extra boot time, then exactly today's behaviour.
#
# Usage: sa02m-wait-carrier.sh <iface> | --auto
# ═══════════════════════════════════════════════════════════════════════════
set -u

NET_CONF=/etc/sa02m_network.conf
IFACE_CONF_DIR=/etc/network/interfaces.d
SYS_NET_DIR=/sys/class/net

DEFAULT_WAIT_SECS=10
# Hard ceiling on the budget. `ifup@.service` and `networking.service` both
# carry TimeoutStartUSec=5min on this platform: a budget that approached it
# would turn a late PHY into a FAILED unit — the very failure this removes.
# 120 s keeps a >2x margin, and an operator value above it is clamped AND
# logged, so a future bump cannot silently cross the start timeout.
MAX_WAIT_SECS=120

log() { logger -t sa02m-wait-carrier -- "$*" 2>/dev/null || true; }

now() { date +%s; }

# Interface names reach `ip link` and a /sys path, so they are allow-listed
# before use rather than quoted-and-hoped (web-code-rigor.md, CGI/system floors).
valid_iface() {
    case "$1" in
        ''|.|..|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    [ "${#1}" -le 15 ] || return 1
    return 0
}

# The `auto` static interfaces that declare a gateway — exactly the set whose
# ifup can fail on the gateway step. Deliberately NOT every conf: a dhcp or
# gateway-less stanza has no route step to lose.
auto_static_gw_ifaces() {
    for conf in "$IFACE_CONF_DIR"/*.conf; do
        [ -f "$conf" ] || continue
        name=$(basename "$conf" .conf)
        valid_iface "$name" || continue
        [ "$name" = "lo" ] && continue
        grep -Eq "^[[:space:]]*auto([[:space:]]+[^[:space:]]+)*[[:space:]]+${name}([[:space:]]|\$)" "$conf" || continue
        grep -Eq "^[[:space:]]*iface[[:space:]]+${name}[[:space:]]+inet[[:space:]]+static([[:space:]]|\$)" "$conf" || continue
        grep -Eq "^[[:space:]]*gateway[[:space:]]+[^[:space:]]" "$conf" || continue
        printf '%s\n' "$name"
    done
}

# The budget is READ, not sourced. /etc/sa02m_network.conf is root-owned but
# hand-edited, and this runs as root before every ifup — sourcing it would
# execute whatever is in it at that point. Extracting digits only is the
# allow-list at that boundary.
read_budget() {
    v=""
    if [ -f "$NET_CONF" ]; then
        v=$(sed -n 's/^[[:space:]]*IFUP_CARRIER_WAIT_SECS[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*$/\1/p' \
            "$NET_CONF" 2>/dev/null | tail -n 1)
    fi
    [ -n "$v" ] || v=$DEFAULT_WAIT_SECS
    # Strip leading zeros BEFORE any arithmetic. `[ -gt ]` reads base 10 but
    # `$(( ))` reads a leading zero as OCTAL: `08`/`09` abort the shell with
    # "value too great for base" — so the script would never reach its
    # unconditional exit 0, the one input that could break that promise — and
    # `010` would silently mean 8 s rather than 10.
    while :; do
        case "$v" in
            0[0-9]*) v=${v#0} ;;
            *) break ;;
        esac
    done
    [ -n "$v" ] || v=0
    # Guard the comparison itself before making it: a 20-digit value can
    # overflow `[ -gt ]` in some shells, which would SKIP the clamp below and
    # produce an unbounded deadline — the one way this script could hang a
    # boot. Anything over six digits is treated as "too large" and clamped.
    [ "${#v}" -le 6 ] || v=$((MAX_WAIT_SECS + 1))
    if [ "$v" -gt "$MAX_WAIT_SECS" ]; then
        log "IFUP_CARRIER_WAIT_SECS=${v} exceeds the ${MAX_WAIT_SECS}s ceiling (ifup@ start timeout is 5 min) — clamped"
        v=$MAX_WAIT_SECS
    fi
    printf '%s\n' "$v"
}

# 0 on carrier, 1 on budget expiry. Never loops past $deadline.
wait_one() {
    _if=$1
    [ -d "$SYS_NET_DIR/$_if" ] || return 0
    _raised=0
    while : ; do
        _c=$(cat "$SYS_NET_DIR/$_if/carrier" 2>/dev/null || echo "")
        [ "$_c" = "1" ] && return 0
        if [ -z "$_c" ] && [ "$_raised" = "0" ]; then
            # The kernel refuses a carrier read on an admin-DOWN device
            # (carrier_show() returns -EINVAL when !netif_running), and before
            # ifup runs the interface usually IS down — so without raising it
            # first the wait could never observe a live link and would burn the
            # whole budget on every boot, cable or no cable. ifup raises the
            # link itself seconds later; this only brings that forward.
            ip link set "$_if" up 2>/dev/null || true
            _raised=1
            continue
        fi
        [ "$(now)" -lt "$deadline" ] || return 1
        sleep 1
    done
}

# Without a working `date` the deadline cannot be computed, and an unbounded
# wait is exactly what this must never be — so skip the wait entirely and leave
# today's behaviour. Fail toward not-waiting, never toward waiting forever.
date +%s >/dev/null 2>&1 || exit 0

budget=$(read_budget)
# 0 disables the wait entirely — a total escape hatch, no side effects at all.
[ "$budget" = "0" ] && exit 0

targets=""
case "${1:-}" in
    --auto)
        targets=$(auto_static_gw_ifaces)
        ;;
    "")
        log "no interface argument — nothing to wait for"
        exit 0
        ;;
    -*)
        log "unknown option — nothing to wait for"
        exit 0
        ;;
    *)
        # Never echo the rejected value back into the log.
        valid_iface "$1" || { log "refusing a malformed interface name"; exit 0; }
        [ "$1" = "lo" ] && exit 0
        targets=$1
        ;;
esac

if [ -z "$targets" ]; then
    log "no matching interface — nothing to wait for"
    exit 0
fi

start=$(now)
# ONE shared deadline across every target: the budget bounds the whole run, not
# each interface, so a 2-eth board with both ports unplugged still costs the
# budget once.
deadline=$(( start + budget ))
missed=""
for iface in $targets; do
    wait_one "$iface" || missed="$missed $iface"
done
elapsed=$(( $(now) - start ))

# Logged on EVERY run, not only when it waited. The default 10 s budget came
# from ONE observed boot, so "how long the wait actually took" is the telemetry
# that confirms or revises it by measurement (docs/contracts/boot-network-dns.md
# §4, §9). Do not make this conditional.
flat() { printf '%s' "$1" | tr '\n' ' '; }
if [ -z "$missed" ]; then
    log "carrier up for [$(flat "$targets")] after ${elapsed}s (budget ${budget}s)"
else
    log "no carrier for [$(flat "$missed")] after ${elapsed}s — budget ${budget}s expired, proceeding anyway"
fi

exit 0
