#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-status-cache.sh — the cost of the slow status parts, and the service row
# that read the wrong process (1.0.7.36: A8-04/A8-05, H5-03/H5-04, H5-06).
#
# Everything here RUNS code extracted from the shipped files by function
# anchor (the idiom of test-rs485-roster-consumer.sh / test-status-cache-
# metadata.sh); the absolute roots are retargeted into a scratch tree and every
# retarget is asserted to have happened.
#
#   S  stale-while-revalidate (status.cgi cache_print_or_build): 5 parallel
#      requests to a services cache past its TTL but inside 3 x TTL -> ONE build,
#      all 5 answered at once from the stale copy, the cache replaced afterwards;
#      the answer does not wait for the background build (stdout released);
#      older than 3 x TTL -> one synchronous build, as before; no stale window
#      argument -> the old path unchanged. The dispatch passes the window for
#      rs485 (8/24), services (45/135) and services&fast=1 (10/30) only.
#   L  the service-list cache: status.cgi load_svc_ctl_states and
#      services_ctrl.cgi GET read list.json younger than 10 s WITHOUT
#      `sudo … list`; older -> one sudo call; a hung helper on services_ctrl
#      -> {"ok":false,"error":"timeout"}. The helper side
#      (etc/sa02m-web-service-ctl.sh): `list` prints cmd_list's bytes unchanged
#      and publishes them 0644; a mutation drops the file and bumps the
#      generation; a list built across a mutation is not published; the
#      dispatcher wires both.
#   B  H5-06: a snapshot holding only /opt/sa02m-modbus-mqtt/sa02m_telemetry.py
#      reads the bridge as inactive with uptime 0; the bridge script reads
#      active with its own uptime; mosquitto's uptime comes from its process
#      (characterization — it was never 0 by the probe, see the handback).
#   P  /proc/<pid>/stat is read with `read`, not awk: the same uptime as the
#      awk formula, and an awk PATH shim that fails is never reached.
#
# RED (2026-11-11, STATUS_CGI_SRC / SERVICES_CTRL_SRC / SVC_CTL_SRC pointed at
# copies of HEAD): S1/S2 (5 builds, answers waited), S6 (no window in the
# dispatch), L1-L7, B1/B2 (telemetry read as the bridge), P1-P2 (awk reached).
#
# Run: bash scripts/dev/test-status-cache.sh   (bash, sh, python3, flock)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${STATUS_CGI_SRC:-www/network_config/cgi-bin/status.cgi}
SCTRL=${SERVICES_CTRL_SRC:-www/network_config/cgi-bin/services_ctrl.cgi}
CTL=${SVC_CTL_SRC:-etc/sa02m-web-service-ctl.sh}
for f in "$SRC" "$SCTRL" "$CTL"; do [ -f "$f" ] || { echo "FAIL  missing $f"; exit 1; }; done
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  cannot source lib_skip.sh"; exit 1; }
PY=""
for p in python3 python; do command -v "$p" >/dev/null 2>&1 && "$p" -c 'import json' >/dev/null 2>&1 && { PY=$p; break; }; done
[ -n "$PY" ] || { echo "FAIL  no python interpreter"; exit 1; }

T=$(mktemp -d) || exit 1
cleanup() { [ -n "${SLEEPER:-}" ] && kill "$SLEEPER" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
BIN="$T/bin"; mkdir -p "$BIN"
fails=0; skips=0
ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { skips=$((skips + 1)); quality_skip "$1"; }
ON_LINUX=0; [ "$(uname -s 2>/dev/null)" = Linux ] && ON_LINUX=1
jfield() { printf '%s' "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)" 2>/dev/null; }

# ═══ S. stale-while-revalidate ═══════════════════════════════════════════
sed -n '/^cache_is_fresh() {/,/^status_timeout_run() {/p' "$SRC" | sed '$d' > "$T/cache.sh"
grep -q '^cache_print_or_build() {' "$T/cache.sh" || { echo "FAIL  cache functions not extracted from $SRC"; exit 1; }
cat > "$T/swr-run.sh" <<'EOF'
. "$CACHE_FNS"
STATUS_PART=services
STATUS_CACHE_LOCK_WAIT_SEC=0.25
slow_build() {
    echo build >> "$COUNTER"
    sleep 1.5
    printf '{"v":"new"}\n'
}
cache_print_or_build "$CACHE_FILE" "$TTL" slow_build ${STALE:-}
EOF
swr_case() {  # $1 = cache age (s), $2 = stale window or "", $3 = parallel requests
    local age=$1 stale=$2 n=$3 i
    rm -rf "$T/swr"; mkdir -p "$T/swr"
    printf '{"v":"old"}\n' > "$T/swr/services.json"
    touch -d "@$(( $(date +%s) - age ))" "$T/swr/services.json"
    : > "$T/swr/counter"
    local t0; t0=$(date +%s%N)
    for (( i=1; i<=n; i++ )); do
        CACHE_FNS="$T/cache.sh" CACHE_FILE="$T/swr/services.json" COUNTER="$T/swr/counter" TTL=45 STALE="$stale" \
            bash "$T/swr-run.sh" > "$T/swr/out.$i" 2>/dev/null &
    done
    wait
    SWR_ANSWER_MS=$(( ($(date +%s%N) - t0) / 1000000 ))
    # Let a background rebuild finish before counting.
    for i in $(seq 1 40); do
        if command -v flock >/dev/null 2>&1; then
            flock -n "$T/swr/services.json.lock" true 2>/dev/null && [ -s "$T/swr/counter" ] && break
        fi
        sleep 0.1
    done
    sleep 0.2
    SWR_BUILDS=$(wc -l < "$T/swr/counter")
    SWR_OLD=0; SWR_NEW=0
    for (( i=1; i<=n; i++ )); do
        case "$(jfield "$(cat "$T/swr/out.$i")" 'd["v"]')" in old) SWR_OLD=$((SWR_OLD+1)) ;; new) SWR_NEW=$((SWR_NEW+1)) ;; esac
    done
    SWR_CACHE=$(jfield "$(cat "$T/swr/services.json")" 'd["v"]')
}
if ! command -v flock >/dev/null 2>&1; then
    skip "S1-S5 no flock(1) on this host (the board has util-linux)"
