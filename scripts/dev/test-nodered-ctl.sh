#!/bin/bash
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
SRC=etc/sa02m-web-service-ctl.sh
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
fails=0
ok() { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }
LOG="$T/install.log"; : > "$LOG"
sed -n '/^# ── Node-RED: staged payload/,/^# ── CODESYS uninstall/p' "$SRC" | sed '$d' > "$T/raw.sh"
grep -q '^nodered_install() {' "$T/raw.sh" || { echo 'FAIL missing delegate'; exit 1; }
grep -q '/usr/bin/python3 -I /usr/local/lib/sa02m-vendor-install.py install node-red --activate' "$T/raw.sh" || { echo 'FAIL fixed invocation missing'; exit 1; }
sed "s|/usr/local/lib/sa02m-vendor-install.py|$T/vendor.py|g" "$T/raw.sh" > "$T/fn.sh"
if grep -q '/usr/local/lib/sa02m-vendor-install.py' "$T/fn.sh"; then echo 'FAIL host helper remains'; exit 1; fi
. "$T/fn.sh"
emit_result() { printf '%s\n' "$1" > "$T/emit.json"; }
last_emit() { cat "$T/emit.json"; }
cat > "$T/vendor.py" <<'PY'
import json,pathlib,sys
root=pathlib.Path(__file__).parent
(root/'argv.json').write_text(json.dumps(sys.argv[1:]))
data=json.loads((root/'control.json').read_text())
print(data['stdout'])
raise SystemExit(data['rc'])
PY
run_case() {
  local name=$1 status=$2 payload=$3 expected_rc=$4 expected_error=${5:-} rc=0
  python3 - "$T/control.json" "$status" "$payload" <<'PY'
import json,sys
open(sys.argv[1],'w').write(json.dumps({'rc':int(sys.argv[2]),'stdout':sys.argv[3]}))
PY
  nodered_install || rc=$?
  if [ "$rc" -ne "$expected_rc" ]; then bad "$name rc=$rc"; return; fi
  if ! python3 - "$T/argv.json" "$T/emit.json" "$expected_error" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))==['install','node-red','--activate']
d=json.load(open(sys.argv[2])); assert d['id']=='node-red'
if sys.argv[3]: assert d['ok'] is False and d['error']==sys.argv[3]
else: assert d['ok'] is True
PY
  then bad "$name output/argv"; else ok "$name"; fi
}
run_case accepted 0 '{"ok":true,"id":"node-red","runtime":{"main_pid":123}}' 0
run_case refused 1 '{"ok":false,"id":"node-red","error":"major_upgrade_refused"}' 1 major_upgrade_refused
run_case unavailable 1 '{"ok":false,"id":"node-red","error":"payload_unavailable"}' 1 payload_unavailable
run_case nonzero_success 1 '{"ok":true,"id":"node-red"}' 1 install_failed
run_case zero_failure 0 '{"ok":false,"id":"node-red","error":"service_masked"}' 1 install_failed
run_case malformed 0 'not JSON' 1 install_failed
run_case wrong_identity 0 '{"ok":true,"id":"other"}' 1 install_failed
run_case wrong_boolean 0 '{"ok":1,"id":"node-red"}' 1 install_failed
run_case non_object 0 '[]' 1 install_failed
run_case multiple_objects 0 '{"ok":true,"id":"node-red"}{}' 1 install_failed
mv "$T/vendor.py" "$T/vendor.absent"
rm -f "$T/argv.json"
rc=0; nodered_install || rc=$?
if [ "$rc" -eq 1 ] && [ ! -e "$T/argv.json" ] && grep -q 'payload_unavailable' "$T/emit.json"; then ok 'missing helper refuses before dispatch'; else bad 'missing helper'; fi

