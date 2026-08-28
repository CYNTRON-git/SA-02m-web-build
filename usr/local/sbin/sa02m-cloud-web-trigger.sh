#!/bin/bash
# SA-02m — privileged helper for cloud.cgi (www-data → root via sudoers)
# Usage:
#   sa02m-cloud-web-trigger.sh pair
#   sa02m-cloud-web-trigger.sh cancel
#   sa02m-cloud-web-trigger.sh token <token> [server_host]
#   sa02m-cloud-web-trigger.sh enable
#   sa02m-cloud-web-trigger.sh disable
#
# SECURITY (audit B1 / finding H2): this helper runs as root with arguments
# www-data chooses, so it validates them ITSELF. It used to build a `sed`
# expression out of the caller's hostname — `sed -i "s|^server_host.*|server_host
# = $SERVER|"` — trusting cloud.cgi's valid_hostname. The sudoers grant makes
# cloud.cgi not the only caller: a `|` in the value closes the s||| expression
# and appends flags, and GNU sed's `e` flag executes the pattern space as a
# shell command while `w <file>` writes it, both as root.
#
# The sudoers verb pin is DEFENCE IN DEPTH, never the fix: `token *` still
# admits an arbitrary argument vector, and a sudo argument `*` is documented to
# match `/` too. What closes the hole is valid_server_host() below plus the
# quoted-heredoc rewriter that treats the value as DATA.
# Gate: .ai-dev/quality/checks/sudoers-pin-contract.sh.
set -euo pipefail

CLOUD_DIR="/etc/sa02m-cloud"
PAIR_FILE="$CLOUD_DIR/pair_request"
TOKEN_FILE="$CLOUD_DIR/activation_token"
CFG="$CLOUD_DIR/agent.conf"
AGENT_UNIT="sa02m-cloud-agent"
FRPC_UNIT="sa02m-cloud-frpc"

mkdir -p "$CLOUD_DIR"
chmod 750 "$CLOUD_DIR"

# A DNS hostname, validated HERE and not by any caller: 1-253 chars, letters,
# digits, dot and hyphen only, no leading or trailing hyphen/dot, no empty
# label. Same character allow-list as valid_hostname in the CGI layer
# (www/network_config/cgi-bin/lib_web_validate.sh) — that one closes the CGI
# path; this one closes the sudo path.
valid_server_host() {
    local h="${1:-}"
    [ -n "$h" ] || return 1
    [ "${#h}" -le 253 ] || return 1
    [[ "$h" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$ ]] || return 1
    return 0
}

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
      if ! valid_server_host "$SERVER"; then
        echo '{"ok":false,"error":"invalid server hostname"}'
        exit 1
      fi
      if [ -f "$CFG" ]; then
        # Rewrite the two keys with the value passed as ENV, not built into a
        # sed script. Atomic (write + os.replace) so a failure never leaves a
        # half-written root config; mode and owner are carried over. Fail-closed:
        # a failed rewrite aborts before the token is armed, rather than starting
        # the agent against a stale server_host.
        if ! SA02M_CFG="$CFG" SA02M_SERVER="$SERVER" python3 - <<'PYCFG'
import os
import re
import tempfile

cfg = os.environ["SA02M_CFG"]
server = os.environ["SA02M_SERVER"]

with open(cfg, encoding="utf-8") as f:
    lines = f.read().splitlines(True)

out = []
for ln in lines:
    if re.match(r"^server_host", ln):
        out.append("server_host = %s\n" % server)
    elif re.match(r"^api_url", ln):
        out.append("api_url = https://%s/api/v1\n" % server)
    else:
        out.append(ln)

st = os.stat(cfg)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg) or ".", prefix=".agent.conf.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(out))
    os.chmod(tmp, st.st_mode & 0o7777)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except (AttributeError, OSError):
        pass
    os.replace(tmp, cfg)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PYCFG
        then
          echo '{"ok":false,"error":"failed to update agent.conf"}'
          exit 1
        fi
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
