#!/bin/bash
# sa02m-update-remedy.sh — finish an update that its runner abandoned, at
# RUNTIME, on a board that will not (or must not) be rebooted right now.
#
# The state it repairs (bench 1.135, 2026-09-23, reproduced from the Skolkovo
# incident): the boot-time recover of a runner < 1.0.6.52 was killed by its own
# TimeoutStartSec=300 mid-rollback and left
#   transaction.json      stage=rolling_back (or verifying/committing/applying)
#   /run/sa02m-imaging.lock   present
#   RuntimeWatchdogUSec   0   — the HARDWARE WATCHDOG IS OFF
#   net-watchdog, sa02m-flasher   stopped
# with no runner process alive. Verified on 1.135 in exactly that state: the
# rollback finishes in ~24 s at runtime (no boot ordering), stage=rolled_back,
# lock gone, watchdog 15000000, net-watchdog/flasher active, no failed units.
#
# Runtime state ONLY — no repo-owned file is edited (invariant 4). Safe to
# re-run: every step is idempotent. REFUSES to do anything while an update
# runner is alive (exit 2) — a live runner owns the lock and the watchdog hold.
#
# What it runs, in order:
#   1. runner ≥ 1.0.6.52 → `sa02m-update-runner reclaim` (the same recovery as
#      at boot, at runtime: rollback finished with restarts, or a complete
#      tree handed to sa02m-update-verify.service; lock cleared; watchdog
#      restored from the transaction or the sa02m-watchdog.conf policy);
#      runner < 1.0.6.52 → `sa02m-update-runner recover` at runtime (it works
#      outside boot ordering) — that runner captured prev=0 and «restores» 0,
#      so step 2 is what puts the watchdog back.
#   2. RuntimeWatchdogUSec ← the policy (RuntimeWatchdogSec=15s → 15000000),
#      explicitly, whatever the runner reported.
#   3. leftover /run/sa02m-imaging.lock removed (again: only with no live runner).
#   4. net-watchdog + sa02m-flasher started (the imaging lock stopped them).
# Expected output and when to run it: docs/deployment.md «Пути деплоя» →
# «Ремонт застрявшего обновления без перезагрузки».
#
# Run as root: bash scripts/sa02m-update-remedy.sh
# Seams (harness only, scripts/dev/test-update-recover-boot.sh section M):
#   SA02M_UPDATE_RUNNER, SA02M_UPDATE_STATEDIR, SA02M_IMAGING_LOCK,
#   SA02M_WATCHDOG_POLICY_FILE, SA02M_WEB_VERSION_FILE.
set -u
R="${SA02M_UPDATE_RUNNER:-/usr/local/libexec/sa02m-update-runner}"
STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
T="$STATEDIR/transaction.json"
LOCK="${SA02M_IMAGING_LOCK:-/run/sa02m-imaging.lock}"
POLICY="${SA02M_WATCHDOG_POLICY_FILE:-/etc/systemd/system.conf.d/sa02m-watchdog.conf}"
VERSION_FILE="${SA02M_WEB_VERSION_FILE:-/var/www/network_config/VERSION}"

st() {
    [ -r "$T" ] || { echo "no-transaction"; return 0; }
    python3 -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("stage"), d.get("result"))' "$T" 2>/dev/null | tr -d '\r' || echo "unreadable"
}
wd() { busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager RuntimeWatchdogUSec 2>/dev/null || echo "unreadable"; }
act() { systemctl is-active "$1" 2>/dev/null || true; }
lock_state() { if [ -f "$LOCK" ]; then echo yes; else echo no; fi; }
runner_alive() { pgrep -f '/sa02m-update/runner/|sa02m-update-runner (apply|recover|verify|reclaim)' >/dev/null 2>&1; }
# Policy → µs (RuntimeWatchdogSec=15s → 15000000); empty when unparsable.
policy_usec() {
    local raw num unit
    [ -r "$POLICY" ] || return 1
    raw=$(grep -E '^[[:space:]]*RuntimeWatchdogSec=' "$POLICY" | tail -n1) || true
    raw=${raw#*=}; raw=$(printf '%s' "$raw" | tr -d '[:space:]\r')
    num=${raw%%[!0-9]*}; unit=${raw#"$num"}
    [ -n "$num" ] || return 1
    case "$unit" in
        ''|s|sec) echo $((num * 1000000)) ;;
        ms|msec)  echo $((num * 1000)) ;;
        us|usec)  echo "$num" ;;
        m|min)    echo $((num * 60000000)) ;;
        *) return 1 ;;
    esac
}

echo "before: stage=$(st) lock=$(lock_state) watchdog=$(wd) net-watchdog=$(act net-watchdog) VERSION=$(tail -1 "$VERSION_FILE" 2>/dev/null | tr -d '\r')"
if runner_alive; then
    echo "an update runner is still RUNNING — nothing done, wait for it (or watch the panel)"
    exit 2
fi
[ -x "$R" ] || { echo "runner not found at $R — nothing done"; exit 1; }

systemctl reset-failed sa02m-update-recover.service 2>/dev/null || true
if grep -q '^cmd_reclaim() {' "$R" 2>/dev/null; then
    echo "runner supports reclaim — finishing the abandoned transaction at runtime..."
    "$R" reclaim; echo "reclaim rc=$?"
else
    case "$(st)" in
        rolling_back*|verifying*|committing*|applying*)
            echo "old runner: finishing the rollback at runtime (no boot ordering here)..."
            "$R" recover; echo "recover rc=$?" ;;
        *) echo "transaction not stuck ($(st)) — skipping recover" ;;
    esac
fi

# The watchdog, explicitly: a runner < 1.0.6.52 captured prev=0 and «restored»
# 0; even the new one cannot help on a board without a parsable policy file.
if usec=$(policy_usec); then
    busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager RuntimeWatchdogUSec t "$usec" \
        && echo "hardware watchdog set to ${usec}us (policy $POLICY)"
else
    echo "WARN: no parsable RuntimeWatchdogSec in $POLICY — watchdog left as is: $(wd)"
fi
if ! runner_alive; then rm -f "$LOCK"; fi
systemctl start net-watchdog 2>/dev/null || true
systemctl start sa02m-flasher 2>/dev/null || true
sleep 2
echo "after:  stage=$(st) lock=$(lock_state) watchdog=$(wd) net-watchdog=$(act net-watchdog) flasher=$(act sa02m-flasher) nginx=$(act nginx) fcgiwrap=$(act fcgiwrap) VERSION=$(tail -1 "$VERSION_FILE" 2>/dev/null | tr -d '\r')"
systemctl --failed --no-legend 2>/dev/null | head -3
