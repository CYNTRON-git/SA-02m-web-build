#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-kernel-service-guard.sh  •  Kernel-conditional service policy
#
# The ONE home of the CODESYS/CodeMeter/docker autostart policy logic.
# Device home: /usr/local/sbin/ (installed by scripts/01-system.sh — same
# convention as sa02m-kernel-select.sh).
# Contract: docs/contracts/kernel-conditional-services.md
#
# Subcommands (fixed words from unit files / installer only — no
# request-derived input; unknown subcommand exits non-zero, fail-closed):
#
#   apply-policy   — persistent, idempotent normalization, callable by
#                    installer re-runs and deploys: disable CODESYS/CodeMeter
#                    autostart (SysV rc links via update-rc.d; plus systemctl
#                    disable where a REAL unit file exists). Absent services
#                    are skipped silently (codesys-less devices stay clean).
#
#   boot-guard     — boot-time only (sa02m-kernel-service-guard.service):
#                    on a non-RT kernel STOP any policy service that
#                    something (SysV leftovers) started anyway. On RT: exit 0,
#                    do nothing. NO continuous enforcement — a manual start
#                    after boot (web panel / systemctl) is never fought.
#
#   docker-capable — ExecCondition probe for docker.service (drop-in
#                    etc/systemd/system/docker.service.d/sa02m-kernel-guard.conf):
#                    exit 0 iff the running kernel supports docker's
#                    iptables-nft path (nft_compat module or builtin).
# ═══════════════════════════════════════════════════════════════════════════
set -u

POLICY_SERVICES="codesyscontrol codemeter codemeter-logger codemeter-webadmin"

log() {
    # journald when available (unit runs / installer runs); stderr always.
    if command -v logger >/dev/null 2>&1; then
        logger -t sa02m-kernel-service-guard -- "$*" 2>/dev/null || true
    fi
    printf 'sa02m-kernel-service-guard: %s\n' "$*" >&2
}

# Same one-line predicate as preempt_rt_active() in sa02m-kernel-select.sh —
# duplicated deliberately (kernel-select is a command script, not a lib).
preempt_rt_active() {
    grep -q 'PREEMPT_RT' /proc/version 2>/dev/null
}

# A REAL unit file (systemctl disable works on it) — NOT a SysV-generated
# runtime unit under /run/systemd/generator.late/, which systemctl disable
# refuses (prior art: codesys_rc_disable in etc/sa02m-web-service-ctl.sh).
real_unit_file_exists() {
    local u=$1 d
    for d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
        [ -f "$d/${u}.service" ] && return 0
    done
    return 1
}

apply_policy() {
    local svc
    for svc in $POLICY_SERVICES; do
        if [ -f "/etc/init.d/$svc" ]; then
            if command -v update-rc.d >/dev/null 2>&1; then
                update-rc.d "$svc" disable >/dev/null 2>&1 \
                    || log "apply-policy: update-rc.d $svc disable failed"
            fi
            log "apply-policy: $svc SysV autostart disabled"
        fi
        if real_unit_file_exists "$svc"; then
            systemctl disable "$svc" >/dev/null 2>&1 \
                || log "apply-policy: systemctl disable $svc failed"
        fi
        # Absent service: skipped silently — idempotent on every device.
    done
    return 0
}

policy_procs_active() {
    pgrep -f '[c]odesyscontrol\.bin' >/dev/null 2>&1 && return 0
    # Matches CodeMeterLin + the CodeMeter webadmin/logger helpers.
    pgrep -f '[C]odeMeter' >/dev/null 2>&1 && return 0
    return 1
}

boot_guard() {
    local kr
    kr=$(uname -r 2>/dev/null || echo '?')
    if preempt_rt_active; then
        log "boot-guard: PREEMPT_RT kernel ($kr) — CODESYS/CodeMeter allowed, nothing to do"
        return 0
    fi
    # Cheap path first: O(1) greps, sub-second when nothing needs stopping.
    if ! policy_procs_active; then
        log "boot-guard: non-RT kernel ($kr), no CODESYS/CodeMeter processes — nothing to stop"
        return 0
    fi
    log "boot-guard: non-RT kernel ($kr) — stopping CODESYS/CodeMeter boot leftovers"
    local svc
    for svc in $POLICY_SERVICES; do
        if [ -x "/etc/init.d/$svc" ]; then
            "/etc/init.d/$svc" stop >/dev/null 2>&1 \
                || log "boot-guard: /etc/init.d/$svc stop failed (pkill fallback follows)"
        fi
    done
    # pkill fallback per the service-ctl idiom: init.d stop can miss a
    # process running outside its pidfile.
    sleep 1
    if pgrep -f '[c]odesyscontrol\.bin' >/dev/null 2>&1; then
        pkill -f '[c]odesyscontrol\.bin' 2>/dev/null || true
    fi
    if pgrep -f '[C]odeMeter' >/dev/null 2>&1; then
        pkill -f '[C]odeMeter' 2>/dev/null || true
    fi
    if policy_procs_active; then
        # A failed stop leaves the pre-existing state (no new privilege) —
        # logged, visible in the unit's journal, never silent.
        log "boot-guard: WARNING — some CODESYS/CodeMeter processes survived the stop pass"
    else
        log "boot-guard: CODESYS/CodeMeter leftovers stopped"
    fi
    return 0
}

docker_capable() {
    # Capability probe, not kernel-name matching: docker's iptables-nft path
    # needs the xtables compat shim. `modprobe -qn` (dry-run) succeeds when
    # nft_compat is a loadable module OR builtin — exactly the bit verified
    # missing on the bench 6.1.0-rc6 kernel (iptables-nft: "addrtype …
    # revision 0 not supported"). Probe failure ⇒ docker does NOT start
    # (fails closed on capability).
    if modprobe -qn nft_compat 2>/dev/null; then
        log "docker-capable: nft_compat available — docker allowed on $(uname -r 2>/dev/null || echo '?')"
        return 0
    fi
    log "docker-capable: kernel $(uname -r 2>/dev/null || echo '?') lacks nft_compat — docker.service skipped (docs/contracts/kernel-conditional-services.md)"
    return 1
}

case "${1:-}" in
    apply-policy)   apply_policy ;;
    boot-guard)     boot_guard ;;
    docker-capable) docker_capable ;;
    *)
        printf 'Usage: %s apply-policy | boot-guard | docker-capable\n' "$0" >&2
        exit 2
        ;;
esac
