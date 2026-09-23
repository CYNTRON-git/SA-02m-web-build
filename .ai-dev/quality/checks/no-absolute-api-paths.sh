#!/usr/bin/env bash
# no-absolute-api-paths — the served JS names the board's own HTTP surface by
# RELATIVE path only (`api/…`, `cgi-bin/…`, `static/…`), never `/api/…`.
#
# WHY. Behind cloud.cyntron.ru the panel lives under `/devcfg/<id>/`; a
# relative `api/devices` resolves to `/devcfg/<id>/api/devices` and reaches the
# board, while an absolute `/api/devices` resolves against the CLOUD root and
# reaches the board only through the proxy's Referer-based 307 bounce — which a
# Referer-less request (a privacy setting, a prefetch) turns into a 404 (cloud
# `backend/server.py` device_proxy; 2026-09-23). Every other asset and CGI call
# went relative in 1.0.5.43; `devices.js` kept eight absolute `/api/devices…`
# sites — the last family (1.0.6.53, F-B3). The proxy's requirements live in
# docs/contracts/cloud-panel-proxy.md; this row keeps the panel's half true.
#
# PINS, comment-stripped via lib_check.sh (a `//`-commented site is neither a
# hit nor a floor):
#   1. fail-IF-PRESENT sweep: no string literal starting with `/api/`,
#      `/cgi-bin/` or `/static/` (the three prefixes nginx serves for the panel)
#      in any *.js under www/network_config/static/js/ — the addition case: a
#      new absolute site in ANY bundle fails, named with file:line.
#   2. presence floor (non-vacuity + the comment-mutation case): devices.js
#      carries >= DEVICES_MIN_SITES literal `"api/devices` sites — the relative
#      form the fix produced. Commenting those sites out (or a rewrite that
#      drops the calls) turns this RED instead of passing on "nothing absolute
#      left".
#   3. the sweep sees >= JS_MIN_FILES bundles and the root exists — a moved
#      directory FAILS loudly.
#
# Deliberate NON-flags: a `/api/` that is not the start of a string literal (a
# comment, a regex, a doc URL like `https://…/api/`) — the pin anchors on the
# opening quote, so `"https://cloud…/api/v1"` in cloud.js is not a hit.
#
# RED, measured 2026-09-23 on 91157d5 (1.0.6.52): 8 absolute sites in
# devices.js (:583 :598 :862 :871 :1080 :1182 :2378 :2441) and the presence
# floor at 0 — 9 failures; GREEN after the rewrite (8 relative sites).
#
# Run: bash .ai-dev/quality/checks/no-absolute-api-paths.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck source=lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

JS_ROOT=www/network_config/static/js
DEVICES=$JS_ROOT/devices.js
JS_MIN_FILES=5
DEVICES_MIN_SITES=4
# A string literal (", ' or `) opening directly on one of the served prefixes.
ABS_RE='["'"'"'`]/(api|cgi-bin|static)/'

fails=0
ok()   { printf 'no-absolute-api-paths: ok    %s\n' "$1"; }
bad()  { printf 'no-absolute-api-paths: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -d "$JS_ROOT" ] || { bad "served JS root missing: $JS_ROOT"; echo "no-absolute-api-paths: $fails FAILED"; exit 1; }
[ -r "$DEVICES" ] || { bad "devices.js missing: $DEVICES"; echo "no-absolute-api-paths: $fails FAILED"; exit 1; }

# Capture the file list first (never a producer piped into an early-exit
# consumer — quality-gate-rigor.md shape f).
files=$(find "$JS_ROOT" -type f -name '*.js' | sort)
n_files=$(printf '%s\n' "$files" | grep -c .)
if [ "$n_files" -ge "$JS_MIN_FILES" ]; then ok "sweep sees $n_files served JS file(s) under $JS_ROOT (floor $JS_MIN_FILES)"
else bad "sweep sees only $n_files JS file(s) under $JS_ROOT (floor $JS_MIN_FILES) — the tree moved or the sweep is dead"; fi

# Pin 1 — fail-IF-PRESENT: an absolute served prefix opening a string literal.
hits=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  text=$(stripped_text "$f")
  [ -n "$text" ] || continue
  matched=$(grep -nE "$ABS_RE" <<<"$text" || true)
  [ -n "$matched" ] || continue
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    hits=$((hits + 1))
    bad "absolute served path in $f:${line%%:*} — ${line#*:}"
  done <<<"$matched"
done <<<"$files"
[ "$hits" -eq 0 ] && ok "no string literal opens on /api/, /cgi-bin/ or /static/ in the served JS"

# Pin 2 — presence floor: the relative form is really there in devices.js.
sites=$(stripped_count "$DEVICES" '"api/devices')
if [ "$sites" -ge "$DEVICES_MIN_SITES" ]; then ok "devices.js carries $sites relative \"api/devices…\" site(s) (floor $DEVICES_MIN_SITES)"
else bad "devices.js carries only $sites relative \"api/devices…\" site(s) (floor $DEVICES_MIN_SITES) — the calls moved, were commented out, or went absolute"; fi

echo
if [ "$fails" -eq 0 ]; then
  echo "no-absolute-api-paths: ALL OK — the panel reaches its API by relative path only (cloud /devcfg/<id>/ safe)"
  exit 0
fi
echo "no-absolute-api-paths: $fails FAILED"
exit 1
