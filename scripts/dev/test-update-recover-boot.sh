#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-update-recover-boot.sh — recover at boot never rolls a COMPLETE deployed
# tree back; verification moves to sa02m-update-verify.service, ordered after
# the web stack; the runtime-watchdog hold survives the handover. Quality row
# `update-recover-boot`. Field incident 2026-09-23 (Skolkovo, six boards
# 1.0.6.49 → 1.0.6.50; plan 1.0.6.52, F2 + F6 + F4b).
#
# Why: sa02m-update-recover.service is ordered Before=nginx.service
# fcgiwrap.service. On a transaction left at `verifying` with every file on disk
# (the runner had been SIGKILLed at the health gate), cmd_recover called the
# whole restart+health path: `restart fcgiwrap` / `reload nginx` are jobs on
# units ordered AFTER the still-running recover job, so each hit its timeout,
# `units_active` then found nginx inactive, and a GOOD tree was rolled back —
# ≈3–4 min without web, then the old version. The CHANGELOG 1.0.6.8 intent
# («recover при полном deploy на verifying завершает обновление») cannot hold
# at boot by construction. Now: recover hands a complete tree to
# `sa02m-update-verify.service` (static, After=nginx fcgiwrap devices-api) and
# exits; `runner verify` runs enable+tmpfiles and the health gate only — no
# restart sets (every unit already started from the deployed tree) — and rolls
# back ONLY on a real health failure, with the reason recorded (F4b:
# rollback_from_journal takes an error code + message). In boot context a
# rollback's restart_after_rollback skips nginx/fcgiwrap for the same reason.
# F6: RUNTIME_WDT_PREV (the watchdog value held off for the apply window) is a
# shell variable lost across re-exec / cgroup escape / reboot — the value is
# persisted in transaction.json (runtime_wdt_prev_usec) and reloaded by every
# entry point that inherits an existing imaging lock; a transaction written by
# an OLDER launcher (no field, live value 0) restores the configured policy.
#
# Method: extract the SHIPPED cmd_recover / cmd_verify / rollback_from_journal /
# restart_after_rollback / _systemctl_bounded / _journal_has_dst_prefix /
# install_imaging_lock / cleanup_imaging_lock / load_runtime_wdt_prev /
# runtime_wdt_policy_usec / the two watchdog helpers (literal-prefix awk
# slices, extract-if-present so a pre-fix runner reaches the assertions and
# FAILS them instead of aborting); stub the collaborators (ensure_dirs,
# acquire_lock, log → a file, manifest_path, commit_markers → a marker file,
# FILE-backed txn_get / txn_patch / txn_has_key, health_check /
# services_enable_and_tmpfiles / restart_services_and_health → recording stubs
# whose rc is $T/health.rc); PATH
# shims for systemctl (records argv; `start --no-block` rc from $T/start.rc),
# systemd-run (records argv; rc from $T/run.rc), busctl (a writable manager
# backed by $T/wdt.value), nginx, sync, and the CRLF-normalising python3 of the
# sibling harnesses. cmd_recover/cmd_verify `exit`, so each case runs in a
# subshell. Nothing touches the real systemd/filesystem, no root, no device.
#
# Section U pins the unit files and the three installer/packer unit lists
# COMMENT-STRIPPED through lib_check.sh; those pins ARE registered in
# comment-mutation-proof (this harness therefore carries no exemption marker).
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show 6ba943d:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-recover-boot.sh   → R1/R2 (recover calls the
#   restart+health path and rolls back), R3 (reload nginx from boot context),
#   R4/R5 (no cmd_verify, no handoff), W1/W2 (no persistence) go RED. PINNED ref
#   (6ba943d = main at 1.0.6.50, the field boards' runner), not `main`.
#   RED observed 2026-09-23 on that tree: 38 FAIL — R1 «recover bounced
#   nginx/fcgiwrap from a Before=nginx unit: systemctl restart fcgiwrap;
#   systemctl reload nginx» and «stage=rolled_back (want verifying) — a good
#   tree was rolled_back», R2 the same on committing, R3 «error_code=E_APPLY
#   (want E_POWER)» + empty message, R4 rc=127 (no cmd_verify), R5 no
#   systemd-run and rolled_back, W1/W2 field absent / nothing restored, U1 unit
#   file and the three lists missing. (38 after review 1.0.6.52 finding 1 added
#   the R5a --no-block assert — which was also RED on the first fixed tree:
#   «fallback systemd-run lacks --no-block».)
#
# Run: bash scripts/dev/test-update-recover-boot.sh   (bash + python3 + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${UPDATE_RUNNER_SRC:-etc/sa02m-update-runner.sh}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable (runner requires it)"; exit 0; }
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# TW: the sandbox root in the form python receives — on git-bash a native
# python cannot open an MSYS /tmp path handed to it in an environment variable
# (test-update-conditional-restart.sh, header class 2). PATH keeps $T.
TW=$T
if command -v cygpath >/dev/null 2>&1; then TW=$(cygpath -m "$T") || TW=$T; fi

cat "$SRC" > "$T/runner.sh" 2>/dev/null || { echo "FAIL  cannot read runner source: $SRC"; exit 1; }
SRC="$T/runner.sh"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

extract() {
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC"
}
: > "$T/fn.sh"
# Always present (any runner since 1.0.6.41); a missing one is a broken extraction.
for fn in cmd_recover rollback_from_journal _systemctl_bounded install_imaging_lock cleanup_imaging_lock \
          sa02m_runtime_watchdog_usec sa02m_runtime_watchdog_set; do
    extract "$fn" >> "$T/fn.sh"
    grep -q "^$fn() {" "$T/fn.sh" \
        || { echo "FAIL  could not extract $fn() from $SRC — the marker moved; fix this harness, do not delete it"; exit 1; }
done
# Present on the fixed side only — extract-if-present (the drive-to-failure run
# must reach and FAIL the assertions).
for fn in restart_after_rollback _journal_has_dst_prefix cmd_verify schedule_boot_verify load_runtime_wdt_prev runtime_wdt_policy_usec; do
    grep -q "^$fn() {" "$SRC" && extract "$fn" >> "$T/fn.sh"
done
[ "$(tail -n1 "$T/fn.sh")" = "}" ] || { echo "FAIL  extraction did not stop at a closing brace"; exit 1; }

# ── Globals the extracted functions read ────────────────────────────────────
STATEDIR="$TW/state"
TXN_FILE="$STATEDIR/transaction.json"
LOCKFILE="$STATEDIR/update.lock"
IMAGING_LOCK="$TW/imaging.lock"
VERSION_FILE="$TW/VERSION"
RUNNER_BIN_DST="$TW/runner-bin"
SA02M_WATCHDOG_POLICY_FILE="$TW/sa02m-watchdog.conf"
TXN="abcdef12-0000-4000-8000-000000000001"
STAGE="$STATEDIR/staging/$TXN"
IMAGING_HELD=0
LOCK_HELD=0
RUNTIME_WDT_PREV=""
RUNNER_CONTEXT=""
HEALTH_FAIL_REASON=""
CALLS="$T/calls.log"
LOG="$T/log"
export CALLS SA02M_WATCHDOG_POLICY_FILE
mkdir -p "$STAGE/meta" "$STATEDIR/rollback" "$STATEDIR/staging"
printf '9.9.9.9\n' > "$VERSION_FILE"
printf '#!/bin/bash\nexit 0\n' > "$RUNNER_BIN_DST"

# ── Stubs ───────────────────────────────────────────────────────────────────
# txn_get / txn_patch / txn_has_key are FILE-backed key=value stubs (the
# test-update-deploy-skip.sh idiom): the shipped ones are python with
# os.fdatasync + a directory fsync, which a Windows CPython has not got, and the
# assertions read values, not JSON. `null` is stored as empty, as txn_get prints it.
TXNVARS="$STATEDIR/txn.vars"
txn_patch()   { local kv; for kv in "$@"; do case "$kv" in *=null) kv="${kv%=null}=" ;; esac; printf '%s\n' "$kv" >> "$TXNVARS"; done; }
txn_get()     { local k=${1:-} v; [ -n "$k" ] || return 0; v=$(grep "^$k=" "$TXNVARS" 2>/dev/null | tail -n1); printf '%s\n' "${v#*=}"; }
txn_has_key() { grep -q "^${1:-}=" "$TXNVARS" 2>/dev/null; }
txn_exists()  { [ -f "$TXNVARS" ]; }
log()                          { printf '%s\n' "$*" >> "$LOG"; }
utc_now()                      { echo 1970-01-01T00:00:00Z; }
ensure_dirs()                  { mkdir -p "$STATEDIR/staging" "$STATEDIR/rollback" "$STATEDIR/state"; }
migrate_legacy_state()         { :; }
acquire_lock()                 { printf '%s\n' "$$" > "$LOCKFILE"; LOCK_HELD=1; }
manifest_path()                { printf '%s\n' "$STAGE/meta/manifest.json"; }
wipe_incoming_staging()        { :; }
commit_markers()               { : > "$T/commit.marker"; }
health_check()                 { echo "fn health_check $1" >> "$CALLS"; HEALTH_FAIL_REASON="unit not active: nginx (inactive)"; return "$(cat "$T/health.rc")"; }
services_enable_and_tmpfiles() { echo "fn services_enable_and_tmpfiles $1" >> "$CALLS"; return 0; }
services_restart_sets()        { echo "fn services_restart_sets $1" >> "$CALLS"; return 0; }
restart_services_and_health()  { echo "fn restart_services_and_health $1" >> "$CALLS"; HEALTH_FAIL_REASON="unit not active: nginx (inactive)"; return "$(cat "$T/health.rc")"; }

