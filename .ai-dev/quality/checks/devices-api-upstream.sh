#!/usr/bin/env bash
# devices-api-upstream — nginx reaches sa02m-devices-api through its AF_UNIX
# socket first, and the three homes of that socket path agree.
#
# WHY. Until 1.0.6.65 the daemon listened on 127.0.0.1:8765 with no auth of its
# own: any local process (mosquitto, nodered, CODESYS, a cmd_exec.cgi shell as
# www-data) read the archive and changed widgets past the panel session
# (backlog «sa02m-devices-api listens on 127.0.0.1:8765 with no auth of its
# own»). The fix moves the daemon behind /run/sa02m-devices/api.sock (0660
# root:www-data — the fs mode is the boundary) with nginx as its only client,
# keeps 127.0.0.1:8765 as the nginx `backup` for bench 1.135 (the stand's
# gunicorn owns the port, no socket exists) and for an OTA-only board (GitHub-OTA
# does not carry the site file — docs/deployment.md), and lets the daemon close
# TCP itself once the LIVE site file names the socket (STAND_API_TCP_COMPAT=auto).
# That last decision reads the exact string this gate pins: change it in one
# home and the daemon closes TCP while nginx still targets it → 502.
#
# PINS, comment-stripped via lib_check.sh (a `#` on a pinned line is a miss):
#   1. etc/nginx/network_config.conf: an `upstream sa02m_devices_api {` block whose
#      FIRST live `server` line is `server unix:/run/sa02m-devices/api.sock;`
#      (primary — no `backup` word) and which carries `server 127.0.0.1:8765 backup;`
#      — the comment-mutation case is the primary line.
#   2. every location block whose head names /api/devices proxies to
#      `http://sa02m_devices_api` and none still proxies to a literal
#      127.0.0.1:8765; >= LOC_MIN such blocks (non-vacuity).
#   3. scripts/11-devices.sh renders the site file BEFORE it captures/applies the
#      devices units (the daemon reads the site file at start to decide TCP
#      compat — rendering after the restart leaves TCP open until the next
#      reboot), and its OK log line names the socket.
#   4. etc/systemd/sa02m-devices-api.service carries RuntimeDirectory=sa02m-devices
#      and STAND_API_SOCKET=/run/sa02m-devices/api.sock.
#   5. opt/sa02m-devices/sa02m_devices/api.py carries the same two literals
#      (SOCKET_PATH_DEFAULT / SOCKET_UPSTREAM_MARK) — the daemon's half of the
#      three-home agreement.
#
# PROVEN RED (2026-09-28, 1.0.6.65) on the 1.0.6.56 tree: no upstream block, both
# locations on the literal port, installer order and log string old, unit and
# daemon without the socket → 9 FAIL; GREEN after the change.
#
# Run: bash .ai-dev/quality/checks/devices-api-upstream.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

CONF=etc/nginx/network_config.conf
INST=scripts/11-devices.sh
UNIT=etc/systemd/sa02m-devices-api.service
API=opt/sa02m-devices/sa02m_devices/api.py
SOCK='/run/sa02m-devices/api.sock'
UP='sa02m_devices_api'
LOC_MIN=2

