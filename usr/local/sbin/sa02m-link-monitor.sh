#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-link-monitor.sh — starts fix-eth@<if>.service on an Ethernet link-up
# seen on netlink (`ip -o monitor link`). Run by sa02m-link-monitor.service
# (Type=simple, Restart=always).
#
# Why: a carrier change on eth0 emits no udev event on the board's kernel
# (docs/bugs/BUGLOG.md 1.0.7.22 / 1.0.7.25), so between net-watchdog passes
# (30 s; the first 60 s after its unit starts) nothing reacted to a plugged
# cable — routes, DNS and the default route waited for the next pass. Netlink
# does carry it: on bench 1.135 `ip monitor link` printed LOWER_UP 2.2 s after
# `ip link set eth0 up` (2026-11-02). iproute2 is on every board, so no new
# package. net-watchdog stays the fallback (it also runs inet-failover and
# the LED sync); this only shortens the wait.
#
# Contract: docs/contracts/boot-network-routes.md §1. Harness:
# scripts/dev/test-link-monitor.sh (it sed-retargets the two roots below).
#
# Behaviour, per interface with /etc/network/interfaces.d/<if>.conf (eth*/end*,
# the same discovery as net-watchdog.sh):
#   - a RISING edge of carrier (LOWER_UP where the last seen state had none;
#     the initial state is read from sysfs at start) starts fix-eth@<if>;
#   - an edge while that instance is still running (a oneshot is `activating`)
#     waits for it in the background, bounded, and starts a fresh pass then —
#     a `start` on a running oneshot joins the running job, and the pass in
#     flight may already have read carrier=0; one such waiter per interface;
#   - at most one start (immediate or queued) per MIN_START_INTERVAL seconds
#     of uptime per interface (monotonic: a wall-clock step does not move it)
#     — a later edge inside it is logged and skipped;
#   - repeated up lines, falling edges (logged only), other interfaces
#     (docker0, lo, veth…) start nothing.
# When `ip monitor` ends the script exits 1 so systemd restarts it.
# ═══════════════════════════════════════════════════════════════════════════
set -u

CONF_DIR="/etc/network/interfaces.d"
SYS_NET="/sys/class/net"
UPTIME="/proc/uptime"   # monotonic seconds: the start interval must not follow a wall-clock step
BUSY_WAIT_MAX=60    # seconds; fix-eth@ has TimeoutSec=45, so a pass ends inside it
# Seconds between two monitor-initiated starts per interface. A failed
# ifdown/ifup in fix-eth.sh bounces the link and raises the next edge itself,
# and fix-eth.sh's cooldown does not span completed passes; without this the
# monitor would re-run it every few seconds until fix-eth@ hits its start limit.
MIN_START_INTERVAL=60

log() { echo "$*"; }    # stdout -> journal, SyslogIdentifier=sa02m-link-monitor

command -v ip >/dev/null 2>&1 || { log "ip (iproute2) не найден — монитор линка не запущен"; exit 1; }

managed() {     # $1 iface: an Ethernet port fix-eth looks after
    case "$1" in eth*|end*) ;; *) return 1 ;; esac
    [[ $1 =~ ^[A-Za-z0-9_.-]+$ ]] || return 1    # the name lands in a unit name
    [ -f "$CONF_DIR/$1.conf" ]
}

read_carrier() {    # sysfs refuses the read (EINVAL) on an admin-down port -> 0
    local c=""
    { read -r c < "$SYS_NET/$1/carrier"; } 2>/dev/null
    [ "$c" = 1 ] && echo 1 || echo 0
}

unit_busy() {   # $1 iface: a fix-eth@ pass is running right now
    local st
    st=$(timeout 5 systemctl is-active "fix-eth@${1}.service" 2>/dev/null)
    case "$st" in activating|active|deactivating|reloading) return 0 ;; esac
    return 1
}

start_fix() {   # $1 iface; --no-block: the job may wait behind networking.service at boot
    local rc=0
    timeout 10 systemctl --no-block start "fix-eth@${1}.service" || rc=$?
    [ "$rc" = 0 ] || log "$1: запуск fix-eth@$1 не удался (rc=$rc) — подхватит net-watchdog"
}

