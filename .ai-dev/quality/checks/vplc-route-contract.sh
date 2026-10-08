#!/usr/bin/env bash
# vplc-route-contract — the panel's nginx route to the vPLC page server
# (`location ^~ /vplc/` in etc/nginx/network_config.conf) keeps every directive
# its security rests on.
#
# WHY. The vPLC page server authenticates nobody and is mounted on the panel's
# own origin (docs/threat-model.md §3 «Страница vPLC»). Everything that makes
# that safe lives in this one nginx block: the panel session is checked here,
# the panel cookie never reaches the vPLC process, the vPLC process cannot set
# cookies on the panel origin, the upstream is a unix socket only root can
# create (not a TCP port any local process could squat), and a request other
# than GET/HEAD must carry X-SA02M-CSRF (docs/decisions/selective-csrf-policy.md
# «Демоны»: SameSite=Lax does not stop a page on the board's own IP at another
# port). Dropping any one line silently reopens a hole while the page keeps
# working, and `nginx -t` (which runs only on the board, at install) would not
# notice — it checks syntax, not intent.
#
# PINS, comment-stripped via lib_check.sh (whole-line and trailing ` #`
# comments are blanked, so `#`-commenting a line neither satisfies a pin nor
# hides a block). Lines are compared whitespace-normalised and EXACTLY:
#   1. exactly ONE `location ^~ /vplc/ {` block (non-vacuity: a renamed or
#      dropped location FAILS instead of passing on zero blocks), carrying
#        auth_request /_auth_check;
#        proxy_set_header Cookie "";
#        proxy_hide_header Set-Cookie;
#        proxy_set_header X-Forwarded-Prefix /vplc;
#        proxy_pass http://unix:/run/vplc-plant-ui/ui.sock:/;   (the ONLY
#          proxy_pass in the block — a TCP upstream FAILS)
#        if ($vplc_csrf_bad) { return 403; }   (three consecutive lines)
#   2. exactly ONE `map "$request_method:$http_x_sa02m_csrf" $vplc_csrf_bad {`
#      at brace depth 0 (the http context the site file is included in — a map
#      inside `server {}` is an nginx error), whose body carries
#        "~^(GET|HEAD):" 0;   BEFORE   "~^[^:]+:$" 1;
#      (regex keys are tried in order; the catch-all first would gate GET too).
#   3. exactly ONE `location = /vplc {` block carrying `return 301 vplc/;`.
# Each pin's comment-out case is registered in comment-mutation-proof.
#
# RED, measured 2026-10-07 on the 80947bc4 site file (TCP 8088 upstream, no
# CSRF gate): 5 FAIL (socket proxy_pass, the 403 line, the map head and both
# map entries). On a scratch copy of the GREEN file: RED with each of the 13
# pinned lines commented out, with a second `proxy_pass http://127.0.0.1:8088/`
# added, with the socket swapped for that TCP address, and with the map's
# catch-all moved ahead of the GET/HEAD entry.
#
# DOES NOT prove: that nginx accepts the file (`nginx -t` runs on the board,
# scripts/03-webserver.sh), that the vPLC page really sends X-SA02M-CSRF (a
# seam condition on the vPLC side), or the socket's owner/mode (the vPLC
# team's unit, docs/plans/vplc-integration.md §9.6).
#
# Run: bash .ai-dev/quality/checks/vplc-route-contract.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

CONF=etc/nginx/network_config.conf
V_HEAD='location ^~ /vplc/ {'
M_HEAD='map "$request_method:$http_x_sa02m_csrf" $vplc_csrf_bad {'
R_HEAD='location = /vplc {'
UPSTREAM='proxy_pass http://unix:/run/vplc-plant-ui/ui.sock:/;'

fails=0
ok()  { printf 'vplc-route-contract: ok    %s\n' "$1"; }
bad() { printf 'vplc-route-contract: FAIL  %s\n' "$1"; fails=$((fails + 1)); }
finish() {
  echo
  if [ "$fails" -eq 0 ]; then
    echo "vplc-route-contract: ALL OK — /vplc/ is session-gated, cookie-tight, socket-only and CSRF-gated"
    exit 0
  fi
  echo "vplc-route-contract: $fails FAILED"
  exit 1
}

[ -r "$CONF" ] || { bad "nginx conf missing: $CONF"; finish; }
# Capture once; every match below is in-shell or a here-string (never a
# producer piped into an early-exit consumer — quality-gate-rigor (f)).
text=$(stripped_text_inline "$CONF")
[ -n "$text" ] || { bad "$CONF is empty after comment stripping"; finish; }

