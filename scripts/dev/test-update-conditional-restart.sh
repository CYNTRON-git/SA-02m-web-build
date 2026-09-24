#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (shim invocations recorded, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-update-conditional-restart.sh — regression for the conditional restart
# sets in etc/sa02m-update-runner.sh restart_services_and_health()
# (services.restart_if_active[] / services.restart_if_changed{}) and its
# _journal_has_dst_prefix() change gate. Quality row `update-conditional-restart`.
#
# Why: the 1.0.6.37 bench incident class — OTA/offline deployed new /opt/sa02m-*
# code but restarted NONE of the services holding it in memory (sa02m-rules,
# sa02m-alice-client, sa02m-cloud-control, sa02m-alice-config, sa02m-modbus-mqtt),
# so a cloud scenario push was processed by STALE code (trigger/end silently
# stripped) until the next reboot. The fix restarts them after apply — but the
# opt-in units (Alice family ships `app off`, scripts/06-alice.sh) must be
# restarted ONLY when active: `systemctl restart` on an inactive unit STARTS it,
# which would widen an operator's OFF on every OTA. And the RS-485 bridge must
# additionally be change-gated: a www-only patch must not bounce industrial
# polling (port-lease invariant).
#
# Method: extract the SHIPPED _systemctl_bounded / _journal_has_dst_prefix /
# restart_services_and_health (literal-prefix awk slices, portable across
# mawk/gawk — see test-update-deploy-skip.sh for the rationale), stub only
# log/manifest_path, and shim systemctl/nginx/systemd-tmpfiles as PATH
# EXECUTABLES (`timeout systemctl ...` inside _systemctl_bounded execs a PATH
# lookup, so a shell function would never be seen). The systemctl shim records
# every argv to a calls log and answers `is-active` from a file-backed unit set.
# Nothing touches the real systemd/filesystem, no root, no device.
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show 1295837~1:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-conditional-restart.sh   → the conditional
#   restart assertions go RED (the pre-fix runner has no restart_if_active /
#   restart_if_changed loops and no sa02m-rules in the manifest restart[] the
#   fixture mirrors, so the expected shim calls never happen). PINNED ref, not
#   `main`: once 1.0.6.39 merges, main carries the loops and a `main` recipe
#   would silently stop going RED (audit 2026-09-08, D13). 1295837 is the
#   commit that added the loops; its parent is the last runner without them.
#   The pre-fix runner defines no _journal_has_dst_prefix, so this harness
#   extracts it only when present (see HAS_GATE) — the drive-to-failure run
#   must reach and FAIL the assertions, not abort on a missing marker. Run 5
#   (rollback re-runs the restart sets, 1.0.6.39) goes RED the same way against
#   any pre-1.0.6.39 runner: rollback_from_journal is extracted from whatever
#   source is under test, and without restart_after_rollback it bounces nothing.
#
# Windows dev box (git-bash) — TWO environment classes, both normalised by the
# HARNESS, never in the shipped runner (on the board, Linux CPython, neither
# exists). Both are recorded in .ai-dev/notes/quality-gate-environment.md
# (audit 2026-09-08, D4).
# (1) CRLF: the runner feeds its loops from `python3 -c print(u)` via process
#     substitution, and Windows CPython writes text-mode stdout as CRLF, so
#     `read -r u` keeps a trailing \r in the unit name and
#     `IFS=$'\t' read -r u prefix` puts it on the prefix — is-active then
#     never matches. A `python3` PATH shim strips \r from python's stdout
#     (exit status preserved via PIPESTATUS), and the systemctl shim strips a
#     trailing \r from every argv word before recording/answering, so a stray
#     CR can neither hide a call from `called` nor defeat is-active.
# (2) MSYS argv path conversion: an argument that looks like a POSIX path is
#     rewritten when a NATIVE executable is spawned, so the change gate's
#     `/opt/sa02m-modbus-mqtt/` prefix reached python.exe as
#     `C:/Program Files/Git/opt/sa02m-modbus-mqtt/` and startswith() was
#     always False — the failure the audit attributed to CRLF alone survives
#     the CR fix (proven: MSYS_NO_PATHCONV=1 python3 -c 'print(sys.argv[1])'
#     /opt/x/ → '/opt/x/', without it → 'C:/Program Files/Git/opt/x/').
#     The harness exports MSYS_NO_PATHCONV=1 and, since that also stops the
#     conversion python NEEDS for real files, hands python every sandbox path
#     in mixed form (`cygpath -m`, TW below) while PATH keeps the MSYS form
#     (a C:/ PATH entry is not searched). cygpath exists only on MSYS; on
#     Linux TW == T and the export is inert.
#
# Run 6 (1.0.6.52): the health gate's settle window (two consecutive `active`
#   samples, `activating` waits), the Condition-off skip and the status
#   excerpt — the systemctl shim answers `is-active` from a per-unit state
#   SEQUENCE file and `is-enabled` / `show -p ConditionResult` from per-unit
#   answer files (see SHIM_STATE). Against a pre-split runner it runs the
#   monolithic restart_services_and_health and goes RED on 6a/6b/6c/6e/6g.
#
# Round 4 (1.0.6.54): every run calls the function under test the way the
# runner does — `if ! fn` inside a set -e subshell, errexit suspended — so the
# harness cannot be stricter than production. Run 7: the python3 shim fails the
# one manifest read whose argv contains PY_FAIL_MATCH; each dead read (health
# facts, the three restart sets, enable[]) must FAIL its step with a
# `manifest read failed` reason, a dead daemon_reload read must still reload,
# and a key absent by design must still skip. RED on 35f4e8d (2026-09-24,
# WSL): 12 FAIL — every dead read left an empty list and returned 0.
# Round 5: run 3b (a present journal whose read dies counts as CHANGED, with
# a WARN) and run 5c (restart_after_rollback: a dead set read WARNs by name,
# the rollback stays 0, the other sets still run); the log stub records to
# $RUNNER_LOG for these. RED on 2b3224c: 3 FAIL.
#
# Run: bash scripts/dev/test-update-conditional-restart.sh   (bash + python3 +
#   coreutils; no systemd — the shims replace it).
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${UPDATE_RUNNER_SRC:-etc/sa02m-update-runner.sh}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable (runner requires it)"; exit 0; }
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# TW: the sandbox root in the form python receives (header, class 2).
TW=$T
if command -v cygpath >/dev/null 2>&1; then
    TW=$(cygpath -m "$T") || TW=$T
    export MSYS_NO_PATHCONV=1