# ── PATH shims ──────────────────────────────────────────────────────────────
mkdir -p "$T/bin"
REAL_PY=$(command -v python3)
cat > "$T/bin/python3" <<SH
#!/bin/bash
"$REAL_PY" "\$@" | tr -d '\r'
exit "\${PIPESTATUS[0]}"
SH
cat > "$T/bin/systemctl" <<'SH'
#!/bin/bash
args=(); for a in "$@"; do args+=("${a%$'\r'}"); done; set -- "${args[@]}"
printf 'systemctl %s\n' "$*" >> "$CALLS"
case "${1:-}" in
    start) [ "${2:-}" = "--no-block" ] && exit "$(cat "${START_RC_FILE:-/dev/null}" 2>/dev/null || echo 0)"; exit 0 ;;
    is-active) exit 1 ;;
    *) exit 0 ;;
esac
SH
cat > "$T/bin/systemd-run" <<'SH'
#!/bin/bash
printf 'systemd-run %s\n' "$*" >> "$CALLS"
exit "$(cat "${RUN_RC_FILE:-/dev/null}" 2>/dev/null || echo 0)"
SH
cat > "$T/bin/busctl" <<'SH'
#!/bin/bash
printf 'busctl %s\n' "$*" >> "$CALLS"
case "${1:-}" in
    get-property) printf 't %s\n' "$(cat "$WDT_VALUE_FILE")" ;;
    set-property) v=""; for a in "$@"; do v=$a; done; printf '%s\n' "$v" > "$WDT_VALUE_FILE" ;;
