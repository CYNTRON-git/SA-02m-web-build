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
# Per-device udev-initialized gate (1.0.5.56): a name-EXISTENCE check cannot
# tell a settled canonical name from a kernel-native name udev is about to
# rename — 6.1-class kernels register ethN natively and udev renames to endN
# asynchronously, so "eth0 exists" at unit start may mean "udev has not run
# yet". The per-device settled signal is udev's own initialized state:
# /run/udev/data/n<ifindex> exists exactly when udev has processed the
# device's add event and applied its naming policy, so an INITIALIZED device's
# current name is its settled name. The script acts only on initialized
# devices, within the bounded wait below. Whole-queue `udevadm settle` is NOT
# used: it is deprecated and stalls on unrelated queue churn (120 s observed
# on the bench) while the per-device gate converges in ~1 s.
#
# Mechanism 4 (1.0.5.57): the db entry can appear a few MILLISECONDS BEFORE
# udev applies its rename — so the initialized-gate is necessary but NOT
# sufficient (bench 2026-07-29 20:52: settled-eth0 verdict at 10.1008 s, udev
# renamed eth0 -> end0 at 10.1155 s, 15 ms later). The closure lives OUTSIDE
# this script: etc/98-sa02m-iface-canonical.rules retriggers it (via
# sa02m-iface-canonical-retry.service) on every end* add/move event; the
# convergent + idempotent design below is what makes each re-run safe, and
# the failed-rename path treats "legacy vanished, canonical present" as a
# concurrent run having won — success, not an error.
#
# Degraded fallback: if the udev db never appears within the budget (udevd
# dead/wedged, or a future udev moves the db path), act on the device's
# CURRENT name with an honest WARN — i.e. degrade to the pre-gate behaviour,
# never worse. Worst pathological cost: 2 x WAIT_SECS of waiting.
#
# Convergent: legacy -> canonical only, never the reverse; a no-op when the
# canonical name already exists settled, so running it twice cannot corrupt
# any state.
#
# Exit status: 0 when every pair ended canonical or absent; non-zero when a
# legacy device is still present after a FAILED rename — `systemctl --failed`
# is the project's standing health check, so a naming failure is visible rather
# than silent. The unit orders itself Before= networking.service only (ordering,
# not a requirement), so a failed run never blocks the network coming up.
#
# Known bound (pre-existing, documented): an operator override
# IFACE_CANONICAL_WAIT_SECS greater than ~14 can push the 2 x wait worst case
# past the unit's TimeoutStartSec=30; the unit is then killed mid-wait, lands
# in `systemctl --failed`, and networking proceeds (Before= is ordering only).
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

# The two kernel/udev roots, defined once — the test seam: the gate test
# (scripts/dev/test-iface-canonical-gate.sh) retargets ONLY these two into a
# scratch tree. No env override: this runs as root at boot; the only sourced
# input stays the root-owned /etc/sa02m_network.conf below.
SYS_NET="/sys/class/net"
UDEV_DB="/run/udev/data"

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

# ── iface_admin_up <iface> — IFF_UP (bit 0) of $SYS_NET/<if>/flags ─────────
iface_admin_up() {
    local flags
    flags=$(cat "$SYS_NET/$1/flags" 2>/dev/null) || return 1
    case "$flags" in
        ''|*[!0-9a-fA-Fx]*) return 1 ;;
    esac
    [ $(( flags & 1 )) -eq 1 ]
}

# ── udev_initialized <iface> — has udev finished naming this device? ───────
# $UDEV_DB/n<ifindex> exists exactly when udev has processed the device's add
# event and applied its naming policy — so its current name is its settled
# name. ifindex is rename-stable, which makes the check race-free against a
# mid-check rename; a torn read (the name just changed) returns "not
# initialized" and the caller's next tick re-evaluates. Fail direction is
# CLOSED: uncertain means keep waiting, within the caller's budget.
udev_initialized() {
    local ifindex
    ifindex=$(cat "$SYS_NET/$1/ifindex" 2>/dev/null) || return 1
    case "$ifindex" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ -e "$UDEV_DB/n$ifindex" ]
}

# ── canonicalize_pair <legacy> <canonical> ────────────────────────────────
# 0 = pair ended canonical or absent; 1 = legacy still present after a failure.
canonicalize_pair() {
    local legacy="$1" canonical="$2"
    local ticks=0 max_ticks was_up=0 err="" degraded=0

    # 1. Per-device udev-initialized gate, bounded. Act only when udev's
    #    naming policy has fired for the device: an uninitialized canonical
    #    name is the boot-0 trap (kernel-named eth0 that udev is about to take
    #    away), so it keeps the wait alive; when udev renames it to end0 and
    #    initializes it, the next tick sees an initialized legacy device and
    #    renames it back.
    max_ticks=$(( WAIT_SECS * 2 ))
    while :; do
        if [ -d "$SYS_NET/$canonical" ] && udev_initialized "$canonical"; then
            log INFO "$canonical: already canonical (settled)"
            return 0
        fi
        if [ -d "$SYS_NET/$legacy" ] && udev_initialized "$legacy"; then
            break    # settled legacy device — rename it below
        fi
        if [ "$ticks" -ge "$max_ticks" ]; then
            degraded=1
            break
        fi
        sleep 0.5
        ticks=$(( ticks + 1 ))
    done

    # 2. Degraded fallback: the udev db never appeared within the budget
    #    (udevd dead/wedged, or the db path moved). Act on the device's
    #    CURRENT name with an honest log, keeping today's exit-code
    #    discipline — degrade to the pre-gate behaviour, never worse.
    if [ "$degraded" = "1" ]; then
        if [ -d "$SYS_NET/$canonical" ]; then
            log WARN "$canonical: udev db entry absent after ${WAIT_SECS}s — treating current name as settled"
            return 0
        fi
        if [ ! -d "$SYS_NET/$legacy" ]; then
            log INFO "$legacy: absent after ${WAIT_SECS}s — nothing to do"
            return 0
        fi
        log WARN "$legacy: udev db entry absent after ${WAIT_SECS}s — attempting the rename on the current name"
        # fall through to the rename path below
    fi

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

    # Rename failed. Concurrent-run convergence (the mechanism-4 retry unit
    # can run alongside the boot unit): if the legacy name vanished because
    # another instance already renamed it — canonical present — the pair IS
    # canonical: report success, not a misleading FAILED. A fully vanished
    # device (both names gone) is the standing "absent pair" no-op.
    if [ ! -d "$SYS_NET/$legacy" ]; then
        if [ -d "$SYS_NET/$canonical" ]; then
            log INFO "$legacy -> $canonical: legacy vanished mid-flight, canonical present — converged by a concurrent run (${err:-})"
        else
            log INFO "$legacy: vanished mid-flight — nothing to do"
        fi
        return 0
    fi

    # Restore the previous admin state so a live board keeps its working link
    # on the legacy name, and report the failure upward.
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