fi

# Materialise SRC to a regular file ONCE, then read only the copy — under
# drive-to-failure UPDATE_RUNNER_SRC is a non-seekable process substitution
# that yields data on the FIRST read only (same trap as test-update-deploy-skip).
cat "$SRC" > "$T/runner.sh" 2>/dev/null || { echo "FAIL  cannot read runner source: $SRC"; exit 1; }
SRC="$T/runner.sh"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract each shipped function (start marker → first column-0 close) ──────
# LITERAL prefix match (index==1), not a string-built regex — mawk reads the
# escaped parens of a dynamic regex as a group and the extraction silently
# yields zero lines (see test-update-deploy-skip.sh).
extract() {
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC"
}

# _journal_has_dst_prefix is the gate THIS change adds; the pre-fix runner has
# none. Extract it only when the source defines it, so the drive-to-failure run
# still RUNS the assertions (the missing loops make them RED) instead of
# aborting on a missing marker.
HAS_GATE=0
grep -q '^_journal_has_dst_prefix() {' "$SRC" && HAS_GATE=1
# restart_after_rollback is the 1.0.6.39 addition (D9); same extract-if-present
# rule so the drive-to-failure run reaches run 5 and FAILS it.
HAS_RB_RESTART=0
grep -q '^restart_after_rollback() {' "$SRC" && HAS_RB_RESTART=1

funcs="_systemctl_bounded restart_services_and_health rollback_from_journal"
[ "$HAS_GATE" = "1" ] && funcs="_systemctl_bounded _journal_has_dst_prefix restart_services_and_health rollback_from_journal"
[ "$HAS_RB_RESTART" = "1" ] && funcs="$funcs restart_after_rollback"
# 1.0.6.52: restart_services_and_health is the composition of three named
# halves (services_enable_and_tmpfiles / services_restart_sets / health_check)
# plus the per-unit settle probe unit_settled. Extract-if-present, so a
# pre-split runner still RUNS run 6 against its monolithic function and FAILS
# the settle / Condition assertions there (RED), instead of aborting here.
HAS_HEALTH_SPLIT=0
grep -q '^health_check() {' "$SRC" && HAS_HEALTH_SPLIT=1
for fn in unit_settled health_check services_enable_and_tmpfiles services_restart_sets; do
    grep -q "^$fn() {" "$SRC" && funcs="$funcs $fn"
done
: > "$T/fn.sh"
for fn in $funcs; do
    extract "$fn" >> "$T/fn.sh"
    grep -q "^$fn() {" "$T/fn.sh" \
        || { echo "FAIL  could not extract $fn() from $SRC — the marker moved; fix this harness, do not delete it"; exit 1; }
done
# When the source HAS the gate, the extracted health function MUST reference
# both conditional sets — else the extraction range silently dropped them and
# every assertion below is vacuous.
if [ "$HAS_GATE" = "1" ]; then
    grep -q 'restart_if_active' "$T/fn.sh" \
        || { echo "FAIL  extracted restart_services_and_health has no restart_if_active loop — extraction range broke"; exit 1; }
    grep -q 'restart_if_changed' "$T/fn.sh" \
        || { echo "FAIL  extracted restart_services_and_health has no restart_if_changed loop — extraction range broke"; exit 1; }
