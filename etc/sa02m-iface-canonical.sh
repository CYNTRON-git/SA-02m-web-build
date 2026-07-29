#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-iface-canonical.sh  —  canonical Ethernet interface names
#
# Renames end0/end1 -> eth0/eth1 before networking.service, on EVERY boot.
#
# Why not a systemd .link file (1.0.3.39 reverted those): a .link file is an
# INPUT to systemd-udevd's naming policy, so an apt upgrade of systemd/udev can
# change or drop its effect and the network dies with "ifup: Cannot find
# device". This script instead calls `ip link set ... name` through the stable
# RTM_SETLINK netlink ABI, and it re-runs every boot — a boot that somehow came
# up legacy-named is re-canonicalized by the next one.
#
# Convergent: legacy -> canonical only, never the reverse; a no-op when the
# canonical name already exists, so running it twice cannot corrupt any state.
#
# Exit status: 0 when every pair ended canonical or absent; non-zero when a
# legacy device is still present after a FAILED rename — `systemctl --failed`
# is the project's standing health check, so a naming failure is visible rather
# than silent. The unit orders itself Before= networking.service only (ordering,
# not a requirement), so a failed run never blocks the network coming up.
#
# Usage: sa02m-iface-canonical.sh [--live]
#   --live   installer path (SA02M_CANONICAL_IFACE_NOW=1): a renamed interface
#            that was administratively UP is brought back up and its canonical
#            name is appended to /run/sa02m-iface-canonical.renamed, the signal
#            that the caller must re-ifup it. At boot the link is DOWN anyway
#            (ifupdown has not run yet — that is what the ordering buys), so
#            without --live this is inert.
#
# Contract: docs/contracts/ethernet-iface-naming.md
# ═══════════════════════════════════════════════════════════════════════════
set -o pipefail

LOG_FILE="/var/log/sa02m-iface-canonical.log"
LOG_MAX_BYTES=262144          # 256 KB — same rotation idiom as fix-eth.sh
RENAMED_STATE="/run/sa02m-iface-canonical.renamed"
WAIT_SECS_DEFAULT=10

# Hardcoded allow-list. The script NEVER globs /sys/class/net — only these four
# names ever reach an `ip` command line (web-code-rigor.md: allow-list before a
# value becomes a shell word, applied to a root-run boot script).
IFACE_PAIRS="end0:eth0 end1:eth1"

# Optional operator overrides. Root-owned file, but the wait is used in
# arithmetic, so it is sanitized to digits below regardless.
# shellcheck disable=SC1091
[ -f /etc/sa02m_network.conf ] && . /etc/sa02m_network.conf

WAIT_SECS="${IFACE_CANONICAL_WAIT_SECS:-$WAIT_SECS_DEFAULT}"
case "$WAIT_SECS" in
    ''|*[!0-9]*) WAIT_SECS=$WAIT_SECS_DEFAULT ;;
esac

LIVE_MODE=0
[ "${1:-}" = "--live" ] && LIVE_MODE=1

# ── Logging ────────────────────────────────────────────────────────────────
log() {
    local level=$1; shift
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [${level}] $*" >> "$LOG_FILE" 2>/dev/null || true
    # stdout -> journald (the unit sets StandardOutput=journal)
    echo "[${ts}] [${level}] $*"
}

rotate_log() {
    [ -f "$LOG_FILE" ] || return 0
    local size; size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$size" -gt "$LOG_MAX_BYTES" ] 2>/dev/null; then
        tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null \
            && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
    fi
}

# ── iface_admin_up <iface> — IFF_UP (bit 0) of /sys/class/net/<if>/flags ───
iface_admin_up() {
    local flags
    flags=$(cat "/sys/class/net/$1/flags" 2>/dev/null) || return 1
    case "$flags" in
        ''|*[!0-9a-fA-Fx]*) return 1 ;;
    esac
    [ $(( flags & 1 )) -eq 1 ]
}

