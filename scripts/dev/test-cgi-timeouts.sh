#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-cgi-timeouts.sh — every systemctl / `sudo -n` call in the polled and
# status CGIs is bounded in time, and one panel session cannot take the whole
# fcgiwrap pool (1.0.7.36: A8-11, A8-07, A8-12).
#
# (T) Closed list of CGIs (below), text with comments blanked
#     (.ai-dev/quality/checks/lib_check.sh): every line that RUNS `systemctl
#     <verb>` or `sudo -n` must carry `timeout` (or status.cgi's
#     status_timeout_run wrapper) before the call on the same line. The only
#     lines allowed without one are in EXEMPT, each with its reason — the
#     background mutations that already answered `pending`, and the POST
#     mutations whose helper must not be killed half-way (outside A8-11, which
#     is about the polled read paths). An exemption that matches no live line
#     FAILS (stale ledger), and so does a listed file that is missing or holds no
#     such call at all (the sweep would be vacuous).
# (N) nginx (etc/nginx/network_config.conf, comments blanked): the per-session
#     zone `limit_conn_zone $cookie_session_token zone=sa02m_cgi_sess`, its 429
#     status, `limit_conn sa02m_cgi_sess 6` in `location /cgi-bin/` and in every
#     other location that passes to fcgiwrap (the monitor included — its stream
#     holds a worker like any request), plus a SEPARATE zone sa02m_mon_sess with
#     `limit_conn sa02m_mon_sess 1` in mqtt_monitor.cgi only. One zone for both
#     limits FAILS: nginx counts per key per zone, so 1 in the shared zone
#     refuses the monitor while any other request of the session is in flight.
#     What nginx does with it is NOT run here (no nginx in CI) — bench H6.
# (M) mqtt_monitor.cgi execs its stream under `timeout` (60 s life).
#
# RED (2026-11-11, pre-change tree): T lists 12 unbounded lines in 7 files; N
# fails on the missing zone and every missing limit; M fails.
# Shape: (T) is a fail-IF-PRESENT sweep over an allow-list; its non-vacuity
# floors (each file present and holding a call) and (N)/(M) are presence pins
# that a comment-out turns RED — cased in comment-mutation-proof (step C).
#
# Run: bash scripts/dev/test-cgi-timeouts.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }
ROOT=${CGI_TIMEOUTS_ROOT:-.}
CGI=$ROOT/www/network_config/cgi-bin
NGINX=$ROOT/etc/nginx/network_config.conf

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

FILES=(gateway_status.cgi cloud.cgi status.cgi services_ctrl.cgi mplc_project_deploy.cgi cpu_profile.cgi kernel_ctrl.cgi)
# file|literal fragment of the exempt line|reason
EXEMPT=(
  'services_ctrl.cgi|nohup sudo -n "$CTL" "$ACTION" "$SID" "$OP_ID"|background mutation after the pending answer; install runs for minutes, the panel bounds it (600 s)'
  'mplc_project_deploy.cgi|nohup sudo -n "$HELPER" "$STAGED_ZIP"|background deploy after the pending answer; the helper owns its own deadline'
  'cloud.cgi|OUT=$(sudo -n "$CLOUD_TRIGGER" pair|POST mutation (enrollment); killing the trigger half-way is not designed'
  'cloud.cgi|OUT=$(sudo -n "$CLOUD_TRIGGER" cancel|POST mutation'
  'cloud.cgi|OUT=$(sudo -n "$CLOUD_TRIGGER" "$ACTION"|POST mutation'
  'cloud.cgi|OUT=$(sudo -n "$CLOUD_TRIGGER" token|POST mutation'
  'cpu_profile.cgi|OUT=$(sudo -n "$SET" "$PROFILE"|POST mutation (CPU profile write)'
  'cpu_profile.cgi|OUT=$(sudo -n "$CTL" set "$PROFILE"|POST mutation'
  'kernel_ctrl.cgi|OUT=$(sudo -n "$CTL" refresh|POST mutation (zImage refresh)'
  'kernel_ctrl.cgi|OUT=$(sudo -n "$CTL" set "$PROFILE"|POST mutation (kernel swap: a kill half-way is worse than a held handler)'
)

