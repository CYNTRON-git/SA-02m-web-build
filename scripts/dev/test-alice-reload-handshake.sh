#!/usr/bin/env bash
# Gate for the Alice reload handshake (1.0.6.19).
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
#
# `config_watch` is a STRING shared across two languages: the Python client
# writes it into /run/sa02m-alice/status.json, the privileged shell helper
# reads it to decide whether a binding edit still needs a unit restart.
# Renaming it on one side leaves the helper restarting forever — safe, but the
# whole fix would be dead and nothing would say so. Same shape as the repo's
# other cross-file pins (kernel-policy-contract, iface-naming-contract).
#
# Part A — static pins:
#   1. `config_watch` appears in BOTH the client and the helper.
#   2. The helper's `restart` verb still carries the systemctl-restart
#      fallback (deleting the fail-closed path must fail this gate).
#   3. Every systemctl in the helper is timeout-bounded (web-code-rigor
#      "timeouts everywhere" — the CGI calls this synchronously inside
#      nginx's 20 s budget).
# Part B — behavioural: the SHIPPED alice_reload_capable() is extracted and
#   run against a sandboxed status file with a scripted systemctl shim, over
#   the whole fail-closed matrix. A static grep cannot see that the decision
#   is right; this is where "the helper skips a restart it needed to do" —
#   the one genuine regression class — is caught.
#
# No root, no device, no systemd.
set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
HELPER="$HERE/usr/local/sbin/sa02m-alice-web-trigger.sh"
CLIENT="$HERE/opt/sa02m-alice/sa02m_alice/client/main.py"
CONSTANTS="$HERE/opt/sa02m-alice/sa02m_alice/common/constants.py"
fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

for f in "$HELPER" "$CLIENT" "$CONSTANTS"; do
    [ -r "$f" ] || { echo "alice-reload-handshake: cannot read $f"; exit 1; }
done

echo "A. static pins"

# (1) the capability string lives on BOTH sides of the seam
grep -q 'config_watch' "$CLIENT" \
    && ok "(1a) the client writes config_watch" \
    || bad "(1a) config_watch is gone from $CLIENT — the helper would restart forever"
grep -q 'config_watch' "$HELPER" \
    && ok "(1b) the helper reads config_watch" \
    || bad "(1b) config_watch is gone from the helper — the capability gate is dead"

# (2) the restart branch keeps its fail-closed fallback
restart_branch="$(sed -n '/^  restart)/,/^    ;;/p' "$HELPER")"
if [ -z "$restart_branch" ]; then
    bad "(2) the restart branch could not be extracted — helper changed shape"
elif printf '%s\n' "$restart_branch" \
        | grep -Eq 'systemctl restart sa02m-alice-client\.service'; then
    ok "(2) the restart verb still falls back to systemctl restart"
else
    bad "(2) no systemctl restart fallback in the restart branch — a client that cannot reload would never be updated"
fi

# (3) every systemctl call is timeout-bounded (comment lines masked first)
sysctl_lines="$(sed 's/#.*$//' "$HELPER" | grep -n 'systemctl' || true)"
sysctl_count="$(printf '%s\n' "$sysctl_lines" | grep -c 'systemctl' || true)"
if [ "${sysctl_count:-0}" -lt 4 ]; then
    bad "(3) only ${sysctl_count:-0} systemctl call(s) seen — extraction is vacuous"
else
    unbounded="$(printf '%s\n' "$sysctl_lines" | grep -v 'timeout[[:space:]]\+[0-9]\+[[:space:]]\+systemctl' || true)"
    if [ -z "$unbounded" ]; then
        ok "(3) all $sysctl_count systemctl calls are timeout-bounded"
    else
        bad "(3) unbounded systemctl call(s): $unbounded"
    fi
fi

# (4) the decision requires a LIVE session, not merely a capable binary
fn_static="$(sed -n '/^alice_reload_capable() {/,/^}/p' "$HELPER")"
if [ -z "$fn_static" ]; then
    bad "(4) alice_reload_capable() could not be extracted"
elif printf '%s\n' "$fn_static" | grep -q '"state"' \
        && printf '%s\n' "$fn_static" | grep -q 'connected'; then
    ok "(4) the decision also requires state=connected"
else
    bad "(4) no state=connected requirement — config_watch is a property of the BINARY (written in every state), so without this the skip also silences the complete_link cert nudge that shares this verb"
fi

# (5) the stale-window constant agrees across the two languages
py_stale="$(grep -E '^STATUS_STALE_S[[:space:]]*=' "$CONSTANTS" | grep -Eo '[0-9]+' | head -n 1)"
sh_stale="$(grep -E '^STATUS_STALE_S=' "$HELPER" | grep -Eo '[0-9]+' | head -n 1)"
if [ -n "$py_stale" ] && [ "$py_stale" = "$sh_stale" ]; then
    ok "(5) STATUS_STALE_S agrees on both sides ($py_stale s)"