fi

# ── Stubs + shims ───────────────────────────────────────────────────────────
STATEDIR="$TW/state"
TXN="TXN"
STAGE="$STATEDIR/staging/$TXN"
mkdir -p "$STAGE/meta"
CALLS_LOG="$T/calls.log"
ACTIVE_FILE="$T/active.units"
# Per-unit shim state (run 6): seq.<unit> = one is-active state per line,
# consumed per call, last line repeats; enabled.<unit> = the is-enabled answer;
# cond.<unit> = the ConditionResult answer. Absent ⇒ the run 1–5 defaults.
SHIM_STATE="$T"
export CALLS_LOG ACTIVE_FILE SHIM_STATE

# Recorded (round 5): runs 3b / 5c assert the WARN a failed read must leave.
RUNNER_LOG="$T/runner.log"; : > "$RUNNER_LOG"
log()                  { printf '%s\n' "$*" >> "$RUNNER_LOG"; }
manifest_path()        { printf '%s\n' "$STAGE/meta/manifest.json"; }
# rollback_from_journal's collaborators (run 5): txn state is irrelevant here.
txn_patch()            { :; }
txn_get()              { :; }
cleanup_imaging_lock() { :; }
utc_now()              { echo 1970-01-01T00:00:00Z; }

# PATH shims. systemctl: record argv; is-active answers from $ACTIVE_FILE;
# is-enabled says enabled; every other verb succeeds. nginx/systemd-tmpfiles:
# succeed silently (the health path runs `nginx -t` unconditionally).
# python3: the CRLF normaliser (header) — resolved BEFORE the PATH prepend so
# the shim forwards to the real interpreter, never to itself.
mkdir -p "$T/bin"
REAL_PY=$(command -v python3)
cat > "$T/bin/python3" <<SH
#!/bin/bash
# Harness shim: strip CR from CPython's text-mode stdout (Windows emits CRLF),
# keeping python's own exit status — _journal_has_dst_prefix reads it.
# Run 7: a call whose argv contains \$PY_FAIL_MATCH fails (exit 1) instead —
# a manifest read that dies (traceback, ENOMEM, unreadable file).
if [ -n "\${PY_FAIL_MATCH:-}" ]; then
    for a in "\$@"; do
        case "\$a" in *"\$PY_FAIL_MATCH"*) echo "shim: injected python failure" >&2; exit 1 ;; esac
    done
fi
"$REAL_PY" "\$@" | tr -d '\r'
exit "\${PIPESTATUS[0]}"
SH
cat > "$T/bin/systemctl" <<'SH'
#!/bin/bash
# Strip a trailing CR from every word (CRLF class — header) before recording
# or answering, so a stray \r neither hides a call nor defeats is-active.
args=()
for a in "$@"; do args+=("${a%$'\r'}"); done
set -- "${args[@]}"
printf 'systemctl %s\n' "$*" >> "$CALLS_LOG"
cmd=${1:-}; [ $# -gt 0 ] && shift
case "$cmd" in
    is-active)
        quiet=0; [ "${1:-}" = "--quiet" ] && { quiet=1; shift; }
        u=${1:-}
        seqf="$SHIM_STATE/seq.$u"
        if [ -f "$seqf" ]; then
            # A state SEQUENCE: line n on the n-th call, the last line forever.
            n=$(cat "$seqf.n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$seqf.n"
            total=$(grep -c . "$seqf"); [ "$n" -gt "$total" ] && n=$total
            state=$(sed -n "${n}p" "$seqf")
        elif grep -qxF "$u" "$ACTIVE_FILE" 2>/dev/null; then
            state=active
        else
            state=inactive
        fi
        [ "$quiet" = 1 ] || echo "$state"
        [ "$state" = active ]
        ;;
    is-enabled)
        u=${1:-}
        if [ -f "$SHIM_STATE/enabled.$u" ]; then cat "$SHIM_STATE/enabled.$u"; else echo enabled; fi
        ;;
    show)
        # systemctl show -p <Property> --value <unit>; answered per property:
        # ConditionResult from cond.<unit> (default yes), ConditionTimestampMonotonic
        # from condts.<unit> (default 0 — never evaluated this boot, as systemd
        # reports for a unit whose start was never attempted).
        u=""; for a in "$@"; do u=$a; done
        prop=""; for a in "$@"; do case "$a" in -p) ;; --value) ;; *) [ -z "$prop" ] && prop=$a ;; esac; done
        case "$prop" in
            ConditionResult) if [ -f "$SHIM_STATE/cond.$u" ]; then cat "$SHIM_STATE/cond.$u"; else echo yes; fi ;;
            ConditionTimestampMonotonic) if [ -f "$SHIM_STATE/condts.$u" ]; then cat "$SHIM_STATE/condts.$u"; else echo 0; fi ;;
            *) echo "" ;;
        esac
        ;;
    status)
        echo "● ${1:-}: shim status excerpt"
        ;;
    *)
        exit 0
        ;;