else
    swr_case 60 135 5
    if [ "$SWR_BUILDS" -eq 1 ] && [ "$SWR_OLD" -eq 5 ] && [ "$SWR_CACHE" = new ]; then
        ok "S1 5 parallel requests, cache 60 s old (TTL 45, window 135): 1 build, 5 stale answers, cache replaced"
    else
        bad "S1 builds=$SWR_BUILDS stale-answers=$SWR_OLD fresh-answers=$SWR_NEW cache=$SWR_CACHE (want 1 build, 5 stale, cache=new)"
    fi
    if [ "$SWR_ANSWER_MS" -lt 1300 ]; then
        ok "S2 the answers did not wait for the 1.5 s background build (${SWR_ANSWER_MS} ms)"
    else
        bad "S2 the answers took ${SWR_ANSWER_MS} ms — the stale answer waited for a build (or the build held stdout)"
    fi
    swr_case 200 135 1
    if [ "$SWR_BUILDS" -eq 1 ] && [ "$SWR_NEW" -eq 1 ]; then
        ok "S3 a cache older than 3 x TTL is rebuilt in line (fresh answer, 1 build)"
    else
        bad "S3 cache 200 s old: builds=$SWR_BUILDS fresh-answers=$SWR_NEW (want a synchronous build)"
    fi
    swr_case 60 "" 1
    if [ "$SWR_BUILDS" -eq 1 ] && [ "$SWR_NEW" -eq 1 ]; then
        ok "S4 without a stale window the old path is unchanged (synchronous build)"
    else
        bad "S4 no window: builds=$SWR_BUILDS fresh-answers=$SWR_NEW"
    fi
    swr_case 10 135 1
    if [ "$SWR_BUILDS" -eq 0 ] && [ "$SWR_OLD" -eq 1 ]; then
        ok "S5 a fresh cache is served with no build"
    else
        bad "S5 fresh cache: builds=$SWR_BUILDS"
    fi
fi
# S6: the dispatch passes the window for exactly the three slow parts.
disp=$(stripped_text "$SRC")
s6=0
for want in 'cache_print_or_build "${CACHE_DIR}/rs485.json" 8 build_rs485_json 24' \
            'cache_print_or_build "${CACHE_DIR}/services.json" 45 build_services_json 135' \
            'cache_print_or_build "${CACHE_DIR}/services_fast.json" 10 build_services_json 30'; do
    text_has "$disp" "$want" || { s6=1; bad "S6 the dispatch line is missing (or commented out): $want"; }
done
[ "$s6" -eq 0 ] && ok "S6 rs485 / services / services&fast=1 pass a 3 x TTL stale window"

# ═══ L. the service-list cache ═══════════════════════════════════════════
LIST_JSON='{"ok":true,"flasher_busy":false,"services":[{"id":"mosquitto","label":"MQTT","unit":"mosquitto.service","active":"active","enabled":true,"masked":false,"user_disabled":false,"installed":true}]}'
SUDO_LIST='{"ok":true,"flasher_busy":false,"services":[{"id":"mosquitto","label":"MQTT","unit":"mosquitto.service","active":"inactive","enabled":true,"masked":false,"user_disabled":false,"installed":true}]}'
cat > "$BIN/sudo" <<EOF
#!/bin/sh
echo "\$*" >> "$T/sudo.log"
[ -n "\${SUDO_SLEEP:-}" ] && sleep "\$SUDO_SLEEP"
printf '%s\n' '$SUDO_LIST'
EOF
chmod +x "$BIN/sudo"
mkdir -p "$T/svc"
printf '#!/bin/sh\nexit 0\n' > "$T/ctl"; chmod +x "$T/ctl"