else
    bad "(5) STATUS_STALE_S skew: python='$py_stale' shell='$sh_stale'"
fi

# (6) the state string the helper matches is the one the client writes
py_connected="$(grep -E '^STATE_CONNECTED[[:space:]]*=' "$CONSTANTS" | sed -E 's/.*"([^"]+)".*/\1/')"
if [ -n "$py_connected" ] && printf '%s\n' "$fn_static" | grep -q "\"$py_connected\""; then
    ok "(6) the helper matches the client's STATE_CONNECTED value ('$py_connected')"
else
    bad "(6) the helper does not match STATE_CONNECTED='$py_connected' — the live-session gate would never open"
fi

echo
echo "B. behavioural — the shipped alice_reload_capable() decision"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
fn="$(sed -n '/^alice_reload_capable() {/,/^}/p' "$HELPER")"
if ! printf '%s\n' "$fn" | grep -q 'return 0'; then
    bad "(B) alice_reload_capable() could not be extracted — nothing behavioural was run"
else
    {
        echo 'STATUS_FILE="$SANDBOX/status.json"'
        echo "STATUS_STALE_S=$sh_stale"
        # systemctl/timeout shims: FAKE_ACTIVE drives the is-active answer.
        echo 'systemctl(){ if [ "$1" = "is-active" ]; then return "${FAKE_ACTIVE:-0}"; fi; return 0; }'
        echo 'timeout(){ shift; "$@"; }'
        printf '%s\n' "$fn"
        echo 'if alice_reload_capable; then echo RELOAD; else echo RESTART; fi'
    } > "$SANDBOX/probe.sh"

    NOW="$(date +%s)"
    verdict(){ SANDBOX="$SANDBOX" FAKE_ACTIVE="${2:-0}" bash "$SANDBOX/probe.sh" 2>/dev/null; }
    expect(){ # <label> <expected> <status-json> [fake_active]
        printf '%s' "$3" > "$SANDBOX/status.json"
        got="$(verdict "$1" "${4:-0}")"
        [ "$got" = "$2" ] && ok "(B) $1 -> $got" || bad "(B) $1 -> $got (expected $2)"
    }

    # The only cell that may skip the restart: a live session, on a build that
    # watches, that has ticked recently. Every other cell restarts.
    expect "connected + fresh flag + active unit" RELOAD \
        "{\"state\":\"connected\",\"ts\":$NOW,\"config_watch\": true}"
    expect "same, without json spacing" RELOAD \
        "{\"state\":\"connected\",\"ts\":$NOW,\"config_watch\":true}"
    expect "older client, no flag" RESTART \
        "{\"state\":\"connected\",\"ts\":$NOW}"
    expect "flag explicitly false" RESTART \
        "{\"state\":\"connected\",\"ts\":$NOW,\"config_watch\": false}"
    expect "stale heartbeat" RESTART \
        "{\"state\":\"connected\",\"ts\":$((NOW - sh_stale - 1)),\"config_watch\": true}"
    expect "future-dated ts (clock jump)" RESTART \
        "{\"state\":\"connected\",\"ts\":$((NOW + 60)),\"config_watch\": true}"
    expect "no ts at all" RESTART \
        '{"state":"connected","config_watch": true}'
    expect "unparseable file" RESTART 'not json at all'
    expect "empty file" RESTART ''
    expect "unit not active" RESTART \
        "{\"state\":\"connected\",\"ts\":$NOW,\"config_watch\": true}" 3

    # The `state` dimension. Every one of these is a LIVE process with a fresh
    # heartbeat and the capability flag set — `is-active`, `config_watch` and
    # `ts` all pass — yet the watchdog loop (and with it the document watch) is
    # NOT running, so the restart must still happen. missing_cert is the one
    # that matters most: `complete_link` shares this verb, and skipping there
    # leaves the card at «нет сертификата» until the client's own 60 s
    # re-check.
    for state in missing_cert error offline connecting disabled missing_deps; do
        expect "state=$state (not watching)" RESTART \
            "{\"state\":\"$state\",\"ts\":$NOW,\"config_watch\": true}"
    done
    expect "no state key at all" RESTART \
        "{\"ts\":$NOW,\"config_watch\": true}"
    expect "state value merely contains 'connected'" RESTART \
        "{\"state\":\"disconnected\",\"ts\":$NOW,\"config_watch\": true}"

    rm -f "$SANDBOX/status.json"
    got="$(verdict "absent status file" 0)"
    [ "$got" = "RESTART" ] && ok "(B) absent status file -> RESTART" \
        || bad "(B) absent status file -> $got (expected RESTART)"
fi

echo
[ "$fails" -eq 0 ] && { echo "alice-reload-handshake: ALL OK"; exit 0; }
echo "alice-reload-handshake: $fails FAILURE(S)"; exit 1
