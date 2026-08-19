#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Self-upgrade BRIDGE launcher — a one-off bridge artifact, NEVER on main's etc/.
#
# WHAT: the file that REPLACES `etc/sa02m-repair-web-env.sh` in the bridge
#   commit `tools/update-bridge/publish-bridge.sh` publishes onto a stale
#   version branch (origin/main tip + this one file). The pre-1.0.5.75
#   `sa02m-web-update-apply` on such a board clones ITS version branch, copies
#   www/ + helpers, installs this file to /usr/local/sbin/sa02m-repair-web-env
#   and EXECUTES it at once, then writes `done` — that hook is the lever.
# WHY: a board < 1.0.5.75 checks its own version branch, so it never sees main
#   (CHANGELOG 1.0.5.75), and its old apply ships no installer — the full
#   install from main has to be started by something the old apply already
#   runs. Procedure + risks: docs/deployment.md «Мост самообновления».
# HOW: append one line to the legacy update.log (the old UI tails it), detach
#   the full install into its OWN transient unit (systemd-run → own cgroup, so
#   the nginx/fcgiwrap restarts inside install.sh cannot kill it), exit 0 so
#   the old apply finishes normally. The unit: clone main → run
#   scripts/offline-full-update.sh --unattended (legacy status file + log).
# REMOVAL: scripts/03-webserver.sh reinstalls the genuine repair-web-env from
#   the cloned main during that install — the launcher is gone afterwards.
# TEST HOOK: SA02M_BRIDGE_DRY_RUN=1 in the environment passes --dry-run to the
#   wrapper (no backups, no install.sh). Never set on a real board; the old
#   apply does not export it.
# ═══════════════════════════════════════════════════════════════════════════
set -u

STATEDIR=/var/lib/sa02m-web-build
UPDATE_LOG="$STATEDIR/update.log"   # the status file ($STATEDIR/update_status) is owned by the unit body below
LIB=/usr/local/lib/sa02m-web-build-lib.sh
# shellcheck disable=SC1090
[ -f "$LIB" ] && . "$LIB"
# Same default the old apply and the check use when the lib sets no URL.
REPO_URL="${SA02M_WEB_BUILD_REPO_URL:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
TS="$(date +%Y%m%d-%H%M%S)"
UNIT="sa02m-bridge-full-update-${TS}"
DRY_FLAG=""
[ "${SA02M_BRIDGE_DRY_RUN:-0}" = 1 ] && DRY_FLAG="--dry-run"

mkdir -p "$STATEDIR"
blog() { printf '%s мост: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$UPDATE_LOG" 2>/dev/null || true; }

# Idempotent: a second «Применить» while a bridge unit still runs must not
# start a second install (the wrapper's PID file would refuse it anyway, but a
# second clone + error status would confuse the UI).
if command -v systemctl >/dev/null 2>&1 \
   && systemctl list-units --plain --no-legend 'sa02m-bridge-full-update-*' 2>/dev/null | grep -q .; then
    blog "полная установка уже идёт (юнит sa02m-bridge-full-update-*) — повторный запуск пропущен"
    exit 0
fi

# The detached body. Passed to the unit as text (`declare -f`), so it neither
# depends on this file staying on disk (03-webserver replaces it mid-install)
# nor on shell quoting of an inline -c string. Args: ts repo_url dry_flag.
bridge_body() {
    local ts=$1 repo=$2 dry=${3:-}
    local statedir=/var/lib/sa02m-web-build clone="/root/sa02m-bridge-${ts}" rc
    exec >> "/root/install-bridge-${ts}.log" 2>&1
    sleep 5   # let the old apply write its `done` first; from here the unit owns the status
    printf 'running' > "$statedir/update_status"
    printf '%s мост: клонирую %s (main) в %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$repo" "$clone" >> "$statedir/update.log"
    rm -rf "$clone"
    if ! git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 clone --depth=1 --branch main "$repo" "$clone"; then
        printf '%s мост: git clone не удался (нет интернета?) — статус error, лог /root/install-bridge-%s.log\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "$ts" >> "$statedir/update.log"
        printf 'error' > "$statedir/update_status"
        return 1
    fi
    # shellcheck disable=SC2086  # $dry is "" or --dry-run, deliberately unquoted
    bash "$clone/scripts/offline-full-update.sh" --unattended $dry \
        --status-file "$statedir/update_status" --log "$statedir/update.log"
    rc=$?
    [ "$rc" -eq 0 ] && rm -rf "$clone"   # keep the clone on failure for diagnosis
    return "$rc"
}

blog "запускаю полную установку из main в фоне (юнит ${UNIT}), лог /root/install-bridge-${TS}.log; подождите 10–15 мин, затем обновите страницу${DRY_FLAG:+ [DRY-RUN]}"

if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --unit="$UNIT" --collect -q /bin/bash -c "$(declare -f bridge_body); bridge_body '$TS' '$REPO_URL' '$DRY_FLAG'" \
        || blog "systemd-run не удался — установка не запущена (см. journalctl -u ${UNIT})"
else
    # Fallback without systemd-run: the child stays in the caller's cgroup
    # (fcgiwrap → sudo → old apply), so a `systemctl restart fcgiwrap/nginx`
    # inside install.sh may kill it mid-way. Accepted only where systemd-run
    # is absent; nohup/setsid shield it from HUP and the terminal, not cgroups.
    setsid nohup /bin/bash -c "$(declare -f bridge_body); bridge_body '$TS' '$REPO_URL' '$DRY_FLAG'" >/dev/null 2>&1 &
fi
exit 0
