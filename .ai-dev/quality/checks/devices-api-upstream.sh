#!/usr/bin/env bash
# devices-api-upstream — nginx reaches sa02m-devices-api ONLY through its
# AF_UNIX socket, and the three homes of that socket path agree.
#
# WHY. Until 1.0.6.65 the daemon listened on 127.0.0.1:8765 with no auth of its
# own: any local process (mosquitto, nodered, CODESYS, a cmd_exec.cgi shell as
# www-data) read the archive and changed widgets past the panel session
# (backlog «sa02m-devices-api listens on 127.0.0.1:8765 with no auth of its
# own»). Two things close that now: the daemon checks the panel session itself
# (daemon-csrf-behaviour), and nginx talks to it over /run/sa02m-devices/api.sock
# (0660 root:www-data). The socket mode does NOT make nginx the only client —
# every www-data process can connect; the session check is what holds there.
#
# NO TCP SERVER IN THE UPSTREAM — Operator decision 2026-09-29 (review B1). A
# `server 127.0.0.1:8765 backup;` line shipped in 1.0.6.65's first cut: once the
# site file names the socket the daemon frees :8765 on purpose, and nginx then
# forwarded the panel's session cookie, X-SA02M-CSRF and the body to whatever
# local non-root process had bound the free port, every time the socket leg
# failed (each daemon restart / OTA, a crash, an operator stop). The upstream is
# the socket and nothing else; bench 1.135 binds its stand gunicorn to the same
# socket instead (docs/bench-board-target-state.md). An OTA-only board is not
# affected: its old site file still proxies to the literal port, which the
# daemon keeps open for it (STAND_API_TCP_COMPAT=auto), and this gate judges
# the repo's site file, not that board's.
#
# PINS, comment-stripped via lib_check.sh (a `#` on a pinned line is a miss):
#   1. etc/nginx/network_config.conf: an `upstream sa02m_devices_api {` block
#      whose live `server` lines are EXACTLY one — `server unix:/run/sa02m-devices/api.sock;`.
#      Any other live server line (a `backup`, a TCP address, a second socket)
#      FAILS naming it. The comment-mutation case is the socket line.
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
# daemon without the socket → 9 FAIL; GREEN after the change. RED again
# (2026-09-29) on 5d898efa for pin 1's widened half: the shipped
# `server 127.0.0.1:8765 backup;` line FAILS; GREEN once it was removed.
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
    n_servers=$(grep -c . <<<"$servers")
    n_sock=0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if grep -qE "^[[:space:]]*server[[:space:]]+unix:${SOCK}[[:space:]]*;" <<<"$line"; then
            n_sock=$((n_sock + 1))
        else
            bad "upstream $UP: live server line '${line#"${line%%[![:space:]]*}"}' — the upstream must be the socket ONLY (a backup/TCP server hands the panel session to whoever binds that port; Operator decision 2026-09-29)"
        fi
    done <<<"$servers"
    if [ "$n_sock" -eq 1 ] && [ "$n_servers" -eq 1 ]; then
        ok "upstream $UP: exactly one server, unix:$SOCK"
    elif [ "$n_sock" -eq 0 ]; then
        bad "upstream $UP: no live 'server unix:$SOCK;' line — nginx cannot reach the daemon's socket"
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
