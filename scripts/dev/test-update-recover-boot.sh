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
#   Round 2 (bench 1.135 RED run, 2026-09-23 — recover killed by its own
#   TimeoutStartSec=300 mid-rollback, stage rolling_back + lock + watchdog 0
#   left behind): R3 now requires NO restarts in boot context, R6 (the residue
#   converges on one boot), R7 (on_exit restores the watchdog on a non-zero
#   exit at a rolling stage; INT/TERM trap), R8 (`runner reclaim`), W3 (a new
#   hold over a residue-0 manager keeps the policy value), M (the field remedy
#   script). RED on the round-1 tree: 12 FAIL — R3 «issued restarts: systemctl
#   restart sa02m-rules», R6 same, R7 «left the watchdog at 0» + «no INT/TERM
#   trap», R8 rc=127 (no cmd_reclaim), W3 «runtime_wdt_prev_usec='0'».
#   Round 3 (review): R8d (runtime reclaim of a COMPLETE tree runs the restart
#   sets the dead apply never reached, before the verify handoff; boot context
#   still runs none), R8e (a held lock → reclaim exits 3, nothing touched), Me
#   (the remedy stops on rc 3). RED on the round-2 tree: 4 FAIL — R8d «handed a
#   complete tree to verify WITHOUT the restart sets», R8e «rc=0 (want 3)», Me
#   «remedy exited 0» + «went on after a refused reclaim: lock removed, busctl
#   set-property …, systemctl start sa02m-flasher».
#   1.0.6.54 round 4: R4d — a failing services_enable_and_tmpfiles (a dead
#   manifest read) makes verify roll back with E_HEALTH and its reason. RED on
#   0f0fc86: rc=1, stage left at verifying (set -e killed verify mid-way).
#   Round 6: R10 — a torn last journal line converges in ONE run, through boot
#   recover (R10a) and runtime reclaim (R10b). RED on fb78136: rc=1, stage
#   frozen at rolling_back, lock kept, file NEW.
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
for fn in restart_after_rollback _journal_has_dst_prefix cmd_verify schedule_boot_verify load_runtime_wdt_prev runtime_wdt_policy_usec restore_runtime_wdt on_exit cmd_reclaim recover_transaction; do
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
try_lock()                     { [ "$(cat "$T/trylock.rc" 2>/dev/null || echo 0)" = 0 ] || return 1; printf '%s\n' "$$" > "$LOCKFILE"; LOCK_HELD=1; }
manifest_path()                { printf '%s\n' "$STAGE/meta/manifest.json"; }
wipe_incoming_staging()        { :; }
commit_markers()               { : > "$T/commit.marker"; }
health_check()                 { echo "fn health_check $1" >> "$CALLS"; HEALTH_FAIL_REASON="unit not active: nginx (inactive)"; return "$(cat "$T/health.rc")"; }
services_enable_and_tmpfiles() { echo "fn services_enable_and_tmpfiles $1" >> "$CALLS"; [ -f "$T/enable.rc" ] || return 0; HEALTH_FAIL_REASON="manifest read failed: services.enable"; return "$(cat "$T/enable.rc")"; }
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
# Bench 1.135 RED run (2026-09-23): recover's restart set at boot ran into its
# own TimeoutStartSec=300 and was SIGKILLed mid-rollback (stage rolling_back,
# imaging lock + watchdog 0 left behind). At boot every unit starts from the
# restored files anyway: a boot-context rollback restarts NOTHING.
unit_restarts() { grep -E '^systemctl (restart|start|reload) ' "$CALLS" 2>/dev/null | grep -v 'net-watchdog'; }   # the hold's own start (--no-block) is not a restart set
if [ -n "$(unit_restarts)" ]; then
    bad "R3 boot-context rollback issued restarts (each bounded 60 s — enough to hit recover's 300 s timeout): $(unit_restarts | tr '\n' ';')"
else
    ok "R3 boot-context rollback restarts nothing (boot starts every unit from the restored tree)"
