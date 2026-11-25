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
UPTIME_FILE=/proc/uptime

DEFAULT_WAIT_SECS=10
# Hard ceiling on the budget. `ifup@.service` and `networking.service` both
# carry TimeoutStartUSec=5min on this platform: a budget that approached it
# would turn a late PHY into a FAILED unit — the very failure this removes.
# 120 s keeps a >2x margin, and an operator value above it is clamped AND
# logged, so a future bump cannot silently cross the start timeout.
MAX_WAIT_SECS=120

log() { logger -t sa02m-wait-carrier -- "$*" 2>/dev/null || true; }

# Whole Linux boot seconds (including suspend); never RTC/NTP wall time.
# Nine decimal digits leave room for the deadline even on 32-bit shells.
now() {
    _uptime=$(cat "$UPTIME_FILE" 2>/dev/null) || return 1
    _uptime=${_uptime%% *}
    case "$_uptime" in ''|*[!0-9.]*|.*|*.|*.*.*) return 1 ;; esac
    case "$_uptime" in *.*) ;; *) return 1 ;; esac
    _uptime=${_uptime%%.*}
    while :; do case "$_uptime" in 0[0-9]*) _uptime=${_uptime#0} ;; *) break ;; esac; done
    [ "${#_uptime}" -le 9 ] || return 1
    printf '%s\n' "$_uptime"
}

tick() {
    current=$(now) || return 1
    [ "$current" -ge "$last_tick" ] || return 1
    last_tick=$current
}

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

# 0 ready, 1 expired, 2 deferred to ifupdown, 3 clock failure.
wait_one() {
    _if=$1
    [ -d "$SYS_NET_DIR/$_if" ] || { log "$_if: interface absent, deferred to ifupdown"; return 2; }
    tick || return 3
    _remaining=$((deadline - current))
    [ "$_remaining" -gt 0 ] || return 1
    [ "$_remaining" -le 2 ] || _remaining=2
    # Query effective ifupdown options, including sourced stanzas. A failed or
    # ambiguous query must never become permission to raise a MAC-owned link.
    if ! command -v timeout >/dev/null 2>&1 || ! command -v ifquery >/dev/null 2>&1; then
        log "$_if: bounded query unavailable, deferred to ifupdown"; return 2
    fi
    _options=$(timeout "$_remaining" ifquery --no-mappings "$_if" 2>/dev/null) || {
        log "$_if: effective query failed, deferred to ifupdown"; return 2;
    }
    tick || return 3
    _policy=$(printf '%s\n' "$_options" | awk '
      /^[[:space:]]*$/ { next }
      !/^[a-zA-Z0-9_-]+:[[:space:]]+[^[:space:]]/ { bad=1; next }
      { key=$1; sub(/:$/, "", key); n++; if (key=="hwaddress") mac=1;
        if (++seen[key]>1 && key!="up" && key!="down" && key!="pre-up" && key!="post-down") bad=1 }
      END { if (bad || !n) print "unknown"; else print mac ? "mac" : "clean" }')
    case "$_policy" in
        mac) log "$_if: hwaddress MAC/link initialization deferred to ifupdown"; return 2 ;;
        clean) ;;
        *) log "$_if: ambiguous effective query, deferred to ifupdown"; return 2 ;;
    esac
    _raised=0
    while : ; do
        tick || return 3
        [ "$current" -lt "$deadline" ] || return 1
        _c=$(cat "$SYS_NET_DIR/$_if/carrier" 2>/dev/null || echo "")
        [ "$_c" = "1" ] && return 0
        if [ -z "$_c" ] && [ "$_raised" = "0" ]; then
            # The kernel refuses a carrier read on an admin-DOWN device
            # (carrier_show() returns -EINVAL when !netif_running), and before
            # ifup runs the interface usually IS down — so without raising it
            # first the wait could never observe a live link and would burn the
            # whole budget on every boot. The effective stanza above proves
            # ifupdown has no hwaddress assignment requiring a DOWN device.
            ip link set "$_if" up 2>/dev/null || true
            _raised=1
            continue
        fi
        sleep 1
    done
}

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

start=$(now) || { log "uptime unavailable; carrier wait skipped"; exit 0; }
last_tick=$start
# ONE shared deadline across every target: the budget bounds the whole run, not
# each interface, so a 2-eth board with both ports unplugged still costs the
# budget once.
deadline=$(( start + budget ))
missed=""
ready=""
deferred=""
for iface in $targets; do
    wait_one "$iface"
    case "$?" in
        0) ready="${ready}${ready:+ }$iface" ;;
        1) missed="$missed $iface" ;;
        2) deferred="$deferred $iface" ;;
        3) log "uptime unavailable or decreasing; carrier wait stopped"; exit 0 ;;
    esac
done
tick || { log "uptime unavailable or decreasing; carrier wait stopped"; exit 0; }
elapsed=$(( current - start ))

# Logged on EVERY run, not only when it waited. The default 10 s budget came
# from ONE observed boot, so "how long the wait actually took" is the telemetry
# that confirms or revises it by measurement (docs/contracts/boot-network-dns.md
# §4, §9). Do not make this conditional.
flat() { printf '%s' "$1" | tr '\n' ' '; }
if [ -n "$ready" ]; then
    log "carrier up for [$(flat "$ready")] after ${elapsed}s (budget ${budget}s)"
fi
if [ -n "$missed" ]; then
    log "no carrier for [$(flat "$missed")] after ${elapsed}s — budget ${budget}s expired, proceeding anyway"
fi
[ -z "$deferred" ] || log "carrier deferred for [$(flat "$deferred")] after ${elapsed}s (budget ${budget}s); ifupdown owns initialization"

exit 0