# L1-L2: status.cgi load_svc_ctl_states
{
    grep -E '^SVC_CTL_LIST_CACHE=' "$SRC"
    awk '/^cache_is_fresh\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SRC"
    awk '/^load_svc_ctl_states\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SRC"
} > "$T/svcstate.sh"
sed -i -e "s|/usr/local/sbin/sa02m-web-service-ctl.sh|$T/ctl|g" \
       -e "s|/var/run/sa02m-svcctl/list.json|$T/svc/list.json|g" "$T/svcstate.sh"
grep -q '^load_svc_ctl_states() {' "$T/svcstate.sh" || bad "L1 load_svc_ctl_states() not extracted from $SRC"
grep -qF '/usr/local/sbin/sa02m-web-service-ctl.sh' "$T/svcstate.sh" && bad "L1 the CTL path was not retargeted"
svc_state_of() {  # prints "<state> <sudo calls>"
    : > "$T/sudo.log"
    PATH="$BIN:$PATH" bash -c '. "$1"; declare -A SVC_CTL_STATE; load_svc_ctl_states; echo "${SVC_CTL_STATE[mosquitto]:-none}"' _ "$T/svcstate.sh" 2>/dev/null
    wc -l < "$T/sudo.log"
}
# A native Windows python3 ends its lines with CR; strip it for the comparison.
svc_state_of_plain() { svc_state_of | tr -d '\r' | tr '\n' ' '; }
printf '%s\n' "$LIST_JSON" > "$T/svc/list.json"
r=$(svc_state_of_plain)
if [ "$r" = "inactive 1 " ]; then
    ok "an unowned legacy cache cannot override current service discovery"
else
    bad "unowned legacy cache served: $r"
fi
cat /proc/sys/kernel/random/boot_id > "$T/svc/owned-v1"
r=$(svc_state_of_plain)
if [ "$r" = "active 0 " ]; then
    ok "L1 status.cgi reads a list.json younger than 10 s without sudo (mosquitto=active, 0 sudo calls)"
else
    bad "L1 fresh list.json: got '$r' (want 'active 0') — the second sudo CTL list still runs"
fi
touch -d "@$(( $(date +%s) - 30 ))" "$T/svc/list.json"
r=$(svc_state_of_plain)
if [ "$r" = "inactive 1 " ]; then
    ok "L2 a list.json 30 s old is ignored: one sudo CTL list, its answer used"
else
    bad "L2 stale list.json: got '$r' (want 'inactive 1')"
fi

# L3-L5: services_ctrl.cgi GET
mkdir -p "$T/sc"
printf 'web_session_check_cookie() { return 0; }\n' > "$T/sc/lib_web_auth.sh"
sed -e "s|CTL=/usr/local/sbin/sa02m-web-service-ctl.sh|CTL=$T/ctl|" \
    -e "s|RESULT_DIR=/var/run/sa02m-svcctl|RESULT_DIR=$T/svc|" \
    -e "s|timeout -k 2 100 sudo|timeout -k 1 1 sudo|" "$SCTRL" > "$T/sc/services_ctrl.cgi"
grep -qF "RESULT_DIR=$T/svc" "$T/sc/services_ctrl.cgi" || bad "L3 RESULT_DIR was not retargeted in services_ctrl.cgi"
sc_get() {
    : > "$T/sudo.log"
    PATH="$BIN:$PATH" REQUEST_METHOD=GET QUERY_STRING='' bash "$T/sc/services_ctrl.cgi" 2>/dev/null | sed '1,/^$/d'
}
printf '%s\n' "$LIST_JSON" > "$T/svc/list.json"
body=$(sc_get); calls=$(wc -l < "$T/sudo.log")
if [ "$body" = "$LIST_JSON" ] && [ "$calls" -eq 0 ]; then
    ok "L3 services_ctrl.cgi GET serves a fresh list.json byte for byte, no sudo"
else
    bad "L3 fresh list.json: sudo calls=$calls body=$body"
fi
touch -d "@$(( $(date +%s) - 30 ))" "$T/svc/list.json"
body=$(sc_get); calls=$(wc -l < "$T/sudo.log")
if [ "$body" = "$SUDO_LIST" ] && [ "$calls" -eq 1 ]; then
    ok "L4 a stale list.json falls back to one sudo CTL list"
else
    bad "L4 stale list.json: sudo calls=$calls body=$body"