fi
called "systemctl daemon-reload" && ok "R3 boot-context rollback still daemon-reloads the restored unit files" \
    || bad "R3 no daemon-reload after the boot rollback"
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

# R4d (review 1.0.6.54 round 4): services_enable_and_tmpfiles can now FAIL (a
# manifest read that dies). cmd_verify runs under `set -e` at top level, so an
# unchecked failing call there would kill verify mid-way — stage frozen at
# verifying, lock held — instead of deciding. It must be a health failure:
# rollback with E_HEALTH and the reason, health_check not reached.
reset_run; printf '0\n' > "$T/health.rc"; printf '1\n' > "$T/enable.rc"
write_txn verifying 3 3 boot_verify_pending=true runtime_wdt_prev_usec=15000000
date -Iseconds > "$IMAGING_LOCK"
run_verify
rm -f "$T/enable.rc"
if [ "$rc" -ne 0 ] && [ "$(txn_field stage)" = rolled_back ] && [ "$(txn_field error_code)" = E_HEALTH ] \
   && [ "$(txn_field error_message)" = "manifest read failed: services.enable" ]; then
    ok "R4d a failed enable step → verify rolls back with E_HEALTH and the reason (never dies mid-verify)"
else
    bad "R4d failed enable step: rc=$rc stage=$(txn_field stage) error_code=$(txn_field error_code) error_message='$(txn_field error_message)'"
fi
called_re '^fn health_check ' && bad "R4d health_check still ran after the enable step failed" \
    || ok "R4d health_check not reached after the enable step failed"

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
[ ! -f "$IMAGING_LOCK" ] && called_re '^systemctl start (--no-block )?net-watchdog$' && ok "R5b imaging lock released (watchdogs back) when nobody will verify" \
    || bad "R5b imaging lock left held with no verifier scheduled"
logged "cannot schedule post-boot verification" && ok "R5b the failure is logged with its cause" \
    || bad "R5b no 'cannot schedule post-boot verification' log line"

# ── R6: the field residue — stage rolling_back, lock + watchdog 0, dead runner ─
# The state bench 1.135 (and most likely the Skolkovo boards) sit in after the
# RED run: recover killed by its own timeout mid-rollback. One boot must
# converge: replay the journal, no restarts (boot), lock cleared, watchdog back
# from the POLICY (the 1.0.6.49 transaction carries no runtime_wdt_prev_usec).
echo "── R6: recover on rolling_back with lock + watchdog-0 residue ──"
reset_run; printf '1\n' > "$T/health.rc"
write_txn rolling_back 3 3
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
run_recover
[ "$rc" -eq 0 ] && ok "R6 recover exits 0" || bad "R6 recover rc=$rc"
[ "$(txn_field stage)" = rolled_back ] && ok "R6 rolling_back → rolled_back" || bad "R6 stage=$(txn_field stage) (want rolled_back)"
[ "$(txn_field error_code)" = E_POWER ] && [ -n "$(txn_field error_message)" ] && ok "R6 E_POWER + reason recorded: $(txn_field error_message)" \
    || bad "R6 error_code=$(txn_field error_code) message='$(txn_field error_message)'"
[ ! -f "$IMAGING_LOCK" ] && ok "R6 imaging lock cleared" || bad "R6 imaging lock still present"
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "R6 watchdog restored to 15000000 from the policy (no field in the old transaction)" \
    || bad "R6 watchdog NOT restored: $(grep busctl "$CALLS" | tr '\n' ';')"
called_re '^systemctl start (--no-block )?net-watchdog$' && ok "R6 net-watchdog started again (--no-block: recover runs before the network is up)" || bad "R6 net-watchdog not started"
called_re '^systemctl (restart|start|reload) (nginx|fcgiwrap|sa02m-rules)' && bad "R6 restarts issued from boot context: $(grep -E 'restart|reload' "$CALLS" | tr '\n' ';')" \
    || ok "R6 no unit restarts from boot context"

