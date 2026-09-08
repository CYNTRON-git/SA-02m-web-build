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
export CALLS_LOG ACTIVE_FILE

log()                  { :; }
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
        [ "${1:-}" = "--quiet" ] && shift
        grep -qxF "${1:-}" "$ACTIVE_FILE" 2>/dev/null
        ;;
    is-enabled)
        echo enabled
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
run_health() { : > "$CALLS_LOG"; ( set -euo pipefail; restart_services_and_health "$TXN" ) >/dev/null 2>&1; run_rc=$?; }

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

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