echo "── the unit file lands atomically (tmp + fdatasync + rename-over) ─────"
# Why: `install -m` TRUNCATES the live path first and fills it later. A hard
# reset in that window leaves a 0-byte /etc/systemd/system/nodered.service, and
# systemd reads an empty unit as MASKED (bench 1.136 booted with the flasher
# unit masked that way, 2026-09-08 — web-code-rigor.md ## System scripts). This
# script runs standalone on the board as /bin/sh and cannot source
# scripts/lib.sh, so it carries its own atomic_install_unit() — the runner's
# atomic_install_file() shape: tmp beside the target → fdatasync → rename-over
# → dir fsync (one home for "which etc/ scripts carry a copy, and why":
# scripts/dev/codemod-install-atomic.py's docstring).
#
# Method: the helper is EXTRACTED from the shipped file and RUN under PATH shims
# modelling the failure instants (rename never happens: `mv` fails; the copy is
# torn: `install` writes 8 bytes and dies; the fdatasync is refused) —
# test-install-atomic.sh cases 2/3/8c/8d — plus a RECORDING `sync` shim that
# notes what the live unit holds at each call, which proves the ORDER: the tmp is
# synced while the live unit still reads OLD, the directory is synced once it
# reads NEW. The success path also runs under `sh` (dash on the board and on
# CI): a bashism in a /bin/sh script is a boot-time error no bash harness sees.
# The wiring (nodered_install_offline() lands the unit through the helper) and
# the rule (no raw `install -m` anywhere in this file writes a LIVE path — the
# codemod's prefixes, decided by the DESTINATION word, never the whole line:
# test-install-atomic.sh section 9's lesson) are source-text pins, comment-blind
# like the ones above, driven to failure by a raw unit write planted on a scratch
# copy behind the same `>>"$LOG" 2>&1; then` tail the real site carries.
#
# RED on the pre-1.0.6.60 script, observed 2026-09-28 (Windows git-bash, bash
# 5.3, dash present): 3 FAILURES — "nodered_install_offline() does not land
# the unit through atomic_install_unit", the sweep naming
# "1359:/etc/systemd/system/nodered.service" as a raw live-path site, and
# "defines no atomic_install_unit()" (every behavioural case skipped behind the
# missing helper — nothing to run); the non-vacuity floor and the
# drive-to-failure plant still passed. GREEN after the helper + the converted
# site, same host.
AIU_SRC_FN=atomic_install_unit
awk -v start="${AIU_SRC_FN}() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC" > "$T/aiu.sh"