# ── R7: the EXIT trap on a non-zero exit mid-rollback keeps the lock but ──────
# restores the watchdog — SIGTERM (recover's timeout) must not leave the board
# without a hardware watchdog until the next boot.
echo "── R7: on_exit at a rolling stage ──"
reset_run
write_txn rolling_back 3 3 runtime_wdt_prev_usec=15000000
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
( set +e; IMAGING_HELD=1; RUNTIME_WDT_PREV=15000000; false; on_exit ) >/dev/null 2>&1
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "R7 on_exit (ec≠0, stage rolling_back) restores the watchdog" \
    || bad "R7 on_exit left the watchdog at 0 on a non-zero exit mid-rollback: $(grep busctl "$CALLS" | tr '\n' ';')"
[ -f "$IMAGING_LOCK" ] && ok "R7 on_exit keeps the imaging lock at a rolling stage (the next boot finishes)" || bad "R7 on_exit removed the lock mid-rollback"
stripped_matches "$SRC" "^trap 'exit 143' INT TERM" && ok "R7 INT/TERM are turned into an exit so the EXIT trap runs (the timeout SIGTERM)" \
    || bad "R7 no INT/TERM trap — a SIGTERM from recover's timeout skips on_exit"

# ── R8: runner reclaim — the residue on a board that neither reboots nor applies ─
echo "── R8: runner reclaim ──"
run_reclaim() { ( set -euo pipefail; cmd_reclaim ) >/dev/null 2>&1; rc=$?; }
# (a) terminal stage + leftover lock + watchdog 0 + no field → lock cleared, policy restored
reset_run
write_txn rolled_back 3 3
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
run_reclaim
[ "$rc" -eq 0 ] && [ ! -f "$IMAGING_LOCK" ] && ok "R8a reclaim on a terminal stage clears the leftover lock (rc 0)" \
    || bad "R8a reclaim rc=$rc lock present: $([ -f "$IMAGING_LOCK" ] && echo yes || echo no)"
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "R8a reclaim restores the watchdog from the policy" \
    || bad "R8a watchdog not restored: $(grep busctl "$CALLS" | tr '\n' ';')"
[ "$(txn_field stage)" = rolled_back ] && ok "R8a the terminal stage is untouched" || bad "R8a stage=$(txn_field stage)"
# (b) rolling_back with a dead runner → the rollback is finished (non-boot: restarts allowed)
reset_run
write_txn rolling_back 3 3
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
run_reclaim
[ "$(txn_field stage)" = rolled_back ] && [ "$(txn_field error_code)" = E_POWER ] && ok "R8b reclaim finishes a rollback the runner died in (rolled_back, E_POWER)" \
    || bad "R8b stage=$(txn_field stage) error_code=$(txn_field error_code)"
[ ! -f "$IMAGING_LOCK" ] && called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "R8b lock cleared, watchdog restored" \
    || bad "R8b lock present: $([ -f "$IMAGING_LOCK" ] && echo yes || echo no), busctl: $(grep busctl "$CALLS" | tr '\n' ';')"
called "systemctl restart sa02m-rules" && ok "R8b outside boot the rollback re-bounces restart[] (web is up)" \
    || bad "R8b restart[] not re-bounced outside boot context"
# (d) a COMPLETE verifying tree reclaimed at RUNTIME must first run the restart
# sets the dead apply never reached (nginx -t + reload, restart[] …) — verify
# alone would end `done` with daemons still holding old code in memory (the D9
# class); in boot context (R1/R9) the sets stay off.
reset_run; printf '0\n' > "$T/health.rc"
write_txn verifying 3 3
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
run_reclaim
called_re '^fn services_restart_sets ' && ok "R8d runtime reclaim of a complete tree runs services_restart_sets before the handoff" \
    || bad "R8d runtime reclaim handed a complete tree to verify WITHOUT the restart sets (old code stays in memory): $(tr '\n' ';' < "$CALLS")"
