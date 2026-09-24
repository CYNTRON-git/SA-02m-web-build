#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped escape_foreign_cgroup / acquire_lock in a sandbox (the systemd-run shim's recorded argv, the ESCAPED flag, the lock state, the log lines), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; it pins no source line of its own.
# test-update-cgroup-escape.sh — the update runner leaves fcgiwrap's cgroup
# before it does anything destructive. Quality row `update-cgroup-escape`.
# Field incident 2026-09-23 (Skolkovo; plan 1.0.6.52, F3 / root cause H3).
#
# Why: «Применить» in the panel runs `nohup sudo -n sa02m-web-update-apply &`
# from web_update_apply.cgi, i.e. from a fcgiwrap worker. sudo's PAM stack on
# the board carries no pam_systemd, so the launcher, the runner and every
# descendant stay in 0::/system.slice/fcgiwrap.service (measured on 1.135). The
# health gate's FIRST step after `stage=verifying` is `systemctl restart
# fcgiwrap`, and fcgiwrap.service has KillMode=mixed: once the main process
# exits, every remaining member of the cgroup is SIGKILLed — the runner
# included. No EXIT trap runs; transaction.json stays verifying/85, the
# imaging lock stays, the watchdog stays held, nginx keeps serving the new
# tree from disk. That is «прогресс-бар завис на 85 %» on six boards. The fix
# is runner-side, at cmd_apply entry (NOT effective in the update that delivers
# it: self_reexec_before_deploy execs a copy of the INSTALLED runner, so that
# first OTA runs under the old code and completes at the next boot via
# recover → verify — see docs/deployment.md): read /proc/self/cgroup,
# and when a line ends in /fcgiwrap.service re-launch this runner as a
# transient unit `sa02m-update-apply-<txn8>` via systemd-run (KillMode=process,
# the SA02M_* seams passed with --setenv), hand the lock over and exit 0. The
# offline path (sa02m-update.service) and an SSH launch never match.
#
# Method: extract the SHIPPED escape_foreign_cgroup + acquire_lock
# (extract-if-present for the escape: against a pre-fix runner the assertions
# go RED instead of aborting), stub log/txn_get, put a systemd-run shim first
# on PATH (records argv, rc from $T/run.rc), and drive the function with
# SA02M_UPDATE_CGROUP_FILE fixtures. Each case runs in a subshell. Where the
# host has no flock (git-bash) a no-op flock shim keeps acquire_lock runnable
# and the «lock held again» assertion of E5 is printed as SKIP — CI (Linux) is
# the authority for it.
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show 6ba943d:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-cgroup-escape.sh   → E1/E2/E5/E6/E7 RED (no
#   escape_foreign_cgroup: nothing is launched, nothing logged). PINNED ref.
#   RED observed 2026-09-23 on that tree: 17 FAIL — E1 «systemd-run NOT
#   invoked — the runner stays in fcgiwrap's cgroup» + every argv/flag/log
#   assertion, E2 «v1 form not recognised», E4/E6 «cgroup not logged», E5/E7
#   no WARN line; E3/E4/E5-rc/E6/E7-rc hold on both trees (nothing to escape
#   with, so nothing launched).
#
# Run: bash scripts/dev/test-update-cgroup-escape.sh   (bash + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${UPDATE_RUNNER_SRC:-etc/sa02m-update-runner.sh}"
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
TW=$T
if command -v cygpath >/dev/null 2>&1; then TW=$(cygpath -m "$T") || TW=$T; fi

cat "$SRC" > "$T/runner.sh" 2>/dev/null || { echo "FAIL  cannot read runner source: $SRC"; exit 1; }
SRC="$T/runner.sh"

fails=0
ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }

extract() {
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC"
}
: > "$T/fn.sh"
extract acquire_lock >> "$T/fn.sh"
grep -q '^acquire_lock() {' "$T/fn.sh" || { echo "FAIL  could not extract acquire_lock() from $SRC — the marker moved; fix this harness, do not delete it"; exit 1; }
# acquire_lock delegates to try_lock since round 2 (reclaim needs a non-exiting
# form); extract it when present.
grep -q '^try_lock() {' "$SRC" && extract try_lock >> "$T/fn.sh"
HAS_ESCAPE=0
if grep -q '^escape_foreign_cgroup() {' "$SRC"; then
    HAS_ESCAPE=1
    extract escape_foreign_cgroup >> "$T/fn.sh"
fi
[ "$(tail -n1 "$T/fn.sh")" = "}" ] || { echo "FAIL  extraction did not stop at a closing brace"; exit 1; }

