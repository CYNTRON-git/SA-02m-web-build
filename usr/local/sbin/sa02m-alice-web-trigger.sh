#!/bin/bash
# Privileged helper for the smart-home CGI (enable/disable the two client
# units: sa02m-alice-client = Yandex profile, sa02m-cloud-control = cloud
# profile; `restart` = make every enabled client serve the current document).
set -euo pipefail

STATUS_FILE=/run/sa02m-alice/status.json
STATUS_FILE_CLOUD=/run/sa02m-alice/status-cloud.json
CLIENT_CONF=/etc/sa02m-alice/sa02m-alice-client.conf
ALICE_UNIT=sa02m-alice-client.service
CLOUD_UNIT=sa02m-cloud-control.service
# Must match STATUS_STALE_S in opt/sa02m-alice/sa02m_alice/common/constants.py
# (3x the client's status heartbeat).
STATUS_STALE_S=90

# Is the client re-reading its device document RIGHT NOW?
#
# Args: [unit] [status-file] — default the Yandex unit and its status file;
# the cloud unit passes its own pair (same binary, same handshake keys).
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
    local unit="${1:-sa02m-alice-client.service}" file="${2:-$STATUS_FILE}"
    # 1. The unit is actually running (stopped/failed/crashed => restart).
    timeout 5 systemctl is-active --quiet "$unit" || return 1

    [ -r "$file" ] || return 1
    local raw ts now age
    raw=$(head -c 4096 "$file" 2>/dev/null) || return 1
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

# Is a client flag set in the shared conf? (`client_enabled` /
# `cloud_control_enabled`.) The CGI already gated on the same grep; re-read
# here so the restart verb touches only a unit the operator has opted into.
conf_flag_true() {
    grep -Eq "^[[:space:]]*$1[[:space:]]*=[[:space:]]*[Tt]rue" "$CLIENT_CONF" 2>/dev/null
}

# enable/disable one unit. Do not force the conf flag here — CGI/Python
# already wrote it. Defensive unmask: cmd_stop's mask SKIPS these units (real
# fragments in /etc/systemd/system — unit_can_mask declines), so this only
# repairs a unit left masked by hand or by legacy states; harmless otherwise.
# Every systemctl is bounded: the CGI calls this trigger SYNCHRONOUSLY inside
# nginx's 20 s fastcgi budget on a shared 8-worker fcgiwrap — an unbounded
# call on a wedged unit/dbus pins workers and 504s the request
# (web-code-rigor "timeouts everywhere" floor).
unit_enable() {
    timeout 10 systemctl unmask "$1" >/dev/null 2>&1 || true
    timeout 10 systemctl enable "$1" >/dev/null 2>&1 || true
    timeout 10 systemctl restart "$1" || true
}

# Stop, then restart: with the flag false the client exits 0 at once, and that
# run is what writes the `disabled` status the card shows.
unit_disable() {
    timeout 10 systemctl stop "$1" >/dev/null 2>&1 || true
    timeout 10 systemctl restart "$1" >/dev/null 2>&1 || true
}

ACTION="${1:-}"
case "$ACTION" in
  enable)
    unit_enable "$ALICE_UNIT"
    echo '{"ok":true,"action":"enable"}'
    ;;
  disable)
    unit_disable "$ALICE_UNIT"
    echo '{"ok":true,"action":"disable"}'
    ;;
  cloud-enable)
    unit_enable "$CLOUD_UNIT"
    echo '{"ok":true,"action":"cloud-enable"}'
    ;;
  cloud-disable)
    unit_disable "$CLOUD_UNIT"
    echo '{"ok":true,"action":"cloud-disable"}'
    ;;
  restart)
    # The verb means "make the running client serve the CURRENT device
    # document" — not "restart the unit". Since 1.0.6.19 the client re-reads
    # the document in place, so a binding edit no longer has to take the
    # Socket.IO session down with the process (a restart cost the account
    # 60-150 s with zero devices). The CGI is unchanged and knows none of
    # this: the decision lives here, where the client and this file ship
    # together in one 06-alice.sh run. Bounded — see unit_enable.
    #
    # TWO CGI call sites send this verb — a binding mutation and the
    # `complete_link` cert nudge. The skip must therefore be safe for both,
    # which is what the state check buys: outside a live session the client
    # is not watching anything, so the restart still happens.
    #
    # Since 1.0.6.26 the same document feeds the cloud-control unit too: it
    # gets the same reload-or-restart decision against ITS status file, but
    # only when its flag is on — a disabled unit is left alone (the Yandex
    # unit keeps its unconditional pre-1.0.6.26 behaviour: the CGI already
    # gates it, and the cert nudge must reach a client in standby).
    if alice_reload_capable; then
        applied=reload
    else
        timeout 10 systemctl restart sa02m-alice-client.service || true
        applied=restart
    fi
    cloud_applied=skipped
    if conf_flag_true cloud_control_enabled; then
        if alice_reload_capable "$CLOUD_UNIT" "$STATUS_FILE_CLOUD"; then
            cloud_applied=reload
        else
            timeout 10 systemctl restart "$CLOUD_UNIT" || true
            cloud_applied=restart
        fi
    fi
    echo "{\"ok\":true,\"action\":\"restart\",\"applied\":\"$applied\",\"cloud_applied\":\"$cloud_applied\"}"
    ;;
  *)
    echo '{"ok":false,"error":"unknown_action"}'
    exit 1
    ;;
esac
