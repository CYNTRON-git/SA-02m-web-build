#!/bin/bash
# Privileged helper for Alice CGI (enable/disable client unit).
set -euo pipefail

STATUS_FILE=/run/sa02m-alice/status.json
# Must match STATUS_STALE_S in opt/sa02m-alice/sa02m_alice/common/constants.py
# (3x the client's status heartbeat).
STATUS_STALE_S=90

# Is the client re-reading its device document RIGHT NOW?
#
# The burden of proof is on the SKIP: this helper is choosing not to do
# something that always happened before, so every failure path — unreadable
# file, missing key, wrong state, unparseable ts, no heartbeat, systemctl
# timeout — falls back to the restart (web-code-rigor fail-closed floor).
# FOUR pieces of fresh evidence are required, in cost order.
#
# The capability flag cannot be forged by the web layer: /run/sa02m-alice is
# 0755 root and status.json is written 0644 by the root client. Nothing here
# evaluates the file's contents — two fixed patterns and one integer range.
alice_reload_capable() {
    # 1. The unit is actually running (stopped/failed/crashed => restart).
    timeout 5 systemctl is-active --quiet sa02m-alice-client.service || return 1

    [ -r "$STATUS_FILE" ] || return 1
    local raw ts now age
    raw=$(head -c 4096 "$STATUS_FILE" 2>/dev/null) || return 1
    [ -n "$raw" ] || return 1

    # 2. The capability handshake. An older client never writes this key, and
    #    that is exactly the version-skew case: opt/ and this helper reach a
    #    board only through 06-alice.sh, while the CGI arrives by web update.
    #    Matched in-shell rather than through a pipeline: under `pipefail` a
    #    `grep -q` that exits on the first match can SIGPIPE its writer, and
    #    the flag would then read as absent on a client that has it.
    case "$raw" in
        *'"config_watch": true'*|*'"config_watch":true'*) : ;;
        *) return 1 ;;
    esac

    # 3. The client must be CONNECTED. config_watch is a static property of the
    #    binary — it is written in every state, including the missing-cert
    #    standby and the reconnect backoff, where the document is NOT polled
    #    (the watch lives in the watchdog loop, which runs only while the
    #    Socket.IO session is up). Without this check the helper would also
    #    silence the `complete_link` cert nudge, which shares this one verb:
    #    a client in missing_cert standby would sit at «нет сертификата» until
    #    its own 60 s re-check. `state` answers "is it watching right now?",
    #    which the other three probes cannot.
    case "$raw" in
        *'"state": "connected"'*|*'"state":"connected"'*) : ;;
        *) return 1 ;;
    esac

    # 4. That evidence must be FRESH. A present-but-stale state means "a client
    #    that was connected once and has not ticked since" — alive but not
    #    ticking is precisely what `is-active` cannot see.
    [[ "$raw" =~ \"ts\"[[:space:]]*:[[:space:]]*([0-9]{1,11}) ]] || return 1
    ts="${BASH_REMATCH[1]}"
    now=$(date +%s) || return 1
    case "$now" in ''|*[!0-9]*) return 1 ;; esac
    # A future-dated ts (clock jump) fails too — an unexplained clock is not
    # evidence of liveness.
    [ "$ts" -le "$now" ] || return 1
    age=$(( now - ts ))
    [ "$age" -le "$STATUS_STALE_S" ] || return 1
    return 0
}

ACTION="${1:-}"
case "$ACTION" in
  enable)
    # Do not force client_enabled here — CGI/Python already wrote the conf.
    # Defensive unmask: cmd_stop's mask SKIPS this unit (real fragment in
    # /etc/systemd/system — unit_can_mask declines), so this only repairs a
    # unit left masked by hand or by legacy states; harmless otherwise.
    # Every systemctl is bounded: the CGI calls this trigger SYNCHRONOUSLY
    # inside nginx's 20 s fastcgi budget on a shared 8-worker fcgiwrap — an
    # unbounded call on a wedged unit/dbus pins workers and 504s the request
    # (web-code-rigor "timeouts everywhere" floor).
    timeout 10 systemctl unmask sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl enable sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl restart sa02m-alice-client.service || true
    echo '{"ok":true,"action":"enable"}'
    ;;
  disable)
    timeout 10 systemctl stop sa02m-alice-client.service >/dev/null 2>&1 || true
    timeout 10 systemctl restart sa02m-alice-client.service >/dev/null 2>&1 || true
    echo '{"ok":true,"action":"disable"}'
    ;;
  restart)
    # The verb means "make the running client serve the CURRENT device
    # document" — not "restart the unit". Since 1.0.6.19 the client re-reads
    # the document in place, so a binding edit no longer has to take the
    # Socket.IO session down with the process (a restart cost the account
    # 60-150 s with zero devices). The CGI is unchanged and knows none of
    # this: the decision lives here, where the client and this file ship
    # together in one 06-alice.sh run. Bounded — see enable.
    #
    # TWO CGI call sites send this verb — a binding mutation and the
    # `complete_link` cert nudge. The skip must therefore be safe for both,
    # which is what the state check buys: outside a live session the client
    # is not watching anything, so the restart still happens.
    if alice_reload_capable; then
        echo '{"ok":true,"action":"restart","applied":"reload"}'
    else
        timeout 10 systemctl restart sa02m-alice-client.service || true
        echo '{"ok":true,"action":"restart","applied":"restart"}'
    fi
    ;;
  *)
    echo '{"ok":false,"error":"unknown_action"}'
    exit 1
    ;;
esac
