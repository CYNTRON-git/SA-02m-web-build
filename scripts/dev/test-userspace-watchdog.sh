#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-userspace-watchdog.sh — behavioural regression for the two userspace
# health daemons (etc/sa02m-userspace-watchdog.sh, etc/sa02m-failure-monitor.sh)
# and the static half of the units that let nginx/fcgiwrap heal themselves.
#
# Why this exists (audit 2026-11-11 A3-04, A3-05, A3-06, H5-01):
#   * both daemons probed http://127.0.0.1:9999 hard-coded, while a board
#     installed with `--port N` serves the panel on N — the watchdog then
#     counted a healthy panel as down and rebooted the board after 10 min;
#   * the watchdog probed the STATIC login.html, which nginx serves without
#     fcgiwrap — a dead fcgiwrap (every CGI 502) was invisible to it, and when
#     it was seen, the only cure on offer was a board reboot, never a restart of
#     the one service;
#   * nginx had no Restart= at all, fcgiwrap gave up for good after 5 crashes
#     in 10 s (`failed`, no StartLimitIntervalSec);
#   * the failure monitor rewrote its snapshot (ss -ltnp, ps … args, a second
#     unit-state probe) on EVERY 5-second pass, changed or not.
#
# Method: the SHIPPED daemons run for real, briefly, against shims on PATH
# (curl, ss, pgrep, ps, tail, systemctl, reboot, sleep) that record their argv.
# The `sleep` shim makes every pass instant and stops the daemon (SIGTERM to its
# parent) after a fixed number of passes, so the pass count is exact, not a
# timing guess. CONF_FILE / LOG_FILE / STATE_DIR / SNAPSHOT_FILE are the daemons'
# own env seams; SA02M_WEB_PORT_LIB points at the repo's port library and
# SA02M_WEB_PORT_CONF at a sandbox port file saying 8080.
#
# Safety: the watchdog's reboot path calls `systemctl reboot`, `reboot -f` and
# writes /dev/watchdog. The black-box half therefore runs ONLY on Linux, ONLY
# when every one of those commands resolves to a shim on the PATH it runs with,
# and NEVER on a host that has a /dev/watchdog character device — otherwise it
# is reported SKIP (lib_skip.sh), never PASS. The static half runs everywhere.
#
# Cases:
#   A1  watchdog probes the port from /etc/sa02m_web_port.conf (8080), not 9999,
#       for both the static page and the CGI layer.          RED: :9999 only
#   A2  a persistent auth_check.cgi 502 (fcgiwrap dead): `systemctl restart
#       fcgiwrap.service` is issued and NO board reboot ever follows — the cgi
#       check is heal-only (Operator 2026-11-11).  RED: the cgi check reboots
#   A2b control: a missing required process (sshd) still reboots.
#   A2c the other web-only faults are heal-only too (Operator J-2): nginx process
#       missing (web-proc), the panel port not listening (web-port), login.html
#       not served (http) -> nginx restarted, NO reboot; one restart per pass when
#       they fail together.  RED on 51d31d08: each reaches FORCED REBOOT.
#   A2d control: ssh-port closed still reboots.
#   A5  a probe TIMEOUT (saturated fcgiwrap pool) restarts nothing until
#       CGI_BUSY_RESTART_AT consecutive timeouts (A5b: then one restart, never
#       a reboot) — a restart would kill an in-flight upload.  RED: restarts
#   A6  the SHIPPED auth_check.cgi, run through the real lib_web_auth.sh: no
#       cookie -> exactly `Status: 401 Unauthorized` + body `unauthorized`
#       (A6a), live session -> 204 (A6b), and the daemon's own check_cgi reads
#       it as healthy (A6c). Plus C4: nginx still routes /cgi-bin/, not internal.
#   A3  a healthy CGI layer (401 + body `unauthorized`) is not a failure: no
#       restart, no reboot.
#   A4  sourced with SA02M_WD_SOURCE_ONLY=1 the file only defines; with no port
#       library resolve_web_port falls back to 9999 and logs that once.
#   B1  failure monitor probes login.html and status.cgi on :8080. RED: :9999
#   B2  (H5-01) 6 passes with nothing changing write the snapshot ONCE.  RED: 6
#   B2b (same run, mplc4 down) one `pgrep` per mplc4 state read, and the
#       snapshot reuses the pass's unit states: <= 8 reads each. RED: 40+ / 14
#   B3  a state change (nginx goes down) writes the snapshot in that same pass,
#       and the snapshot carries the new state.
#   C1  nginx drop-in exists, carries Restart=on-failure, RestartSec=5 and
#       StartLimitIntervalSec=0 (comment-stripped).       RED: no drop-in
#   C2  scripts/03-webserver.sh installs it through sa02m_atomic_install.
#   C3  fcgiwrap.service: StartLimitIntervalSec=0, RestartSec=3.
#
# Non-vacuous: a missing daemon/unit/installer file FAILS; a daemon that never
# reached a single probe FAILS; zero recorded passes FAILS.
#
# Run: bash scripts/dev/test-userspace-watchdog.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

