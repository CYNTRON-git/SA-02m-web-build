#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-offline-update-wrapper.sh — regression harness for the offline wrapper
# (scripts/offline-full-update.sh): the post-check table (D5 step D) and the
# detached launch line (D5 step E). Quality row `offline-update-wrapper`.
# 8D bench-136 reset (.ai-dev/8d/bench-136-reset.md).
#
# Comment-mutation: section 6 pins live lines and IS registered in
# comment-mutation-proof (commenting the launch line out turns 6a RED on
# non-vacuity). Sections 1–5 are behavioural — post_checks is EXTRACTED from
# the shipped wrapper and RUN against a systemctl shim and a scratch unit dir;
# every guarantee there is a PASS/FAIL row observed in its output, not a
# needle grep, and the extraction guards abort the run when the shipped block
# moves, so a commented-out line changes the measured rows instead of hiding.
#
# Why this exists: the 1.136 re-run at 22:48 printed PASS 13/13 with
# «служба sa02m-flasher: не был запущен до обновления — состояние сохранено»
# while the flasher unit was a 0-byte file systemd reported as MASKED — the
# svc-before snapshot said `inactive`, so the outage read as a preserved
# operator stop. A core unit that is masked, or whose /etc fragment is empty,
# is a FAIL row whatever the snapshot says: a masked unit can never come back
# on its own, and «состояние сохранено» must never describe a dead unit.
#
# And why section 6 exists: install.sh was launched with `nohup … &` only, so
# it stayed in the SSH transport's session — a paramiko exec_command whose
# channel closes tears that session down, and the inherited stdin is a dead
# channel. The 22:48 re-run that finished used its own session and a real
# /dev/null stdin. The launch must keep `setsid` and `</dev/null`, and the
# --dry-run line must print the SAME form (it is the only launch form an
# operator can see without root).
#
# Method: extract post_checks + its helpers + row + say from the shipped
# wrapper (awk by function name, the idiom of test-update-deploy-skip.sh),
# stub log / read_version / the globals it reads, put a systemctl shim first
# on PATH (is-active, is-enabled from a per-unit state table; list-units
# --failed empty), point the unit-dir seam SA02M_OFU_UNIT_DIR at a scratch
# dir, run, and assert the exact row for each service state. Section 6 reads
# the launch lines COMMENT-STRIPPED through .ai-dev/quality/checks/lib_check.sh
# (a `#`-disabled line must not satisfy a pin) and fails on a missing line.
#
# Drive-to-failure, measured 2026-09-09 on a scratch copy passed as OFU_SRC=
# with the D/E changes reverted: cases 1 and 2 print PASS «состояние
# сохранено» for a masked / 0-byte unit, 6a reports the bare
# `nohup env … bash install.sh`, 6b the DRY-RUN line that no longer mirrors
# it — 4 FAILURE(S). A pre-1.0.6.41 wrapper (no ofu_unit_fragment_empty at
# all) is RED one step earlier, at the extraction guard.
#
# Run: bash scripts/dev/test-offline-update-wrapper.sh   (bash + awk + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
SRC=${OFU_SRC:-scripts/offline-full-update.sh}
[ -f "$SRC" ] || { echo "FAIL  missing $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract the shipped functions (abort loudly if the block moved) ────────
extract() {  # extract <fn> → $T/fn.sh
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC"
}
: > "$T/fn.sh"
for fn in say row ofu_unit_fragment_empty post_checks; do
    extract "$fn" >> "$T/fn.sh"
    grep -q "^$fn() {" "$T/fn.sh" || { echo "FAIL  could not extract $fn() from $SRC — the shipped block moved; fix the extractor, do not skip"; exit 1; }
done
grep -q 'CORE_SERVICES' "$T/fn.sh" || { echo "FAIL  extracted post_checks has no CORE_SERVICES loop — wrong function body"; exit 1; }

# ── Shims ──────────────────────────────────────────────────────────────────
mkdir -p "$T/bin" "$T/units" "$T/www"
ST="$T/state"; mkdir -p "$ST"
cat > "$T/bin/systemctl" <<SHIM
#!/bin/bash
ST="$ST"
SHIM
cat >> "$T/bin/systemctl" <<'SHIM'
cmd=${1:-}; shift || true
case "$cmd" in
    is-active)   v=$(cat "$ST/$1.active" 2>/dev/null || echo inactive); printf '%s\n' "$v"; [ "$v" = active ] && exit 0 || exit 3 ;;
    is-enabled)  v=$(cat "$ST/$1.enabled" 2>/dev/null || echo disabled); printf '%s\n' "$v"; [ "$v" = enabled ] && exit 0 || exit 1 ;;
    cat)         [ -f "$ST/$1.enabled" ] && exit 0 || exit 1 ;;
    list-units)  exit 0 ;;
    *)           exit 0 ;;