# One record per line of interest: `<tag>\t<payload>`.
#   H<x>\t<depth>   a head of block x (V/M/R) seen at that brace depth
#   x\t<line>       a non-empty, whitespace-normalised line inside block x
records=$(awk -v vh="$V_HEAD" -v mh="$M_HEAD" -v rh="$R_HEAD" '
  {
    line = $0
    gsub(/[[:space:]]+/, " ", line); sub(/^ /, "", line); sub(/ $/, "", line)
    if (cur == "") {
      if (line == vh) { cur = "V" } else if (line == mh) { cur = "M" } else if (line == rh) { cur = "R" }
      if (cur != "") { printf "H%s\t%d\n", cur, depth; open_at = depth }
    } else if (line != "") {
      printf "%s\t%s\n", cur, line
    }
    n = gsub(/\{/, "{", line); m = gsub(/\}/, "}", line)
    depth += n - m
    if (cur != "" && depth <= open_at && (n + m) > 0) cur = ""
  }
' <<<"$text")

body() {  # $1 = tag ; prints that block's lines, one per line
  local tag line out=""
  while IFS=$'\t' read -r tag line; do
    [ "$tag" = "$1" ] && out+="$line"$'\n'
  done <<<"$records"
  printf '%s' "$out"
}
heads() {  # $1 = tag ; prints the depth of each head of that block
  local tag d out=""
  while IFS=$'\t' read -r tag d; do
    [ "$tag" = "H$1" ] && out+="$d"$'\n'
  done <<<"$records"
  printf '%s' "$out"
}
count_lines() { [ -n "$1" ] && printf '%s\n' "$1" | wc -l | tr -d ' ' || echo 0; }
has_line() {  # $1 = body, $2 = exact line
  local l
  while IFS= read -r l; do [ "$l" = "$2" ] && return 0; done <<<"$1"
  return 1
}
line_no() {  # $1 = body, $2 = exact line ; prints the 1-based index, empty when absent
  local l i=0
  while IFS= read -r l; do i=$((i + 1)); [ "$l" = "$2" ] && { echo "$i"; return 0; }; done <<<"$1"
}

# ── 1. location ^~ /vplc/ ────────────────────────────────────────────────────
nv=$(count_lines "$(heads V)")
if [ "$nv" = 1 ]; then ok "exactly one '$V_HEAD' block"
else bad "found $nv '$V_HEAD' block(s), want exactly 1 — the route moved, vanished or is doubled (every pin below reads that one block)"; fi
vb=$(body V)
for pin in 'auth_request /_auth_check;' \
           'proxy_set_header Cookie "";' \
           'proxy_hide_header Set-Cookie;' \
           'proxy_set_header X-Forwarded-Prefix /vplc;' \
           "$UPSTREAM"; do
  if has_line "$vb" "$pin"; then ok "/vplc/ carries '$pin'"
  else bad "/vplc/ lacks a live '$pin'"; fi
done
npp=0
while IFS= read -r l; do case "$l" in proxy_pass\ *) npp=$((npp + 1)) ;; esac; done <<<"$vb"
if [ "$npp" = 1 ]; then ok "/vplc/ has exactly one proxy_pass (the vPLC unix socket)"
else bad "/vplc/ has $npp proxy_pass line(s), want exactly 1 — the socket upstream only, never a TCP port"; fi
i=$(line_no "$vb" 'if ($vplc_csrf_bad) {')
if [ -n "$i" ] && [ "$(sed -n "$((i + 1))p" <<<"$vb")" = 'return 403;' ] && [ "$(sed -n "$((i + 2))p" <<<"$vb")" = '}' ]; then
  ok "/vplc/ refuses a non-GET/HEAD request without X-SA02M-CSRF (if (\$vplc_csrf_bad) { return 403; })"
else
  bad "/vplc/ lacks a live 'if (\$vplc_csrf_bad) { return 403; }' — a page on another port of the board could POST as the operator"
fi

# ── 2. the CSRF map, http level ──────────────────────────────────────────────
mh=$(heads M)
nm=$(count_lines "$mh")
if [ "$nm" = 1 ] && [ "$mh" = 0 ]; then ok "exactly one '$M_HEAD' at brace depth 0 (http context)"
else bad "want exactly one '$M_HEAD' at brace depth 0, found $nm at depth(s) [$(printf '%s' "$mh" | tr '\n' ' ')] — without it \$vplc_csrf_bad is undefined and nginx refuses the site"; fi
mb=$(body M)
ig=$(line_no "$mb" '"~^(GET|HEAD):" 0;')
ic=$(line_no "$mb" '"~^[^:]+:$" 1;')
if [ -n "$ic" ]; then ok "the map gates an empty X-SA02M-CSRF ('\"~^[^:]+:\$\" 1;')"
else bad "the map lacks a live '\"~^[^:]+:\$\" 1;' — \$vplc_csrf_bad is never 1, the 403 never fires"; fi
if [ -n "$ig" ] && { [ -z "$ic" ] || [ "$ig" -lt "$ic" ]; }; then ok "the map lets GET/HEAD through ('\"~^(GET|HEAD):\" 0;', before the catch-all)"
else bad "the map lacks a live '\"~^(GET|HEAD):\" 0;' ahead of the catch-all — every GET without the header would be refused"; fi

# ── 3. location = /vplc ──────────────────────────────────────────────────────
nr=$(count_lines "$(heads R)")
rb=$(body R)
if [ "$nr" = 1 ] && has_line "$rb" 'return 301 vplc/;'; then ok "'$R_HEAD' redirects to vplc/ (relative)"
else bad "want exactly one '$R_HEAD' carrying a live 'return 301 vplc/;' (found $nr block(s))"; fi

finish
