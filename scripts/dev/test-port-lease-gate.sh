#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-port-lease-gate.sh — regression test for the RS-485 port-lease gate in
# etc/sa02m-web-service-ctl.sh (flasher_poll_lock_held → cmd_list's
# `flasher_busy` field and cmd_start's mplc4/mqtt-bridge refusal).
#
# Why this exists: the gate was DEAD from 2026-07-12 to 2026-08-06 and nothing
# noticed. The probe sent `Cookie: session_token=cyntron_session` — a token the
# session model had retired — and `curl -sS` WITHOUT `-f` exits 0 on the
# resulting HTTP 401, so `|| return 1` never fired, `{"error":"unauthorized"}`
# contained no `"busy":true`, and the function answered "not busy" forever.
# Every symptom of that is invisible: no syntax error, no failed unit, no red
# gate — just Пуск buttons that stay enabled while a flash runs.
#
# The one thing that could have caught it is the one thing the original work
# did not have: an assertion that a HELD lease reads as BUSY. docs/bugs/
# BUGLOG.md:1562 records the feature's verification as «flasher_busy=false,
# кнопки Пуск доступны» — the IDLE observation, which is bit-identical to the
# broken state. Hence case 1 below, and hence the negative controls.
#
# METHOD — and the part that makes this test worth having:
# The `curl` PATH shim MODELS THE DAEMON'S AUTH RULE rather than returning a
# fixed answer. A hex session_token of 32..128 chars is authorized; anything
# else gets a real HTTP 401 with `{"error":"unauthorized"}` on stdout and
# EXIT 0 (curl's real behaviour without -f); /health is served regardless of
# cookie, exactly as the daemon dispatches it above _check_auth(). A shim that
# always answered "busy" would make case 1 fail before AND after the fix and
# would prove nothing — so the shim's own rule is asserted first (§0).
#
# RED-BEFORE / GREEN-AFTER, reproducible on demand:
#   git show <pre-fix-rev>:etc/sa02m-web-service-ctl.sh > /tmp/prefix.sh
#   PORT_LEASE_SRC=/tmp/prefix.sh bash scripts/dev/test-port-lease-gate.sh
# Cases 1, 4a-4d, 6, 7 and 9-10 FAIL there and pass against the fixed file.
#
# Sandbox retargets (declared, and each asserted to have actually happened —
# a silently missed retarget would run these cases against the real /run):
#   1. the socket path        → a scratch path
#   2. `[ -S "$_sock" ]`      → `[ -e "$_sock" ]`, so a plain file can stand in
#      for a unix socket (creating one portably from bash is not possible;
#      §5 pins that the SHIPPED file still uses -S, so the relaxation here
#      cannot hide a regression there)
#
# Run: bash scripts/dev/test-port-lease-gate.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${PORT_LEASE_SRC:-etc/sa02m-web-service-ctl.sh}
[ -r "$SRC" ] || { echo "FAIL  cannot read $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; STATE="$T/shim"; MINBIN="$T/minbin"
mkdir -p "$BIN" "$STATE" "$MINBIN"
SOCK="$T/flasher.sock"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract every shipped function (everything above the arg dispatch) ──────
sed '/^ACTION=/,$d' "$SRC" > "$T/raw.sh"
for f in flasher_poll_lock_held flasher_blocks_com_pollers cmd_list cmd_start \
         resolve_unit_for_id validate_id json_escape svc_is_installable; do
    grep -q "^${f}() {" "$T/raw.sh" \
        || { echo "FAIL  could not extract ${f}() from $SRC — the dispatch marker moved; fix this harness, do not delete it"; exit 1; }
done
[ "$(wc -l < "$T/raw.sh")" -gt 400 ] || { echo "FAIL  extraction is suspiciously short — check the sed range"; exit 1; }

# Retarget 1: the socket path (matches both the pre-fix hard-coded literal and
# the post-fix FLASHER_SOCK default).
sed "s#/run/sa02m-flasher/flasher.sock#${SOCK}#g" "$T/raw.sh" > "$T/s1.sh"
grep -q "$SOCK" "$T/s1.sh" \
    || { echo "FAIL  socket-path retarget matched nothing — the probe would talk to the REAL /run socket"; exit 1; }
# Retarget 2: relax the socket-type test so a plain file can stand in for it.
sed 's#\[ -S "\$_sock" \]#[ -e "$_sock" ]#' "$T/s1.sh" > "$T/fn.sh"
grep -q '\[ -e "\$_sock" \]' "$T/fn.sh" \
    || { echo "FAIL  socket-type retarget matched nothing — the sandbox socket would never be seen"; exit 1; }

# Pre-set the env-seam defaults BEFORE sourcing (they are `${VAR:-...}` reads).
export FLASHER_SOCK="$SOCK"
export FLASHER_PROBE_STAMP="$T/probe-warn.stamp"
# shellcheck disable=SC1090
. "$T/fn.sh"
# Hard assignments in the shipped preamble — override AFTER sourcing. The
# SC2034 waivers below: each var is read by the SHIPPED code sourced above,
# through a generated temp file the linter cannot follow. (Keep each directive
# on its own line with nothing after it — a wrapped continuation that begins
# with the magic comment prefix is parsed as a directive and is a parse ERROR
# that suppresses linting for the whole file; it cost a red CI row once.)
# shellcheck disable=SC2034
LOG="$T/install.log"; : >"$LOG"
# shellcheck disable=SC2034
RESULT_DIR="$T/svcctl"
# shellcheck disable=SC2034
FLASHER_PROBE_LOG_EVERY=0        # no throttling inside a test run

# ── The curl shim: a MODEL OF THE DAEMON, not a canned answer ───────────────
cat > "$BIN/curl" <<SHIM
#!/bin/sh
# Models sa02m-flasher's HTTP contract closely enough to be discriminating:
#   * /health  — dispatched BEFORE _check_auth(); served with no cookie.
#   * anything else — requires session_token matching [a-f0-9]{32,128}
#     (opt/sa02m-flasher/sa02m_flasher/auth.py). Otherwise HTTP 401 with
#     {"error": "unauthorized"} — printed to stdout with EXIT 0 unless -f was
#     passed, which is precisely the curl behaviour the dead gate relied on.
# Body spacing mirrors json.dumps' default ": " separator.
STATE="$STATE"
cookie=""; url=""; fflag=0
while [ \$# -gt 0 ]; do
    case "\$1" in
        -H) shift; case "\$1" in Cookie:*) cookie="\$1" ;; esac ;;
        --max-time|--unix-socket|-o|-X|-d|-b) shift ;;
        -*) case "\$1" in *f*) fflag=1 ;; esac ;;
        http*) url="\$1" ;;
    esac
    shift
