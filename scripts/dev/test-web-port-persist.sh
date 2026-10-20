#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-web-port-persist.sh — the panel port chosen by `install.sh --port N`
# survives every later re-render and every update. Quality row
# `web-port-persist`.
#
# The defect it pins (backlog 2026-09-24, audit 2026-09-28 L4): the port was
# rendered into nginx and saved nowhere. scripts/03-webserver.sh and
# scripts/11-devices.sh each defaulted `PORT:=9999`, so the next www-only
# deploy (update-www-only.sh → 11-devices.sh sets no PORT) or `install.sh`
# without --port put a --port board back on 9999; the offline pack deployed a
# site rendered at 9999; and the update runner's health gate probed
# 127.0.0.1:9999 on a board serving N, so every OTA rolled back. SILENT: every
# syntax gate stays green, the board just moves its panel.
#
# Method — the SHIPPED code, run in a sandbox under $T (nothing outside it is
# ever written; TMPDIR is pointed inside it too):
#   A. etc/sa02m-web-port-lib.sh — the grammar (valid/invalid ports), the
#      reader (absent ⇒ 9999 rc 0; invalid ⇒ 9999 rc 3; the file is never
#      sourced — a `$(…)`/`;` payload never runs), the site parse and rewrite.
#   B. scripts/lib.sh sourced with SA02M_WEB_PORT_CONF / SA02M_NGINX_SITE /
#      LOG_FILE retargeted: the core scenario is TWO SEPARATE PROCESSES —
#      `--port 8080` (persist + render), then a plain re-render (what www-only
#      and `install.sh --refresh` do) — and the port must survive. Plus: no
#      --port ever ⇒ 9999 and NO file; an invalid saved value ⇒ WARN + 9999,
#      file untouched; a pre-fix --port board carried over from its live site;
#      an invalid --port refused (rc 2, nothing written); a template with no
#      __PORT__ refused with the live site untouched; atomic writes (no tmp
#      left, mode 0644).
#   C. The wiring, read COMMENT-STRIPPED through lib_check.sh (a `#` on a
#      pinned line is a miss): install.sh persists --port; 03-webserver.sh and
#      11-devices.sh render through the shared renderer and keep no PORT
#      default; the renderer is the ONLY `__PORT__` substitution in install.sh,
#      scripts/*.sh and etc/*.sh (an allow-list sweep — a new renderer anywhere
#      fails here); the lib is installed where the runner reads it (03,
#      update-www-only, the offline deploy-map); the runner fits the package
#      BEFORE apply_deploy_items in cmd_apply.
#   D. The update runner's shipped board_web_port + localize_staged_package_port
#      + health_check, extracted and run under `set -euo pipefail` with a
#      recording curl shim, against a package staged from the PACKER's own
#      render_nginx_template: on a --port board the staged site listens on N
#      (every other byte unchanged) and health_check probes :N; a default-port
#      board, a board with no lib, a non-loopback URL and a site-less manifest
#      are left exactly as they were; an unprovable rewrite fails the apply.
#
# Non-vacuous: a missing file, a function that cannot be sourced or extracted,
# a pin matching no live line, or a shim never invoked FAILS.
#
# Drive-to-failure: WEB_PORT_TREE=<dir> reads every file under test from <dir>
# instead of this checkout (`git archive 23abf686 | tar -x -C <dir>` = the
# pre-fix tree — C and D go RED on the defect, B on the missing renderer).
# Mutations recorded in the commit body; comment-out cases are registered in
# comment-mutation-proof.
#
# Run: bash scripts/dev/test-web-port-persist.sh   (bash, coreutils, python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
REPO=$(pwd)
TREE=${WEB_PORT_TREE:-$REPO}
TREE=$(cd "$TREE" && pwd) || { echo "FAIL  WEB_PORT_TREE not a directory"; exit 1; }

PORT_LIB="$TREE/etc/sa02m-web-port-lib.sh"
LIB_SH="$TREE/scripts/lib.sh"
TPL="$TREE/etc/nginx/network_config.conf"
RUNNER="$TREE/etc/sa02m-update-runner.sh"
PACKER="$TREE/scripts/pack-offline-update.py"
DEPLOY_MAP="$TREE/scripts/offline-update-deploy-map.json"
LIB_CHECK="$REPO/.ai-dev/quality/checks/lib_check.sh"

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/tmp" "$T/bin"
export TMPDIR="$T/tmp"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# shellcheck source=../../.ai-dev/quality/checks/lib_check.sh
. "$LIB_CHECK" || { echo "FAIL  cannot source $LIB_CHECK"; exit 1; }