called "systemctl start --no-block sa02m-update-verify.service" && [ "$(txn_field stage)" = verifying ] && ok "R8d …then hands it to the verify unit (stage verifying)" \
    || bad "R8d no handoff after the restart sets: stage=$(txn_field stage) calls: $(tr '\n' ';' < "$CALLS")"
if grep -q '^fn services_restart_sets' "$CALLS" && grep -q '^systemctl start --no-block sa02m-update-verify' "$CALLS"; then
    [ "$(grep -n '^fn services_restart_sets' "$CALLS" | head -1 | cut -d: -f1)" -lt "$(grep -n '^systemctl start --no-block sa02m-update-verify' "$CALLS" | head -1 | cut -d: -f1)" ] \
        && ok "R8d restart sets run BEFORE the handoff" || bad "R8d restart sets ran after the handoff"
fi
# (e) the lock is held by a live runner → reclaim refuses with a DISTINCT code
# (3), so the remedy can stop instead of arming the watchdog under it
reset_run
write_txn rolling_back 3 3
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"; printf '1\n' > "$T/trylock.rc"
run_reclaim
rm -f "$T/trylock.rc"
[ "$rc" -eq 3 ] && ok "R8e held lock → reclaim exits 3 (refused, distinct from done)" || bad "R8e held lock → rc=$rc (want 3)"
[ -f "$IMAGING_LOCK" ] && ! called_re '^busctl set-property' && [ "$(txn_field stage)" = rolling_back ] && ok "R8e nothing touched under a held lock" \
    || bad "R8e touched state under a held lock: lock present $([ -f "$IMAGING_LOCK" ] && echo yes || echo no), calls: $(tr '\n' ';' < "$CALLS")"
# (c) nothing to reclaim → no-op
reset_run
write_txn done 3 3
run_reclaim
[ "$rc" -eq 0 ] && ! called_re '^busctl set-property' && [ "$(txn_field stage)" = done ] && ok "R8c no lock, terminal stage → no-op" \
    || bad "R8c rc=$rc calls: $(tr '\n' ';' < "$CALLS")"

# ── R9: the delivering OTA's exact residue on a ≤1.0.6.51 board ──────────────
# self_reexec_before_deploy copies the INSTALLED (old) runner and execs the
# copy, so the whole delivering apply — the health gate included — runs under
# the OLD code and dies at `restart fcgiwrap`. What the NEW recover/verify
# then find at the next boot: stage=verifying, files complete, VERSION = the
# new one, imaging lock present, RuntimeWatchdogUSec=0 with NO
# runtime_wdt_prev_usec field (old code wrote none), net-watchdog /
# sa02m-flasher stopped by the old runner (both enabled — boot starts them;
# nothing here may stop them again), the failed recover unit of the previous
# boot, the old CGI's legacy update_status=running. One boot must end with
# stage=done, lock gone, watchdog 15000000 (policy fallback), units untouched.
echo "── R9: the old runner's residue → recover → verify ──"
reset_run; printf '0\n' > "$T/health.rc"
write_txn verifying 3 3                      # no runtime_wdt_prev_usec, no boot_verify_pending
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
run_recover
[ "$rc" -eq 0 ] && called "systemctl start --no-block sa02m-update-verify.service" && ok "R9 recover hands the complete tree to the verify unit" \
    || bad "R9 recover rc=$rc calls: $(tr '\n' ';' < "$CALLS")"
logged "rollback from journal" && bad "R9 recover rolled the delivered tree back" || ok "R9 no rollback"
[ "$(txn_field runtime_wdt_prev_usec)" = 15000000 ] && ok "R9 recover persisted the POLICY value (old transaction had no field, manager at 0)" \
    || bad "R9 runtime_wdt_prev_usec='$(txn_field runtime_wdt_prev_usec)' (want 15000000)"
called_re '^systemctl stop .*(sa02m-flasher|nginx|fcgiwrap)' && bad "R9 recover stopped a unit boot must start: $(grep 'stop' "$CALLS" | tr '\n' ';')" \
    || ok "R9 recover stops nothing boot must start (net-watchdog stop is the hold's own, a no-op at boot)"