esac
exit 0
SH
printf '#!/bin/bash\nexit 0\n' > "$T/bin/nginx"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/sync"
chmod 755 "$T/bin/"*
PATH="$T/bin:$PATH"
export START_RC_FILE="$T/start.rc" RUN_RC_FILE="$T/run.rc" WDT_VALUE_FILE="$T/wdt.value"
printf '0\n' > "$T/start.rc"; printf '0\n' > "$T/run.rc"; printf '15000000\n' > "$T/wdt.value"

# shellcheck disable=SC1090
. "$T/fn.sh"

# ── Fixtures ────────────────────────────────────────────────────────────────
cat > "$STAGE/meta/manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "9.9.9.9",
  "deploy": [],
  "services": {
    "daemon_reload": true,
    "enable": [],
    "restart": ["fcgiwrap", "nginx", "sa02m-rules"],
    "health": {"http_url": "", "units_active": ["nginx"], "version_file": "$VERSION_FILE"}
  }
}
JSON
write_txn() {  # $1=stage $2=files_done $3=files_total [$4... extra key=value]
    printf '%s\n' "id=$TXN" "operation=update" "source=github" "stage=$1" "progress_pct=85" \
        "files_total=$3" "files_done=$2" "result=pending" "error_code=" "error_message=" \
        "target_version=9.9.9.9" "imaging_lock=true" > "$TXNVARS"
    shift 3
    [ $# -gt 0 ] && printf '%s\n' "$@" >> "$TXNVARS"
    return 0
}
reset_run() {
    : > "$CALLS"; : > "$LOG"; rm -f "$T/commit.marker" "$IMAGING_LOCK"
    printf '0\n' > "$T/start.rc"; printf '0\n' > "$T/run.rc"; printf '15000000\n' > "$T/wdt.value"
    IMAGING_HELD=0; RUNTIME_WDT_PREV=""; RUNNER_CONTEXT=""
}
called()      { grep -qxF "$1" "$CALLS" 2>/dev/null; }
called_re()   { grep -qE "$1" "$CALLS" 2>/dev/null; }
logged()      { grep -qF -- "$1" "$LOG" 2>/dev/null; }
txn_field()   { txn_get "$1"; }
run_recover() { ( set -euo pipefail; cmd_recover ) >/dev/null 2>&1; rc=$?; }
run_verify()  { ( set -euo pipefail; cmd_verify ) >/dev/null 2>&1; rc=$?; }
rc=0

no_web_restart() {  # the restart jobs that deadlock under a Before=nginx unit
    ! called_re '^systemctl (restart|reload|start) (nginx|fcgiwrap)(\.service)?$'
}

# ── R1: verifying, deploy complete → handoff, no restarts, no rollback ──────
echo "── R1: recover on verifying with a complete tree ──"
reset_run; printf '1\n' > "$T/health.rc"     # the health path CANNOT pass at boot
write_txn verifying 3 3
run_recover
[ "$rc" -eq 0 ] && ok "R1 recover exits 0" || bad "R1 recover rc=$rc"
no_web_restart && ok "R1 no restart/reload of nginx or fcgiwrap from recover (units ordered after it)" \
                || bad "R1 recover bounced nginx/fcgiwrap from a Before=nginx unit — the boot deadlock: $(grep -E 'nginx|fcgiwrap' "$CALLS" | tr '\n' ';')"
if called_re '^fn (restart_services_and_health|health_check|services_restart_sets) '; then
    bad "R1 recover ran the restart/health path at boot: $(grep '^fn ' "$CALLS" | tr '\n' ';')"
else
    ok "R1 recover did not run the restart/health path itself"
fi
called "systemctl start --no-block sa02m-update-verify.service" \
    && ok "R1 recover started sa02m-update-verify.service (--no-block)" \
    || bad "R1 no 'systemctl start --no-block sa02m-update-verify.service' — verification not handed off"
[ "$(txn_field stage)" = verifying ] && ok "R1 transaction stays at verifying for the verify unit" \
    || bad "R1 transaction stage=$(txn_field stage) (want verifying) — a good tree was $(txn_field stage)"
[ "$(txn_field boot_verify_pending)" = true ] && ok "R1 boot_verify_pending=true recorded" \
    || bad "R1 boot_verify_pending=$(txn_field boot_verify_pending) (want true)"
[ -f "$IMAGING_LOCK" ] && ok "R1 imaging lock kept for the verify unit" || bad "R1 imaging lock released before verification"
logged "rollback from journal" && bad "R1 recover rolled a complete tree back" || ok "R1 no rollback of the complete tree"

# ── R2: committing, deploy complete → same handoff ──────────────────────────
echo "── R2: recover on committing with a complete tree ──"
reset_run; printf '1\n' > "$T/health.rc"
write_txn committing 3 3
run_recover
no_web_restart && called "systemctl start --no-block sa02m-update-verify.service" && [ "$(txn_field stage)" = verifying ] \
    && ok "R2 committing + complete tree → handed to the verify unit, stage verifying, no web restarts" \
    || bad "R2 committing: stage=$(txn_field stage), calls: $(tr '\n' ';' < "$CALLS")"

# ── R3: verifying, deploy INCOMPLETE → rollback, but no nginx/fcgiwrap jobs ─
echo "── R3: recover on verifying with an incomplete tree ──"
reset_run; printf '1\n' > "$T/health.rc"
write_txn verifying 2 3
run_recover
[ "$(txn_field stage)" = rolled_back ] && ok "R3 incomplete tree is still rolled back" \
    || bad "R3 incomplete tree: stage=$(txn_field stage) (want rolled_back)"
called "systemctl restart sa02m-rules" && ok "R3 rollback re-bounces restart[] (sa02m-rules)" \
    || bad "R3 sa02m-rules not restarted after the boot rollback"
no_web_restart && ok "R3 boot-context rollback skips nginx/fcgiwrap (systemd starts them on the restored tree)" \
    || bad "R3 boot-context rollback bounced nginx/fcgiwrap: $(grep -E 'nginx|fcgiwrap' "$CALLS" | tr '\n' ';')"
[ "$(txn_field error_code)" = E_POWER ] && ok "R3 rollback reason recorded as E_POWER (power loss during verifying)" \
    || bad "R3 error_code=$(txn_field error_code) (want E_POWER)"
[ -n "$(txn_field error_message)" ] && ok "R3 error_message names the cause: $(txn_field error_message)" \
    || bad "R3 error_message empty after the rollback"

# ── R4: cmd_verify — health ok → done; health fails → rollback with reason ──
echo "── R4: runner verify ──"
reset_run; printf '0\n' > "$T/health.rc"
write_txn verifying 3 3 boot_verify_pending=true runtime_wdt_prev_usec=15000000
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"   # the hold is in force from before the reboot
run_verify
[ "$rc" -eq 0 ] && ok "R4a verify exits 0 on a passing health gate" || bad "R4a verify rc=$rc (cmd_verify missing?)"
[ -f "$T/commit.marker" ] && ok "R4a commit markers written" || bad "R4a no commit markers"
[ "$(txn_field stage)" = done ] && [ "$(txn_field result)" = success ] && ok "R4a transaction done/success" \
    || bad "R4a stage=$(txn_field stage) result=$(txn_field result)"
[ -z "$(txn_field error_code)" ] && ok "R4a error_code cleared" || bad "R4a error_code=$(txn_field error_code)"
[ "$(txn_field boot_verify_pending)" = false ] && ok "R4a boot_verify_pending=false" || bad "R4a boot_verify_pending=$(txn_field boot_verify_pending)"
called_re '^fn services_enable_and_tmpfiles ' && ok "R4a verify ran enable+tmpfiles (idempotent, boot does not)" \
    || bad "R4a verify skipped services_enable_and_tmpfiles"
called_re '^fn health_check ' && ok "R4a verify ran the health gate" || bad "R4a verify never ran health_check"
called_re '^fn (services_restart_sets|restart_services_and_health) ' \
    && bad "R4a verify re-ran the restart sets after boot" || ok "R4a verify did NOT re-run the restart sets"
[ ! -f "$IMAGING_LOCK" ] && ok "R4a imaging lock released after verification" || bad "R4a imaging lock still present"
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' \
    && ok "R4a runtime watchdog restored to 15000000 from the persisted field (F6)" \
    || bad "R4a watchdog not restored: $(grep busctl "$CALLS" | tr '\n' ';')"

reset_run; printf '1\n' > "$T/health.rc"
write_txn verifying 3 3 boot_verify_pending=true runtime_wdt_prev_usec=15000000
date -Iseconds > "$IMAGING_LOCK"
run_verify
[ "$rc" -ne 0 ] && ok "R4b verify exits non-zero on a failed health gate" || bad "R4b verify rc=0 despite a failed health gate"
[ "$(txn_field stage)" = rolled_back ] && ok "R4b failed post-boot health → rolled back" || bad "R4b stage=$(txn_field stage)"
[ "$(txn_field error_code)" = E_HEALTH ] && ok "R4b error_code=E_HEALTH (not the blanket E_APPLY)" \
    || bad "R4b error_code=$(txn_field error_code) (want E_HEALTH)"
[ "$(txn_field error_message)" = "unit not active: nginx (inactive)" ] && ok "R4b the health reason reaches the transaction" \
    || bad "R4b error_message='$(txn_field error_message)' (want the health reason)"
called "systemctl restart sa02m-rules" && ok "R4b rollback after verify restarts the sets (web is up now)" \
    || bad "R4b rollback after verify did not restart sa02m-rules"

for st in done rolled_back; do
    reset_run; printf '0\n' > "$T/health.rc"
    write_txn "$st" 3 3
    run_verify
    if [ "$rc" -eq 0 ] && ! called_re '^fn health_check ' && [ "$(txn_field stage)" = "$st" ]; then
        ok "R4c verify on stage=$st is a no-op (rc 0, no health call)"
    else
        bad "R4c verify on stage=$st: rc=$rc stage=$(txn_field stage) calls: $(tr '\n' ';' < "$CALLS")"
    fi
done

# ── R5: the static unit cannot start → systemd-run fallback; both fail → left ─
echo "── R5: handoff fallbacks ──"
reset_run; printf '1\n' > "$T/health.rc"; printf '1\n' > "$T/start.rc"
write_txn verifying 3 3
run_recover
called_re "^systemd-run .*--unit=sa02m-update-verify-abcdef12 " && ok "R5a fallback: systemd-run --unit=sa02m-update-verify-<txn8>" \
    || bad "R5a no systemd-run fallback with the txn-named unit: $(grep systemd-run "$CALLS" | tr '\n' ';')"
# --no-block is load-bearing: without it systemd-run waits for the transient
# unit's start job, which is ordered after nginx, which is ordered after the
# recover unit this code runs in — a guaranteed 30 s timeout and a false E_CMD.
called_re "^systemd-run .*--no-block " && ok "R5a fallback is --no-block (a blocking start deadlocks against recover's own Before=nginx)" \
    || bad "R5a fallback systemd-run lacks --no-block — it can only time out from inside recover: $(grep systemd-run "$CALLS" | tr '\n' ';')"
called_re "^systemd-run .*-p After=nginx\.service " && ok "R5a fallback orders the transient unit After=nginx.service" \
    || bad "R5a fallback lacks -p After=nginx.service"
called_re "^systemd-run .* $RUNNER_BIN_DST verify$" && ok "R5a fallback runs '<runner> verify'" \
    || bad "R5a fallback argv does not end in '$RUNNER_BIN_DST verify'"
[ "$(txn_field stage)" = verifying ] && ok "R5a stage stays verifying for the transient unit" || bad "R5a stage=$(txn_field stage)"

reset_run; printf '1\n' > "$T/health.rc"; printf '1\n' > "$T/start.rc"; printf '1\n' > "$T/run.rc"
write_txn verifying 3 3
run_recover
[ "$(txn_field stage)" = verifying ] && ok "R5b both handoffs fail → transaction LEFT at verifying (next boot retries), never rolled back" \
    || bad "R5b both handoffs fail → stage=$(txn_field stage) (want verifying)"
logged "rollback from journal" && bad "R5b a complete tree was rolled back because the verify unit could not start" \
    || ok "R5b no rollback"
[ ! -f "$IMAGING_LOCK" ] && called "systemctl start net-watchdog" && ok "R5b imaging lock released (watchdogs back) when nobody will verify" \
    || bad "R5b imaging lock left held with no verifier scheduled"
logged "cannot schedule post-boot verification" && ok "R5b the failure is logged with its cause" \
    || bad "R5b no 'cannot schedule post-boot verification' log line"

# ── W1/W2: the watchdog hold value survives the handover (F6) ────────────────
echo "── W: runtime watchdog value persistence ──"
reset_run
write_txn applying 0 3
( set -euo pipefail; install_imaging_lock ) >/dev/null 2>&1
[ "$(txn_field runtime_wdt_prev_usec)" = 15000000 ] && ok "W1 install_imaging_lock persists runtime_wdt_prev_usec=15000000 in the transaction" \
    || bad "W1 runtime_wdt_prev_usec='$(txn_field runtime_wdt_prev_usec)' (want 15000000) — the hold value dies with the process"
[ "$(cat "$T/wdt.value")" = 0 ] && ok "W1 hold taken (manager reads 0)" || bad "W1 manager value $(cat "$T/wdt.value") after the hold"
: > "$CALLS"
( set -euo pipefail; RUNTIME_WDT_PREV=""; load_runtime_wdt_prev; IMAGING_HELD=1; cleanup_imaging_lock ) >/dev/null 2>&1
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' \
    && ok "W1 a fresh process (RUNTIME_WDT_PREV empty) reloads the field and restores 15000000" \
    || bad "W1 fresh process did not restore the watchdog: $(grep busctl "$CALLS" | tr '\n' ';')"

reset_run
write_txn verifying 3 3               # an OLDER launcher's transaction: no field at all
printf '0\n' > "$T/wdt.value"         # the hold is in force
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
( set -euo pipefail; RUNTIME_WDT_PREV=""; load_runtime_wdt_prev; IMAGING_HELD=1; cleanup_imaging_lock ) >/dev/null 2>&1
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' \
    && ok "W2 field absent + live 0 + policy 15s → the configured policy (15000000) is restored" \
    || bad "W2 older-launcher transaction: watchdog left at 0: $(grep busctl "$CALLS" | tr '\n' ';')"
logged "previous value unknown" && ok "W2 the fallback is logged" || bad "W2 no 'previous value unknown' log line"
reset_run
write_txn verifying 3 3
printf '0\n' > "$T/wdt.value"
rm -f "$SA02M_WATCHDOG_POLICY_FILE"
( set -euo pipefail; RUNTIME_WDT_PREV=""; load_runtime_wdt_prev; IMAGING_HELD=1; cleanup_imaging_lock ) >/dev/null 2>&1
called_re '^busctl set-property' && bad "W2b no policy file → a value was invented: $(grep busctl "$CALLS" | tr '\n' ';')" \
    || ok "W2b no policy file → nothing restored (never invents a value)"
logged "WARN" && ok "W2b the unknown value is logged as a WARN" || bad "W2b no WARN logged for the unknown value"

# ── U: the units and the installer/packer lists (static, comment-stripped) ──
echo "── U: unit files + installer lists ──"
VU=etc/systemd/sa02m-update-verify.service
RU=etc/systemd/sa02m-update-recover.service
if [ -r "$VU" ]; then
    stripped_has "$VU" 'Type=oneshot' && ok "U1 verify unit is Type=oneshot" || bad "U1 verify unit not Type=oneshot"
    stripped_has "$VU" 'ExecStart=/usr/local/libexec/sa02m-update-runner verify' && ok "U1 verify unit runs 'runner verify'" \
        || bad "U1 verify unit ExecStart is not '/usr/local/libexec/sa02m-update-runner verify'"
    for dep in nginx.service fcgiwrap.service sa02m-devices-api.service sa02m-update-recover.service; do
        stripped_matches "$VU" "^After=.*\b$dep\b" && ok "U1 verify unit After= names $dep" || bad "U1 verify unit After= lacks $dep"
    done
    stripped_matches "$VU" '^Before=' && bad "U1 verify unit carries a Before= (it must run after the web stack)" || ok "U1 verify unit has no Before="
    stripped_matches "$VU" '^\[Install\]' && bad "U1 verify unit has an [Install] section (it is static: recover starts it)" || ok "U1 verify unit is static (no [Install])"
else
    bad "U1 $VU is missing"
fi
stripped_matches "$RU" '^Before=.*nginx\.service.*fcgiwrap\.service' && ok "U1 recover unit still Before=nginx fcgiwrap (the ordering the handoff exists for)" \
    || bad "U1 recover unit lost its Before=nginx.service fcgiwrap.service"
for f in scripts/03-webserver.sh scripts/update-www-only.sh; do
    n=$(stripped_count "$f" 'for _upd_unit in .*sa02m-update-recover\.service sa02m-update-verify\.service')
    [ "${n:-0}" -ge 1 ] && ok "U1 $f installs sa02m-update-verify.service next to the recover unit ($n loop(s))" \
        || bad "U1 $f does not list sa02m-update-verify.service in its unit loop"
done
stripped_has scripts/pack-offline-update.py '"sa02m-update-verify.service"' && ok "U1 pack-offline-update.py packs the verify unit" \
    || bad "U1 scripts/pack-offline-update.py does not pack sa02m-update-verify.service"

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
