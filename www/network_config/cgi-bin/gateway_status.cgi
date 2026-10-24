#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# Returns gateway daemon status (from /run/sa02m-gateway/status.json)
# plus systemd service state.

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

STATUS_FILE="/run/sa02m-gateway/status.json"

svc_state="$(systemctl show -p ActiveState --value sa02m-serial-gateway.service 2>/dev/null)"
python3 - "$STATUS_FILE" "${svc_state:-unknown}" <<'PYEOF'
import json, os, stat, sys, time
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    data = {"daemon": "sa02m-serial-gateway", "ports": {}}
data["service_state"] = sys.argv[2]
data["service_active"] = sys.argv[2] == "active"
data["stale"] = not data["service_active"] or time.time() - data.get("timestamp", 0) > 30
if data["stale"]:
    for port in data.get("ports", {}).values():
        port["running"] = False
try:
    ownership = json.loads(Path("/run/sa02m-port-coordinator/status.json").read_text())
    pid = int(ownership.get("broker_pid", 0))
    proc = Path("/proc/%d/stat" % pid).read_text().split(") ", 1)[1].split()
    ownership["available"] = (proc[0] != "Z" and proc[19] == ownership.get("broker_start_ticks")
        and stat.S_ISSOCK(os.stat("/run/sa02m-port-coordinator/control.sock").st_mode))
except (OSError, ValueError, KeyError, IndexError, TypeError):
    ownership = locals().get("ownership", {})
    ownership["available"] = False
data["port_coordinator"] = ownership
print(json.dumps(data))
PYEOF