# Node-RED unit transaction coverage now lives in test-vendor-install.py.
# (2) the rule — no raw `install -m` in this file writes a live path. Destination
# = the SECOND positional word after `install -m <mode>` (flags and their values
# skipped), read up to the first redirection or control operator; quotes
# stripped. "The last word" (section 9 of test-install-atomic.sh) would read
# `then` on the nodered line, which ends in `>>"$LOG" 2>&1; then`.
svc_install_sites() {   # "<line>:<text>" for every non-comment `install -m` line of $1
    grep -n -E '(^|[^[:alnum:]_/.-])install[[:space:]]+-m[[:space:]]' "$1" 2>/dev/null \
        | tr -d '\r' | grep -v -E '^[0-9]+:[[:space:]]*#' || true
}
svc_install_dst() {     # destination word of one `install -m` command line
    printf '%s\n' "$1" | awk '{
        if (!match($0, /(^|[^[:alnum:]_\/.-])install[[:space:]]+-m[[:space:]]+/)) { print ""; exit }
        s = substr($0, RSTART + RLENGTH)
        n = split(s, t, /[[:space:]]+/); pos = 0; dst = ""; skip = 1   # t[1] is the -m value
        for (k = 1; k <= n; k++) {
            w = t[k]; if (w == "") continue
            if (skip) { skip = 0; continue }
            if (w ~ /^([0-9]*>|<|;|&&|\|)/) break
            if (w ~ /^-[mog]$/) { skip = 1; continue }
            if (w ~ /^-/) continue
            pos++; if (pos == 2) { dst = w; break }
        }
        print dst
    }'
}
svc_dst_is_live() {     # the codemod rule: read by PID 1 / the shell without a restart
    case "$1" in
        /etc/systemd/system/*|/etc/systemd/system.conf.d/*|/etc/systemd/journald.conf.d/*) return 0 ;;
        /usr/local/bin/*|/usr/local/sbin/*|/usr/local/lib/*|/usr/local/libexec/*) return 0 ;;
        *) return 1 ;;
    esac
}
svc_raw_live_sites() {  # "<line>:<dst>" for every site of $1 whose destination is live
    local line dst
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        dst=$(svc_install_dst "${line#*:}"); dst=${dst#[\"\']}; dst=${dst%[\"\']}
        svc_dst_is_live "$dst" && printf '%s:%s\n' "${line%%:*}" "$dst"
    done <<EOF
$(svc_install_sites "$1")
EOF
}
MIN_SVC_INSTALL_SITES=3   # the two /opt/mplc4 plugin copies + the helper's own staging copy
raw=$(svc_raw_live_sites "$SRC" | sed '/^$/d')
n_sites=$(svc_install_sites "$SRC" | sed '/^$/d' | wc -l | tr -d ' ')
if [ -z "$raw" ]; then
    ok "no raw 'install -m' site in $SRC writes a live path"
else
    bad "$(printf '%s\n' "$raw" | wc -l | tr -d ' ') raw live-path 'install -m' site(s) in $SRC (line:destination): $(printf '%s\n' "$raw" | tr '\n' ' ')"
fi
if [ "$n_sites" -ge "$MIN_SVC_INSTALL_SITES" ]; then
    ok "non-vacuity: the sweep sees $n_sites 'install -m' sites in $SRC (floor $MIN_SVC_INSTALL_SITES)"
else
    bad "non-vacuity: the sweep sees only $n_sites 'install -m' sites (floor $MIN_SVC_INSTALL_SITES) — it stopped reading the file; fix the sweep, do not delete it"
fi
# drive-to-failure: a raw unit write planted behind the real site's tail
plant="$T/plant-svc.sh"
cp "$SRC" "$plant"
printf '%s\n' '    if ! install -m 0644 "$_nr_unit" /etc/systemd/system/planted.service >>"$LOG" 2>&1; then :; fi' >> "$plant"
pln=$(wc -l < "$plant" | tr -d ' ')
planted=$(svc_raw_live_sites "$plant" | sed '/^$/d')
case "$planted" in
    *"${pln}:/etc/systemd/system/planted.service"*) ok "drive-to-failure: a raw unit write planted at line $pln (with the \`>>\"\$LOG\" 2>&1; then\` tail) is flagged by the sweep" ;;
    *) bad "drive-to-failure: the planted raw unit write at line $pln was NOT flagged (got: $(printf '%s\n' "$planted" | tr '\n' ' ')) — the sweep is hollow" ;;
esac

# (3) behaviour — the extracted helper under shims
if [ ! -s "$T/aiu.sh" ]; then
    bad "$SRC defines no ${AIU_SRC_FN}() — the unit is written with a truncating 'install -m' (nothing to run; behavioural cases skipped)"
else
    ok "extracted ${AIU_SRC_FN}() from $SRC ($(wc -l < "$T/aiu.sh" | tr -d ' ') lines)"
    # shellcheck disable=SC1090
    . "$T/aiu.sh"
    U="$T/aiu"; mkdir -p "$U/live" "$U/src" "$U/bin-sync" "$U/bin-nomv" "$U/bin-torn"
    export AI_DST="$U/live/nodered.service" AI_TRACE="$T/aiu.trace"
    printf 'NEW unit body, longer than the old one\n' > "$U/src/nodered.service"
    # recording `sync`: argv + the first 3 bytes the LIVE unit holds at call time
    cat > "$U/bin-sync/sync" <<'SHIM'
#!/bin/sh
printf 'sync %s | dst=%s\n' "$*" "$(head -c 3 "$AI_DST" 2>/dev/null)" >> "$AI_TRACE"
if [ "${AI_SYNC_FAIL_D:-0}" = 1 ] && [ "${1:-}" = -d ]; then exit 1; fi
exit 0
SHIM
    printf '#!/bin/sh\nexit 1\n' > "$U/bin-nomv/mv"
    cat > "$U/bin-torn/install" <<'SHIM'
#!/bin/sh
# model of a copy killed mid-body: 8 bytes of the source (second-to-last
# argument) land in the target (last argument), then it dies
src=""; last=""
for a; do src=$last; last=$a; done
head -c 8 "$src" > "$last"
exit 1
SHIM
    chmod +x "$U/bin-sync/sync" "$U/bin-nomv/mv" "$U/bin-torn/install"
    aiu_reset()    { printf 'OLD unit body\n' > "$AI_DST"; : > "$AI_TRACE"; }
    aiu_live()     { cat "$AI_DST" 2>/dev/null; }
    aiu_tmp_left() { ls "$U/live"/nodered.service.tmp.* >/dev/null 2>&1; }

    aiu_reset; rc=0
    ( PATH="$U/bin-sync:$PATH"; atomic_install_unit "$U/src/nodered.service" "$AI_DST" 0644 ) || rc=$?
    if [ "$rc" -eq 0 ] && cmp -s "$U/src/nodered.service" "$AI_DST" && ! aiu_tmp_left; then
        ok "success: the live unit carries the NEW bytes, rc=0, no tmp left behind"
    else
        bad "success path: rc=$rc live='$(aiu_live | head -c 40)' dir='$(ls "$U/live" | tr '\n' ' ')'"
    fi
    # ORDER, from the trace: fdatasync of the tmp while the live unit still reads
    # OLD (the rename has not happened), then the directory fsync once it reads NEW.
    l_tmp=$(grep -n -E "^sync -d -- $U/live/nodered\.service\.tmp\.[0-9]+ \| dst=OLD\$" "$AI_TRACE" 2>/dev/null | head -1 | cut -d: -f1)
    l_dir=$(grep -n -E "^sync -- $U/live \| dst=NEW\$" "$AI_TRACE" 2>/dev/null | head -1 | cut -d: -f1)
    if [ -n "$l_tmp" ] && [ -n "$l_dir" ] && [ "$l_tmp" -lt "$l_dir" ]; then
        ok "order: 'sync -d -- <tmp>' with the live unit still OLD (trace line $l_tmp), then 'sync -- <dir>' with it NEW (line $l_dir)"
    else
        bad "order: expected 'sync -d -- <tmp> | dst=OLD' before 'sync -- <dir> | dst=NEW'; trace: $(tr '\n' ';' < "$AI_TRACE")"
    fi

    aiu_reset; rc=0
    ( PATH="$U/bin-nomv:$U/bin-sync:$PATH"; atomic_install_unit "$U/src/nodered.service" "$AI_DST" 0644 ) || rc=$?
    if [ "$rc" -ne 0 ] && [ "$(aiu_live)" = "OLD unit body" ] && ! aiu_tmp_left; then
        ok "failed rename: the live unit is still OLD, rc=$rc, tmp cleaned"
    else
        bad "failed rename: rc=$rc live='$(aiu_live)' dir='$(ls "$U/live" | tr '\n' ' ')'"
    fi

    aiu_reset; rc=0
    ( PATH="$U/bin-torn:$U/bin-sync:$PATH"; atomic_install_unit "$U/src/nodered.service" "$AI_DST" 0644 ) || rc=$?
    if [ "$rc" -ne 0 ] && [ "$(aiu_live)" = "OLD unit body" ] && ! aiu_tmp_left; then
        ok "torn copy: the live unit is still OLD (never the 8-byte prefix), rc=$rc, tmp cleaned"
    else
        bad "torn copy: rc=$rc live='$(aiu_live)' dir='$(ls "$U/live" | tr '\n' ' ')'"
    fi

    aiu_reset; rc=0
    ( PATH="$U/bin-sync:$PATH"; AI_SYNC_FAIL_D=1 atomic_install_unit "$U/src/nodered.service" "$AI_DST" 0644 ) || rc=$?
    if [ "$rc" -ne 0 ] && [ "$(aiu_live)" = "OLD unit body" ] && ! aiu_tmp_left; then
        ok "refused fdatasync: no rename over a tmp that may not be on disk — live unit OLD, rc=$rc, tmp cleaned"
    else
        bad "refused fdatasync: rc=$rc live='$(aiu_live)' dir='$(ls "$U/live" | tr '\n' ' ')' — a failed 'sync -d' must stop the rename"
    fi

    # NEGATIVE CONTROL — the torn shim is not decoration: the PRE-FIX shape (a
    # bare `install -m` onto the live path) under the same shim must leave the
    # live unit BROKEN, or the torn-copy case above asserts nothing.
    aiu_reset
    ( PATH="$U/bin-torn:$PATH"; install -m 0644 "$U/src/nodered.service" "$AI_DST" ) >/dev/null 2>&1
    pfx_bytes=$(wc -c < "$AI_DST" 2>/dev/null | tr -d ' ')
    if [ "$(aiu_live)" != "OLD unit body" ] && [ "$pfx_bytes" -lt 20 ]; then
        ok "negative control: the pre-fix 'install -m' onto the live path leaves it truncated to $pfx_bytes bytes under the same shim"
    else
        bad "negative control: the pre-fix shape survived the torn-copy shim ($pfx_bytes bytes) — the torn-copy case models nothing"
    fi

    # POSIX: the shipped file is /bin/sh — dash on the board and on CI. Run the
    # success path under dash (preferred: on git-bash `sh` IS bash, which would
    # prove nothing) so a bashism in the helper fails here, not at install time.
    posix_sh=$(command -v dash 2>/dev/null || command -v sh 2>/dev/null || true)
    if [ -n "$posix_sh" ]; then
        aiu_reset; rc=0
        ( PATH="$U/bin-sync:$PATH"; "$posix_sh" -c '. "$1"; atomic_install_unit "$2" "$3" 0644' _ "$T/aiu.sh" "$U/src/nodered.service" "$AI_DST" ) || rc=$?
        sh_kind=$("$posix_sh" -c 'echo "${BASH_VERSION:+bash }${BASH_VERSION:-posix sh}"')
        if [ "$rc" -eq 0 ] && cmp -s "$U/src/nodered.service" "$AI_DST" && ! aiu_tmp_left; then
            ok "POSIX: the helper runs under $(basename "$posix_sh") ($sh_kind) — no bashism in the /bin/sh script"
        else
            bad "POSIX: the helper failed under $(basename "$posix_sh") ($sh_kind; rc=$rc, live='$(aiu_live | head -c 40)') — a bashism in a /bin/sh script"
        fi
    else
        echo "SKIP  POSIX run (neither 'dash' nor 'sh' on this host) — CI is the authority"
    fi
fi

echo
if [ "$fails" -gt 0 ]; then
    printf 'test-nodered-ctl: %d FAILED\n' "$fails"
    exit 1
fi
printf 'test-nodered-ctl: all checks passed\n'