declare -A used=()
loose=""; total=0
for f in "${FILES[@]}"; do
    p="$CGI/$f"
    if [ ! -f "$p" ]; then bad "T  listed CGI is missing: $p"; continue; fi
    n=0
    while IFS= read -r line; do
        [[ "$line" =~ (^|[^A-Za-z0-9_-])(sudo[[:space:]]+-n|systemctl[[:space:]]+[a-z]) ]] || continue
        n=$((n + 1)); total=$((total + 1))
        call=${BASH_REMATCH[2]}
        pre=${line%%"$call"*}
        [[ "$pre" =~ (^|[^A-Za-z0-9_])(timeout|status_timeout_run)[[:space:]] ]] && continue
        exempt=0
        for e in "${EXEMPT[@]}"; do
            ef=${e%%|*}; rest=${e#*|}; frag=${rest%%|*}
            if [ "$ef" = "$f" ] && [[ "$line" == *"$frag"* ]]; then exempt=1; used[$e]=1; break; fi
        done
        [ "$exempt" -eq 1 ] || loose="$loose
  $f: $(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
    done < <(stripped_text "$p")
    [ "$n" -gt 0 ] || bad "T  $f holds no systemctl / sudo -n call at all — the sweep stopped seeing it"
done
if [ -z "$loose" ]; then
    ok "T  all $total systemctl / sudo -n lines in ${#FILES[@]} CGIs are bounded or exempt with a reason"
else
    bad "T  unbounded systemctl / sudo -n calls:$loose"
fi
stale=""
for e in "${EXEMPT[@]}"; do [ -n "${used[$e]:-}" ] || stale="$stale
  $e"; done
[ -z "$stale" ] && ok "T  every exemption still matches a live line" || bad "T  stale exemptions (match no live line):$stale"

# ── (N) nginx per-session limit ────────────────────────────────────────────
if [ ! -f "$NGINX" ]; then
    bad "N  $NGINX missing"
else
    ng=$(stripped_text_inline "$NGINX")
    text_matches "$ng" '^limit_conn_zone[[:space:]]+\$cookie_session_token[[:space:]]+zone=sa02m_cgi_sess:[0-9]+m;' \
        && ok "N  the per-session zone is keyed by the session cookie" \
        || bad "N  no \`limit_conn_zone \$cookie_session_token zone=sa02m_cgi_sess\` at file level"
    text_matches "$ng" '^limit_conn_status[[:space:]]+429;' \
        && ok "N  a refused request answers 429" || bad "N  limit_conn_status 429 missing"
    # The monitor's limit of 1 needs a zone of its own: nginx counts per key per
    # ZONE, so 1 in the shared zone would refuse the monitor whenever any other
    # request of the session is in flight.
    text_matches "$ng" '^limit_conn_zone[[:space:]]+\$cookie_session_token[[:space:]]+zone=sa02m_mon_sess:[0-9]+m;' \
        && ok "N  the monitor has its own per-session zone (sa02m_mon_sess)" \
        || bad "N  no separate \`limit_conn_zone \$cookie_session_token zone=sa02m_mon_sess\` for the monitor"
    # Walk the locations: every /cgi-bin/ fcgiwrap location has sa02m_cgi_sess 6;
    # the monitor additionally sa02m_mon_sess 1; nobody else uses sa02m_mon_sess.
    loc=""; has_pass=0; lim=""; mon=""; nl=0; nbad=""
    check_loc() {
        [ -n "$loc" ] && [ "$has_pass" -eq 1 ] || return 0
        case "$loc" in /cgi-bin/*) ;; *) return 0 ;; esac
        nl=$((nl + 1))
        wantmon=""; [ "$loc" = /cgi-bin/mqtt_monitor.cgi ] && wantmon=1
        [ "$lim" = 6 ] || nbad="$nbad
  location $loc: limit_conn sa02m_cgi_sess '${lim:-none}', want 6"
        [ "$mon" = "$wantmon" ] || nbad="$nbad
  location $loc: limit_conn sa02m_mon_sess '${mon:-none}', want '${wantmon:-none}'"
    }
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*location[[:space:]]+(=[[:space:]]*)?([^[:space:]]+)[[:space:]]*\{ ]]; then
            check_loc; loc=${BASH_REMATCH[2]}; has_pass=0; lim=""; mon=""
        fi
        [[ "$line" == *fastcgi_pass* ]] && has_pass=1
        [[ "$line" =~ ^[[:space:]]*limit_conn[[:space:]]+sa02m_cgi_sess[[:space:]]+([0-9]+)\; ]] && lim=${BASH_REMATCH[1]}
        [[ "$line" =~ ^[[:space:]]*limit_conn[[:space:]]+sa02m_mon_sess[[:space:]]+([0-9]+)\; ]] && mon=${BASH_REMATCH[1]}
    done <<< "$ng"
    check_loc
    if [ "$nl" -lt 10 ]; then
        bad "N  only $nl fcgiwrap locations under /cgi-bin/ found — the walk is vacuous"
    elif [ -n "$nbad" ]; then
        bad "N  locations with a wrong per-session limit:$nbad"
    else
        ok "N  all $nl /cgi-bin/ fcgiwrap locations carry sa02m_cgi_sess 6; mqtt_monitor.cgi also sa02m_mon_sess 1"
    fi
fi

# ── (M) the monitor stream's life ──────────────────────────────────────────
MON=$CGI/mqtt_monitor.cgi
if [ -f "$MON" ] && text_matches "$(stripped_text "$MON")" '^exec timeout -k [0-9]+ [0-9]+ /usr/bin/python3 -u "\$STREAM_PY"'; then
    ok "M  mqtt_monitor.cgi execs the stream under timeout"
else
    bad "M  mqtt_monitor.cgi runs its stream without a time limit"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  cgi-timeouts"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
