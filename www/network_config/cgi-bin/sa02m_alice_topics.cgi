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

# ?format=inventory → the structured device/channel inventory the binding
# picker groups by COM port; anything else → the flat topic list (the shape
# every pre-1.0.6.38 caller expects). File reads only either way — no gateway
# and no bus is touched — but the inventory answer is ~40 KB and measured
# 1.1-2.4 s on bench 1.135, so the old 5 s left no headroom on a loaded board.
SA02M_TOPICS_FORMAT=flat
case "${QUERY_STRING:-}" in
    *format=inventory*) SA02M_TOPICS_FORMAT=inventory ;;
esac
export SA02M_TOPICS_FORMAT

# Collected, then emitted once. Streaming it straight out with `|| echo` on the
# end appended the fallback AFTER a body that was already complete whenever
# python exited non-zero late (bench 1.135: `...,"count":12}` followed by
# `{"ok":false,...}` — two objects, so the picker's JSON.parse threw and the
# tab fell back to manual entry with every channel on the board in hand).
# A fallback must REPLACE the answer, never trail it.
if TOPICS_JSON="$(timeout 15 python3 - <<'PY'
import json, os
try:
    if os.environ.get("SA02M_TOPICS_FORMAT") == "inventory":
        from sa02m_alice.config.inventory import build_mqtt_inventory
        payload = build_mqtt_inventory()
    else:
        from sa02m_alice.config.topics import list_mqtt_topics
        payload = list_mqtt_topics()
    print(json.dumps(payload, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "error": "topics_failed", "message": str(exc)}))
PY
)" && [ -n "$TOPICS_JSON" ]; then
    printf '%s\n' "$TOPICS_JSON"
else
    echo '{"ok":false,"error":"topics_failed"}'
fi