esac
SHIM
printf '#!/bin/bash\nprintf 200\n' > "$T/bin/curl"
printf '#!/bin/bash\nexit 0\n'     > "$T/bin/visudo"
printf '#!/bin/bash\nshift\nexec "$@"\n' > "$T/bin/timeout"
chmod +x "$T/bin"/*
PATH="$T/bin:$PATH"

# ── Globals post_checks reads ──────────────────────────────────────────────
LOG="$T/install.log"; printf 'Установка завершена\n' > "$LOG"
WEB_ROOT="$T/www"; TARGET_VER=1.0.6.41; printf '%s\n' "$TARGET_VER" > "$WEB_ROOT/VERSION"
SVC_BEFORE="$T/svc-before"
CORE_SERVICES="svc-alive svc-masked svc-empty svc-stopped"
UPDATE_CHECK="$T/no-such-update-check"   # absent → its own FAIL row, not under test
STATE_JSON="$T/no-such-check.json"
EXTRA_LOG=""; N_PASS=0; N_FAIL=0
export SA02M_OFU_UNIT_DIR="$T/units"
log() { :; }
read_version() { [ -f "$1" ] || return 0; tr -d '\r' < "$1" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1; }
# shellcheck disable=SC1090
. "$T/fn.sh"

# ── Seed: four core services, one per state ────────────────────────────────
printf 'active'   > "$ST/svc-alive.active";   printf 'enabled'  > "$ST/svc-alive.enabled"
printf 'inactive' > "$ST/svc-masked.active";  printf 'masked'   > "$ST/svc-masked.enabled"
printf 'inactive' > "$ST/svc-empty.active";   printf 'masked'   > "$ST/svc-empty.enabled"
: > "$T/units/svc-empty.service"                                   # the 1.136 shape: 0-byte fragment
printf 'inactive' > "$ST/svc-stopped.active"; printf 'disabled' > "$ST/svc-stopped.enabled"
printf 'svc-alive active\nsvc-masked inactive\nsvc-empty inactive\nsvc-stopped inactive\n' > "$SVC_BEFORE"

out=$(post_checks 0 2>&1 | tr -d '\r')
row_for() { printf '%s\n' "$out" | grep -F "служба $1" | head -1; }

echo "── 1. masked core unit ⇒ FAIL even though svc-before says inactive ──"
r=$(row_for svc-masked)
case "$r" in
    *FAIL*замаскирован*) ok "1 svc-masked: $r" ;;
    *) bad "1 svc-masked expected a FAIL row naming the mask, got: ${r:-<no row>}" ;;
esac

echo "── 2. 0-byte /etc fragment ⇒ FAIL naming the torn install ──"
r=$(row_for svc-empty)
case "$r" in
    *FAIL*"файл юнита пуст"*) ok "2 svc-empty: $r" ;;
    *) bad "2 svc-empty expected a FAIL row naming the empty fragment, got: ${r:-<no row>}" ;;
esac

echo "── 3. the refresh guarantee still holds: a preserved operator stop is PASS ──"
r=$(row_for svc-stopped)
case "$r" in
    *PASS*"состояние сохранено"*) ok "3 svc-stopped: $r" ;;
    *) bad "3 svc-stopped expected PASS «состояние сохранено», got: ${r:-<no row>}" ;;
esac

echo "── 4. an active unit is PASS ──"
r=$(row_for svc-alive)
case "$r" in
    *PASS*active*) ok "4 svc-alive: $r" ;;
    *) bad "4 svc-alive expected PASS active, got: ${r:-<no row>}" ;;
esac

echo "── 5. non-vacuity: four service rows were printed ──"
n=$(printf '%s\n' "$out" | grep -c 'служба svc-')
[ "$n" -eq 4 ] && ok "5 four «служба» rows" || bad "5 expected 4 service rows, got $n — the loop did not run over CORE_SERVICES"

echo "── 6. the launch keeps its own session and a live stdin (D5 step E) ──"
# shellcheck source=/dev/null
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_first_line >/dev/null; then
    live_n=$(stripped_count "$SRC" '^[[:space:]]*nohup .* bash install\.sh')
    live_l=$(stripped_first_line "$SRC" '^[[:space:]]*nohup .* bash install\.sh')
    live=""; [ -n "$live_l" ] && live=$(stripped_text "$SRC" | sed -n "${live_l}p")
    if [ "$live_n" != 1 ] || [ -z "$live_l" ]; then
        bad "6a launch line: expected exactly one live \`nohup … bash install.sh\` in $SRC, found ${live_n:-0}"
    elif [[ "$live" == *setsid* ]] && [[ "$live" == *"</dev/null"* ]]; then
        ok "6a live launch (l.$live_l) runs install.sh in its own session with a real stdin: $(printf '%s' "$live" | sed 's/^[[:space:]]*//')"
    else
        bad "6a live launch (l.$live_l) lacks setsid and/or </dev/null — a closed SSH session can kill the install: $live"
    fi
    dry_l=$(stripped_first_line "$SRC" 'DRY-RUN: выполнил бы')
    dry=""; [ -n "$dry_l" ] && dry=$(stripped_text "$SRC" | sed -n "${dry_l}p")
    if [ -z "$dry_l" ]; then
        bad "6b no DRY-RUN launch line in $SRC — the only launch form an operator can see is gone"
    elif [[ "$dry" == *setsid* ]] && [[ "$dry" == *"</dev/null"* ]]; then
        ok "6b DRY-RUN line (l.$dry_l) mirrors the live launch (setsid + </dev/null)"
    else
        bad "6b DRY-RUN line (l.$dry_l) does not mirror the live launch — it advertises a form that is not run: $dry"
    fi
else
    bad "6 cannot source .ai-dev/quality/checks/lib_check.sh — the launch pins did NOT run (a skip is not a pass)"
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "offline-update-wrapper: ALL OK"
    exit 0
fi
echo "offline-update-wrapper: $fails FAILURE(S)"
exit 1