# ── canonicalize_pair <legacy> <canonical> ────────────────────────────────
# 0 = pair ended canonical or absent; 1 = legacy still present after a failure.
canonicalize_pair() {
    local legacy="$1" canonical="$2"
    local ticks=0 max_ticks was_up=0 err=""

    # 1. Already canonical — the no-op path on every current SA-02m board and
    #    on every re-run.
    if [ -d "/sys/class/net/$canonical" ]; then
        log INFO "$canonical: already canonical"
        return 0
    fi

    # 2. Bounded wait for the legacy device: udev may still be settling. The
    #    canonical name is re-checked each tick so a late udev naming pass ends
    #    the wait immediately instead of burning the full budget.
    max_ticks=$(( WAIT_SECS * 2 ))
    while [ ! -d "/sys/class/net/$legacy" ]; do
        if [ -d "/sys/class/net/$canonical" ]; then
            log INFO "$canonical: already canonical (appeared during wait)"
            return 0
        fi
        if [ "$ticks" -ge "$max_ticks" ]; then
            log INFO "$legacy: absent after ${WAIT_SECS}s — nothing to do"
            return 0
        fi
        sleep 0.5
        ticks=$(( ticks + 1 ))
    done

    # 3. Altname collision guard. The kernel resolves altnames in the SAME name
    #    namespace as primary names (netdev_name_in_use -> netdev_name_node_lookup,
    #    altname support since 5.5; this board runs 5.10.35), so renaming onto a
    #    name somebody holds as an altname returns EEXIST. Conditional and
    #    observable: inert (one read-only `ip -d link show`) on a board that has
    #    no altnames, and its failure never aborts the rename that follows.
    #    NOTE: this necessity is reasoned, NOT verified on real hardware — see
    #    the honesty section of docs/contracts/ethernet-iface-naming.md.
    if ip -d link show "$legacy" 2>/dev/null \
        | grep -qE "^[[:space:]]*altname[[:space:]]+${canonical}[[:space:]]*$"; then
        log INFO "$legacy: holds altname $canonical — dropping it before the rename"
        ip link property del dev "$legacy" altname "$canonical" 2>/dev/null \
            || log WARN "$legacy: could not drop altname $canonical (continuing anyway)"
    fi

    # 4. The kernel refuses `ip link set ... name` on an UP device (EBUSY), so
    #    record the admin state, take the link down, rename, and leave it down —
    #    networking.service/ifup owns bringing it up. At boot the link is already
    #    down, so this is a no-op step there.
    if iface_admin_up "$legacy"; then
        was_up=1
        ip link set dev "$legacy" down 2>/dev/null || true
    fi

    if err=$(ip link set dev "$legacy" name "$canonical" 2>&1); then
        log OK "$legacy -> $canonical: renamed"
        if [ "$LIVE_MODE" = "1" ] && [ "$was_up" = "1" ]; then
            ip link set dev "$canonical" up 2>/dev/null || true
            printf '%s\n' "$canonical" >> "$RENAMED_STATE" 2>/dev/null || true
            log INFO "$canonical: brought back up (--live) — caller must re-ifup it"
        fi
        return 0
    fi

    # Rename failed: restore the previous admin state so a live board keeps its
    # working link on the legacy name, and report the failure upward.
    log ERROR "$legacy -> $canonical: rename FAILED: ${err:-unknown error}"
    if [ "$was_up" = "1" ]; then
        ip link set dev "$legacy" up 2>/dev/null || true
        log INFO "$legacy: previous admin state (up) restored"
    fi
    return 1
}

# ── main ──────────────────────────────────────────────────────────────────
rotate_log
: > "$RENAMED_STATE" 2>/dev/null || true

rc=0
for pair in $IFACE_PAIRS; do
    canonicalize_pair "${pair%%:*}" "${pair##*:}" || rc=1
done

if [ "$rc" != "0" ]; then
    log ERROR "one or more interfaces are still legacy-named — see the lines above"
fi
exit "$rc"