# python3 shim: CPython on Windows writes CRLF to a text-mode stdout; strip CR,
# keep the exit status. Resolved before the PATH prepend (never itself).
REAL_PY=$(command -v python3) || { echo "FAIL  python3 is required"; exit 1; }
cat > "$T/bin/python3" <<SH
#!/bin/bash
"$REAL_PY" "\$@" | tr -d '\r'
exit "\${PIPESTATUS[0]}"
SH
chmod +x "$T/bin/python3"
# curl shim for D: records the URL (the last argument), answers 200.
cat > "$T/bin/curl" <<'SH'
#!/bin/bash
for a in "$@"; do last=$a; done
printf '%s\n' "$last" >> "$CURL_LOG"
exit 0
SH
chmod +x "$T/bin/curl"
export PATH="$T/bin:$PATH"

IS_WIN=0
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;; esac

for f in "$PORT_LIB" "$LIB_SH" "$TPL" "$RUNNER" "$PACKER" "$DEPLOY_MAP" \
         "$TREE/install.sh" "$TREE/scripts/03-webserver.sh" "$TREE/scripts/11-devices.sh" \
         "$TREE/scripts/update-www-only.sh"; do
    [ -f "$f" ] || bad "file under test missing: ${f#"$TREE"/}"
done
# A missing file FAILS above; the sections still run as far as they can, so a
# drive-to-failure run on the pre-fix tree reports the DEFECT (C, D), not only
# the absence of the new lib.

# The template rendered the way the packer renders it (9999 + the web root) —
# the reference every "site rendered at port N" below is compared with.
render_ref() {  # $1=port → stdout
    sed "s|__PORT__|$1|g; s|__WEB_ROOT__|/var/www/network_config|g" "$TPL"
}
listen_of() {  # $1=file → every `listen N default_server;` port, one per line
    sed -n 's/^[[:space:]]*listen[[:space:]]\{1,\}\([0-9]\{1,\}\)[[:space:]]\{1,\}default_server[[:space:]]*;.*$/\1/p' "$1"
}

echo "── A. etc/sa02m-web-port-lib.sh: grammar, reader, site parse/rewrite ──"
A="$T/a"; mkdir -p "$A"
lib_run() {  # $1=conf path; rest = shell to eval with the lib sourced
    local conf=$1; shift
    # shellcheck disable=SC1090
    ( SA02M_WEB_PORT_CONF=$conf; . "$PORT_LIB" || exit 99; eval "$*" )
}
if ! lib_run "$A/none" 'type sa02m_web_port_valid sa02m_web_port sa02m_web_port_from_site sa02m_web_port_set_listen' >/dev/null 2>&1; then
    bad "A0 the lib does not define its four functions — nothing below can be measured"
