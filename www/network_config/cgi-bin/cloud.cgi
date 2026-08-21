#!/bin/bash
# SA-02m Cloud Agent CGI — status & activation via web UI
# GET  → JSON status
# POST {"action":"pair"}    → request a cloud pairing code (primary flow)
# POST {"action":"cancel"}  → cancel a pending pairing
# POST {"action":"enable"}  → enable+start cloud agent
# POST {"action":"disable"} → stop tunnel+agent, disable unit (no internet)
# POST {"token":"..."}      → enroll-token fallback (installers)

STATUS_FILE="/run/sa02m-cloud-status.json"
ACTIVATION_TOKEN_FILE="/etc/sa02m-cloud/activation_token"
CLOUD_TRIGGER="/usr/local/sbin/sa02m-cloud-web-trigger.sh"

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"
# shellcheck source=lib_web_validate.sh
. "$(dirname "$0")/lib_web_validate.sh"

echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

# Auth gate — this endpoint writes an activation token and rewrites the cloud
# agent config as root; it is NOT in the nginx auth_request set, so it must
# guard itself. Fail closed.
if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

read_status() {
    if [ -f "$STATUS_FILE" ]; then
        cat "$STATUS_FILE"
    else
        echo '{"state":"unknown"}'
    fi
}

# ── Cloud reachability probe (cached) ───────────────────────────────────────
# The «Облако»/«Алиса» UI gates its online connect actions on whether the
# device can actually reach the cloud. A bare curl per 5 s status poll would
# fork a network call on every poll on the shared ARM target, so cache the
# verdict ~60 s behind a flock (mirrors status.cgi cache_print_or_build), and
# bound the probe hard (--max-time 5) so a poll never hangs on a wedged WAN.
CLOUD_REACH_HOST="cloud.cyntron.ru"
CLOUD_CACHE_DIR="/tmp/sa02m_cloud_cache"
CLOUD_REACH_CACHE="$CLOUD_CACHE_DIR/reachable"
CLOUD_REACH_TTL="${CLOUD_REACH_TTL:-60}"
CLOUD_REACH_LOCK_WAIT="${CLOUD_REACH_LOCK_WAIT:-0.25}"