esac
SH
cat > "$T/bin/nginx" <<'SH'
#!/bin/bash
exit 0
SH
cat > "$T/bin/systemd-tmpfiles" <<'SH'
#!/bin/bash
exit 0
SH
chmod 755 "$T/bin/python3" "$T/bin/systemctl" "$T/bin/nginx" "$T/bin/systemd-tmpfiles"
PATH="$T/bin:$PATH"

# shellcheck disable=SC1090
. "$T/fn.sh"

# ── Fixtures ────────────────────────────────────────────────────────────────
# VERSION gate inside the health check: manifest version must equal the file.
printf '9.9.9.9\n' > "$TW/VERSION"

write_manifest() {  # $1 = with|without conditional keys
    if [ "$1" = "with" ]; then
        cat > "$STAGE/meta/manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "9.9.9.9",
  "deploy": [],
  "services": {
    "daemon_reload": true,
    "stop_before_apply": [],
    "enable": [],
    "restart": ["sa02m-rules"],
    "restart_if_active": ["sa02m-alice-client", "sa02m-cloud-control"],
    "restart_if_changed": {"sa02m-modbus-mqtt": "/opt/sa02m-modbus-mqtt/"},
    "health": {"http_url": "", "units_active": [], "version_file": "$TW/VERSION"}
  }
}
JSON
    else
        cat > "$STAGE/meta/manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "9.9.9.9",
  "deploy": [],
  "services": {
    "daemon_reload": true,
    "stop_before_apply": [],
    "restart": ["sa02m-rules"],
    "health": {"http_url": "", "units_active": [], "version_file": "$TW/VERSION"}
  }
}
JSON
    fi
}

# Active set: alice-client and the bridge RUN; cloud-control is OFF (opt-in).
printf '%s\n' sa02m-alice-client sa02m-modbus-mqtt > "$ACTIVE_FILE"

called()   { grep -qxF "systemctl $1" "$CALLS_LOG" 2>/dev/null; }
run_rc=0
# cmd_apply calls `if ! restart_services_and_health` and cmd_verify `if !
# health_check`: bash suspends errexit for the whole body of a function tested
# that way, so only an EXPLICIT status check fails the gate. Every run goes
# through that shape (round 4) — the harness must never be stricter than the
# runner (`set -e` + last command would abort on an unchecked failure that
# production silently continues past).
run_health() { : > "$CALLS_LOG"; ( set -euo pipefail; if ! restart_services_and_health "$TXN"; then exit 1; fi ) >/dev/null 2>&1; run_rc=$?; }

# ── Run 1: full manifest, bridge prefix NOT in the apply journal ────────────
write_manifest with
printf '%s\n' '{"op":"replace","dst":"/opt/sa02m-rules/sa02m_rules/engine.py","backup":"/x","mode":"0644","owner":"root:root"}' \
    > "$STAGE/journal.jsonl"
run_health
[ "$run_rc" -eq 0 ] && ok "run1: health returns 0" \
                    || bad "run1: health FAILED (rc=$run_rc)"
called "restart sa02m-rules" \
    && ok "run1: restart[] hard-restarts sa02m-rules (no is-active gate — core unit)" \
    || bad "run1: sa02m-rules NOT restarted — the stale-engine regression"
called "is-active --quiet sa02m-alice-client" \
    && ok "run1: restart_if_active probes is-active (gate consulted)" \
    || bad "run1: restart_if_active never probed is-active — loop missing or unconditional"
called "restart sa02m-alice-client" \
    && ok "run1: ACTIVE opt-in unit (alice-client) restarted on fresh code" \
    || bad "run1: active alice-client NOT restarted — the 1.0.6.37 stale-code class"
if called "restart sa02m-cloud-control" || called "start sa02m-cloud-control"; then
    bad "run1: INACTIVE opt-in unit (cloud-control) was restarted/started — never-widen violated"
else
    ok "run1: inactive opt-in unit (cloud-control) left alone (never-widen)"
fi
if called "restart sa02m-modbus-mqtt" || called "start sa02m-modbus-mqtt"; then
    bad "run1: bridge restarted although /opt/sa02m-modbus-mqtt/ unchanged — port-lease bounce"
else
    ok "run1: bridge NOT restarted when its /opt prefix is unchanged (change gate)"
fi

# ── Run 2: bridge prefix PRESENT in the apply journal ───────────────────────
printf '%s\n' '{"op":"replace","dst":"/opt/sa02m-modbus-mqtt/bridge_mqtt.py","backup":"/x","mode":"0755","owner":"root:root"}' \
    > "$STAGE/journal.jsonl"
run_health
called "restart sa02m-modbus-mqtt" \
    && ok "run2: ACTIVE bridge restarted when its package changed" \
    || bad "run2: bridge NOT restarted despite a changed /opt/sa02m-modbus-mqtt/ — stale bridge"