WD_SRC=${WD_SRC:-etc/sa02m-userspace-watchdog.sh}
FM_SRC=${FM_SRC:-etc/sa02m-failure-monitor.sh}
PORT_LIB=etc/sa02m-web-port-lib.sh
NGINX_DROPIN=etc/systemd/system/nginx.service.d/sa02m-restart.conf
FCGI_UNIT=etc/systemd/fcgiwrap.service
WEBSRV=scripts/03-webserver.sh

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
# shellcheck source=.ai-dev/quality/checks/lib_skip.sh
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  lib_skip.sh not loadable"; exit 1; }
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  lib_check.sh not loadable"; exit 1; }

for f in "$WD_SRC" "$FM_SRC" "$PORT_LIB" "$FCGI_UNIT" "$WEBSRV"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

# ═══ C: static half (every host) ════════════════════════════════════════════
if [ -s "$NGINX_DROPIN" ]; then
    di=$(stripped_text "$NGINX_DROPIN")
    c1=1
    text_matches "$di" '^\[Unit\]' && text_matches "$di" '^StartLimitIntervalSec=0[[:space:]]*$' || { bad "C1: drop-in lacks [Unit] StartLimitIntervalSec=0"; c1=0; }
    text_matches "$di" '^\[Service\]' && text_matches "$di" '^Restart=on-failure[[:space:]]*$' || { bad "C1: drop-in lacks [Service] Restart=on-failure"; c1=0; }
    text_matches "$di" '^RestartSec=5[[:space:]]*$' || { bad "C1: drop-in lacks RestartSec=5"; c1=0; }
    [ "$c1" = 1 ] && ok "C1: nginx drop-in carries StartLimitIntervalSec=0, Restart=on-failure, RestartSec=5"
else
    bad "C1: $NGINX_DROPIN missing — nginx has no automatic restart"
fi
if stripped_matches "$WEBSRV" 'sa02m_atomic_install[[:space:]]+-m[[:space:]]+644[[:space:]]+"\$SYSTEMD_DIR/system/nginx\.service\.d/sa02m-restart\.conf"[[:space:]]+/etc/systemd/system/nginx\.service\.d/sa02m-restart\.conf' \
   && stripped_matches "$WEBSRV" 'install[[:space:]]+-d[[:space:]]+-m[[:space:]]+755[[:space:]]+/etc/systemd/system/nginx\.service\.d'; then
    ok "C2: $WEBSRV creates nginx.service.d and installs the drop-in via sa02m_atomic_install"
else
    bad "C2: $WEBSRV does not install the nginx drop-in atomically"
fi
fu=$(stripped_text "$FCGI_UNIT")
fu_unit=$(awk '/^\[/{s=$0} s=="[Unit]"' <<< "$fu")
fu_svc=$(awk '/^\[/{s=$0} s=="[Service]"' <<< "$fu")
if text_matches "$fu_unit" '^StartLimitIntervalSec=0[[:space:]]*$' && text_matches "$fu_svc" '^RestartSec=3[[:space:]]*$' \
   && text_matches "$fu_svc" '^Restart=always[[:space:]]*$'; then
    ok "C3: fcgiwrap.service — [Unit] StartLimitIntervalSec=0, [Service] Restart=always RestartSec=3"
else
    bad "C3: fcgiwrap.service lacks [Unit] StartLimitIntervalSec=0 or RestartSec=3"
fi