fails=0
ok()  { printf 'devices-api-upstream: ok    %s\n' "$1"; }
bad() { printf 'devices-api-upstream: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$CONF" "$INST" "$UNIT" "$API"; do
    [ -r "$f" ] || { bad "missing: $f"; }
done
[ "$fails" -eq 0 ] || { echo "devices-api-upstream: $fails FAILED"; exit 1; }

# ── 1. the upstream block ────────────────────────────────────────────────────
conf=$(stripped_text "$CONF")
[ -n "$conf" ] || { bad "$CONF is empty after comment stripping"; echo "devices-api-upstream: $fails FAILED"; exit 1; }
upblock=$(awk -v up="$UP" '
  /^[[:space:]]*upstream[[:space:]]+/ && index($0, up) > 0 { inblk = 1 }
  inblk { print; if ($0 ~ /}/) exit }
' <<<"$conf")
if [ -z "$upblock" ]; then
    bad "$CONF has no live 'upstream $UP {' block — nginx cannot reach the daemon's socket"
else
    servers=$(grep -E '^[[:space:]]*server[[:space:]]' <<<"$upblock")
    first=${servers%%$'\n'*}
    if grep -qE "^[[:space:]]*server[[:space:]]+unix:${SOCK}[[:space:]]*;" <<<"$first"; then
        ok "upstream $UP: primary is unix:$SOCK"
    else
        bad "upstream $UP: the FIRST live server line is not 'server unix:$SOCK;' (got: ${first:-<none>}) — the socket must be primary"
    fi
    if grep -qE '^[[:space:]]*server[[:space:]]+127\.0\.0\.1:8765[[:space:]]+backup[[:space:]]*;' <<<"$upblock"; then
        ok "upstream $UP: 127.0.0.1:8765 is the backup (bench 1.135 / OTA-only boards)"
    else
        bad "upstream $UP: no live 'server 127.0.0.1:8765 backup;' — bench 1.135 and OTA-only boards lose the tab"
    fi
fi

# ── 2. the /api/devices locations ───────────────────────────────────────────
records=$(awk -v up="$UP" '
  function flush() {
    if (inblk && head ~ /\/api\/devices/) printf "%s\x1f%d\x1f%d\n", head, has_up, has_port
    inblk = 0; depth = 0; head = ""; has_up = 0; has_port = 0
  }
  /^[[:space:]]*location[[:space:]]/ && !inblk { inblk = 1; head = $0; sub(/^[[:space:]]+/, "", head) }
  inblk {
    if ($0 ~ /proxy_pass[[:space:]]+http:\/\/sa02m_devices_api[[:space:]]*;/) has_up = 1
    if ($0 ~ /proxy_pass[[:space:]]+http:\/\/127\.0\.0\.1:8765/) has_port = 1
    n = gsub(/\{/, "{"); m = gsub(/\}/, "}")
    depth += n - m
    if (depth <= 0 && (n + m) > 0) flush()
  }
  END { flush() }
' <<<"$conf")
n_loc=$(printf '%s\n' "$records" | grep -c $'\x1f')
if [ "$n_loc" -ge "$LOC_MIN" ]; then ok "$n_loc /api/devices location block(s) (floor $LOC_MIN)"
else bad "only $n_loc /api/devices location block(s) (floor $LOC_MIN) — a location vanished or the block parser is dead"; fi
while IFS=$'\x1f' read -r head has_up has_port; do
    [ -n "$head" ] || continue
    if [ "$has_up" = 1 ] && [ "$has_port" = 0 ]; then ok "${head%%\{*}— proxy_pass http://$UP"
    else bad "${head%%\{*}— must proxy_pass http://$UP (socket-primary), never the literal 127.0.0.1:8765"; fi
done <<<"$records"

# ── 3. installer order + log string ─────────────────────────────────────────
render_ln=$(stripped_first_line "$INST" '> */etc/nginx/sites-available/network_config')
capture_ln=$(stripped_first_line "$INST" 'sa02m_svc_capture sa02m-devices-api\.service')
if [ -n "$render_ln" ] && [ -n "$capture_ln" ] && [ "$render_ln" -lt "$capture_ln" ]; then
    ok "$INST renders nginx (line $render_ln) before the devices units are captured/applied (line $capture_ln)"
else
    bad "$INST: the nginx render (line ${render_ln:-none}) must precede the devices unit capture/apply (line ${capture_ln:-none}) — the daemon reads the site file at start"
fi
if stripped_has "$INST" "nginx: /api/devices* → unix:$SOCK"; then ok "$INST log line names the socket"
else bad "$INST OK log line no longer names unix:$SOCK — the operator log would claim the old port"; fi

# ── 4. the unit ─────────────────────────────────────────────────────────────
if stripped_has_inline "$UNIT" 'RuntimeDirectory=sa02m-devices'; then ok "$UNIT: RuntimeDirectory=sa02m-devices"
else bad "$UNIT lacks RuntimeDirectory=sa02m-devices — /run/sa02m-devices is never created for the socket"; fi
if stripped_has_inline "$UNIT" "STAND_API_SOCKET=$SOCK"; then ok "$UNIT: STAND_API_SOCKET=$SOCK"
else bad "$UNIT lacks Environment=STAND_API_SOCKET=$SOCK"; fi

# ── 5. the daemon's literals ────────────────────────────────────────────────
if stripped_has "$API" "SOCKET_PATH_DEFAULT = \"$SOCK\""; then ok "$API: SOCKET_PATH_DEFAULT = $SOCK"
else bad "$API: SOCKET_PATH_DEFAULT is not \"$SOCK\""; fi
if stripped_has "$API" 'SOCKET_UPSTREAM_MARK = "unix:" + SOCKET_PATH_DEFAULT'; then ok "$API: SOCKET_UPSTREAM_MARK derives from the same literal"
else bad "$API: SOCKET_UPSTREAM_MARK no longer derives from SOCKET_PATH_DEFAULT — the auto-compat sniff can drift from nginx"; fi

echo
if [ "$fails" -eq 0 ]; then
    echo "devices-api-upstream: ALL OK — socket-primary upstream, both locations on it, installer order, unit and daemon agree on $SOCK"
    exit 0
fi
echo "devices-api-upstream: $fails FAILED"
exit 1
