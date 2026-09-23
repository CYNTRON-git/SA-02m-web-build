#!/usr/bin/env bash
# flasher-auth-header-strip — nginx never forwards a CLIENT-supplied
# `X-SA02M-Auth` to the flasher daemon: both flasher proxy locations overwrite
# it with an empty value.
#
# WHY. opt/sa02m-flasher/sa02m_flasher/service.py `_check_auth` accepts the
# request when `X-SA02M-Auth` equals INTERNAL_TOKEN (/etc/sa02m_flasher.conf) —
# documented as an nginx↔daemon shared secret, i.e. a value the EDGE is meant
# to attach, never the browser. Yet etc/nginx/network_config.conf neither set
# nor stripped it (found by the cloud team's review, 2026-09-23): with
# `proxy_pass` nginx forwards every request header it does not override, so a
# client-supplied value reached the daemon verbatim, and once the cloud proxy
# forwards the whole `X-SA02M-` family (docs/contracts/cloud-panel-proxy.md)
# it would arrive through the tunnel too. Inert today (INTERNAL_TOKEN defaults
# to empty, and `auth_request /_auth_check` gates both locations on the
# session anyway) — the class is a trust header reachable from the client
# side, closed at the edge so it stays inert whatever the config says.
#
# PINS, comment-stripped via lib_check.sh (`#`-commenting a line neither
# satisfies a presence pin nor hides a location):
#   1. etc/nginx/network_config.conf exists and carries >= FLASHER_MIN
#      location blocks whose `proxy_pass` targets the flasher socket
#      (`sa02m-flasher/flasher.sock`) — non-vacuity: a renamed socket or a
#      dropped location FAILS instead of passing on zero blocks.
#   2. EVERY such block carries a live `proxy_set_header X-SA02M-Auth ""` line
#      (any whitespace; the empty-string value is what drops the header —
#      nginx omits a header set to "") — the strip. This is the comment-out
#      case registered in comment-mutation-proof.
#   3. The daemon still reads the header (`self.headers.get("X-SA02M-Auth")` in
#      service.py) — the reason this gate exists; if the daemon stops reading
#      it, retire the gate rather than keep a strip for nothing.
#
# Deliberate NON-flag: a `proxy_set_header X-SA02M-Auth $some_variable` in a
# future location where nginx really is the sender would FAIL pin 2 — by
# design: that location must then carry its own case here, and the value must
# come from an nginx-held file, never from `$http_x_sa02m_auth`.
#
# RED, measured 2026-09-23 on ee27494 (1.0.6.53 before this change): 2 FAIL —
# both flasher blocks (the SSE `~ ^/api/flasher/jobs/[^/]+/events$` and
# `/api/flasher/`) without the strip line; GREEN after the two lines landed.
#
# Run: bash .ai-dev/quality/checks/flasher-auth-header-strip.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

CONF=etc/nginx/network_config.conf
SERVICE=opt/sa02m-flasher/sa02m_flasher/service.py
SOCK='sa02m-flasher/flasher.sock'
STRIP_RE='^[[:space:]]*proxy_set_header[[:space:]]+X-SA02M-Auth[[:space:]]+""[[:space:]]*;'
FLASHER_MIN=2

fails=0
ok()  { printf 'flasher-auth-header-strip: ok    %s\n' "$1"; }
bad() { printf 'flasher-auth-header-strip: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -r "$CONF" ] || { bad "nginx conf missing: $CONF"; echo "flasher-auth-header-strip: $fails FAILED"; exit 1; }
[ -r "$SERVICE" ] || { bad "flasher service missing: $SERVICE"; echo "flasher-auth-header-strip: $fails FAILED"; exit 1; }

# Capture the stripped conf once; every match below is in-shell or a
# here-string (never a producer piped into an early-exit consumer).
text=$(stripped_text "$CONF")
[ -n "$text" ] || { bad "$CONF is empty after comment stripping"; echo "flasher-auth-header-strip: $fails FAILED"; exit 1; }

# Split into `location … { … }` blocks by brace depth; emit one record per
# block that proxies to the flasher socket: `<location line>` US `<has strip>`.
records=$(awk -v sock="$SOCK" -v strip_re="$STRIP_RE" '
  function flush() {
    if (inblk && blk ~ sock) printf "%s\x1f%d\n", head, has_strip
    inblk = 0; depth = 0; blk = ""; head = ""; has_strip = 0
  }
  /^[[:space:]]*location[[:space:]]/ && !inblk { inblk = 1; head = $0; sub(/^[[:space:]]+/, "", head) }
  inblk {
    blk = blk "\n" $0
    if ($0 ~ strip_re) has_strip = 1
    n = gsub(/\{/, "{"); m = gsub(/\}/, "}")
    depth += n - m
    if (depth <= 0 && (n + m) > 0) flush()
  }
  END { flush() }
' <<<"$text")

n_blocks=$(printf '%s\n' "$records" | grep -c $'\x1f')
if [ "$n_blocks" -ge "$FLASHER_MIN" ]; then ok "$n_blocks location block(s) proxy to $SOCK (floor $FLASHER_MIN)"
else bad "only $n_blocks location block(s) proxy to $SOCK (floor $FLASHER_MIN) — the socket moved, a location vanished, or the block parser is dead"; fi

while IFS=$'\x1f' read -r head has; do
  [ -n "$head" ] || continue
  if [ "$has" = 1 ]; then ok "${head%%\{*}— strips the client X-SA02M-Auth (proxy_set_header … \"\")"
  else bad "${head%%\{*}— no live 'proxy_set_header X-SA02M-Auth \"\";' — a client-supplied INTERNAL_TOKEN header reaches the flasher daemon"; fi
done <<<"$records"

if stripped_has "$SERVICE" 'self.headers.get("X-SA02M-Auth")'; then ok "the daemon still reads X-SA02M-Auth ($SERVICE) — the strip is load-bearing"
else bad "$SERVICE no longer reads X-SA02M-Auth — retire this gate (and the nginx strip) rather than keep a check for nothing"; fi

echo
if [ "$fails" -eq 0 ]; then
  echo "flasher-auth-header-strip: ALL OK — nginx drops a client-supplied X-SA02M-Auth on every flasher location"
  exit 0
fi
echo "flasher-auth-header-strip: $fails FAILED"
exit 1
