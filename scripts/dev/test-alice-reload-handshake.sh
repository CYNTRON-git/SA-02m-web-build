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
        | grep -Eq 'systemctl restart ("\$ALICE_UNIT"|sa02m-alice-client\.service)'; then
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
echo "C. behavioural — disable writes status.json (never-linked board), both profiles"

# unit_disable + write_disabled_status: a never-started client leaves no
# status file; gold then reads state=unknown. The helper must write
# state=disabled itself so the card is honest without a successful restart —
# for BOTH units (different file, profile and flag key: the card reads the
# flag, so the key is asserted, not just the state), and only as a FALLBACK:
# a file the client wrote during the call is never clobbered, a stale one is.
fn_disable="$(sed -n '/^write_disabled_status() {/,/^}/p' "$HELPER")"
fn_unit="$(sed -n '/^unit_disable() {/,/^}/p' "$HELPER")"
if [ -z "$fn_disable" ] || [ -z "$fn_unit" ]; then
    bad "(C) write_disabled_status/unit_disable could not be extracted"
else
    CBOX="$(mktemp -d)"
    # sandbox_prelude <status-file> — the constants + shims the extracted
    # functions read; logger is captured so a failure LOG can be asserted.
    sandbox_prelude() {
        echo "STATUS_FILE=\"$1\""
        echo "STATUS_FILE_CLOUD=\"$CBOX/status-cloud.json\""
        echo "ALICE_UNIT=sa02m-alice-client.service"
        echo "CLOUD_UNIT=sa02m-cloud-control.service"
        # restart = the client's own run: with CLIENT_WRITES set the shim writes the
        # richer status.json the real client writes when it starts (C3 models the
        # file appearing DURING the call — a pre-written file raced the 1-s mtime
        # guard on a loaded box and made this case flake in the full suite).
        echo 'systemctl(){ if [ "$1" = restart ] && [ -n "${CLIENT_WRITES:-}" ]; then printf '"'"'{"state":"disabled","ts":1,"config_watch": true,"client_enabled":false,"richer":true}
'"'"' > "$STATUS_FILE"; fi; return 0; }'
        echo 'timeout(){ shift; "$@"; }'
        echo "logger(){ printf '%s\\n' \"\$*\" >> \"$CBOX/logger.log\"; }"
        printf '%s\n' "$fn_disable"
        printf '%s\n' "$fn_unit"
    }
    run_disable() {  # run_disable <unit> — runs the SHIPPED unit_disable in the sandbox
        { sandbox_prelude "$CBOX/status.json"; echo "unit_disable \"$1\""; } > "$CBOX/disable.sh"
        bash "$CBOX/disable.sh" >/dev/null 2>&1 || true
    }
    # (C1) Yandex unit: file + state + the flag key the card reads
    rm -f "$CBOX"/status*.json
    run_disable sa02m-alice-client.service
    if [ -f "$CBOX/status.json" ] && grep -q '"state":"disabled"' "$CBOX/status.json" \
       && grep -q '"client_enabled":false' "$CBOX/status.json" \
       && grep -q '"profile":"yandex"' "$CBOX/status.json"; then
        ok "(C1) alice-client disable writes status.json: state=disabled, client_enabled=false, profile=yandex"
    else
        bad "(C1) alice-client disable left no/incomplete status.json: $(cat "$CBOX/status.json" 2>/dev/null)"
    fi
    [ -f "$CBOX/status-cloud.json" ] \
        && bad "(C1) alice-client disable also wrote the CLOUD status file" \
        || ok "(C1) alice-client disable touches only its own status file"
    # (C2) cloud unit: its own file, profile and flag key
    rm -f "$CBOX"/status*.json
    run_disable sa02m-cloud-control.service
    if [ -f "$CBOX/status-cloud.json" ] && grep -q '"state":"disabled"' "$CBOX/status-cloud.json" \
       && grep -q '"cloud_control_enabled":false' "$CBOX/status-cloud.json" \
       && grep -q '"profile":"cloud"' "$CBOX/status-cloud.json"; then
        ok "(C2) cloud-control disable writes status-cloud.json: state=disabled, cloud_control_enabled=false, profile=cloud"
    else
        bad "(C2) cloud-control disable left no/incomplete status-cloud.json: $(cat "$CBOX/status-cloud.json" 2>/dev/null)"
    fi
    [ -f "$CBOX/status.json" ] \
        && bad "(C2) cloud-control disable also wrote the YANDEX status file" \
        || ok "(C2) cloud-control disable touches only its own status file"
    # (C3) a file the CLIENT wrote during the call (fresh mtime) is never clobbered —
    # the shim writes it on `restart`, i.e. after `since` was captured, as the client does
    rm -f "$CBOX"/status*.json
    CLIENT_WRITES=1 run_disable sa02m-alice-client.service
    grep -q '"richer":true' "$CBOX/status.json" \
        && ok "(C3) a status.json fresher than the call (the client's own write) is kept, not clobbered by the skeleton" \
        || bad "(C3) the client's fresher status.json was overwritten by the fallback skeleton"
    # (C4) a STALE file (older than the call) is replaced — the stuck-card case
    rm -f "$CBOX"/status*.json
    printf '{"state":"connected","ts":1,"config_watch": true}\n' > "$CBOX/status.json"
    touch -t 200001010000 "$CBOX/status.json"
    run_disable sa02m-alice-client.service
    grep -q '"state":"disabled"' "$CBOX/status.json" \
        && ok "(C4) a stale pre-existing status.json is replaced with state=disabled" \
        || bad "(C4) stale status.json survived the disable — the card keeps showing 'connected'"
    # (C5) an unwritable target is LOGGED, never silent, and never fails the caller:
    # a DIRECTORY sits where the temp file must go, so printf fails on every OS
    # (a read-only parent would not fail under git-bash on Windows, and a
    # directory at the FINAL path lets mv move the temp file INTO it).
    rm -rf "$CBOX/ro"; mkdir -p "$CBOX/ro/status.json.tmp"
    { sandbox_prelude "$CBOX/ro/status.json"; echo 'unit_disable sa02m-alice-client.service; echo RC=$?'; } > "$CBOX/fail.sh"
    : > "$CBOX/logger.log"
    out="$(bash "$CBOX/fail.sh" 2>/dev/null)"
    if [ "$out" = "RC=0" ] && grep -q 'write_disabled_status' "$CBOX/logger.log"; then
        ok "(C5) a failed status write is logged via logger and the caller still returns 0"
    else
        bad "(C5) failed write: out='$out' logger='$(cat "$CBOX/logger.log" 2>/dev/null)' — silent failure or a failed caller"
    fi
    rm -rf "$CBOX"
fi

echo
[ "$fails" -eq 0 ] && { echo "alice-reload-handshake: ALL OK"; exit 0; }
echo "alice-reload-handshake: $fails FAILURE(S)"; exit 1
