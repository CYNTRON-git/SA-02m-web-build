#!/bin/bash
# Returns gateway daemon status (from /run/sa02m-gateway/status.json)
# plus systemd service state.

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

STATUS_FILE="/run/sa02m-gateway/status.json"

# systemd service state
svc_active=0
svc_state="inactive"
if systemctl is-active --quiet sa02m-serial-gateway 2>/dev/null; then
    svc_active=1
    svc_state="active"
fi

if [ -f "$STATUS_FILE" ]; then
    python3 - "$STATUS_FILE" "$svc_active" "$svc_state" <<'PYEOF'
import sys, json, time
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    data["service_active"] = (sys.argv[2] == "1")
    data["service_state"]  = sys.argv[3]
    # mark stale if status file is >30s old
    age = time.time() - data.get("timestamp", 0)
    data["stale"] = (age > 30)
    print(json.dumps(data))
except Exception as e:
    print(json.dumps({
        "error": str(e),
        "service_active": (sys.argv[2] == "1"),
        "service_state":  sys.argv[3],
        "ports": {}
    }))
PYEOF
else
    # Daemon not yet started or no status file
    python3 - "$svc_active" "$svc_state" <<'PYEOF'
import sys, json
print(json.dumps({
    "daemon": "sa02m-serial-gateway",
    "service_active": (sys.argv[1] == "1"),
    "service_state":  sys.argv[2],
    "ports": {},
    "stale": True,
}))
PYEOF
fi