# ── Run 3: journal ABSENT (power-loss recover, tmpfs staging) → changed ─────
rm -f "$STAGE/journal.jsonl"
run_health
called "restart sa02m-modbus-mqtt" \
    && ok "run3: missing journal treated as CHANGED (recover fails toward freshness)" \
    || bad "run3: missing journal treated as unchanged — recover leaves stale code"

# ── Run 3b: journal PRESENT but its read DIES → changed (round 5) ───────────
# _journal_has_dst_prefix answered "not found" with exit 1 — the same status a
# crashed python (traceback, OOM-kill handled as 1 by the shim) returns, so a
# failed journal read read as "bridge unchanged" and a changed bridge kept its
# stale code. The journal here names only the rules engine; the shim kills the
# read. Expected: treated as CHANGED (the missing-journal rule of run 3 — fail
# toward freshness), the bridge restarted, a WARN naming the failed read.
printf '%s\n' '{"op":"replace","dst":"/opt/sa02m-rules/sa02m_rules/engine.py","backup":"/x","mode":"0644","owner":"root:root"}' \
    > "$STAGE/journal.jsonl"
: > "$RUNNER_LOG"
export PY_FAIL_MATCH=journal.jsonl
run_health
unset PY_FAIL_MATCH
called "restart sa02m-modbus-mqtt" \
    && ok "run3b: a dead journal read is treated as CHANGED (bridge restarted)" \
    || bad "run3b: a dead journal read read as 'unchanged' — the changed-bridge gate fails toward stale code"
grep -qF 'WARN: journal read failed' "$RUNNER_LOG" \
    && ok "run3b: the failed journal read is logged (WARN)" \
    || bad "run3b: no WARN for the failed journal read"
rm -f "$STAGE/journal.jsonl"

# ── Run 4: legacy manifest WITHOUT the conditional keys → clean no-op ───────
write_manifest without
run_health
[ "$run_rc" -eq 0 ] && ok "run4: legacy manifest (no restart_if_* keys) applies cleanly" \
                    || bad "run4: legacy manifest broke the runner (rc=$run_rc) — backward compat"
called "restart sa02m-rules" \
    && ok "run4: restart[] still honoured for the legacy manifest" \
    || bad "run4: restart[] broke for the legacy manifest"
if called "restart sa02m-alice-client"; then
    bad "run4: conditional restart fired without any restart_if_active key"
else
    ok "run4: no conditional restarts without the keys (no phantom widening)"
fi

# ── Run 5: rollback re-runs daemon-reload + the restart sets (D9) ──────────
# After an E_HEALTH rollback the files are old again but every unit the apply
# path bounced still runs the NEW code from memory. The rollback must bounce
# them once more, with the same never-widen / change-gate discipline; a lost
# manifest (recover) is logged, never fatal. Journal: the rules engine AND the
# bridge were replaced (backup paths do not exist ⇒ the restore itself is a
# sandbox no-op), so the bridge's change gate says CHANGED.
write_manifest with
printf '%s\n' \
    '{"op":"replace","dst":"/opt/sa02m-rules/sa02m_rules/engine.py","backup":"/x","mode":"0644","owner":"root:root"}' \
    '{"op":"replace","dst":"/opt/sa02m-modbus-mqtt/bridge_mqtt.py","backup":"/x","mode":"0755","owner":"root:root"}' \
    > "$STAGE/journal.jsonl"
: > "$CALLS_LOG"
( set -euo pipefail; rollback_from_journal "$TXN" ) >/dev/null 2>&1; rb_rc=$?
[ "$rb_rc" -eq 0 ] && ok "run5: rollback returns 0" || bad "run5: rollback FAILED (rc=$rb_rc)"
called "daemon-reload" \
    && ok "run5: rollback re-runs daemon-reload on the restored unit files" \
    || bad "run5: no daemon-reload after rollback — restored units run stale definitions"
called "restart sa02m-rules" \
    && ok "run5: rollback re-bounces restart[] (sa02m-rules back on the restored engine)" \
    || bad "run5: sa02m-rules NOT restarted after rollback — new code stays in memory over old files (D9)"
called "restart sa02m-alice-client" \
    && ok "run5: ACTIVE opt-in unit (alice-client) re-bounced after rollback" \
    || bad "run5: active alice-client NOT restarted after rollback"
if called "restart sa02m-cloud-control" || called "start sa02m-cloud-control"; then
    bad "run5: INACTIVE opt-in unit (cloud-control) started by the rollback — never-widen violated"
else
    ok "run5: inactive opt-in unit left alone by the rollback (never-widen)"
fi
called "restart sa02m-modbus-mqtt" \
    && ok "run5: bridge re-bounced after rollback because its /opt prefix was in the journal" \
    || bad "run5: bridge NOT restarted after rollback although the journal shows its package changed"