fi
rm -f "$T/svc/list.json"
body=$(SUDO_SLEEP=5 sc_get)
if [ "$(jfield "$body" 'd.get("error")')" = timeout ]; then
    ok "L5 a hung helper ends in {\"ok\":false,\"error\":\"timeout\"} (bound shortened to 1 s here)"
else
    bad "L5 a hung helper did not end in the timeout answer: $body"
fi

# L6-L8: execute the private module now sourced by the shipped helper.
mkdir -p "$T/res"
cat /proc/sys/kernel/random/boot_id > "$T/res/owned-v1"
{
    grep -E '^RESULT_DIR=' "$CTL"
    cat usr/local/lib/sa02m-service-cache.sh
} > "$T/svclist.sh"
sed -i "s|^RESULT_DIR=.*|RESULT_DIR=$T/res|" "$T/svclist.sh"
if ! grep -q '^svc_list_publish() {' "$T/svclist.sh" || ! grep -q '^svc_list_cache_invalidate() {' "$T/svclist.sh"; then
    bad "L6 svc_list_publish / svc_list_cache_invalidate not found in $CTL"
else
    out=$(LJ=$LIST_JSON sh -c '. "$1"; cmd_list() { printf "%s\n" "$LJ"; }; svc_list_publish' _ "$T/svclist.sh")
    if [ "$out" = "$LIST_JSON" ] && [ "$(cat "$T/res/list.json" 2>/dev/null)" = "$LIST_JSON" ]; then
        ok "L6 \`list\` prints cmd_list's answer unchanged and publishes the same bytes"
    else
        bad "L6 publish: printed=$out file=$(cat "$T/res/list.json" 2>/dev/null)"
    fi
    if [ "$ON_LINUX" -eq 1 ]; then
        mode=$(stat -c %a "$T/res/list.json" 2>/dev/null)
        [ "$mode" = 644 ] && ok "L6 list.json is 0644 (www-data reads it)" || bad "L6 list.json mode $mode, want 644"
    else
        skip "L6 file mode (POSIX modes are not meaningful on $(uname -s)); CI is the authority"
    fi
    sh -c '. "$1"; svc_list_cache_invalidate' _ "$T/svclist.sh"
    if [ ! -e "$T/res/list.json" ] && [ "$(cat "$T/res/list.gen" 2>/dev/null)" = 1 ]; then
        ok "L7 a mutation drops list.json and bumps the generation"
    else
        bad "L7 invalidate: list.json present=$([ -e "$T/res/list.json" ] && echo yes || echo no) gen=$(cat "$T/res/list.gen" 2>/dev/null)"
    fi
    out=$(LJ=$LIST_JSON sh -c '. "$1"; cmd_list() { svc_list_cache_invalidate; printf "%s\n" "$LJ"; }; svc_list_publish' _ "$T/svclist.sh")
    if [ "$out" = "$LIST_JSON" ] && [ ! -e "$T/res/list.json" ]; then
        ok "L8 a list built across a mutation is printed but NOT published"
    else
        bad "L8 a list built across a mutation was published (stale state for 10 s)"
    fi
fi
ctltext=$(stripped_text "$CTL")
l9=0
[[ "$ctltext" =~ list\)[[:space:]]*svc_list_publish ]] || { l9=1; bad "L9 the dispatcher's list) branch does not call svc_list_publish"; }
text_has "$ctltext" 'begin_result "$ID" "$ACTION" || exit 1' || { l9=1; bad "L9 the mutation case changed shape (begin_result line not found)"; }
text_has "$ctltext" 'svc_cache_begin "$_cache_units"' || { l9=1; bad "L9 mutation does not register its private lease"; }
text_has "$ctltext" "trap 'svc_cache_exit' EXIT" || { l9=1; bad "L9 missing lease finalization trap"; }
text_has "$ctltext" 'svc_cache_finish' || { l9=1; bad "L9 missing lease finalization"; }
[ "$l9" -eq 0 ] && ok "L9 the dispatcher publishes on list and invalidates before and after every mutation"

# ═══ B / P. process probes ════════════════════════════════════════════════
awk '/^_PROC_SNAPSHOT=""/{f=1} /^gather_optional_platform_services\(\) \{/{f=0} f{print}' "$SRC" > "$T/probe.sh"
sed -i "s|/sys/fs/cgroup|$T/cg|g" "$T/probe.sh"
grep -q '^fast_service_state() {' "$T/probe.sh" && grep -q '^uptime_from_cgroup_slice() {' "$T/probe.sh" \
    || { echo "FAIL  probe functions not extracted from $SRC"; exit 1; }