cloud_cache_is_fresh() {
    # $1 file, $2 ttl-seconds. Fresh = exists and mtime within ttl (and not in
    # the future — a clock skew must not pin a stale verdict forever).
    local file=$1 ttl=$2 now mtime
    [ -f "$file" ] || return 1
    now=$(date +%s 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    [ "$mtime" -gt "$now" ] && return 1
    [ $((now - mtime)) -lt "$ttl" ]
}

cloud_probe_reachable() {
    # Emits "true"/"false". A response of ANY HTTP status proves the server is
    # reachable, so we do NOT use curl -f (the api base answers only POST — a
    # HEAD to it 404/405s but that still means the WAN + DNS + TLS all worked).
    # Only a network-layer failure (DNS/connect/timeout/TLS) is "unreachable".
    local out lock="${CLOUD_REACH_CACHE}.lock"
    mkdir -p "$CLOUD_CACHE_DIR" 2>/dev/null || true
    if cloud_cache_is_fresh "$CLOUD_REACH_CACHE" "$CLOUD_REACH_TTL"; then
        cat "$CLOUD_REACH_CACHE"
        return 0
    fi
    # Serialise the refresh so concurrent polls don't stampede curl; a poll
    # that can't grab the lock re-probes itself rather than serve stale.
    exec 9>"$lock" 2>/dev/null || true
    if command -v flock >/dev/null 2>&1 && flock -w "$CLOUD_REACH_LOCK_WAIT" 9 >/dev/null 2>&1; then
        if cloud_cache_is_fresh "$CLOUD_REACH_CACHE" "$CLOUD_REACH_TTL"; then
            cat "$CLOUD_REACH_CACHE"
            exec 9>&- 2>/dev/null || true
            return 0
        fi
    fi
    if command -v curl >/dev/null 2>&1 && \
       curl -s -o /dev/null -I --max-time 5 "https://${CLOUD_REACH_HOST}" >/dev/null 2>&1; then
        out=true
    else
        out=false
    fi
    printf '%s' "$out" > "$CLOUD_REACH_CACHE" 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
    printf '%s' "$out"
}

if [ "$REQUEST_METHOD" = "POST" ]; then
    # CSRF guard BEFORE any mutation — every action below runs a privileged
    # sudo trigger. Headers were already emitted above, so validate directly
    # and print the shared error-JSON shape (web_csrf_require would re-emit
    # headers into the body). Mirrors web_update_apply.cgi's CSRF check.
    if ! web_csrf_validate; then
        echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
        exit 0
    fi

    # Read POST body
    read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

    ACTION=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('action',''))" 2>/dev/null)
    TOKEN=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)
    SERVER=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('server','cloud.cyntron.ru'))" 2>/dev/null)

    if [ "$ACTION" = "pair" ]; then
        # Writes under /etc/sa02m-cloud require root (dir is 750 root:root).
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" pair 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ "$ACTION" = "cancel" ]; then
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" cancel 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ "$ACTION" = "enable" ] || [ "$ACTION" = "disable" ]; then
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" "$ACTION" 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ -z "$TOKEN" ]; then
        echo '{"ok":false,"error":"token is required"}'
        exit 0
    fi

    # Validate token format (basic: non-empty, reasonable length)
    TOKEN_LEN=${#TOKEN}
    if [ "$TOKEN_LEN" -lt 8 ] || [ "$TOKEN_LEN" -gt 256 ]; then
        echo '{"ok":false,"error":"invalid token format"}'
        exit 0
    fi

    # Validate hostname BEFORE handing it to the privileged helper (sed).
    if [ -n "$SERVER" ] && [ "$SERVER" != "cloud.cyntron.ru" ]; then
        if ! valid_hostname "$SERVER"; then
            echo '{"ok":false,"error":"invalid server hostname"}'
            exit 0
        fi
    fi

    if ! OUT=$(sudo -n "$CLOUD_TRIGGER" token "$TOKEN" "$SERVER" 2>/dev/null); then
        echo '{"ok":false,"error":"cloud trigger unavailable"}'
        exit 0
    fi
    echo "$OUT"

elif [ "$REQUEST_METHOD" = "GET" ]; then
    STATUS=$(read_status)

    # «Проверить снова» forces a fresh probe by busting the 60 s cache.
    if [[ "${QUERY_STRING:-}" == *recheck=1* ]]; then
        rm -f "$CLOUD_REACH_CACHE" "${CLOUD_REACH_CACHE}.lock" 2>/dev/null || true
    fi
    SERVER_REACHABLE=$(cloud_probe_reachable)
    [ "$SERVER_REACHABLE" = "true" ] || SERVER_REACHABLE=false

    # Append service state (is-active/is-enabled exit ≠0 when inactive/disabled —
    # must not append "unknown" via ||, or we get "inactive\nunknown").
    SVC_STATE=$(systemctl is-active sa02m-cloud-agent 2>/dev/null || true)
    [ -n "$SVC_STATE" ] || SVC_STATE=unknown
    SVC_ENABLED=$(systemctl is-enabled sa02m-cloud-agent 2>/dev/null || true)
    [ -n "$SVC_ENABLED" ] || SVC_ENABLED=unknown
    # www-data cannot look inside /etc/sa02m-cloud (750 root:root).
    HAS_TOKEN=false
    if [ -r "$ACTIVATION_TOKEN_FILE" ]; then
        HAS_TOKEN=true
    fi

    # Merge extra fields into status JSON (env avoids shell-quote traps under fcgiwrap)
    STATUS="$STATUS" SVC_STATE="$SVC_STATE" SVC_ENABLED="$SVC_ENABLED" HAS_TOKEN="$HAS_TOKEN" SERVER_REACHABLE="$SERVER_REACHABLE" python3 -c '
import os, json, sys
try:
    d = json.loads(os.environ.get("STATUS") or "{}")
except Exception:
    d = {"state": "unknown"}
d["service_active"] = os.environ.get("SVC_STATE", "unknown")
d["service_enabled"] = os.environ.get("SVC_ENABLED", "unknown")
d["has_token_file"] = os.environ.get("HAS_TOKEN", "false").lower() == "true"
d["server_reachable"] = os.environ.get("SERVER_REACHABLE", "false").lower() == "true"
print(json.dumps(d))
' 2>/dev/null || echo "$STATUS"

else
    echo '{"ok":false,"error":"method not allowed"}'
fi
