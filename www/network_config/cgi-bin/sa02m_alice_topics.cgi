#!/bin/bash
# SA-02m Alice — MQTT topic inventory for device picker (offline-capable).

set -u

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"

echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

ALICE_ROOT="${SA02M_ALICE_ROOT:-/opt/sa02m-alice}"
if [ ! -d "$ALICE_ROOT/sa02m_alice" ]; then
    _here="$(cd "$(dirname "$0")/../../.." && pwd)"
    if [ -d "$_here/opt/sa02m-alice/sa02m_alice" ]; then
        ALICE_ROOT="$_here/opt/sa02m-alice"
    fi
fi
export PYTHONPATH="$ALICE_ROOT${PYTHONPATH:+:$PYTHONPATH}"

timeout 5 python3 - <<'PY' || echo '{"ok":false,"error":"topics_failed"}'
import json, sys
try:
    from sa02m_alice.config.topics import list_mqtt_topics
    print(json.dumps(list_mqtt_topics(), ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "error": "topics_failed", "message": str(exc)}))
PY