# Run 5b: rollback with the manifest gone (lost staging) ⇒ still 0, nothing bounced.
rm -f "$STAGE/meta/manifest.json"; : > "$CALLS_LOG"
( set -euo pipefail; rollback_from_journal "$TXN" ) >/dev/null 2>&1; rb_rc=$?
if [ "$rb_rc" -eq 0 ] && ! grep -q 'restart' "$CALLS_LOG" 2>/dev/null; then
    ok "run5b: rollback without a manifest returns 0 and restarts nothing (recover path stays soft)"
else
    bad "run5b: rollback without a manifest rc=$rb_rc calls: $(tr '\n' ';' < "$CALLS_LOG")"
fi

# ── Run 5c: a dead read in the rollback's restart sets → WARN, continue ─────
# restart_after_rollback fed its three sets from `done < <(python3 … || true)`:
# a read that died bounced nothing and said nothing. It must stay soft (a
# rollback that restored the files never fails on a restart) but WARN, naming
# the set, and still run the sets it could read. The shim kills only the
# restart_if_active read.
write_manifest with
printf '%s\n' '{"op":"replace","dst":"/opt/sa02m-rules/sa02m_rules/engine.py","backup":"/x","mode":"0644","owner":"root:root"}' \
    > "$STAGE/journal.jsonl"
: > "$CALLS_LOG"; : > "$RUNNER_LOG"
( set -euo pipefail; export PY_FAIL_MATCH=restart_if_active; rollback_from_journal "$TXN" ) >/dev/null 2>&1; rb_rc=$?
[ "$rb_rc" -eq 0 ] && ok "run5c: rollback with a dead restart_if_active read still returns 0" \
                   || bad "run5c: rollback FAILED on a dead read (rc=$rb_rc) — a rollback must stay soft"
grep -qF 'WARN: rollback: manifest read failed (restart_if_active)' "$RUNNER_LOG" \
    && ok "run5c: the dead read is logged, naming the set (restart_if_active)" \
    || bad "run5c: no WARN naming restart_if_active — the set was silently skipped"
called "restart sa02m-rules" \
    && ok "run5c: the sets that COULD be read still ran (sa02m-rules restarted)" \
    || bad "run5c: restart[] skipped because another set's read died"
rm -f "$STAGE/journal.jsonl"

# ── Run 6: the health gate settles, tolerates Condition-off, names the state ─
# Field incident 2026-09-23 (1.0.6.52, F1): one `is-active` probe immediately
# after `restart` (Type=simple returns at exec) read a still-`activating`
# sa02m-devices-api as DOWN and rolled a good update back (1.135 sample
# 2026-08-20); a unit kept off by its own `Condition*=` (the stand drop-in) was
# never recognised as operator-configured. The gate must: wait for two
# consecutive `active` samples within the settle window (a Restart=always crash
# loop alternates activating↔active and must NOT pass on one sample); skip a
# masked/disabled unit (the existing skip, functional at last — backlog
# 2026-08-20) AND a unit whose ConditionResult is `no`; and log the last state
# plus a `systemctl status` excerpt on a real failure.
# RED on the pre-split runner (6ba943d), observed 2026-09-23: 7 FAIL — 6a (one
# probe → rc 1, 1 probe recorded), 6b (no status excerpt), 6c (a single active
# sample PASSED the crash loop), 6e (no Condition skip, ConditionResult never
# read), 6g (ConditionTimestampMonotonic never read; its rc 1 holds there by
# accident — the old gate failed every inactive unit). 6d/6f hold on both
# trees. 6g against the FIRST fixed tree (ConditionResult alone): «a
# never-started enabled required unit was waved through as Condition-off
# (rc=0)» — review 1.0.6.52, finding 2.
echo "── run 6: health gate settle window / Condition-off skip ──"
HEALTH_FN=restart_services_and_health
[ "$HAS_HEALTH_SPLIT" = "1" ] && HEALTH_FN=health_check
# 6 s window at a 1 s step: 6a needs four samples (~3 s) and must not sit on the
# deadline — under the quality runner other rows load the box and SECONDS is
# whole-second granular, so a 3 s window failed 6a there while passing alone.
export SA02M_UPDATE_HEALTH_SETTLE_SEC=6 SA02M_UPDATE_HEALTH_SETTLE_STEP=1
write_units_manifest() {  # $1 = unit name for units_active
    cat > "$STAGE/meta/manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "9.9.9.9",
  "deploy": [],
  "services": {
    "daemon_reload": false,
    "restart": [],
    "health": {"http_url": "", "units_active": ["$1"], "version_file": "$TW/VERSION"}
  }
}
JSON
}
run_units_health() {  # $1 = unit
    write_units_manifest "$1"
    rm -f "$SHIM_STATE"/seq.*.n
    : > "$CALLS_LOG"
    ( set -euo pipefail; if ! "$HEALTH_FN" "$TXN"; then exit 1; fi ) >/dev/null 2>&1; run_rc=$?
}
n_probes() { grep -c "^systemctl is-active \(--quiet \)\?$1\$" "$CALLS_LOG" 2>/dev/null || true; }