sleep 120 & SLEEPER=$!
sleep 0.2
REAL_UP=$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || echo 0)
CLK=$(getconf CLK_TCK 2>/dev/null || echo 100)
probe() {  # $1 = snapshot line(s), $2 = command
    PROBE_SNAP=$1 PROBE_UP=$((REAL_UP + 1000)) PROBE_CLK=$CLK bash -c '
        . "$1"; _PROC_SNAPSHOT=$PROBE_SNAP; _PROC_CLK_TCK=$PROBE_CLK; UPTIME_SEC=$PROBE_UP
        eval "$2"' _ "$T/probe.sh" "$2" 2>/dev/null
}
TELE="$SLEEPER python3 /usr/bin/python3 /opt/sa02m-modbus-mqtt/sa02m_telemetry.py"
BRIDGE="$SLEEPER python3 /usr/bin/python3 /opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py"
MOSQ="$SLEEPER mosquitto /usr/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf"
st=$(probe "$TELE" 'fast_service_state sa02m-modbus-mqtt')
up=$(probe "$TELE" 'fast_service_uptime sa02m-modbus-mqtt.service')
[ "$st" = inactive ] && ok "B1 only the telemetry daemon runs -> «MQTT мост» inactive" \
                     || bad "B1 the telemetry process (/opt/sa02m-modbus-mqtt/sa02m_telemetry.py) reads as the bridge: state=$st"
[ "$up" = 0 ] && ok "B2 ... and its uptime is 0, not the telemetry's" \
              || bad "B2 the bridge uptime is the telemetry's: $up s"
st=$(probe "$BRIDGE" 'fast_service_state sa02m-modbus-mqtt')
up=$(probe "$BRIDGE" 'fast_service_uptime sa02m-modbus-mqtt.service')
if [ "$st" = active ] && [ "${up:-0}" -ge 900 ]; then
    ok "B3 the bridge script reads active with its own uptime (${up} s)"
else
    bad "B3 the running bridge was not recognised: state=$st uptime=$up"
fi
up=$(probe "$MOSQ" 'fast_service_uptime mosquitto.service')
if [ "${up:-0}" -ge 900 ]; then
    ok "B4 mosquitto's uptime comes from its process (${up} s)"
else
    bad "B4 mosquitto uptime from the probe is $up"
fi
if [ -r "/proc/$SLEEPER/stat" ]; then
    want=$(( REAL_UP + 1000 - $(awk '{print $22}' "/proc/$SLEEPER/stat") / CLK ))
    mkdir -p "$T/awkshim"
    printf '#!/bin/sh\necho awk >> "%s/awk.log"\nexit 1\n' "$T" > "$T/awkshim/awk"; chmod +x "$T/awkshim/awk"
    got=$(PATH="$T/awkshim:$PATH" probe "" "_proc_uptime_from_pidlist $SLEEPER")
    mkdir -p "$T/cg/system.slice/demo.service"; echo "$SLEEPER" > "$T/cg/system.slice/demo.service/cgroup.procs"
    got2=$(PATH="$T/awkshim:$PATH" probe "" "uptime_from_cgroup_slice demo")
    if [ "$got" = "$want" ] && [ "$got2" = "$want" ]; then
        ok "P1 /proc stat parsed in-shell gives the awk formula's uptime ($want s) for both readers"
    else
        bad "P1 uptime differs from the awk formula: pidlist=$got cgroup=$got2 want=$want"
    fi
    if [ -s "$T/awk.log" ]; then
        bad "P2 the uptime readers still start awk ($(wc -l < "$T/awk.log") call(s))"
    else
        ok "P2 no awk process is started (the shim was never reached)"
    fi
else
    skip "P1-P2 /proc/<pid>/stat is not readable on $(uname -s)"
fi

# CPU sampler: execute the shipped functions; the Python harness owns only fixtures.
if "$PY" - "$SRC" <<'CPU_TESTS'
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

source = Path(sys.argv.pop(1)).read_text()
root = Path.cwd()
functions = list(re.finditer(r'^([a-zA-Z_][a-zA-Z_0-9]*)\(\) \{', source, re.M))
def extract(name):
    hits = [i for i, match in enumerate(functions) if match[1] == name]
    assert len(hits) == 1, ('missing/ambiguous actual function', name)
    i = hits[0]
    body = source[functions[i].start():]
    end = re.search(r'^}\n(?=\n|$)', body, re.M)
    assert end, ('missing function end', name)
    return body[:end.end()]

names = [m[1] for m in functions if m[1].startswith('cpu_') and m[1] != 'cpu_temp']
assert {'cpu_usage', 'cpu_usage_from_stat_lines'} <= set(names)
names += ['gather_priority_metrics', 'build_priority_json', 'build_cpu_json',
          'build_core_json', 'build_full_json', 'print_cpu_json',
          'print_priority_json', 'status_emit_live', 'status_emit_snapshot', 'status_cache_failure',
          'cache_is_fresh', 'status_emit_cache', 'cache_print_stale_and_refresh', 'cache_print_or_build',
          'build_temp_json', 'print_temp_json']