# ── Globals + stubs ─────────────────────────────────────────────────────────
STATEDIR="$TW/state"
LOCKFILE="$STATEDIR/update.lock"
LOCK_HELD=0
ESCAPED=0
ESCAPE_UNIT=""
TXN="abcdef12-0000-4000-8000-000000000001"
LOG="$T/log"
CALLS="$T/calls.log"
export CALLS
mkdir -p "$STATEDIR"
log()     { printf '%s\n' "$*" >> "$LOG"; }
txn_get() { case "${1:-}" in id) printf '%s\n' "$TXN" ;; *) : ;; esac; }

mkdir -p "$T/bin"
cat > "$T/bin/systemd-run" <<'SH'
#!/bin/bash
printf 'systemd-run %s\n' "$*" >> "$CALLS"
exit "$(cat "${RUN_RC_FILE:-/dev/null}" 2>/dev/null || echo 0)"
SH
chmod 755 "$T/bin/systemd-run"
HAVE_FLOCK=1
if ! command -v flock >/dev/null 2>&1; then
    HAVE_FLOCK=0
    printf '#!/bin/bash\nexit 0\n' > "$T/bin/flock"; chmod 755 "$T/bin/flock"
fi
PATH="$T/bin:$PATH"
export RUN_RC_FILE="$T/run.rc"

# shellcheck disable=SC1090
. "$T/fn.sh"

SELF=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")

# run_case CGROUP-TEXT [extra env assignments...] → runs the escape in a
# subshell holding the lock (as cmd_apply does), records ESCAPED and whether
# the lock is still held by that shell afterwards.
run_case() {
    local cg="$1"; shift
    printf '%s\n' "$cg" > "$T/cgroup"
    : > "$CALLS"; : > "$LOG"; rm -f "$T/result"
    (
        set -euo pipefail
        export SA02M_UPDATE_CGROUP_FILE="$T/cgroup"
        for kv in "$@"; do export "${kv?}"; done
        # E7: make `command -v systemd-run` fail without touching PATH (a CI
        # runner has a real systemd-run — never let the harness reach it).
        if [ "${NO_SYSTEMD_RUN:-0}" = 1 ]; then
            command() { if [ "${1:-}" = -v ] && [ "${2:-}" = systemd-run ]; then return 1; fi; builtin command "$@"; }
        fi
        acquire_lock
        if [ "$HAS_ESCAPE" = 1 ]; then escape_foreign_cgroup "$TXN"; fi
        held=unknown
        if [ "$HAVE_FLOCK" = 1 ]; then
            if flock -n "$LOCKFILE" true 2>/dev/null; then held=0; else held=1; fi
        fi
        printf 'ESCAPED=%s\nHELD=%s\n' "${ESCAPED:-0}" "$held" > "$T/result"
    ) >/dev/null 2>&1
    case_rc=$?
}
result() { sed -n "s/^$1=//p" "$T/result" 2>/dev/null; }
launched() { grep -q '^systemd-run ' "$CALLS" 2>/dev/null; }
argv_has() { grep -qF -- "$1" "$CALLS" 2>/dev/null; }
logged()   { grep -qF -- "$1" "$LOG" 2>/dev/null; }
case_rc=0

# ── E1: cgroup v2, inside fcgiwrap.service → re-launch ──────────────────────
echo "── E1: cgroup v2 fcgiwrap ──"
printf '0\n' > "$T/run.rc"
run_case '0::/system.slice/fcgiwrap.service' SA02M_UPDATE_REEXEC=1
[ "$case_rc" -eq 0 ] && ok "E1 escape returns 0" || bad "E1 escape rc=$case_rc"
launched && ok "E1 systemd-run invoked" || bad "E1 systemd-run NOT invoked — the runner stays in fcgiwrap's cgroup (SIGKILL at the health gate)"
argv_has "--unit=sa02m-update-apply-abcdef12" && ok "E1 unit named after the transaction (sa02m-update-apply-<txn8>)" || bad "E1 unit name missing/wrong: $(cat "$CALLS")"
argv_has "--collect" && ok "E1 --collect (a failed transient unit does not linger)" || bad "E1 no --collect"
argv_has "-p KillMode=process" && ok "E1 KillMode=process on the transient unit" || bad "E1 no KillMode=process"
argv_has "--setenv=SA02M_UPDATE_ESCAPED=1" && ok "E1 loop guard SA02M_UPDATE_ESCAPED=1 passed" || bad "E1 no SA02M_UPDATE_ESCAPED=1 — the re-launched runner would escape again"
argv_has "--setenv=SA02M_UPDATE_STATEDIR=$STATEDIR" && ok "E1 STATEDIR seam passed" || bad "E1 STATEDIR not passed: $(cat "$CALLS")"
argv_has "--setenv=SA02M_UPDATE_REEXEC=1" && ok "E1 REEXEC state passed through (the post-exec copy resumes, not re-validates)" || bad "E1 SA02M_UPDATE_REEXEC not passed"
argv_has " $SELF apply" && ok "E1 argv ends in '<this runner> apply'" || bad "E1 argv does not end in '$SELF apply': $(cat "$CALLS")"
[ "$(result ESCAPED)" = 1 ] && ok "E1 ESCAPED=1 (the caller exits 0 and lets the unit run)" || bad "E1 ESCAPED=$(result ESCAPED)"
logged "runner cgroup: 0::/system.slice/fcgiwrap.service" && ok "E1 the cgroup is logged" || bad "E1 no 'runner cgroup:' log line"
logged "re-launching as transient unit sa02m-update-apply-abcdef12" && ok "E1 the handover is logged with its reason" || bad "E1 no re-launch log line"