# 6a slow unit: activating, activating, active, active → passes after settling
printf '%s\n' activating activating active active > "$SHIM_STATE/seq.u-slow"
run_units_health u-slow
[ "$run_rc" -eq 0 ] && ok "6a still-activating unit settles to active → health rc 0" \
                    || bad "6a a unit still activating right after restart FAILED the gate (rc=$run_rc) — the 2026-08-20 rollback class"
[ "$(n_probes u-slow)" -ge 3 ] && ok "6a the gate re-sampled is-active ($(n_probes u-slow) probes)" \
                               || bad "6a only $(n_probes u-slow) is-active probe(s) — no settle window"
# 6b never leaves activating → rc 1 with a status excerpt logged
printf '%s\n' activating > "$SHIM_STATE/seq.u-slow"
run_units_health u-slow
[ "$run_rc" -ne 0 ] && ok "6b a unit stuck in activating still fails the gate (rc=$run_rc)" \
                    || bad "6b a unit that never becomes active PASSED the gate"
called "status --no-pager -n 5 u-slow" \
    && ok "6b the failure logs a systemctl status excerpt of the unit" \
    || bad "6b no 'systemctl status --no-pager -n 5 u-slow' call — the field log names no cause"
# 6c a Restart=always crash loop: active/inactive alternating → never two in a row
printf '%s\n' active inactive active inactive active inactive active inactive > "$SHIM_STATE/seq.u-flap"
run_units_health u-flap
[ "$run_rc" -ne 0 ] && ok "6c a flapping unit (active,inactive,…) does not pass on one active sample" \
                    || bad "6c a crash-looping unit PASSED the gate on a single active sample"
# 6d operator-disabled: inactive + is-enabled=disabled → skipped, rc 0
printf '%s\n' inactive > "$SHIM_STATE/seq.u-off"
printf 'disabled\n' > "$SHIM_STATE/enabled.u-off"
run_units_health u-off
[ "$run_rc" -eq 0 ] && ok "6d an operator-disabled (is-enabled=disabled) required unit is skipped, not a failure" \
                    || bad "6d disabled unit rolled the update back (rc=$run_rc) — never-widen violated"
# 6e Condition-off: inactive, enabled, ConditionResult=no AND the condition was
# really evaluated this boot (ConditionTimestampMonotonic != 0) → skipped, rc 0
printf '%s\n' inactive > "$SHIM_STATE/seq.u-cond"
printf 'no\n' > "$SHIM_STATE/cond.u-cond"
printf '4823917\n' > "$SHIM_STATE/condts.u-cond"
run_units_health u-cond
[ "$run_rc" -eq 0 ] && ok "6e a unit kept off by its own Condition*= (ConditionResult=no, evaluated) is skipped" \
                    || bad "6e a Condition-off unit (the 1.135 stand drop-in shape) FAILED the gate (rc=$run_rc)"
called "show -p ConditionResult --value u-cond" \
    && ok "6e the gate consulted ConditionResult" \
    || bad "6e ConditionResult was never read"
# 6g the over-broad twin (review 1.0.6.52, finding 2): systemd reports
# ConditionResult=no for a unit whose start was NEVER attempted this boot
# (condition_result is false until unit_test_condition runs), with
# ConditionTimestampMonotonic=0. An enabled required unit in that state is a
# regression (its start job was cancelled/never queued), not an operator
# choice — it must FAIL the gate.
printf '%s\n' inactive > "$SHIM_STATE/seq.u-never"
printf 'no\n' > "$SHIM_STATE/cond.u-never"
printf '0\n' > "$SHIM_STATE/condts.u-never"
run_units_health u-never
[ "$run_rc" -ne 0 ] && ok "6g a never-started enabled unit (ConditionResult=no, ConditionTimestampMonotonic=0) still FAILS the gate" \
                    || bad "6g a never-started enabled required unit was waved through as Condition-off (rc=$run_rc) — the skip is over-broad"
called "show -p ConditionTimestampMonotonic --value u-never" \
    && ok "6g the gate consulted ConditionTimestampMonotonic" \
    || bad "6g ConditionTimestampMonotonic was never read — evaluated-false and never-evaluated are indistinguishable"
# 6f enabled, no Condition, inactive → still fails (the fail path is preserved)
printf '%s\n' inactive > "$SHIM_STATE/seq.u-down"
run_units_health u-down
[ "$run_rc" -ne 0 ] && ok "6f an enabled unit that is simply down still fails the gate" \
                    || bad "6f an enabled-but-down unit PASSED — the gate no longer catches a real regression"
unset SA02M_UPDATE_HEALTH_SETTLE_SEC SA02M_UPDATE_HEALTH_SETTLE_STEP

