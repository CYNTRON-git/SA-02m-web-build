#!/usr/bin/env bash
# Smoke the agent API on a live board. Not a quality gate.
#   SA02M_API_URL=http://192.168.1.136:9999 SA02M_API_TOKEN=sa02m_... \
#     bash scripts/dev/agent-api-smoke.sh
set -euo pipefail
URL="${SA02M_API_URL:-}"
TOK="${SA02M_API_TOKEN:-}"
if [ -z "$URL" ] || [ -z "$TOK" ]; then
    echo "set SA02M_API_URL and SA02M_API_TOKEN" >&2
    exit 2
fi
curl -fsS "${URL%/}/health"
echo
curl -fsS -H "X-SA02M-Token: $TOK" "${URL%/}/api/v1/openapi.json" | head -c 200
echo
curl -fsS -H "X-SA02M-Token: $TOK" -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    "${URL%/}/mcp" | head -c 200
echo
echo "agent-api-smoke: ok"