[ -f "$IMAGING_LOCK" ] && ok "R9 imaging lock kept for verify" || bad "R9 lock released before verification"
# …the verify unit runs (after nginx/fcgiwrap; the health gate passes)
: > "$CALLS"; : > "$LOG"; RUNTIME_WDT_PREV=""; IMAGING_HELD=0
run_verify
[ "$rc" -eq 0 ] && [ "$(txn_field stage)" = done ] && [ "$(txn_field result)" = success ] && ok "R9 verify → done/success" \
    || bad "R9 verify rc=$rc stage=$(txn_field stage) result=$(txn_field result)"
[ ! -f "$IMAGING_LOCK" ] && ok "R9 imaging lock gone" || bad "R9 imaging lock still present after verify"
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "R9 watchdog 15000000 restored (policy fallback carried through the transaction)" \
    || bad "R9 watchdog not restored: $(grep busctl "$CALLS" | tr '\n' ';')"
called_re '^systemctl start (--no-block )?net-watchdog$' && ok "R9 net-watchdog started by verify" || bad "R9 net-watchdog not started"
called_re '^systemctl stop ' && bad "R9 verify stopped a unit: $(grep 'stop' "$CALLS" | tr '\n' ';')" || ok "R9 verify stops nothing (sa02m-flasher/net-watchdog stay up)"
called_re '^fn (services_restart_sets|restart_services_and_health) ' && bad "R9 verify re-ran the restart sets" || ok "R9 verify: enable+tmpfiles + health only"
[ -z "$(txn_field error_code)" ] && ok "R9 error_code cleared" || bad "R9 error_code=$(txn_field error_code)"
[ -f "$T/commit.marker" ] && ok "R9 commit markers written (deployed_version for the panel)" || bad "R9 no commit markers"

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
# W3 a NEW apply on a board carrying the residue (live 0, no hold of its own):
# install_imaging_lock must not "hold off 0" and later "restore 0" — a 0 under a
# configured policy can only be an earlier hold's residue.
reset_run
write_txn applying 0 3
printf '0\n' > "$T/wdt.value"
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
( set -euo pipefail; install_imaging_lock ) >/dev/null 2>&1
[ "$(txn_field runtime_wdt_prev_usec)" = 15000000 ] && ok "W3 a fresh hold over a residue-0 manager persists the POLICY value, not 0" \
    || bad "W3 runtime_wdt_prev_usec='$(txn_field runtime_wdt_prev_usec)' (want 15000000) — a new apply would restore 0 at the end"
: > "$CALLS"
( set -euo pipefail; RUNTIME_WDT_PREV=""; load_runtime_wdt_prev; IMAGING_HELD=1; cleanup_imaging_lock ) >/dev/null 2>&1
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "W3 …and the end of that apply restores 15000000" \
    || bad "W3 the end of the apply left the watchdog at 0: $(grep busctl "$CALLS" | tr '\n' ';')"

