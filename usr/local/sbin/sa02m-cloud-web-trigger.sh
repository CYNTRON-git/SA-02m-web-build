#!/bin/bash
# SA-02m — privileged helper for cloud.cgi (www-data → root via sudoers)
# Usage:
#   sa02m-cloud-web-trigger.sh pair
#   sa02m-cloud-web-trigger.sh cancel
#   sa02m-cloud-web-trigger.sh token <token> [server_host]
#   sa02m-cloud-web-trigger.sh enable
#   sa02m-cloud-web-trigger.sh disable
set -euo pipefail

CLOUD_DIR="/etc/sa02m-cloud"
PAIR_FILE="$CLOUD_DIR/pair_request"
TOKEN_FILE="$CLOUD_DIR/activation_token"
CFG="$CLOUD_DIR/agent.conf"
AGENT_UNIT="sa02m-cloud-agent"
FRPC_UNIT="sa02m-cloud-frpc"

mkdir -p "$CLOUD_DIR"
chmod 750 "$CLOUD_DIR"

case "${1:-}" in
  pair)
    : > "$PAIR_FILE"
    chmod 600 "$PAIR_FILE"
    systemctl enable "$AGENT_UNIT" 2>/dev/null || true
    systemctl start "$AGENT_UNIT" 2>/dev/null || true
    echo '{"ok":true,"message":"pairing requested"}'
    ;;
  cancel)
    rm -f "$PAIR_FILE"
    echo '{"ok":true,"message":"pairing cancelled"}'
    ;;
  token)
    TOKEN="${2:-}"
    SERVER="${3:-}"
    if [ -z "$TOKEN" ]; then
      echo '{"ok":false,"error":"token is required"}'
      exit 1
    fi
    TOKEN_LEN=${#TOKEN}
    if [ "$TOKEN_LEN" -lt 8 ] || [ "$TOKEN_LEN" -gt 256 ]; then
      echo '{"ok":false,"error":"invalid token format"}'
      exit 1
    fi
    if [ -n "$SERVER" ] && [ "$SERVER" != "cloud.cyntron.ru" ]; then
      # Hostname already validated by caller (cloud.cgi valid_hostname).
      if [ -f "$CFG" ]; then
        sed -i "s|^server_host.*|server_host = $SERVER|" "$CFG" || true
        sed -i "s|^api_url.*|api_url = https://$SERVER/api/v1|" "$CFG" || true
      fi
    fi
    printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    systemctl enable "$AGENT_UNIT" 2>/dev/null || true
    systemctl start "$AGENT_UNIT" 2>/dev/null || true
    echo '{"ok":true,"message":"Activation started. Status will update in ~10 seconds."}'
    ;;
  enable)
    # Re-enable cloud agent: boot + now. Tunnel unit is started by the agent.
    systemctl unmask "$AGENT_UNIT" 2>/dev/null || true
    systemctl enable "$AGENT_UNIT" 2>/dev/null || true
    systemctl start "$AGENT_UNIT" 2>/dev/null || true
    echo '{"ok":true,"message":"agent enabled"}'
    ;;
  disable)
    # Stop tunnel + agent and disable so no boot start / no cloud requests.
    rm -f "$PAIR_FILE"
    systemctl stop "$FRPC_UNIT" 2>/dev/null || true
    systemctl disable --now "$AGENT_UNIT" 2>/dev/null || true
    systemctl stop "$AGENT_UNIT" 2>/dev/null || true
    echo '{"ok":true,"message":"agent disabled"}'
    ;;
  *)
    echo '{"ok":false,"error":"usage: pair|cancel|token|enable|disable"}'
    exit 1
    ;;
esac