# ── E2: cgroup v1 name=systemd form ─────────────────────────────────────────
echo "── E2: cgroup v1 ──"
run_case '1:name=systemd:/system.slice/fcgiwrap.service'
launched && [ "$(result ESCAPED)" = 1 ] && ok "E2 v1 hierarchy line also triggers the escape" || bad "E2 v1 form not recognised (launched: $(launched && echo yes || echo no), ESCAPED=$(result ESCAPED))"
argv_has "--setenv=SA02M_UPDATE_REEXEC=0" && ok "E2 REEXEC=0 passed when unset (a fresh launch validates first)" || bad "E2 REEXEC default not passed as 0"

# ── E3: the offline path (sa02m-update.service) → no escape ─────────────────
echo "── E3/E4: cgroups that must NOT escape ──"
run_case '0::/system.slice/sa02m-update.service'
launched && bad "E3 escaped from sa02m-update.service (the offline path is already KillMode=process)" || ok "E3 sa02m-update.service: no escape"
[ "$(result ESCAPED)" = 0 ] && ok "E3 ESCAPED=0" || bad "E3 ESCAPED=$(result ESCAPED)"
# E4 the transient unit itself (a second run of the same code)
run_case '0::/system.slice/sa02m-update-apply-abcdef12.service'
launched && bad "E4 escaped from its own transient unit (endless re-launch)" || ok "E4 sa02m-update-apply-<txn8>.service: no escape"
logged "runner cgroup: 0::/system.slice/sa02m-update-apply-abcdef12.service" && ok "E4 the cgroup is still logged (every launch, one line)" || bad "E4 cgroup not logged on the no-escape path"

# ── E5: systemd-run fails → continue in place, lock re-taken ────────────────
echo "── E5: systemd-run failure ──"
printf '1\n' > "$T/run.rc"
run_case '0::/system.slice/fcgiwrap.service'
[ "$case_rc" -eq 0 ] && ok "E5 a failed systemd-run does not abort the apply (rc 0)" || bad "E5 rc=$case_rc after a failed systemd-run"
[ "$(result ESCAPED)" = 0 ] && ok "E5 ESCAPED=0 — the runner continues in place" || bad "E5 ESCAPED=$(result ESCAPED) after a FAILED systemd-run"
logged "WARN: systemd-run failed" && ok "E5 the failure is logged as a WARN" || bad "E5 no 'WARN: systemd-run failed' log line"
if [ "$HAVE_FLOCK" = 1 ]; then
    [ "$(result HELD)" = 1 ] && ok "E5 the lock is held again after the failed handover" || bad "E5 lock NOT re-taken after the failed handover (HELD=$(result HELD))"
else
    skip "E5 lock re-hold assertion (no flock on this host — CI is the authority)"
fi
printf '0\n' > "$T/run.rc"

# ── E6: loop guard — already escaped ────────────────────────────────────────
echo "── E6/E7: guards ──"
run_case '0::/system.slice/fcgiwrap.service' SA02M_UPDATE_ESCAPED=1
launched && bad "E6 escaped although SA02M_UPDATE_ESCAPED=1 (endless re-launch)" || ok "E6 SA02M_UPDATE_ESCAPED=1: no second escape"
logged "runner cgroup:" && ok "E6 the cgroup is still logged" || bad "E6 cgroup not logged"
# E7 systemd-run missing → WARN, continue
run_case '0::/system.slice/fcgiwrap.service' NO_SYSTEMD_RUN=1
[ "$case_rc" -eq 0 ] && [ "$(result ESCAPED)" = 0 ] && ok "E7 no systemd-run on the box: continues in place (rc 0, ESCAPED=0)" || bad "E7 rc=$case_rc ESCAPED=$(result ESCAPED) without systemd-run"
logged "WARN: systemd-run missing" && ok "E7 the missing tool is logged" || bad "E7 no 'WARN: systemd-run missing' log line"

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
