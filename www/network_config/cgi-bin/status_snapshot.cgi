#!/bin/bash
# The request only reads a prebuilt RAM snapshot. No metric collector runs here.
# Bash formats EPOCHREALTIME with the inherited numeric locale. JSON requires a dot.
export LC_ALL=C
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
printf 'Content-Type: application/json; charset=UTF-8\nCache-Control: no-store\n\n'
if ! web_session_check_cookie; then
    printf '{"error":"unauthorized"}\n'
    exit 0
fi
activity_ack=null
if [[ "${REQUEST_METHOD:-}" == GET && "${HTTP_X_SA02M_DASHBOARD:-}" == active ]]; then
    activity_ack=false
    if /usr/bin/python3 -I -S /usr/local/libexec/sa02m-status-cache.py --heartbeat >/dev/null 2>&1; then
        activity_ack=true
    fi
fi
snapshot=/run/sa02m-dashboard/snapshot.json
if [[ ! -f "$snapshot" || -L "$snapshot" ]] || ! { exec 9<"$snapshot"; } 2>/dev/null; then
    printf '{"error":"status_snapshot_unavailable"}\n'
    exit 0
fi
# Pin one inode while the collector atomically replaces the next snapshot.
# A bounded builtin read avoids an interpreter/fork on this hot request path.
payload=''
IFS= read -r -d '' -n 262145 payload <&9 || true
exec 9<&-
payload=${payload%$'\n'}
if (( ${#payload} > 262144 )) || [[ "$payload" != '{"schema":1,'*'}' ]]; then
    printf '{"error":"status_snapshot_invalid"}\n'
    exit 0
fi
read -r served_uptime _ < /proc/uptime
printf '{"served_at":%s,"served_uptime":%s,"activity_ack":%s,"snapshot":%s}\n' "$EPOCHREALTIME" "$served_uptime" "$activity_ack" "$payload"
