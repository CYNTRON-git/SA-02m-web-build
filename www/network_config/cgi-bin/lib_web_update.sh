#!/bin/bash
# lib_web_update.sh — the ONE home for «is the update runner alive?» on the CGI
# side. Sourced by web_update_apply.cgi (GET status: `runner_alive` / `stale`)
# and reboot.cgi (refuse a reboot while a live runner works). Contract:
# docs/contracts/web-update.md «Жизненный цикл apply».
#
# Why it exists (field incident 2026-09-23, 1.0.6.52): a runner SIGKILLed at
# the health gate (it ran inside fcgiwrap's cgroup and the gate restarts
# fcgiwrap) left transaction.json at verifying/85 forever, and the status CGI
# mapped every running stage to «running» with no liveness test — the panel
# polled «Проверка сервисов…» for 40 minutes with no error at all.
#
# Everything here is READ-ONLY and unprivileged (www-data): the runner's lock
# pid + its /proc cmdline, and `systemctl is-active` / `list-units`. The CGI
# never repairs a transaction — that is recover/verify at boot.
#
# Alive =
#   the pid in $STATEDIR/update.lock exists AND its cmdline names the runner
#   (`sa02m-update-runner` — the installed binary or the self-copy under
#   /var/lib/sa02m-update/runner/<txn>/runner; a reused pid never matches),
#   OR the legacy launcher (sa02m-web-update-apply — the clone/handoff phase,
#   before the runner has taken its own lock) is alive on ITS lock pid,
#   OR sa02m-update.service / sa02m-update-verify.service is active,
#   OR a transient sa02m-update-apply-<txn8>.service (the cgroup escape,
#   etc/sa02m-update-runner.sh escape_foreign_cgroup) is running.
# `systemctl` absent ⇒ that half is false. Every systemctl call is bounded.
#
# The same test lives once more, root-side, in etc/sa02m-web-update-apply.sh
# (the launcher must not source a www-data-writable file) — keep the two in
# step. Harness: scripts/dev/test-web-update-apply-guard.sh sections G and R.

WEB_UPD_STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
WEB_UPD_TXN_FILE="$WEB_UPD_STATEDIR/transaction.json"
WEB_UPD_LEGACY_STATEDIR="${SA02M_WEB_BUILD_STATEDIR:-/var/lib/sa02m-web-build}"
# Stages during which a reboot / a second launch would tear a live apply apart.
WEB_UPD_BUSY_STAGES="applying verifying committing rolling_back"

# _web_upd_pid_cmdline_matches LOCKFILE GLOB... → rc 0 when the lock pid is
# alive and its cmdline matches one of the GLOBs (one per argument: a `|`
# inside an expanded case pattern is a literal bar, not an alternation).
_web_upd_pid_cmdline_matches() {
  local lock="$1" pid cmd pat
  shift
  [ -r "$lock" ] || return 1
  pid=$(tr -d ' \r\n' < "$lock" 2>/dev/null)
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
  for pat in "$@"; do
    # shellcheck disable=SC2254
    case "$cmd" in $pat) return 0 ;; esac
  done
  return 1
}

web_upd_runner_alive() {
  _web_upd_pid_cmdline_matches "$WEB_UPD_STATEDIR/update.lock" '*sa02m-update-runner*' '*/sa02m-update/runner/*' && return 0
  _web_upd_pid_cmdline_matches "$WEB_UPD_LEGACY_STATEDIR/update.lock" '*sa02m-web-update-apply*' && return 0
  if command -v systemctl >/dev/null 2>&1; then
    timeout 5 systemctl is-active --quiet sa02m-update.service 2>/dev/null && return 0
    timeout 5 systemctl is-active --quiet sa02m-update-verify.service 2>/dev/null && return 0
    # Capture, then match in-shell (quality-gate-rigor.md shape f).
    local units
    units=$(timeout 5 systemctl list-units --plain --no-legend 'sa02m-update-apply-*.service' 2>/dev/null) || units=""
    case "$units" in *" running"*) return 0 ;; esac
  fi
  return 1
}

# Prints the transaction stage, empty when there is no readable transaction.
web_upd_txn_stage() {
  [ -r "$WEB_UPD_TXN_FILE" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import json, sys
try:
    print(str(json.load(open(sys.argv[1], encoding="utf-8")).get("stage") or ""))
except Exception:
    pass' "$WEB_UPD_TXN_FILE" 2>/dev/null | tr -d '\r'
}

web_upd_stage_busy() {  # $1=stage
  case " $WEB_UPD_BUSY_STAGES " in *" ${1:-} "*) return 0 ;; esac
  return 1
}