# C4: the watchdog's cgi probe GETs /cgi-bin/auth_check.cgi from outside nginx.
# If the prefix location vanished, went `internal` (like /_auth_check) or an
# exact-match location took auth_check.cgi over, every board would see a
# permanent cgi fault. Comment-stripped; the `location /cgi-bin/ {` block is cut
# by brace depth, and an absent block FAILS (non-vacuous).
NGX=etc/nginx/network_config.conf
if [ -s "$NGX" ]; then
    nx=$(stripped_text "$NGX")
    blk=$(awk '/^[[:space:]]*location[[:space:]]+\/cgi-bin\/[[:space:]]*\{/{on=1} on{print; n+=gsub(/\{/,"{"); m+=gsub(/\}/,"}"); if(n>0&&n==m) exit}' <<< "$nx")
    if [ -z "$blk" ]; then
        bad "C4: $NGX has no 'location /cgi-bin/ {' block — the watchdog's cgi probe has no route"
    elif text_matches "$blk" '(^|[{;[:space:]])internal[[:space:]]*;' || ! text_matches "$blk" 'fastcgi_pass'; then
        bad "C4: location /cgi-bin/ is internal or has no fastcgi_pass — the watchdog's cgi probe cannot reach auth_check.cgi"
    elif text_matches "$nx" 'location[[:space:]]+[=~^]*[[:space:]]*[^[:space:]]*auth_check[.]cgi'; then
        bad "C4: a dedicated auth_check location in $NGX overrides the prefix route the watchdog probes"
    else
        ok "C4: nginx routes /cgi-bin/ (fastcgi_pass, not internal) with no auth_check override — the cgi probe reaches the shipped CGI"
    fi
else
    bad "C4: $NGX missing"
fi

# C5: the shipped failure-monitor conf (repo + firstboot overlay) must not pin the
# probe URLs: a conf URL wins over the port library, so a pinned :9999 keeps the
# monitor probing the wrong port on a board installed with `--port N`. A
# fail-IF-PRESENT pin (the mutation is re-adding the line, not commenting it out);
# a missing or empty conf FAILS.
for fmc in etc/sa02m_failure_monitor.conf tools/imaging/firstboot-overlay/etc/sa02m_failure_monitor.conf; do
    if [ ! -s "$fmc" ]; then bad "C5: $fmc missing or empty"; continue; fi
    if stripped_matches "$fmc" '^[[:space:]]*(HTTP_PROBE_URL|STATUS_PROBE_URL)[[:space:]]*='; then
        bad "C5: $fmc pins a probe URL — it would override the panel's real port"
    else
        ok "C5: $fmc leaves HTTP_PROBE_URL/STATUS_PROBE_URL unset (the monitor follows the real port)"
    fi
done

# ═══ black-box half: Linux + shims only ═════════════════════════════════════
run_blackbox=1
if [ "$(uname -s)" != Linux ]; then
    quality_skip "A1-A4 B1-B3 black-box daemon runs: not Linux ($(uname -s)) — CI is the authoritative run"
    run_blackbox=0
elif [ -c /dev/watchdog ]; then
    quality_skip "A1-A4 B1-B3 black-box daemon runs: this host has /dev/watchdog — the reboot path is never exercised near a real one"
    run_blackbox=0
fi

if [ "$run_blackbox" = 1 ]; then
SH="$T/shim"; mkdir -p "$SH"
export SHIM_LOG="$T/calls.log" SHIM_STATE="$T/state"
mkdir -p "$SHIM_STATE"

cat > "$SH/curl" <<'EOF'
#!/bin/bash
printf 'curl %s\n' "$*" >> "$SHIM_LOG"
url=""; fmt=""; out=""; fail=0
while [ $# -gt 0 ]; do
    case "$1" in
        -w) fmt=$2; shift ;;
        -o) out=$2; shift ;;
        --max-time|-m) shift ;;
        --*) ;;
        -*f*) fail=1 ;;
        -*) ;;
        *) url=$1 ;;
    esac
    shift
done
case "$url" in
    *auth_check.cgi)
        if [ -n "${SHIM_CGI_TIMEOUT:-}" ]; then   # a saturated pool: curl --max-time fires
            [ -n "$fmt" ] && printf '%b' "${fmt//%\{http_code\}/000}"
            exit 28
        fi
        if [ -n "${SHIM_CGI_REAL:-}" ]; then      # the SHIPPED CGI, no cookie, real lib_web_auth.sh
            raw=$(env -i PATH="$PATH" SA02M_SESSION_DIR="$SHIM_STATE/sessions" bash "$SHIM_CGI_REAL" 2>/dev/null)
            code=$(printf '%s\n' "$raw" | sed -n 's/^Status: \([0-9]*\).*/\1/p' | head -n 1)
            # body = everything after the first empty line of the CGI response
            body=$(printf '%s\n' "$raw" | awk 'b{print} /^\r?$/{b=1}')
            [ -n "$code" ] || code=200
        else
            code=${SHIM_CGI_CODE:-401}; body=${SHIM_CGI_BODY:-unauthorized}
        fi ;;
    *login.html) code=${SHIM_HTTP_CODE:-200}; body=ok ;;     # the static page nginx serves
    *) code=200; body=ok ;;