# ── M: scripts/sa02m-update-remedy.sh — the field entry point for R8 ─────────
# Driven with a FAKE runner (records argv; carries the `cmd_reclaim() {` marker
# or not), a pgrep shim (rc from $T/pgrep.rc: 0 = a runner is alive), the
# busctl/systemctl shims above, and a real JSON transaction in the sandbox.
echo "── M: sa02m-update-remedy.sh ──"
REMEDY=scripts/sa02m-update-remedy.sh
FAKE_RUNNER="$TW/fake-runner"
cat > "$T/bin/pgrep" <<'SH'
#!/bin/bash
exit "$(cat "${PGREP_RC_FILE:-/dev/null}" 2>/dev/null || echo 1)"
SH
chmod 755 "$T/bin/pgrep"
export PGREP_RC_FILE="$T/pgrep.rc"
write_fake_runner() {  # $1 = with|without the reclaim marker
    {
        printf '#!/bin/bash\nprintf '"'"'runner %%s\\n'"'"' "$*" >> "$CALLS"\nexit 0\n'
        [ "$1" = with ] && printf 'cmd_reclaim() { :; }\n'
    } > "$FAKE_RUNNER"
    chmod 755 "$FAKE_RUNNER"
}
run_remedy() {
    : > "$CALLS"
    ( export SA02M_UPDATE_RUNNER="$FAKE_RUNNER" SA02M_UPDATE_STATEDIR="$STATEDIR" \
             SA02M_IMAGING_LOCK="$IMAGING_LOCK" SA02M_WEB_VERSION_FILE="$VERSION_FILE"
      bash "$REMEDY" ) > "$T/remedy.out" 2>&1; rc=$?
}
write_json_txn() { printf '{"schema_version":1,"id":"%s","stage":"%s","result":"pending"}\n' "$TXN" "$1" > "$STATEDIR/transaction.json"; }
# Ma live runner → exit 2, nothing touched
printf '0\n' > "$T/pgrep.rc"; write_fake_runner with; write_json_txn rolling_back
date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
run_remedy
[ "$rc" -eq 2 ] && [ -f "$IMAGING_LOCK" ] && ! grep -q '^runner ' "$CALLS" && ! called_re '^busctl set-property' \
    && ok "Ma a live runner → remedy refuses (rc 2), runner not invoked, lock and watchdog untouched" \
    || bad "Ma live runner → rc=$rc lock present: $([ -f "$IMAGING_LOCK" ] && echo yes || echo no) calls: $(tr '\n' ';' < "$CALLS")"
# Mb no runner, runner ≥ 1.0.6.52 → reclaim, watchdog from policy, lock gone, units started
printf '1\n' > "$T/pgrep.rc"
printf '[Manager]\nRuntimeWatchdogSec=15s\n' > "$SA02M_WATCHDOG_POLICY_FILE"
run_remedy
grep -q '^runner reclaim$' "$CALLS" && ok "Mb runner ≥ 1.0.6.52 → \`runner reclaim\` invoked" || bad "Mb reclaim not invoked: $(tr '\n' ';' < "$CALLS")"
called_re '^busctl set-property .* RuntimeWatchdogUSec t 15000000$' && ok "Mb watchdog set to the policy value explicitly" || bad "Mb watchdog not set: $(grep busctl "$CALLS" | tr '\n' ';')"
[ ! -f "$IMAGING_LOCK" ] && ok "Mb leftover imaging lock removed" || bad "Mb imaging lock still present"
called "systemctl start net-watchdog" && called "systemctl start sa02m-flasher" && ok "Mb net-watchdog + sa02m-flasher started" || bad "Mb units not started: $(grep start "$CALLS" | tr '\n' ';')"
grep -q '^after:' "$T/remedy.out" && ok "Mb prints the after: line" || bad "Mb no after: line: $(tail -3 "$T/remedy.out" | tr '\n' ';')"
# Mc old runner (no reclaim) on a stuck stage → recover at runtime
write_fake_runner without; write_json_txn rolling_back; date -Iseconds > "$IMAGING_LOCK"
run_remedy
grep -q '^runner recover$' "$CALLS" && ok "Mc runner < 1.0.6.52 on rolling_back → \`runner recover\` at runtime" || bad "Mc recover not invoked: $(tr '\n' ';' < "$CALLS")"
# Md old runner, terminal stage → no recover, still tidies the residue
write_json_txn done; date -Iseconds > "$IMAGING_LOCK"
run_remedy
! grep -q '^runner ' "$CALLS" && [ ! -f "$IMAGING_LOCK" ] && ok "Md old runner on a terminal stage → no recover, leftover lock still removed" \
    || bad "Md calls: $(tr '\n' ';' < "$CALLS") lock present: $([ -f "$IMAGING_LOCK" ] && echo yes || echo no)"