deferred_start() {  # $1 iface: wait out the running pass (bounded), then a fresh one
    local i=0
    while unit_busy "$1"; do
        i=$((i + 1))
        if [ "$i" -gt "$BUSY_WAIT_MAX" ]; then
            log "$1: fix-eth@$1 занят дольше ${BUSY_WAIT_MAX}с — повторный запуск отменён, подхватит net-watchdog"
            return 0
        fi
        sleep 1
    done
    start_fix "$1"
}

declare -A LAST      # iface -> last seen carrier (0/1)
declare -A PENDING   # iface -> pid of a deferred start still waiting
declare -A LAST_START  # iface -> uptime seconds of the last start this monitor issued or queued

handle_edge() {     # $1 iface whose carrier just rose
    local ifc=$1 pid busy=0 up=0 now d
    unit_busy "$ifc" && busy=1
    pid=${PENDING[$ifc]:-}
    # One deferred start per interface: a flapping link must not pile up waiters.
    [ "$busy" = 1 ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && { log "$ifc: линк поднялся (netlink), повторный запуск fix-eth@$ifc уже ожидает"; return 0; }
    { read -r up _ < "$UPTIME"; } 2>/dev/null
    now=${up%%.*}
    # At most one start per MIN_START_INTERVAL of uptime (a negative difference counts as elapsed):
    # breaks the ifdown/ifup self-trigger loop; net-watchdog picks a skipped edge up.
    [ -n "${LAST_START[$ifc]:-}" ] && d=$((now - LAST_START[$ifc])) && [ "$d" -ge 0 ] && [ "$d" -lt "$MIN_START_INTERVAL" ] && { log "$ifc: линк поднялся (netlink), fix-eth@$ifc запускался ${d}с назад — пропуск (интервал ${MIN_START_INTERVAL}с), подхватит net-watchdog"; return 0; }
    LAST_START[$ifc]=$now
    if [ "$busy" = 1 ]; then
        log "$ifc: линк поднялся (netlink) во время прохода fix-eth@$ifc — повторный запуск после его завершения"
        deferred_start "$ifc" 3<&- &
        PENDING[$ifc]=$!
        return 0
    fi
    log "$ifc: линк поднялся (netlink) — запуск fix-eth@$ifc"
    start_fix "$ifc"
}

# Start the subscription, then read the initial state. Whether `ip` has bound
# its netlink socket before the sysfs read is not guaranteed; either way an
# edge in that window is missed (net-watchdog catches it), never doubled.
exec 3< <(ip -o monitor link)

for conf in "$CONF_DIR"/eth*.conf "$CONF_DIR"/end*.conf; do
    [ -f "$conf" ] || continue
    ifc=$(basename "$conf" .conf)
    LAST[$ifc]=$(read_carrier "$ifc")
done
log "Монитор линка запущен (netlink); начальное состояние: $(for k in "${!LAST[@]}"; do printf '%s=%s ' "$k" "${LAST[$k]}"; done)"

# A record: `3: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu … state UP …`,
# `Deleted 3: eth0: <…> …` when the link goes away, `veth1@if6:` for a peer.
RECORD_RE='^(Deleted )?[0-9]+: ([^:@ ]+)(@[^: ]*)?: <([^>]*)>'
while IFS= read -r line <&3; do
    [[ $line =~ $RECORD_RE ]] || continue      # not a link record: ignore, never exit
    deleted=${BASH_REMATCH[1]}
    ifc=${BASH_REMATCH[2]}
    flags=${BASH_REMATCH[4]}
    managed "$ifc" || continue
    now=0
    [ -z "$deleted" ] && [[ ",$flags," == *,LOWER_UP,* ]] && now=1
    prev=${LAST[$ifc]:-0}
    LAST[$ifc]=$now
    if [ "$now" = 1 ] && [ "$prev" = 0 ]; then
        handle_edge "$ifc"
    elif [ "$now" = 0 ] && [ "$prev" = 1 ]; then
        log "$ifc: линк пропал (netlink)"
    fi
done

log "ip monitor завершился — выход, systemd перезапустит монитор"
wait    # a deferred start still waiting finishes first (bounded by BUSY_WAIT_MAX)
exit 1