actual = '\n'.join(extract(n) for n in names)
assert '/proc/stat' in actual
actual = actual.replace('/proc/stat', '"$CPU_PROC"')
assert '/proc/stat' not in actual
A = 'cpu 100 0 0 300 0 0 0 0 0 0'
B = 'cpu 101 0 0 300 0 0 0 0 0 0'
C = 'cpu 105 0 0 315 0 0 0 0 0 0'
SHIMS = r'''
date() { echo date >> "$CASE/forbidden"; return 1; }
mktemp() { echo mktemp >> "$CASE/forbidden"; return 1; }
mv() { echo mv >> "$CASE/forbidden"; return 1; }
rm() { echo rm >> "$CASE/forbidden"; return 1; }
chmod() { echo chmod >> "$CASE/forbidden"; return 1; }
flock() { echo flock >> "$CASE/forbidden"; return 1; }
read() {
    if [[ $(readlink /proc/self/fd/0) != "$CPU_PROC" ]]; then builtin read "$@"; return $?; fi
    local value
    if [[ -e "$CASE/waited" ]]; then value=$AFTER; echo after >> "$CASE/reads"
    elif [[ -e "$CASE/read-once" ]]; then value=$IMMEDIATE; echo immediate >> "$CASE/reads"
    else value=$FIRST; : > "$CASE/read-once"; echo first >> "$CASE/reads"; fi
    [[ $value != FAIL ]] || return 1
    printf -v "${@: -1}" '%s' "$value"
}
sleep() {
    printf '%s\n' "$*" >> "$CASE/waits"
    [[ ${WAIT_FAIL:-0} == 0 ]] || return 1
    : > "$CASE/waited"
    if [[ -n ${BARRIER:-} ]]; then
        printf 'ready\n' > "$CASE/ready"
        builtin read -r -t 4 _ < "$CASE/go" || return 1
    fi
}
ram_stats() { echo '100 25 75 0 0'; }
cpu_temp() { echo 40; }
root_disk_usage_kb() { echo '100 25 75'; }
gather_main_metrics() { echo UNEXPECTED; return 0; }
print_core_json() { echo UNEXPECTED; }
print_full_json() { echo UNEXPECTED; }
build_rs485_array() { echo UNEXPECTED; }
'''