# Me reclaim refused (rc 3: a runner took the lock after the pgrep) → the remedy
# STOPS: no watchdog arming, no lock removal, no flasher start under a live runner
printf '#!/bin/bash\nprintf '"'"'runner %%s\\n'"'"' "$*" >> "$CALLS"\n[ "${1:-}" = reclaim ] && exit 3\nexit 0\ncmd_reclaim() { :; }\n' > "$FAKE_RUNNER"
write_json_txn rolling_back; date -Iseconds > "$IMAGING_LOCK"; printf '0\n' > "$T/wdt.value"
run_remedy
[ "$rc" -ne 0 ] && ok "Me reclaim refused (rc 3) → the remedy exits non-zero (rc=$rc)" || bad "Me remedy exited 0 after a refused reclaim"
[ -f "$IMAGING_LOCK" ] && ! called_re '^busctl set-property' && ! called "systemctl start sa02m-flasher" \
    && ok "Me the remedy stopped: lock kept, watchdog not armed, flasher not started" \
    || bad "Me the remedy went on after a refused reclaim: lock $([ -f "$IMAGING_LOCK" ] && echo kept || echo removed), calls: $(grep -E 'busctl set|start' "$CALLS" | tr '\n' ';')"
rm -f "$STATEDIR/transaction.json" "$T/bin/pgrep"

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

# ── R10: a TORN journal converges on ONE boot (round 6 — review R3-1) ────────
# The residue a power cut mid-apply leaves: stage applying, one file renamed
# and journalled, a torn last line. Until round 6 the replay died on json.loads
# of the torn line (it is read first — reverse order): recover exited non-zero
# at rolling_back with the lock kept, and every later boot repeated it.
# Expected: recover (boot) and reclaim (runtime) each finish in ONE run —
# rolled_back, E_POWER, the renamed file OLD again, the lock cleared.
echo "── R10: torn journal line — recover / reclaim converge in one run ──"
r10_setup() {
    mkdir -p "$STAGE/backups" "$TW/r10"
    printf 'r10 NEW\n' > "$TW/r10/f.conf"
    printf 'r10 OLD\n' > "$STAGE/backups/r10bak"
    printf '{"op": "replace", "dst": "%s/r10/f.conf", "backup": "%s/backups/r10bak", "mode": "0644", "owner": "root:root"}\n' "$TW" "$STAGE" \
        > "$STAGE/journal.jsonl"
    printf '{"op": "create", "dst": "%s/r10/g.co' "$TW" >> "$STAGE/journal.jsonl"
}
reset_run; r10_setup
write_txn applying 1 3
date -Iseconds > "$IMAGING_LOCK"
run_recover
if [ "$rc" -eq 0 ] && [ "$(txn_field stage)" = rolled_back ] && [ "$(txn_field error_code)" = E_POWER ] \
   && [ "$(cat "$TW/r10/f.conf")" = "r10 OLD" ] && [ ! -f "$IMAGING_LOCK" ]; then
    ok "R10a boot recover over a torn journal: rolled_back / E_POWER in one run, file restored, lock cleared"
else
    bad "R10a boot recover over a torn journal: rc=$rc stage=$(txn_field stage) code=$(txn_field error_code) f=$(cat "$TW/r10/f.conf") lock=$([ -f "$IMAGING_LOCK" ] && echo kept || echo cleared)"
fi
reset_run; r10_setup
write_txn rolling_back 1 3
date -Iseconds > "$IMAGING_LOCK"
run_reclaim
if [ "$rc" -eq 0 ] && [ "$(txn_field stage)" = rolled_back ] && [ "$(cat "$TW/r10/f.conf")" = "r10 OLD" ] && [ ! -f "$IMAGING_LOCK" ]; then
    ok "R10b runtime reclaim of a rollback residue with a torn journal: rolled_back in one run, file restored"
else
    bad "R10b reclaim over a torn journal: rc=$rc stage=$(txn_field stage) f=$(cat "$TW/r10/f.conf") lock=$([ -f "$IMAGING_LOCK" ] && echo kept || echo cleared)"
fi
rm -f "$STAGE/journal.jsonl" "$STAGE/backups/r10bak"

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