esac
if [ -n "$out" ]; then printf '%s\n' "$body" > "$out"; else printf '%s\n' "$body"; fi
[ -n "$fmt" ] && printf '%b' "${fmt//%\{http_code\}/$code}"
[ "$fail" = 1 ] && [ "$code" -ge 400 ] && exit 22
exit 0
EOF
cat > "$SH/ss" <<'EOF'
#!/bin/bash
printf 'ss %s\n' "$*" >> "$SHIM_LOG"
port=""
for a in "$@"; do case "$a" in *"sport = :"*) port=${a##*:} ;; esac; done
[ -n "$port" ] && [ -n "${SHIM_SS_MISS:-}" ] && case " $SHIM_SS_MISS " in *" $port "*) exit 0 ;; esac   # a port nobody listens on
echo "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*"
EOF
cat > "$SH/pgrep" <<'EOF'
#!/bin/bash
printf 'pgrep %s\n' "$*" >> "$SHIM_LOG"
name=${*: -1}
[ -e "$SHIM_STATE/down.$name" ] && exit 1
case "$name" in *mplc*) [ -e "$SHIM_STATE/down.mplc" ] && exit 1 ;; esac
exit 0
EOF
cat > "$SH/ps" <<'EOF'
#!/bin/bash
printf 'ps %s\n' "$*" >> "$SHIM_LOG"
echo "  PID  PPID STAT COMMAND"
EOF
cat > "$SH/tail" <<'EOF'
#!/bin/bash
printf 'tail %s\n' "$*" >> "$SHIM_LOG"
exec /usr/bin/tail "$@"
EOF
cat > "$SH/systemctl" <<'EOF'
#!/bin/bash
printf 'systemctl %s\n' "$*" >> "$SHIM_LOG"
case "$1" in is-active) echo active ;; esac
exit 0
EOF
cat > "$SH/reboot" <<'EOF'
#!/bin/bash
printf 'reboot %s\n' "$*" >> "$SHIM_LOG"
exit 0
EOF
# sleep: instant; counts passes; flips nginx down at SHIM_FLIP_AT; stops the
# daemon (its parent) once SHIM_MAX_SLEEPS is reached.
cat > "$SH/sleep" <<'EOF'
#!/bin/bash
printf 'sleep %s\n' "$*" >> "$SHIM_LOG"
n=$(grep -c '^sleep ' "$SHIM_LOG")
[ -n "${SHIM_FLIP_AT:-}" ] && [ "$n" -eq "$SHIM_FLIP_AT" ] && : > "$SHIM_STATE/down.nginx"
[ "$n" -ge "${SHIM_MAX_SLEEPS:-10}" ] && kill -TERM "$PPID" 2>/dev/null
exit 0
EOF
chmod +x "$SH"/*
SPATH="$SH:$PATH"
for c in curl ss pgrep ps tail systemctl reboot sleep; do
    [ "$(PATH="$SPATH" command -v "$c")" = "$SH/$c" ] || { echo "FAIL  shim $c does not resolve first on PATH — refusing to run a daemon with a real $c"; exit 1; }
done

printf 'SA02M_WEB_PORT=8080\n' > "$T/web_port.conf"
export SA02M_WEB_PORT_CONF="$T/web_port.conf" SA02M_WEB_PORT_LIB="$PWD/$PORT_LIB"

wd_run() {   # $1 = max passes; leaves the call log in $SHIM_LOG
    : > "$SHIM_LOG"; rm -f "$SHIM_STATE"/down.*
    for dn in ${WD_DOWN:-}; do : > "$SHIM_STATE/down.$dn"; done   # processes pgrep reports missing
    cat > "$T/wd.conf" <<EOF
CHECK_INTERVAL=1
FAIL_THRESHOLD=3
CGI_RESTART_AT=1
CGI_BUSY_RESTART_AT=${WD_BUSY_AT:-99}
GRACE_AFTER_BOOT_SEC=0
HEARTBEAT_INTERVAL_SEC=300
IMAGING_LOCK=$T/no-imaging.lock
NET_IFACE=sa02m-test-noiface
REQUIRED_PROCS="nginx sshd"
WATCHED_PROCS="fcgiwrap"
MAX_LOAD1_X100=999999
MIN_MEM_AVAIL_KB=0
EOF
    SHIM_MAX_SLEEPS=$1 CONF_FILE="$T/wd.conf" LOG_FILE="$T/wd.log" PATH="$SPATH" \
        timeout 30 bash "$WD_SRC" > "$T/wd.out" 2>&1
}

# ── A1 + A3: healthy CGI on the configured port ───────────────────────────
: > "$T/wd.log"
SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 4
n_pass=$(grep -c '^sleep ' "$SHIM_LOG")
n_curl=$(grep -c '^curl ' "$SHIM_LOG")
if [ "$n_pass" -lt 3 ] || [ "$n_curl" -lt 1 ]; then
    bad "A1: the watchdog never probed (passes=$n_pass curl=$n_curl) — out: $(tr '\n' '|' < "$T/wd.out" | cut -c1-300)"
else
    if grep -q '^curl .*http://127\.0\.0\.1:8080/login\.html' "$SHIM_LOG" \
       && grep -q '^curl .*http://127\.0\.0\.1:8080/cgi-bin/auth_check\.cgi' "$SHIM_LOG" \
       && ! grep -q '127\.0\.0\.1:9999' "$SHIM_LOG"; then
        ok "A1: watchdog probes :8080 (from the port file) for login.html AND cgi-bin/auth_check.cgi, never :9999"
    else
        bad "A1: watchdog probe URLs: $(grep '^curl' "$SHIM_LOG" | sort -u | tr '\n' '|')"
    fi
    if ! grep -qE '^systemctl (restart|reset-failed|reboot)' "$SHIM_LOG" && ! grep -q '^reboot' "$SHIM_LOG" \
       && ! grep -q 'check cgi: FAIL' "$T/wd.log"; then
        ok "A3: a CGI layer answering 401 'unauthorized' is healthy — no restart, no reboot over $n_pass passes"
    else
        bad "A3: healthy CGI treated as failure: $(grep -E '^(systemctl|reboot)' "$SHIM_LOG" | tr '\n' '|')"
    fi
fi

# ── A2: persistent CGI fault (502) — HEAL ONLY, never the board ────────────
# Operator decision 2026-11-11: a cgi failure alone restarts fcgiwrap and never
# reaches the reboot path, however long it lasts (8 passes here against
# FAIL_THRESHOLD=3). The board keeps running without the panel.
# Heals fall at failures 1, 2 (CGI_RESTART_AT=1, doubled) and then every
# FAIL_THRESHOLD (3, 6): >= 4 restarts in 8 passes pins the periodic re-heal.
: > "$T/wd.log"
SHIM_CGI_CODE=502 SHIM_CGI_BODY='<html>502 Bad Gateway</html>' wd_run 8
n_restart=$(grep -c '^systemctl restart fcgiwrap\.service' "$SHIM_LOG")
n_reboot=$(grep -cE '^systemctl reboot|^reboot' "$SHIM_LOG")
if [ "$n_restart" -ge 4 ] && [ "$n_reboot" -eq 0 ] && ! grep -q 'FORCED REBOOT' "$T/wd.log" \
   && grep -q '^systemctl reset-failed fcgiwrap\.service' "$SHIM_LOG"; then
    ok "A2: persistent CGI 502 -> reset-failed + restart fcgiwrap.service x$n_restart, and NO board reboot over 8 passes (threshold 3)"
else
    bad "A2: cgi fault is not heal-only — restarts=$n_restart reboot calls=$n_reboot; log: $(grep -E 'check cgi|FORCED' "$T/wd.log" | head -n 4 | tr '\n' '|')"
fi

# ── A2b: the OTHER reboot-capable checks still reboot (control for A2) ─────
: > "$T/wd.log"
WD_DOWN=sshd SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 8
if grep -q 'FORCED REBOOT: check=procs' "$T/wd.log" && grep -qE '^systemctl reboot' "$SHIM_LOG"; then
    ok "A2b: a missing required process (sshd) still reaches the board reboot at FAIL_THRESHOLD"
else
    bad "A2b: procs check no longer reboots — log: $(grep -E 'check procs|FORCED' "$T/wd.log" | head -n 3 | tr '\n' '|')"
fi

# ── A2c: the other web-only faults are heal-only too (Operator J-2) ────────
# Each of the three alone, over 8 passes against FAIL_THRESHOLD=3: nginx is
# restarted (and never fcgiwrap) and the board is never rebooted. The conf
# above lists nginx in REQUIRED_PROCS on purpose: the code must keep it off the
# reboot path anyway.
for wc in "web-proc|WD_DOWN=nginx|check web-proc: FAIL" \
          "web-port|SHIM_SS_MISS=8080|check web-port: FAIL" \
          "http|SHIM_HTTP_CODE=503|check http: FAIL"; do
    wlabel=${wc%%|*}; rest=${wc#*|}; wlog=${rest#*|}
    : > "$T/wd.log"
    case "$wlabel" in
        web-proc) WD_DOWN=nginx SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 8 ;;
        web-port) SHIM_SS_MISS=8080 SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 8 ;;
        http)     SHIM_HTTP_CODE=503 SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 8 ;;
    esac
    n_ng=$(grep -c '^systemctl restart nginx\.service' "$SHIM_LOG")
    n_fc=$(grep -c '^systemctl restart fcgiwrap\.service' "$SHIM_LOG")
    n_rb=$(grep -cE '^systemctl reboot|^reboot' "$SHIM_LOG")
    if grep -q "$wlog" "$T/wd.log" && [ "$n_ng" -ge 4 ] && [ "$n_fc" -eq 0 ] && [ "$n_rb" -eq 0 ] \
       && ! grep -q 'FORCED REBOOT' "$T/wd.log"; then
        ok "A2c/$wlabel: a persistent $wlabel fault -> nginx restarted x$n_ng (heal, then every threshold), never fcgiwrap, NO board reboot over 8 passes"
    else
        bad "A2c/$wlabel: not heal-only — nginx restarts=$n_ng fcgiwrap=$n_fc reboot calls=$n_rb; log: $(grep -E 'FAIL|FORCED' "$T/wd.log" | head -n 3 | tr '\n' '|')"
    fi
done

# A dead nginx fails web-proc, web-port and http in the same pass: ONE restart per
# pass, not three.
: > "$T/wd.log"
WD_DOWN=nginx SHIM_SS_MISS=8080 SHIM_HTTP_CODE=503 SHIM_CGI_CODE=502 wd_run 3
n_ng=$(grep -c '^systemctl restart nginx\.service' "$SHIM_LOG")
n_rb=$(grep -cE '^systemctl reboot|^reboot' "$SHIM_LOG")
if [ "$n_ng" -ge 1 ] && [ "$n_ng" -le 3 ] && [ "$n_rb" -eq 0 ]; then
    ok "A2c/dedupe: nginx dead (web-proc + web-port + http + cgi failing together) -> $n_ng nginx restart(s) over 3 passes, one per pass at most, no reboot"
else
    bad "A2c/dedupe: nginx restarts=$n_ng reboot calls=$n_rb over 3 passes (expected 1..3 restarts, 0 reboots)"
fi

# ── A2d: an unreachable SSH port is a whole-board fault — still reboots ─────
: > "$T/wd.log"
SHIM_SS_MISS=22 SHIM_CGI_CODE=401 SHIM_CGI_BODY=unauthorized wd_run 8
if grep -q 'FORCED REBOOT: check=ssh-port' "$T/wd.log" && grep -qE '^systemctl reboot' "$SHIM_LOG"; then
    ok "A2d: ssh-port closed still reaches the board reboot at FAIL_THRESHOLD"
else
    bad "A2d: ssh-port no longer reboots — log: $(grep -E 'check ssh-port|FORCED' "$T/wd.log" | head -n 3 | tr '\n' '|')"
fi

# ── A5: a probe TIMEOUT is a busy pool — no restart mid-upload ─────────────
: > "$T/wd.log"
SHIM_CGI_TIMEOUT=1 WD_BUSY_AT=99 wd_run 8
if ! grep -q '^systemctl restart fcgiwrap' "$SHIM_LOG" && ! grep -qE '^systemctl reboot|^reboot' "$SHIM_LOG" \
   && grep -q 'pool busy' "$T/wd.log"; then
    ok "A5: 8 probe timeouts (busy pool) -> no fcgiwrap restart, no reboot (a restart would kill an in-flight upload)"
else
    bad "A5: a busy pool triggered action: $(grep -E '^(systemctl|reboot)' "$SHIM_LOG" | tr '\n' '|')"
fi
: > "$T/wd.log"
SHIM_CGI_TIMEOUT=1 WD_BUSY_AT=3 wd_run 8
if grep -q '^systemctl restart fcgiwrap\.service' "$SHIM_LOG" && ! grep -qE '^systemctl reboot|^reboot' "$SHIM_LOG"; then
    ok "A5b: a pool still unanswered after CGI_BUSY_RESTART_AT (3) timeouts is healed by one fcgiwrap restart, never a reboot"
else
    bad "A5b: a wedged pool is never healed: $(grep -E '^(systemctl|reboot)' "$SHIM_LOG" | tr '\n' '|')"
fi

# ── A6: the SHIPPED auth_check.cgi through the real lib_web_auth.sh ────────
# The watchdog's cgi check counts any answer other than 401+`unauthorized` (or
# 204) as a fault. The shim above cannot see the real CGI change, so run it:
# no cookie -> exactly this status line and body; a live session -> 204.
AUTHCGI=www/network_config/cgi-bin/auth_check.cgi
mkdir -p "$SHIM_STATE/sessions"
raw=$(env -i PATH="$PATH" SA02M_SESSION_DIR="$SHIM_STATE/sessions" bash "$AUTHCGI" 2>/dev/null)
want=$(printf 'Status: 401 Unauthorized\nContent-type: text/plain\n\nunauthorized')
if [ "$raw" = "$want" ]; then
    ok "A6a: shipped auth_check.cgi, no cookie -> 'Status: 401 Unauthorized' + body 'unauthorized' (the watchdog's healthy answer)"
else
    bad "A6a: auth_check.cgi without a session answers: $(printf '%s' "$raw" | tr '\n' '|')"
fi
tok=0123456789abcdef0123456789abcdef0123
hash=$(printf '%s' "$tok" | sha256sum | cut -d' ' -f1)
printf '%s admin\n' "$(( $(date +%s) + 864000 ))" > "$SHIM_STATE/sessions/$hash"
raw=$(env -i PATH="$PATH" SA02M_SESSION_DIR="$SHIM_STATE/sessions" HTTP_COOKIE="session_token=$tok" bash "$AUTHCGI" 2>/dev/null | head -n 1)
if [ "$raw" = "Status: 204 No Content" ]; then
    ok "A6b: shipped auth_check.cgi, live session -> 204 (the other answer the watchdog accepts)"
else
    bad "A6b: auth_check.cgi with a live session answers '$raw'"
fi
: > "$T/wd.log"
SHIM_CGI_REAL="$PWD/$AUTHCGI" wd_run 4
if grep -q '^curl .*auth_check\.cgi' "$SHIM_LOG" && ! grep -q 'check cgi: FAIL' "$T/wd.log" \
   && ! grep -qE '^systemctl (restart|reboot)' "$SHIM_LOG"; then
    ok "A6c: the daemon's own check_cgi reads the real CGI's no-cookie answer as healthy (no FAIL, no restart)"
else
    bad "A6c: the real auth_check.cgi reads as a fault to the watchdog: $(grep 'check cgi' "$T/wd.log" | head -n 2 | tr '\n' '|')"
fi

# ── A4: source-only mode + fallback when the port library is absent ───────
a4=$(SA02M_WD_SOURCE_ONLY=1 SA02M_WEB_PORT_LIB="$T/absent-lib.sh" CONF_FILE="$T/absent.conf" \
     LOG_FILE="$T/a4.log" PATH="$SPATH" timeout 10 bash -c '. "$1"; resolve_web_port; resolve_web_port; printf "%s %s\n" "$WEB_PORT" "$HTTP_PROBE_URL"' _ "$WD_SRC" 2>&1 | tail -n 1)
n_fb=0; [ -f "$T/a4.log" ] && n_fb=$(grep -c 'port library' "$T/a4.log")
if [ "$a4" = "9999 http://127.0.0.1:9999/login.html" ] && [ "$n_fb" -eq 1 ]; then
    ok "A4: sourced with SA02M_WD_SOURCE_ONLY=1 the file only defines; no port library -> 9999, logged once"
else
    bad "A4: source-only/fallback — got '$a4', fallback log lines=$n_fb"
fi

# ── B: failure monitor ────────────────────────────────────────────────────
fm_run() {   # $1 = max passes, $2 = flip-at pass (or empty); mplc4 is down throughout
    : > "$SHIM_LOG"; rm -f "$SHIM_STATE"/down.*; rm -rf "$T/fmstate"; : > "$T/fm.log"
    : > "$SHIM_STATE/down.mplc"
    cat > "$T/fm.conf" <<EOF
CHECK_INTERVAL=1
HTTP_PROBE_INTERVAL_SEC=1
HEARTBEAT_INTERVAL_SEC=300
MONITOR_UNITS="ssh nginx fcgiwrap mplc4.service"
PORT_CHECK=22
EOF
    SHIM_MAX_SLEEPS=$1 SHIM_FLIP_AT=${2:-} CONF_FILE="$T/fm.conf" STATE_DIR="$T/fmstate" \
        LOG_FILE="$T/fm.log" SNAPSHOT_FILE="$T/fm.snapshot" PATH="$SPATH" \
        timeout 30 bash "$FM_SRC" > "$T/fm.out" 2>&1
}
fm_run 6
n_pass=$(grep -c '^sleep ' "$SHIM_LOG")
n_snap=$(grep -c '^tail -n 25' "$SHIM_LOG")
if [ "$n_pass" -ne 6 ] || ! grep -q '^curl ' "$SHIM_LOG"; then
    bad "B1: the monitor did not run 6 probing passes (passes=$n_pass) — out: $(tr '\n' '|' < "$T/fm.out" | cut -c1-300)"
else
    if grep -q '^curl .*http://127\.0\.0\.1:8080/login\.html' "$SHIM_LOG" \
       && grep -q '^curl .*http://127\.0\.0\.1:8080/cgi-bin/status\.cgi?part=priority' "$SHIM_LOG" \
       && ! grep -q '127\.0\.0\.1:9999' "$SHIM_LOG"; then
        ok "B1: failure monitor probes login.html and status.cgi on :8080, never :9999"
    else
        bad "B1: monitor probe URLs: $(grep '^curl' "$SHIM_LOG" | sort -u | tr '\n' '|')"
    fi
    if [ "$n_snap" -eq 1 ] && [ -s "$T/fm.snapshot" ]; then
        ok "B2: 6 passes with no state change -> snapshot written once ($n_snap)"
    else
        bad "B2: 6 unchanged passes wrote the snapshot $n_snap time(s), expected 1"
    fi
fi
n_mplc=$(grep -c '^pgrep .*mplc' "$SHIM_LOG")
n_unitp=$(grep -c '^pgrep -x nginx' "$SHIM_LOG")
fm_run 8 7
n_snap=$(grep -c '^tail -n 25' "$SHIM_LOG")
if [ "$n_snap" -eq 2 ] && grep -qx 'nginx=inactive' "$T/fm.snapshot" && grep -q 'unit nginx: active -> inactive' "$T/fm.log"; then
    ok "B3: nginx going down at pass 8 -> snapshot rewritten in that pass (2 writes over 8 passes), carrying nginx=inactive"
else
    bad "B3: state change — snapshot writes=$n_snap, snapshot nginx line: $(grep '^nginx=' "$T/fm.snapshot" 2>/dev/null)"
fi
# H5-01 side pins, measured on the 6-pass run above (mplc4 down throughout):
# ONE pgrep per mplc4 state read (not three), and the snapshot reuses the pass's
# unit states — reads = 6 passes + 1 heartbeat + 1 transition dump = 8 each.
if [ "$n_mplc" -le 8 ] && [ "$n_unitp" -le 8 ]; then
    ok "B2b: 6 passes -> $n_mplc mplc pgrep(s), $n_unitp nginx pgrep(s) (one per pass + heartbeat + dump)"
else
    bad "B2b: 6 passes -> $n_mplc mplc pgrep(s), $n_unitp nginx pgrep(s) — expected <= 8 each"
fi
fi  # run_blackbox

echo
if [ "$fails" = 0 ]; then
    echo "test-userspace-watchdog: all cases passed"
    exit 0
fi
echo "test-userspace-watchdog: $fails case(s) FAILED"
exit 1