class CPU(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base/'cache'
        self.cache.mkdir()
        self.serial = 0

    def prepare(self, first=A, after=C, immediate=B, mode='cpu_usage', **extra):
        self.serial += 1
        case = self.base/str(self.serial); case.mkdir()
        proc = case/'stat'; proc.touch()
        script = case/'run.sh'
        script.write_text('#!/bin/bash\nset -u\n'+actual+'\n'+SHIMS+'\n'+extra.pop('shim', '')+'\n'+mode+'\n')
        script.chmod(0o755)
        env = dict(os.environ, CASE=str(case), CPU_PROC=str(proc), CACHE_DIR=str(self.cache),
                   FIRST=first, AFTER=after, IMMEDIATE=immediate, **extra)
        return script, env, case

    def run_case(self, expected='25', waits=1, failure=False, envelope=False, **kwargs):
        script, env, case = self.prepare(**kwargs)
        result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
        observed = (case/'waits').read_text().splitlines() if (case/'waits').exists() else []
        reads = (case/'reads').read_text().splitlines() if (case/'reads').exists() else []
        if not failure or result.stdout or result.returncode == 0 or observed != ['0.05'] * waits:
            print('CPU evidence:', json.dumps(dict(stdout=result.stdout.strip(), rc=result.returncode,
                                                   reads=reads, waits=observed)), flush=True)
        if failure:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            if envelope:
                self.assertEqual(result.stdout, '{"error":"status_build_failed"}\n')
                self.assertNotIn('_status_meta', result.stdout)
                self.assertRegex(result.stderr, r'^status.cgi: status_build_failed part=(cpu|priority|core|full) rc=1\n$')
            else:
                self.assertEqual(result.stdout, '')
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        self.assertEqual(observed, ['0.05'] * waits, 'required wait evidence')
        if not failure:
            self.assertEqual(reads, ['first', 'after'], 'accepted reads must bracket the wait')
        self.assertFalse((case/'forbidden').exists(), 'CPU sampler reached clock/cache operations')
        self.assertFalse(list(self.cache.glob('cpu_sample.state.*')), 'publication temporary files left behind')
        return result, case

    def test_01_false_100_one_tick(self):
        self.run_case()

    def test_preserve_zero_full_floor_iowait_guest(self):
        cases = [('cpu 100 0 0 320 0 0 0 0 0 0', '0'),
                 ('cpu 120 0 0 300 0 0 0 0 0 0', '100'),
                 ('cpu 101 0 0 302 0 0 0 0 0 0', '33'),
                 ('cpu 100 0 0 310 10 0 0 0 0 0', '50'),
                 ('cpu 100 0 0 310 0 0 0 0 10 0', '50')]
        for end, expected in cases:
            with self.subTest(end=end):
                self.run_case(after=end, expected=expected)

    def test_obsolete_cache_untouched(self):
        old = self.cache/'cpu_sample.state'
        for record in ['', '100 '+A+'\n', 'malformed\n', '101 '+C+'\n']:
            old.unlink(missing_ok=True)
            old.write_text(record); old.chmod(0o444)
            before = old.stat()
            self.run_case()
            after = old.stat()
            self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns, before.st_atime_ns),
                             (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_atime_ns))
            self.assertEqual(old.read_text(), record)
        old.unlink(); old.symlink_to(self.base/'does-not-exist')
        before = old.lstat(); self.run_case(); after = old.lstat()
        self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                         (after.st_ino, after.st_mode, after.st_mtime_ns))
        self.assertEqual(os.readlink(old), str(self.base/'does-not-exist'))
        old.unlink(); os.mkfifo(old)  # opening the obsolete path would block and fail the deadline
        self.run_case(); old.unlink(); self.cache.rmdir()
        self.run_case()

    def test_bad_live_read_wait_and_deltas(self):
        cases = [dict(first='FAIL', waits=0), dict(first='cpu nope', waits=0),
                 dict(after='FAIL'), dict(after=A), dict(after='cpu 99 0 0 301 0 0 0 0 0 0'),
                 dict(after='cpu 102 0 0 315 0 0 0 0 0'), dict(after='cpu 105 0 0 315 x 0 0 0 0 0'),
                 dict(WAIT_FAIL='1')]
        invalid = ['', 'intr 1 2 3 4', 'cpu 1 2 3', 'cpu 1 2 3 -4',
                   'cpu 1 2 3 4 5 6 7 8 9 10 11', 'cpu 999999999999999999999 0 0 0',
                   'cpu 1+2 0 0 300', 'cpu 1 0 0 ３００',
                   'cpu a[$(touch '+str(self.base/'INJECTED')+')] 0 0 300']
        cases += [dict(first=line, waits=0) for line in invalid]
        cases += [dict(after=line) for line in invalid]
        for args in cases:
            with self.subTest(args=args): self.run_case(failure=True, **args)
        self.assertFalse((self.base/'INJECTED').exists())

    def test_actual_consumers_and_collector_preserve_last_trusted(self):
        for builder in ['build_cpu_json', 'build_priority_json']:
            (self.cache/'cpu_sample.state').unlink(missing_ok=True)
            script, env, case = self.prepare(mode='status_emit_live '+builder)
            proc = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['cpu_usage'], 25)
        for builder in ['build_cpu_json', 'build_priority_json', 'build_core_json', 'build_full_json']:
            self.run_case(mode=builder, after='FAIL', failure=True)
            self.run_case(mode='status_emit_live '+builder, after='FAIL', failure=True, envelope=True,
                          STATUS_PART=builder.removeprefix('build_').removesuffix('_json'))
        spec = importlib.util.spec_from_file_location('cpu_collector', root/'etc/sa02m-status-cache.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        (self.cache/'cpu_sample.state').unlink(missing_ok=True)
        script, env, case = self.prepare(mode='status_emit_live build_priority_json')
        # The real collector starts the actual extracted builder subprocess.
        from unittest.mock import patch
        with patch.dict(os.environ, env):
            collector = module.Collector(self.base/'runtime', str(script)); collector.runtime.mkdir()
            collector.cycle('priority')
            self.assertEqual(collector.samples['priority']['cpu_usage'], 25)
            trusted = dict(collector.samples['priority']); received = collector.sample_received['priority']
            os.environ['AFTER'] = 'FAIL'
            collector.cycle('priority')
            snapshot = json.loads((collector.runtime/'snapshot.json').read_text())
            self.assertEqual(collector.samples['priority'], trusted)
            self.assertEqual(collector.sample_received['priority'], received)
            self.assertEqual(snapshot['parts']['priority']['cpu_usage'], 25)
            self.assertEqual(snapshot['errors']['priority'], 'collection_failed')

    def test_live_envelope_non_cpu_partial_and_success(self):
        for part, diagnostic in [('temp', 'temp'), ('untrusted\nrequest', 'unknown')]:
            script, env, _ = self.prepare(STATUS_PART=part, mode='status_emit_live failed_non_cpu',
                                         shim='failed_non_cpu() { printf \'{"partial":\'; return 7; }')
            result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
            print('Envelope evidence:', json.dumps(dict(part=diagnostic, rc=result.returncode,
                                                       stdout=result.stdout, stderr=result.stderr)), flush=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout, '{"error":"status_build_failed"}\n')
            self.assertEqual(result.stderr, 'status.cgi: status_build_failed part='+diagnostic+' rc=7\n')
        script, env, _ = self.prepare(STATUS_PART='temp', mode='status_emit_live build_temp_json')
        result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['temp_c'], 40)
        self.assertEqual(data['_status_meta']['ttl_s'], 0)

    def test_failed_rebuild_preserves_actual_cache(self):
        cached = self.cache/'priority.json'
        payload = b'{"cpu_usage":73}\n'
        cached.write_bytes(payload); os.utime(cached, (100, 100))
        before = cached.stat()
        script, env, _ = self.prepare(after='FAIL', STATUS_PART='priority', STATUS_CACHE_LOCK_WAIT_SEC='0.25',
                                     mode='cache_print_or_build "$CACHE_DIR/priority.json" 1 build_priority_json',
                                     shim='rm() { command rm "$@"; }; flock() { command flock "$@"; }')
        result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['cpu_usage'], 73)
        self.assertEqual(data['_status_meta']['measured_at'], 100)
        self.assertGreater(data['_status_meta']['age_s'], 0)
        self.assertEqual(cached.read_bytes(), payload)
        after = cached.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_iowait_decrease_preserves_formula_and_consumer_quality(self):
        first = 'cpu 100 0 0 300 10 0 0 0 0 0'
        after = 'cpu 106 0 0 315 9 0 0 0 0 0'
        self.run_case(first=first, after=after)
        for builder in ['build_cpu_json', 'build_priority_json']:
            script, env, _ = self.prepare(first=first, after=after, mode='status_emit_live '+builder)
            result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['cpu_usage'], 25)
        spec = importlib.util.spec_from_file_location('iowait_collector', root/'etc/sa02m-status-cache.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        script, env, _ = self.prepare(first=first, after=after, mode='status_emit_live build_priority_json')
        from unittest.mock import patch
        with patch.dict(os.environ, env):
            collector = module.Collector(self.base/'runtime', str(script)); collector.runtime.mkdir()
            collector.cycle('priority')
        self.assertEqual(collector.samples['priority']['cpu_usage'], 25)
        trusted = dict(collector.samples['priority']); received = collector.sample_received['priority']
        for invalid in ['cpu 101 0 0 300 9 0 0 0 0 0',  # zero total delta
                        'cpu 100 0 0 300 9 0 0 0 0 0',  # negative total delta
                        'cpu 100 0 0 315 9 0 0 0 0 0']:  # idle delta exceeds total
            with self.subTest(invalid=invalid):
                self.run_case(first=first, after=invalid, failure=True)
                script, env, _ = self.prepare(first=first, after=invalid,
                                             mode='status_emit_live build_priority_json', STATUS_PART='priority')
                collector.command = str(script)
                with patch.dict(os.environ, env):
                    collector.cycle('priority')
                snapshot = json.loads((collector.runtime/'snapshot.json').read_text())
                self.assertEqual(collector.samples['priority'], trusted)
                self.assertEqual(collector.sample_received['priority'], received)
                self.assertEqual(snapshot['parts']['priority']['cpu_usage'], 25)
                self.assertEqual(snapshot['errors']['priority'], 'collection_failed')

    def test_concurrent_independent_callers(self):
        jobs = []
        for end, expected in [(C, '25'), ('cpu 120 0 0 300 0 0 0 0 0 0', '100')]:
            script, env, case = self.prepare(after=end, BARRIER='1')
            os.mkfifo(case/'ready'); os.mkfifo(case/'go')
            proc = subprocess.Popen(['bash', str(script)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
            # FIFO readiness/wakeup are bounded by the subprocess and outer test deadline.
            ready = subprocess.run(['timeout', '5', 'cat', str(case/'ready')], capture_output=True, text=True)
            self.assertEqual(ready.stdout, 'ready\n')
            jobs.append((proc, case, expected))
        for proc, case, expected in jobs:
            wake = subprocess.run(['timeout', '5', 'bash', '-c', 'printf "go\\n" > "$1"', '_',
                                   str(case/'go')], capture_output=True, text=True, timeout=6)
            self.assertEqual(wake.returncode, 0, wake.stderr)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err); self.assertEqual(out.strip(), expected)
            self.assertEqual((case/'waits').read_text(), '0.05\n')
            self.assertEqual((case/'reads').read_text(), 'first\nafter\n')
            self.assertFalse((case/'forbidden').exists())
            self.assertFalse(list(self.cache.iterdir()))

unittest.main(verbosity=2)

CPU_TESTS
then ok "CPU actual sampling and consumer regressions"
else bad "CPU actual sampling and consumer regressions"; fi

echo
[ "$skips" -gt 0 ] && echo "NOTE  $skips case(s) SKIPPED (environment) — skipped is not passed; CI is their authority"
if [ "$fails" -eq 0 ]; then
    echo "PASS  status-cache"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