# ── Run 7: a failed manifest READ fails the step; an absent key still skips ──
# Round 4 of review 1.0.6.54. Every list/value the health path reads from the
# manifest came from `$(python3 …)` or `done < <(python3 …)`: the first is
# unchecked under cmd_apply's `if !`, the second is never observable at all, so
# a read that died left an EMPTY value and the step went on as if the manifest
# had asked for nothing — no units checked, no HTTP probe, no version check, no
# restarts, no enables, and the update committed. A failed read must now FAIL
# the step with HEALTH_FAIL_REASON naming the read; a key ABSENT by design
# (http_url "", no units_active, the fixtures above) still skips, and a failed
# `daemon_reload` read does the reload (fail safe) rather than skip it.
# The python3 shim fails the one call whose argv contains PY_FAIL_MATCH.
echo "── run 7: failed manifest reads fail the step ──"
REASON_F="$T/reason"
run7() {  # <fn> <match> → run_rc, $REASON_F
    : > "$CALLS_LOG"; : > "$REASON_F"
    ( set -euo pipefail; export PY_FAIL_MATCH=$2
      if ! "$1" "$TXN"; then printf '%s' "${HEALTH_FAIL_REASON:-}" > "$REASON_F"; exit 1; fi ) >/dev/null 2>&1
    run_rc=$?
}
reason_has() { grep -qF "$1" "$REASON_F" 2>/dev/null; }
if [ "$HAS_HEALTH_SPLIT" = "1" ]; then
    write_units_manifest nginx; printf '%s\n' active > "$SHIM_STATE/seq.nginx"; rm -f "$SHIM_STATE"/seq.*.n
    for m in units_active http_url version_file; do
        run7 health_check "$m"
        if [ "$run_rc" -ne 0 ] && reason_has "manifest read failed"; then
            ok "7 health_check: the '$m' read dies → gate FAILS (reason: $(cat "$REASON_F"))"
        else
            bad "7 health_check: the '$m' read died and the gate returned $run_rc (reason='$(cat "$REASON_F")') — probe silently skipped"
        fi
    done
    write_manifest with; : > "$STAGE/journal.jsonl"
    for m in '"restart",[]' restart_if_active restart_if_changed; do
        run7 services_restart_sets "$m"
        if [ "$run_rc" -ne 0 ] && reason_has "manifest read failed"; then
            ok "7 services_restart_sets: the $m read dies → step FAILS before any bounce (fcgiwrap restarted: $(called 'restart fcgiwrap' && echo yes || echo no))"
        else
            bad "7 services_restart_sets: the $m read died and the step returned $run_rc — its units silently not restarted"
        fi
        called 'restart fcgiwrap' && bad "7 services_restart_sets ($m): fcgiwrap bounced before the failed read was noticed" || :
    done
    run7 services_enable_and_tmpfiles 'get("enable"'
    if [ "$run_rc" -ne 0 ] && reason_has "manifest read failed"; then
        ok "7 services_enable_and_tmpfiles: the enable[] read dies → step FAILS (reason: $(cat "$REASON_F"))"
    else
        bad "7 services_enable_and_tmpfiles: the enable[] read died and the step returned $run_rc — new units silently not enabled"
    fi
    write_units_manifest nginx   # daemon_reload: false in this fixture
    run7 services_enable_and_tmpfiles daemon_reload
    if [ "$run_rc" -eq 0 ] && called daemon-reload; then
        ok "7 services_enable_and_tmpfiles: the daemon_reload read dies → daemon-reload DONE anyway (fail safe), step continues"
    else
        bad "7 services_enable_and_tmpfiles: the daemon_reload read died → rc=$run_rc, daemon-reload $(called daemon-reload && echo done || echo SKIPPED)"
    fi
    # Non-regression: a health block with NO http_url / version_file keys and
    # an empty units_active is "nothing configured" — the gate passes.
    cat > "$STAGE/meta/manifest.json" <<JSON
{"schema_version": 1, "version": "9.9.9.9", "deploy": [], "services": {"daemon_reload": false, "restart": [], "health": {"units_active": []}}}
JSON
    run7 health_check ""
    [ "$run_rc" -eq 0 ] && ok "7 keys absent by design (no http_url / version_file, empty units_active) → gate passes, as before" \
                        || bad "7 a health block with nothing configured FAILED the gate (rc=$run_rc, reason='$(cat "$REASON_F")')"
    # …and the same through the whole composition, as cmd_apply runs it.
    write_manifest with; : > "$STAGE/journal.jsonl"
    run7 restart_services_and_health units_active
    [ "$run_rc" -ne 0 ] && reason_has "manifest read failed" \
        && ok "7 restart_services_and_health (cmd_apply's shape): a dead units_active read fails the apply's gate" \
        || bad "7 restart_services_and_health: a dead units_active read passed the gate (rc=$run_rc)"
else
    bad "7 runner has no health_check split — run 7 cannot run"
fi

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