else
    ok "A0 the lib sources and defines its four functions"
    bad_valid=""
    for v in 1 80 8080 9999 65535; do
        lib_run "$A/none" "sa02m_web_port_valid '$v'" || bad_valid="$bad_valid $v"
    done
    [ -z "$bad_valid" ] && ok "A1 1/80/8080/9999/65535 are ports" || bad "A1 refused as ports:$bad_valid"
    bad_valid=""
    for v in '' 0 00 080 65536 99999 123456 -1 +80 ' 80' '80 ' 8e3 abc '80;id' '$(id)'; do
        # the value goes in as a word, never through eval text
        # shellcheck disable=SC1090
        ( SA02M_WEB_PORT_CONF="$A/none"; . "$PORT_LIB"; sa02m_web_port_valid "$v" ) && bad_valid="$bad_valid [$v]"
    done
    [ -z "$bad_valid" ] && ok "A2 empty/0/00/080/65536/99999/123456/-1/+80/spaces/8e3/abc/80;id/\$(id) are all refused" \
        || bad "A2 accepted as ports:$bad_valid"

    out=$(lib_run "$A/none" 'sa02m_web_port'); rc=$?
    [ "$out" = 9999 ] && [ "$rc" = 0 ] && ok "A3 no file ⇒ 9999 rc 0 (a board that never used --port)" \
        || bad "A3 no file ⇒ '$out' rc $rc (want 9999 rc 0)"
    printf 'SA02M_WEB_PORT=8080\n' > "$A/c1"
    out=$(lib_run "$A/c1" 'sa02m_web_port'); rc=$?
    [ "$out" = 8080 ] && [ "$rc" = 0 ] && ok "A4 SA02M_WEB_PORT=8080 ⇒ 8080 rc 0" || bad "A4 ⇒ '$out' rc $rc"
    printf '# c\r\nSA02M_WEB_PORT=8080\r\n' > "$A/c2"
    out=$(lib_run "$A/c2" 'sa02m_web_port'); rc=$?
    [ "$out" = 8080 ] && [ "$rc" = 0 ] && ok "A5 a CRLF file with a comment ⇒ 8080" || bad "A5 CRLF ⇒ '$out' rc $rc"
    for case_ in 'SA02M_WEB_PORT=0' 'SA02M_WEB_PORT=70000' 'SA02M_WEB_PORT=' 'OTHER=1' ''; do
        printf '%s\n' "$case_" > "$A/c3"
        out=$(lib_run "$A/c3" 'sa02m_web_port'); rc=$?
        [ "$out" = 9999 ] && [ "$rc" = 3 ] && ok "A6 file «$case_» ⇒ refused: 9999 rc 3" \
            || bad "A6 file «$case_» ⇒ '$out' rc $rc (want 9999 rc 3)"
    done
    printf 'SA02M_WEB_PORT=8080; touch %s/pwned1\nSA02M_WEB_PORT=$(touch %s/pwned2)\n' "$A" "$A" > "$A/c4"
    out=$(lib_run "$A/c4" 'sa02m_web_port'); rc=$?
    if [ "$out" = 9999 ] && [ "$rc" = 3 ] && [ ! -e "$A/pwned1" ] && [ ! -e "$A/pwned2" ]; then
        ok "A7 a shell payload in the file is refused and never executed (the file is read, not sourced)"
    else
        bad "A7 payload file ⇒ '$out' rc $rc, pwned1=$([ -e "$A/pwned1" ] && echo yes || echo no) pwned2=$([ -e "$A/pwned2" ] && echo yes || echo no)"
    fi

    render_ref 9999 > "$A/site9999"
    out=$(lib_run "$A/none" "sa02m_web_port_from_site '$A/site9999'"); rc=$?
    [ "$out" = 9999 ] && [ "$rc" = 0 ] && ok "A8 from_site reads 9999 off the rendered template" || bad "A8 from_site ⇒ '$out' rc $rc"
    grep -v 'default_server' "$A/site9999" > "$A/nolisten"
    lib_run "$A/none" "sa02m_web_port_from_site '$A/nolisten'" >/dev/null && bad "A9 from_site found a port in a site with no listen line" \
        || ok "A9 from_site: no listen … default_server line ⇒ rc 1"
    lib_run "$A/none" "sa02m_web_port_set_listen 8080" < "$A/site9999" > "$A/site8080"
    render_ref 8080 > "$A/ref8080"
    if cmp -s "$A/site8080" "$A/ref8080"; then
        ok "A10 set_listen 8080 on the 9999 render == the template rendered at 8080, byte for byte"
    else
        bad "A10 set_listen output differs from the 8080 render: $(diff "$A/site8080" "$A/ref8080" | head -4 | tr '\n' ' ')"
    fi
    lib_run "$A/none" "sa02m_web_port_set_listen 080" < "$A/site9999" > "$A/x"; rc=$?
    [ "$rc" = 2 ] && [ ! -s "$A/x" ] && ok "A11 set_listen refuses an invalid port (rc 2, nothing written)" \
        || bad "A11 set_listen 080 ⇒ rc $rc, $(wc -c < "$A/x") bytes"
fi

echo "── B. scripts/lib.sh: --port persisted, every re-render keeps it ──"
B="$T/b"
# One installer run = one fresh process that sources the SHIPPED lib.sh with
# the board paths retargeted into the sandbox. Stdout of the eval'd commands is
# kept; the lib's own log lines land in $LOG_FILE.
inst() {  # $1=board dir; rest = shell to eval
    local w=$1; shift
    ( export SA02M_WEB_PORT_CONF="$w/etc/sa02m_web_port.conf"
      export SA02M_NGINX_SITE="$w/etc/nginx/sites-available/network_config"
      export LOG_FILE="$w/install.log"
      # shellcheck disable=SC1090
      . "$LIB_SH" >/dev/null 2>&1 || exit 99
      type sa02m_render_nginx_site sa02m_web_port_persist sa02m_web_port_migrate >/dev/null 2>&1 || exit 98
      eval "$*" )
}
board() {  # $1=name → a fresh board dir
    local w="$B/$1"
    mkdir -p "$w/etc/nginx/sites-available"
    : > "$w/install.log"
    printf '%s\n' "$w"
}
W=$(board b1)
inst "$W" true; rc=$?
if [ "$rc" = 99 ] || [ "$rc" = 98 ]; then
    bad "B0 scripts/lib.sh does not source or lacks the renderer/persist/migrate helpers (rc $rc) — the port has no writer and no single renderer"