done
rest=\${url#*://}
path="/\${rest#*/}"
case "\$rest" in */*) ;; *) path="/" ;; esac

tok=\${cookie#*session_token=}
tok=\${tok%%;*}
authorized=0
case "\$tok" in
    ''|*[!0-9a-f]*) ;;
    *) if [ \${#tok} -ge 32 ] && [ \${#tok} -le 128 ]; then authorized=1; fi ;;
esac

mode=\$(cat "\$STATE/mode" 2>/dev/null || echo ok)
locked=\$(cat "\$STATE/poll_locked" 2>/dev/null || echo false)

if [ "\$path" != "/health" ] && [ "\$authorized" -eq 0 ]; then
    # HTTP 401. Without -f curl prints the body and exits 0 — THE defect.
    if [ "\$fflag" -eq 1 ]; then exit 22; fi
    printf '{"error": "unauthorized"}\n'
    exit 0
fi

case "\$mode" in
    timeout)  exit 28 ;;                        # --max-time fired: no output
    http500)  if [ "\$fflag" -eq 1 ]; then exit 22; fi
              printf '{"error": "boom"}\n'; exit 0 ;;
    garbage)  printf '<html>nginx</html>\n'; exit 0 ;;
    empty)    exit 0 ;;
esac

if [ "\$path" = "/health" ]; then
    case "\$mode" in
        oldhealth) printf '{"ok": true, "version": "1.0.5.66"}\n' ;;
        compact)   printf '{"ok":true,"version":"1.0.5.66","poll_locked":%s}\n' "\$locked" ;;
        *)         printf '{"ok": true, "version": "1.0.5.66", "poll_locked": %s}\n' "\$locked" ;;
    esac
    exit 0
fi
printf '{"busy": %s, "active_jobs": [], "poll_locked": %s}\n' "\$locked" "\$locked"
exit 0
SHIM
chmod +x "$BIN/curl"
PATH="$BIN:$PATH"

setmode()  { printf '%s\n' "$1" >"$STATE/mode"; }
setlock()  { printf '%s\n' "$1" >"$STATE/poll_locked"; }
mksock()   { : >"$SOCK"; }
rmsock()   { rm -f "$SOCK"; }

echo "── §0 the shim models the AUTH RULE, not a fixed answer ───────────────"
# Without this section the whole harness is worthless: a shim that always
# answered 401 (or always "busy") would make case 1 red before AND after the
# fix. These four assertions pin that the shim can say yes, says no for the
# right reason, and exempts /health.
setmode ok; setlock true
_hex=$(printf 'a%.0s' $(seq 64))
out=$(curl -sS -H "Cookie: session_token=$_hex" http://localhost/status); rc=$?
[ "$rc" -eq 0 ] && [ "${out#*\"busy\"}" != "$out" ] \
    && ok "a VALID hex token is authorized on /status (the shim can say yes)" \
    || bad "a valid hex token was refused: rc=$rc out=$out"

out=$(curl -sS -H 'Cookie: session_token=cyntron_session' http://localhost/status); rc=$?
[ "$rc" -eq 0 ] \
    && ok "the retired token gets exit 0 WITHOUT -f — the exact mechanism of the dead gate" \
    || bad "retired token + no -f gave rc=$rc, want 0"
[ "${out#*unauthorized}" != "$out" ] \
    && ok "  …and the body is {\"error\": \"unauthorized\"}, which contains no busy/poll_locked" \
    || bad "  unauthorized body = $out"

curl -fsS -H 'Cookie: session_token=cyntron_session' http://localhost/status >/dev/null 2>&1
[ "$?" -eq 22 ] \
    && ok "the SAME request WITH -f exits 22 — the one flag that separates the two outcomes" \
    || bad "retired token + -f did not exit 22"

out=$(curl -fsS http://localhost/health); rc=$?
[ "$rc" -eq 0 ] && [ "${out#*poll_locked}" != "$out" ] \
    && ok "/health is served with NO cookie (dispatched above _check_auth)" \
    || bad "/health with no cookie: rc=$rc out=$out"

echo
echo "── §1 flasher_poll_lock_held — the six states ─────────────────────────"
mksock; setmode ok

setlock true
flasher_poll_lock_held \
    && ok "case 1: the daemon reports a HELD lease ⇒ BUSY  ← the assertion the original work never had" \
    || bad "case 1: a HELD lease read as NOT BUSY — the gate is dead"

setlock false
flasher_poll_lock_held \
    && bad "case 2: an IDLE daemon read as BUSY — every start would be refused" \
    || ok "case 2: the daemon reports idle ⇒ not busy"

rmsock; setlock true
flasher_poll_lock_held \
    && bad "case 3: no socket read as BUSY — a board without the flasher could never start MPLC4/MQTT" \
    || ok "case 3: socket absent ⇒ not busy (no daemon ⇒ no lease; the one state knowable without asking)"
mksock

# ── The fail-closed quadrant. Each of these answered "not busy" before. ──
setlock false     # the daemon would say idle; the point is we cannot HEAR it
for spec in \
    "timeout|4a: the probe times out"            \
    "http500|4b: the daemon answers HTTP 500"    \
    "garbage|4c: the body is not JSON at all"    \
    "oldhealth|4d: valid JSON, but NO poll_locked field (an older daemon)" \
    "empty|4e: an empty body"                    ; do
    setmode "${spec%%|*}"
    flasher_poll_lock_held \
        && ok "case ${spec#*|} ⇒ BUSY (fail closed)" \
        || bad "case ${spec#*|} ⇒ not busy — an unreadable answer must never read as safe"
done

# The reason must reach a human, or the next silent rot is unobservable.
setmode timeout; : >"$LOG"
flasher_poll_lock_held
grep -q 'port-lease probe' "$LOG" \
    && ok "  the unknown-state reason is written to \$LOG (a silent fail-closed is a new silence)" \
    || bad "  nothing was logged for the unknown state"

# Both JSON spacings: json.dumps emits ": " today, a compact producer would not.
setmode compact; setlock true
flasher_poll_lock_held \
    && ok "case 5: compact JSON (\"poll_locked\":true) is matched too" \
    || bad "case 5: the no-space spelling was missed"
setmode compact; setlock false
flasher_poll_lock_held \
    && bad "case 5b: compact idle read as busy" \
    || ok "case 5b: compact JSON idle ⇒ not busy"

# curl absent while the daemon IS running: unknowable ⇒ busy. PATH is narrowed
# to wrappers for exactly the tools the branch needs, so `command -v curl`
# genuinely fails instead of finding a system curl.
for t in date cat mkdir dirname rm; do
    p=$(command -v "$t" 2>/dev/null) || continue
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$p" > "$MINBIN/$t"
    chmod +x "$MINBIN/$t"
done
setmode ok; setlock false
( PATH="$MINBIN"; flasher_poll_lock_held ) \
    && ok "case 6: daemon running but curl absent ⇒ BUSY (fail closed)" \
    || bad "case 6: no curl read as not busy — the probe cannot ask, so it must not answer 'safe'"

echo
echo "── §2 the JSON the panel actually consumes ────────────────────────────"
# Everything the frontend sees goes through cmd_list / cmd_start, so the helper
# being right is necessary but not sufficient — these pin the wiring.
setmode ok
# All services absent ⇒ the three installable ones emit ghost rows and no
# systemctl is probed. cmd_list itself stays SHIPPED code.
service_present() { return 1; }
emit_result() { printf '%s\n' "$1" >"$T/emit.json"; }
last_emit()   { cat "$T/emit.json" 2>/dev/null; }

mksock; setlock true
out=$(cmd_list)
case "$out" in
    *'"flasher_busy":true'*) ok "case 7: cmd_list with a held lease emits \"flasher_busy\":true" ;;
    *) bad "case 7: cmd_list emitted ${out%%,*}… — the panel never disables the Пуск buttons" ;;
esac
setlock false
out=$(cmd_list)
case "$out" in
    *'"flasher_busy":false'*) ok "case 8: cmd_list with an idle daemon emits \"flasher_busy\":false" ;;
    *) bad "case 8: idle cmd_list = ${out%%,*}…" ;;
esac
case "$out" in
    *'"ok":true'*'"services":['*) ok "  …and the rest of the list JSON is unchanged in shape" ;;
    *) bad "  cmd_list shape changed: $out" ;;
esac

# cmd_start's refusal. resolve_unit_for_id must succeed, so present + existing.
service_present() { return 0; }
unit_exists()     { return 0; }
setlock true
for id in mplc4 mqtt-bridge; do
    : >"$T/emit.json"
    cmd_start "$id" >/dev/null 2>&1
    case "$(last_emit)" in
        *'"ok":false'*'"error":"flasher_busy"'*)
            ok "case 9/10: cmd_start $id during a flash ⇒ {\"ok\":false,\"error\":\"flasher_busy\"}" ;;
        *) bad "case 9/10: cmd_start $id emitted $(last_emit) — a poller would be started onto a held RS-485 line" ;;
    esac
done

# Control: the gate is scoped to the COM pollers, not to every service.
sc_run() { return 0; }; sc_run_slow() { return 0; }
service_runtime_active() { return 0; }; service_admin_on() { return 0; }
: >"$T/emit.json"
cmd_start docker >/dev/null 2>&1
case "$(last_emit)" in
    *'"error":"flasher_busy"'*) bad "control: docker was refused — the gate leaked beyond mplc4/mqtt-bridge" ;;
    *) ok "control: cmd_start docker is NOT gated (only the RS-485 pollers are)" ;;
esac

echo
echo "── §3 static pins on the SHIPPED file ─────────────────────────────────"
# The behavioural cases above run against a RETARGETED copy and cannot see
# these. Each is a way the fix would silently disappear.
probe=$(awk '/^flasher_poll_lock_held\(\) \{/,/^\}$/' "$SRC")
[ -n "$probe" ] || bad "could not read flasher_poll_lock_held() out of $SRC"

printf '%s\n' "$probe" | grep -q 'curl -fsS' \
    && ok "the probe carries -f (without it a 401/500 exits 0 — the whole defect)" \
    || bad "the probe lost -f — a daemon error would read as success again"
printf '%s\n' "$probe" | grep -qi 'cookie' \
    && bad "the probe sends a Cookie again — the daemon route it uses needs none" \
    || ok "the probe sends no credential at all"
printf '%s\n' "$probe" | grep -q '/health' \
    && ok "the probe asks /health (the unauthenticated route)" \
    || bad "the probe no longer asks /health"
printf '%s\n' "$probe" | grep -q 'poll_locked' \
    && ok "the probe reads poll_locked (flock ∪ active jobs), not the narrower busy" \
    || bad "the probe no longer reads poll_locked"
printf '%s\n' "$probe" | grep -q '\[ -S "\$_sock" \]' \
    && ok "the shipped file still requires a real SOCKET (-S), not any file" \
    || bad "the shipped socket test is no longer -S — this harness relaxes it, so nothing else would catch that"

# The wiring the behavioural cases prove today but a future edit could cut.
list_body=$(awk '/^cmd_list\(\) \{/,/^\}$/' "$SRC")
printf '%s\n' "$list_body" | grep -q 'flasher_poll_lock_held' \
    && ok "cmd_list still calls flasher_poll_lock_held" \
    || bad "cmd_list no longer calls the probe — flasher_busy would be a constant"
start_body=$(awk '/^cmd_start\(\) \{/,/^\}$/' "$SRC")
printf '%s\n' "$start_body" | grep -q 'flasher_blocks_com_pollers' \
    && ok "cmd_start still calls flasher_blocks_com_pollers" \
    || bad "cmd_start no longer gates the COM pollers"

# The daemon half: the field must exist AND stay above the auth line.
DSRC=opt/sa02m-flasher/sa02m_flasher/service.py
if [ -r "$DSRC" ]; then
    grep -q 'def health_payload' "$DSRC" \
        && ok "the daemon exposes health_payload()" \
        || bad "health_payload() is gone from $DSRC — the probe would read no field and fail closed forever"
    h=$(grep -n 'p == "/health"' "$DSRC" | head -n1 | cut -d: -f1)
    a=$(grep -n 'if not self._check_auth' "$DSRC" | head -n1 | cut -d: -f1)
    if [ -n "$h" ] && [ -n "$a" ] && [ "$h" -lt "$a" ]; then
        ok "/health is still dispatched ABOVE _check_auth() (line $h < $a)"
    else
        bad "/health is no longer dispatched before the auth check (health=$h auth=$a) — the gate dies silently again"
    fi
else
    bad "could not read $DSRC"
fi

echo
if [ "$fails" -gt 0 ]; then
    printf 'test-port-lease-gate: %d FAILED\n' "$fails"
    exit 1
fi
printf 'test-port-lease-gate: all checks passed\n'