else
    ok "B0 scripts/lib.sh sources with the renderer, the writer and the carry-over"
    # 1. install.sh --port 8080: persist, render (one process)
    inst "$W" "sa02m_web_port_persist 8080 && sa02m_render_nginx_site '$TPL' /var/www/network_config" >/dev/null 2>&1; rc=$?
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    [ "$rc" = 0 ] && [ "$got" = 8080 ] && ok "B1 --port 8080: the site listens on 8080" \
        || bad "B1 --port 8080: rc $rc, site listens on '${got:-nothing}'"
    want=$(printf '%s\n' '# SA-02m web panel port (nginx). Written by install.sh --port; read by every nginx' \
        '# re-render and by the update runner. Absent = 9999. Format: etc/sa02m-web-port-lib.sh' 'SA02M_WEB_PORT=8080')
    [ "$(cat "$W/etc/sa02m_web_port.conf" 2>/dev/null)" = "$want" ] && ok "B2 /etc/sa02m_web_port.conf holds SA02M_WEB_PORT=8080 (exact bytes)" \
        || bad "B2 port file: $(cat "$W/etc/sa02m_web_port.conf" 2>/dev/null | tr '\n' '|' || echo absent)"
    # 2. the plain re-render — a SEPARATE process with no PORT (www-only / refresh)
    ( unset PORT; inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" ) >/dev/null 2>&1; rc=$?
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    [ "$rc" = 0 ] && [ "$got" = 8080 ] && ok "B3 plain re-render in a new process (no --port, no PORT): STILL 8080 — the port survives" \
        || bad "B3 plain re-render: rc $rc, site listens on '${got:-nothing}' — the --port choice was lost"
    # the same with a stray PORT in the environment: the saved port still wins
    ( export PORT=9999; inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" ) >/dev/null 2>&1
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    [ "$got" = 8080 ] && ok "B4 a stray PORT=9999 in the environment does not override the saved port" \
        || bad "B4 PORT=9999 in env moved the site to '$got'"
    render_ref 8080 > "$T/ref8080"
    cmp -s "$W/etc/nginx/sites-available/network_config" "$T/ref8080" \
        && ok "B5 the rendered site == the template at 8080, byte for byte" \
        || bad "B5 rendered site differs from the template at 8080"
    left=$(find "$W" -name '*.sa02m-tmp.*' 2>/dev/null)
    [ -z "$left" ] && ok "B6 no atomic-install tmp left behind" || bad "B6 tmp left: $left"
    if [ "$IS_WIN" = 1 ]; then
        echo "skip  B7 file modes (Windows host: POSIX modes not meaningful)"
    else
        m1=$(stat -c '%a' "$W/etc/sa02m_web_port.conf"); m2=$(stat -c '%a' "$W/etc/nginx/sites-available/network_config")
        [ "$m1" = 644 ] && [ "$m2" = 644 ] && ok "B7 port file and site are 0644" || bad "B7 modes: conf=$m1 site=$m2"
    fi
    # idempotent: persisting the same port again writes nothing
    n1=$(grep -c 'сохранён' "$W/install.log")
    inst "$W" "sa02m_web_port_persist 8080" >/dev/null 2>&1; rc=$?
    n2=$(grep -c 'сохранён' "$W/install.log")
    [ "$rc" = 0 ] && [ "$n1" = 1 ] && [ "$n2" = 1 ] && ok "B8 re-running --port 8080 is a no-op (one save logged, not two)" \
        || bad "B8 idempotency: rc $rc, saves logged $n1 then $n2"

    # never --port: two renders, 9999, and NO file
    W=$(board b2)
    inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" >/dev/null 2>&1
    inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" >/dev/null 2>&1; rc=$?
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    [ "$rc" = 0 ] && [ "$got" = 9999 ] && [ ! -e "$W/etc/sa02m_web_port.conf" ] \
        && ok "B9 a board that never used --port: 9999 on every render and NO port file (behaviour unchanged)" \
        || bad "B9 no --port: rc $rc, listen '$got', port file $([ -e "$W/etc/sa02m_web_port.conf" ] && echo PRESENT || echo absent)"

    # an invalid saved value: refused, WARN, default — and the file is left alone
    W=$(board b3)
    printf 'SA02M_WEB_PORT=70000\n' > "$W/etc/sa02m_web_port.conf"; cp "$W/etc/sa02m_web_port.conf" "$T/b3.orig"
    inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" >/dev/null 2>&1; rc=$?
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    if [ "$rc" = 0 ] && [ "$got" = 9999 ] && grep -q '\[WARN\].*SA02M_WEB_PORT' "$W/install.log" \
        && cmp -s "$W/etc/sa02m_web_port.conf" "$T/b3.orig"; then
        ok "B10 SA02M_WEB_PORT=70000 is refused: WARN logged, site on 9999, the file untouched"
    else
        bad "B10 invalid saved port: rc $rc, listen '$got', WARN $(grep -c 'WARN' "$W/install.log"), file $(cmp -s "$W/etc/sa02m_web_port.conf" "$T/b3.orig" && echo same || echo CHANGED)"
    fi

    # carry-over: a board installed with --port 8181 BEFORE the fix — no file, live site on 8181
    W=$(board b4)
    render_ref 8181 > "$W/etc/nginx/sites-available/network_config"
    inst "$W" "sa02m_render_nginx_site '$TPL' /var/www/network_config" >/dev/null 2>&1; rc=$?
    got=$(listen_of "$W/etc/nginx/sites-available/network_config")
    [ "$rc" = 0 ] && [ "$got" = 8181 ] && grep -qx 'SA02M_WEB_PORT=8181' "$W/etc/sa02m_web_port.conf" 2>/dev/null \
        && ok "B11 pre-fix --port board: 8181 carried over from the live site into the port file, re-render keeps 8181" \
        || bad "B11 carry-over: rc $rc, listen '$got', file $(tr '\n' '|' < "$W/etc/sa02m_web_port.conf" 2>/dev/null || echo absent)"
    # a live site on the default port carries nothing over
    W=$(board b5)
    render_ref 9999 > "$W/etc/nginx/sites-available/network_config"
    inst "$W" "sa02m_web_port_migrate" >/dev/null 2>&1
    [ ! -e "$W/etc/sa02m_web_port.conf" ] && ok "B12 a live site on 9999 writes no port file" \
        || bad "B12 a live 9999 site created a port file"
    # an existing file is never overwritten by the carry-over
    W=$(board b6)
    printf 'SA02M_WEB_PORT=8080\n' > "$W/etc/sa02m_web_port.conf"
    render_ref 8181 > "$W/etc/nginx/sites-available/network_config"
    inst "$W" "sa02m_web_port_migrate" >/dev/null 2>&1
    grep -qx 'SA02M_WEB_PORT=8080' "$W/etc/sa02m_web_port.conf" && ok "B13 the carry-over never overwrites an existing port file" \
        || bad "B13 the carry-over rewrote an existing port file"

    # an invalid --port: rc 2, ERR, nothing written — the payload never runs
    W=$(board b7)
    inst "$W" 'sa02m_web_port_persist "80; touch '"$W"'/pwned"' >/dev/null 2>&1; rc=$?
    if [ "$rc" = 2 ] && [ ! -e "$W/etc/sa02m_web_port.conf" ] && [ ! -e "$W/pwned" ] && grep -q '\[ERR\]' "$W/install.log"; then
        ok "B14 --port '80; touch …' is refused: rc 2, ERR logged, nothing written, nothing run"
    else
        bad "B14 invalid --port: rc $rc, file $([ -e "$W/etc/sa02m_web_port.conf" ] && echo WRITTEN || echo absent), pwned $([ -e "$W/pwned" ] && echo YES || echo no)"
    fi

    # a template that lost its placeholder: refused, the live site untouched
    W=$(board b8)
    render_ref 8080 > "$W/etc/nginx/sites-available/network_config"; cp "$W/etc/nginx/sites-available/network_config" "$T/b8.orig"
    printf 'SA02M_WEB_PORT=8080\n' > "$W/etc/sa02m_web_port.conf"
    sed 's/__PORT__/9999/' "$TPL" > "$T/tpl-noplaceholder"
    inst "$W" "sa02m_render_nginx_site '$T/tpl-noplaceholder' /var/www/network_config" >/dev/null 2>&1; rc=$?
    [ "$rc" = 1 ] && cmp -s "$W/etc/nginx/sites-available/network_config" "$T/b8.orig" \
        && ok "B15 a template with no __PORT__ is refused (rc 1) and the live site is left as it was" \
        || bad "B15 template without placeholder: rc $rc, live site $(cmp -s "$W/etc/nginx/sites-available/network_config" "$T/b8.orig" && echo same || echo CHANGED)"
fi

echo "── C. wiring (comment-stripped) ──"
I="$TREE/install.sh"; M3="$TREE/scripts/03-webserver.sh"; M11="$TREE/scripts/11-devices.sh"; WO="$TREE/scripts/update-www-only.sh"
stripped_matches_inline "$I" '^[[:space:]]*--port\)[[:space:]]+_ARG_PORT="\$2"' \
    && ok "C1 install.sh: --port feeds _ARG_PORT" || bad "C1 install.sh: --port does not feed _ARG_PORT"
stripped_has_inline "$I" 'sa02m_web_port_persist "$_ARG_PORT" || exit 2' \
    && ok "C2 install.sh saves --port (and stops on a refused value)" \
    || bad "C2 install.sh does not call sa02m_web_port_persist \"\$_ARG_PORT\" || exit 2 — --port is not saved"
stripped_matches_inline "$I" '^[[:space:]]*(export[[:space:]]+)?PORT="?9999' \
    && bad "C3 install.sh still sets PORT=9999 itself" || ok "C3 install.sh sets no PORT=9999 of its own"
for m in "$M3" "$M11"; do
    n=${m#"$TREE"/}
    stripped_matches_inline "$m" 'sa02m_render_nginx_site +"\$ETC_DIR/nginx/network_config\.conf" +"\$WEB_ROOT"' \
        && ok "C4 $n renders through sa02m_render_nginx_site" || bad "C4 $n does not render through sa02m_render_nginx_site"
    stripped_matches_inline "$m" 'PORT:=' && bad "C5 $n still defaults PORT (the re-render-to-9999 defect)" \
        || ok "C5 $n keeps no PORT default"
    stripped_matches_inline "$m" '>[[:space:]]*/etc/nginx/sites-available/network_config' \
        && bad "C6 $n still writes the live site with a bare redirect" || ok "C6 $n never writes the live site with a bare redirect"
done
# The allow-list sweep: exactly one __PORT__ substitution among the shell homes.
sweep=$(cd "$TREE" && ls install.sh scripts/*.sh etc/*.sh 2>/dev/null)
nsw=$(printf '%s\n' "$sweep" | grep -c .)
hits=""
for f in $sweep; do
    stripped_has_inline "$TREE/$f" '__PORT__' && hits="$hits $f"
done
if [ "$nsw" -lt 20 ]; then
    bad "C7 the renderer sweep saw only $nsw shell files — the glob is dead"
elif [ "$hits" = " scripts/lib.sh" ]; then
    ok "C7 scripts/lib.sh is the ONLY __PORT__ renderer among $nsw shell files"
else
    bad "C7 __PORT__ substitution outside the one renderer:${hits:- none at all}"
fi
stripped_has_inline "$M3" 'sa02m_atomic_install -m 644 "$SCRIPT_DIR/../etc/sa02m-web-port-lib.sh" /usr/local/lib/sa02m-web-port-lib.sh' \
    && ok "C8 03-webserver.sh installs the lib to /usr/local/lib" || bad "C8 03-webserver.sh does not install the lib where the runner reads it"
stripped_has_inline "$WO" 'sa02m_atomic_install -m 644 "$REPO_ETC/sa02m-web-port-lib.sh" /usr/local/lib/sa02m-web-port-lib.sh' \
    && ok "C9 update-www-only.sh installs the lib to /usr/local/lib" || bad "C9 update-www-only.sh does not install the lib"
# The offline pack selects and maps the lib through the PACKER's own functions
# (its allow-list matcher and deploy-map resolver), not a re-implementation.
c10=$(python3 - "$PACKER" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("packer", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
rel = "etc/sa02m-web-port-lib.sh"
sel = any(mod.path_matches(rel, p) for p in mod.load_allowlist_patterns())
ent = mod.map_deploy_entry(rel, mod.load_deploy_map()) or {}
print("%s %s %s" % ("selected" if sel else "NOT-selected", ent.get("dst", "unmapped"), ent.get("mode", "-")))
PY
)
[ "$c10" = "selected /usr/local/lib/sa02m-web-port-lib.sh 0644" ] \
    && ok "C10 the offline pack selects the lib and lands it at /usr/local/lib (0644)" \
    || bad "C10 offline pack for etc/sa02m-web-port-lib.sh: '${c10:-packer did not load}'"
stripped_has_inline "$RUNNER" 'WEB_PORT_LIB="${SA02M_WEB_PORT_LIB:-/usr/local/lib/sa02m-web-port-lib.sh}"' \
    && ok "C11 the runner reads the lib from where the installers put it" \
    || bad "C11 the runner's WEB_PORT_LIB default is not /usr/local/lib/sa02m-web-port-lib.sh"
fit_ln=$(stripped_first_line "$RUNNER" 'if ! localize_staged_package_port "\$txn"; then')
dep_ln=$(stripped_first_line "$RUNNER" 'if ! apply_deploy_items "\$txn"; then')
if [ -n "$fit_ln" ] && [ -n "$dep_ln" ] && [ "$fit_ln" -lt "$dep_ln" ]; then
    ok "C12 runner cmd_apply fits the package to the board port (line $fit_ln) before deploying (line $dep_ln)"
else
    bad "C12 runner: localize_staged_package_port (line ${fit_ln:-none}) must run before apply_deploy_items (line ${dep_ln:-none})"
fi

echo "── D. update runner: the package fitted to the board's port ──"
extract() {  # LITERAL prefix (index==1) — portable across mawk/gawk
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$RUNNER"
}
FN="$T/runner_fn.sh"; : > "$FN"
dfail=0
for g in WEB_PORT_PACKED NGINX_SITE_DST; do
    line=$(grep -m1 "^$g=" "$RUNNER" || true)
    [ -n "$line" ] && printf '%s\n' "$line" >> "$FN" || { bad "D0 runner has no top-level $g="; dfail=1; }
done
for fn in board_web_port localize_staged_package_port health_check; do
    extract "$fn" >> "$FN"
    grep -q "^$fn() {" "$FN" || { bad "D0 could not extract $fn() from the runner"; dfail=1; }
done
if [ "$dfail" = 1 ] && grep -q '^health_check() {' "$FN"; then
    # Drive-to-failure on a runner without the fit: run its health_check anyway
    # (package as packed) so the RED names the defect — the probed port.
    grep -q '^WEB_PORT_PACKED=' "$FN" || printf 'WEB_PORT_PACKED=9999\n' >> "$FN"
    grep -q '^NGINX_SITE_DST=' "$FN" || printf 'NGINX_SITE_DST=/etc/nginx/sites-available/network_config\n' >> "$FN"
    grep -q '^localize_staged_package_port() {' "$FN" \
        || printf 'localize_staged_package_port() { return 0; }  # absent in this runner\n' >> "$FN"
    dfail=0
fi
PACKED=$(sed -n 's/^WEB_PORT_PACKED=//p' "$FN")
if [ "$dfail" = 0 ]; then
    grep -q '# absent in this runner' "$FN" || ok "D0 runner functions extracted (packed port $PACKED)"
    R="$T/r"
    # The staged package, as the offline packer would seal it: the site from
    # the packer's OWN render_nginx_template, the health URL from its manifest.
    mkdir -p "$R/pack/etc/nginx"
    cp "$TPL" "$R/pack/etc/nginx/network_config.conf"
    if ! python3 - "$PACKER" "$R/pack" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("packer", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.render_nginx_template(Path(sys.argv[2]))
PY
    then bad "D1 could not run the packer's render_nginx_template"; fi
    PACKED_SITE="$R/pack/etc/nginx/network_config.conf"
    [ "$(listen_of "$PACKED_SITE")" = "$PACKED" ] && ok "D1 the packer seals the site at the runner's packed port $PACKED" \
        || bad "D1 the packer's site listens on '$(listen_of "$PACKED_SITE")', the runner assumes $PACKED — they must agree"

    # stage <name> <with_site:0|1> <http_url> → sets S (statedir) and a fresh txn t1
    stage() {
        S="$R/$1/state"; rm -rf "${R:?}/$1"; mkdir -p "$S/staging/t1/meta" "$S/staging/t1/overlay/etc/nginx"
        cp "$PACKED_SITE" "$S/staging/t1/overlay/etc/nginx/network_config.conf"
        python3 - "$S/staging/t1/meta/manifest.json" "$2" "$3" "$NGINX_SITE" <<'PY'
import json, sys
mf, with_site, url, site = sys.argv[1:5]
deploy = [{"src": "www/network_config/VERSION", "dst": "/var/www/network_config/VERSION", "mode": "0644", "owner": ""}]
if with_site == "1":
    deploy.append({"src": "etc/nginx/network_config.conf", "dst": site, "mode": "0644", "owner": "root:root"})
m = {"version": "1.0.6.99", "deploy": deploy,
     "services": {"health": {"http_url": url, "units_active": [], "version_file": "/nonexistent/VERSION"}}}
open(mf, "w", encoding="utf-8").write(json.dumps(m, indent=2, sort_keys=True) + "\n")
PY
        cp "$S/staging/t1/meta/manifest.json" "$R/$1/manifest.orig"
    }
    NGINX_SITE=$(sed -n 's/^NGINX_SITE_DST=//p' "$FN")
    # rrun <name> <lib path> <conf content or -> ; runs fit + health_check like cmd_apply
    rrun() {
        local name=$1 lib=$2 conf=$3
        [ "$conf" = - ] || printf '%s\n' "$conf" > "$R/$name/conf"
        : > "$R/$name/curl.log"; : > "$R/$name/runner.log"
        STATEDIR="$S" CURL_LOG="$R/$name/curl.log" RLOG="$R/$name/runner.log" \
        SA02M_WEB_PORT_CONF="$R/$name/conf" WEB_PORT_LIB="$lib" FN="$FN" \
        bash -c 'set -euo pipefail
            log() { printf "%s\n" "$*" >> "$RLOG"; }
            manifest_path() { printf "%s\n" "$STATEDIR/staging/$1/meta/manifest.json"; }
            unit_settled() { return 0; }
            . "$FN"
            APPLY_FAIL_REASON=""; HEALTH_FAIL_REASON=""
            if ! localize_staged_package_port t1; then echo "FIT_FAIL:$APPLY_FAIL_REASON"; exit 3; fi
            health_check t1 || { echo "HEALTH_FAIL:$HEALTH_FAIL_REASON"; exit 4; }'
    }
    SITE_OF() { printf '%s\n' "$S/staging/t1/overlay/etc/nginx/network_config.conf"; }
    URL9="http://127.0.0.1:$PACKED/login.html"

    stage d2 1 "$URL9"; out=$(rrun d2 "$PORT_LIB" 'SA02M_WEB_PORT=8080'); rc=$?
    render_ref 8080 > "$T/ref8080"
    if [ "$rc" = 0 ] && cmp -s "$(SITE_OF)" "$T/ref8080"; then
        ok "D2 --port board (8080): the staged offline site listens on 8080, every other byte as packed"
    else
        bad "D2 --port board: rc $rc ($out), staged site listens on '$(listen_of "$(SITE_OF)")'"
    fi
    probed=$(cat "$R/d2/curl.log")
    [ "$probed" = "http://127.0.0.1:8080/login.html" ] && ok "D3 --port board: health_check probes http://127.0.0.1:8080/login.html (not $PACKED)" \
        || bad "D3 --port board: health_check probed '${probed:-nothing}' — a panel on 8080 fails a probe on $PACKED and the update rolls back"
    out=$(rrun d2 "$PORT_LIB" -); rc=$?
    [ "$rc" = 0 ] && cmp -s "$(SITE_OF)" "$T/ref8080" && [ "$(cat "$R/d2/curl.log")" = "http://127.0.0.1:8080/login.html" ] \
        && ok "D4 fitting again (the re-exec resume path) is idempotent" || bad "D4 second fit: rc $rc ($out), probe '$(cat "$R/d2/curl.log")'"

    stage d5 1 "$URL9"; out=$(rrun d5 "$PORT_LIB" -); rc=$?   # no port file
    [ "$rc" = 0 ] && cmp -s "$(SITE_OF)" "$PACKED_SITE" && cmp -s "$S/staging/t1/meta/manifest.json" "$R/d5/manifest.orig" \
        && [ "$(cat "$R/d5/curl.log")" = "$URL9" ] \
        && ok "D5 default board (no port file): package untouched byte for byte, probe $URL9" \
        || bad "D5 default board: rc $rc ($out), probe '$(cat "$R/d5/curl.log")', site/manifest $(cmp -s "$(SITE_OF)" "$PACKED_SITE" && echo same || echo CHANGED)"

    stage d6 1 "$URL9"; out=$(rrun d6 "$R/no-such-lib.sh" 'SA02M_WEB_PORT=8080'); rc=$?
    [ "$rc" = 0 ] && cmp -s "$(SITE_OF)" "$PACKED_SITE" && [ "$(cat "$R/d6/curl.log")" = "$URL9" ] \
        && ok "D6 a board with no lib installed behaves as before the lib (packed port, package untouched)" \
        || bad "D6 no lib: rc $rc ($out), probe '$(cat "$R/d6/curl.log")'"

    stage d7 1 "$URL9"; out=$(rrun d7 "$PORT_LIB" 'SA02M_WEB_PORT=0'); rc=$?
    [ "$rc" = 0 ] && cmp -s "$(SITE_OF)" "$PACKED_SITE" && [ "$(cat "$R/d7/curl.log")" = "$URL9" ] && grep -q 'WARN' "$R/d7/runner.log" \
        && ok "D7 an invalid saved port: WARN, the packed default used, package untouched" \
        || bad "D7 invalid saved port: rc $rc ($out), probe '$(cat "$R/d7/curl.log")', WARN $(grep -c WARN "$R/d7/runner.log")"

    stage d8 0 "http://10.1.2.3:$PACKED/x"; out=$(rrun d8 "$PORT_LIB" 'SA02M_WEB_PORT=8080'); rc=$?
    [ "$rc" = 0 ] && [ "$(cat "$R/d8/curl.log")" = "http://10.1.2.3:$PACKED/x" ] && cmp -s "$S/staging/t1/meta/manifest.json" "$R/d8/manifest.orig" \
        && ok "D8 a non-loopback URL and a site-less manifest (GitHub OTA shape) are left as named" \
        || bad "D8 non-loopback/site-less: rc $rc ($out), probe '$(cat "$R/d8/curl.log")'"

    stage d9 1 "$URL9"; grep -v 'default_server' "$PACKED_SITE" > "$(SITE_OF)"; cp "$(SITE_OF)" "$T/d9.orig"
    out=$(rrun d9 "$PORT_LIB" 'SA02M_WEB_PORT=8080'); rc=$?
    [ "$rc" = 3 ] && case "$out" in FIT_FAIL:*"8080"*) true ;; *) false ;; esac && cmp -s "$(SITE_OF)" "$T/d9.orig" \
        && [ ! -s "$R/d9/curl.log" ] \
        && ok "D9 a staged site whose listen line cannot be re-pointed FAILS the apply (APPLY_FAIL_REASON set), nothing probed" \
        || bad "D9 unprovable rewrite: rc $rc ($out) — it must stop the apply rather than deploy a 9999 site"
    left=$(find "$R" -name '*.port.*' 2>/dev/null)
    [ -z "$left" ] && ok "D10 no .port. tmp left in staging" || bad "D10 a .port. tmp was left in staging: $left"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "web-port-persist: ALL OK — --port is saved, every re-render and the update runner keep it"
    exit 0
fi
echo "web-port-persist: $fails FAILED"
exit 1
