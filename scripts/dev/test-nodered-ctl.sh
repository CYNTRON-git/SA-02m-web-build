#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-nodered-ctl.sh — regression test for the Node-RED install logic in
# etc/sa02m-web-service-ctl.sh (the code behind Управление → Службы → Node-RED).
#
# Why this exists: every defect it pins is SILENT on the device. A payload glob
# that picks the wrong tarball, a half-staged directory treated as a payload, a
# cross-major overwrite that migrates flows irreversibly, and above all a
# runtime judged healthy because port 1880 is open while every flow sits
# stopped — none of them produce a syntax error, a failed unit, or a red gate.
# The panel just shows green. No other row reaches this code.
#
# Method: extract the SHIPPED Node-RED block and run its PURE functions against
# a sandbox — payload resolution through the SA02M_NODERED_DIR seam, the
# major-version guard, the flow-level verdict (journalctl PATH shim), the
# install dispatch, and nodered_enable_start with the mutating helpers stubbed.
# Nothing extracts a tarball, writes to /usr/lib or talks to systemd.
#
# Run: bash scripts/dev/test-nodered-ctl.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=etc/sa02m-web-service-ctl.sh
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"
mkdir -p "$BIN"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract the shipped Node-RED block ─────────────────────────────────────
sed -n '/^# ── Node-RED: staged payload/,/^# ── CODESYS uninstall/p' "$SRC" \
    | sed '$d' > "$T/fn.sh"
for f in nodered_staging_dir nodered_installed_version nodered_global_roots nodered_guard_major_upgrade nodered_payload_top_level_ok \
         nodered_flows_healthy nodered_enable_start nodered_payload_resolve nodered_install; do
    if ! grep -q "^${f}() {" "$T/fn.sh"; then
        echo "FAIL  could not extract ${f}() from $SRC — the block markers moved; fix this harness, do not delete it"
        exit 1
    fi
done
[ "$(wc -l < "$T/fn.sh")" -gt 100 ] || { echo "FAIL  extraction is suspiciously short — check the sed range"; exit 1; }

# ── Stubs the extracted block calls (defined BEFORE any test call) ─────────
LOG="$T/install.log"
: >"$LOG"

emit_result() { printf '%s\n' "$1" >"$T/emit.json"; }
last_emit()   { cat "$T/emit.json" 2>/dev/null; }
clear_emit()  { : >"$T/emit.json"; }

STUB_INTERNET=1          # 0 = reachable
STUB_PORT_LISTENING=1    # 0 = listening
unit_exists()           { [ "$1" = nodered.service ]; }
unit_file_installed()   { [ "$1" = nodered.service ]; }
sc_run()                { printf 'sc_run %s\n' "$*" >>"$LOG"; return 0; }
sc_run_slow()           { printf 'sc_run_slow %s\n' "$*" >>"$LOG"; return 0; }
port_listening()        { return "$STUB_PORT_LISTENING"; }
service_runtime_active() { return 1; }

# `sleep` and `journalctl` as PATH shims — nodered_enable_start polls for the
# flow verdict, and a real 45 s wall clock in a unit test is not a test.
printf '#!/bin/sh\nexit 0\n' > "$BIN/sleep"
cat > "$BIN/journalctl" <<SHIM
#!/bin/sh
cat "$T/journal.txt" 2>/dev/null
exit 0
SHIM
chmod +x "$BIN/sleep" "$BIN/journalctl"
PATH="$BIN:$PATH"
: >"$T/journal.txt"

# shellcheck disable=SC1090
. "$T/fn.sh"

# The ONLY seam for the installed-version reader: the list of global module
# roots. `nodered_installed_version` itself stays SHIPPED code, so the parser,
# the pre-release case and the multi-root scan are all really exercised.
ROOT_A="$T/roots/usr-lib"
ROOT_B="$T/roots/usr-local-lib"
nodered_global_roots() { printf '%s\n' "$ROOT_A" "$ROOT_B"; }

mkinstalled() {                  # mkinstalled <root> <version|RAW:text>   ('' = remove)
    local root=$1 spec=${2:-}
    rm -rf "$root/node-red"
    [ -n "$spec" ] || return 0
    mkdir -p "$root/node-red"
    case "$spec" in
        RAW:*) printf '%s\n' "${spec#RAW:}" >"$root/node-red/package.json" ;;
        *)     printf '{\n  "name": "node-red",\n  "version": "%s"\n}\n' "$spec" >"$root/node-red/package.json" ;;
    esac
}
mkdir -p "$ROOT_A" "$ROOT_B"

# Overrides for the mutating paths — the dispatch test must observe WHICH one
# was chosen, never actually run it.
nodered_internet_reachable() { return "$STUB_INTERNET"; }
nodered_install_offline()    { printf 'offline' >"$T/chosen"; return 0; }
nodered_install_online()     { printf 'online'  >"$T/chosen"; return 0; }

mkpayload() {                    # mkpayload <dir> <file>...
    local d=$1; shift
    rm -rf "$d"; mkdir -p "$d"
    local f
    for f in "$@"; do : >"$d/$f"; done
}

echo "── nodered_payload_resolve ────────────────────────────────────────────"
P="$T/payload"

mkpayload "$P"
SA02M_NODERED_DIR="$P" nodered_payload_resolve \
    && bad "an EMPTY staging dir was accepted as a payload" \
    || ok "empty staging dir is not a payload"

mkpayload "$P" node-red-4.1.13.tar.gz
SA02M_NODERED_DIR="$P" nodered_payload_resolve \
    && bad "a payload with no unit was accepted (half-staged = 'installs' nothing)" \
    || ok "tree without nodered.service is not a payload"

mkpayload "$P" nodered.service
SA02M_NODERED_DIR="$P" nodered_payload_resolve \
    && bad "a payload with no node-red tree was accepted" \
    || ok "unit without the node-red tree is not a payload"

mkpayload "$P" node-red-4.1.13.tar.gz nodered.service
if SA02M_NODERED_DIR="$P" nodered_payload_resolve; then
    ok "tree + unit resolves"
    [ "${_nr_tar##*/}" = node-red-4.1.13.tar.gz ] \
        && ok "  _nr_tar = node-red-4.1.13.tar.gz" \
        || bad "  _nr_tar = ${_nr_tar:-<empty>}"
    # THE regression: `node-*.tar.*` also matches node-red-*.tar.gz. Selecting
    # it as the Node tarball made a Node-less board extract the Node-RED tree
    # into /usr/local with --strip-components=1 and then still fail.
    [ -z "$_nr_node_tar" ] \
        && ok "  the Node-tarball slot stays EMPTY — node-red-*.tar.gz is never mistaken for Node" \
        || bad "  _nr_node_tar picked up ${_nr_node_tar##*/} — the D2 glob regression is back"
else
    bad "tree + unit did not resolve"
fi

mkpayload "$P" node-red-4.1.13.tar.gz nodered.service node-v22.23.0-linux-armv7l.tar.xz
if SA02M_NODERED_DIR="$P" nodered_payload_resolve; then
    [ "${_nr_node_tar##*/}" = node-v22.23.0-linux-armv7l.tar.xz ] \
        && ok "with both tarballs present each lands in its own slot" \
        || bad "node tarball slot = ${_nr_node_tar:-<empty>}"
    [ "${_nr_tar##*/}" = node-red-4.1.13.tar.gz ] \
        && ok "  node-red slot unaffected by the Node tarball" \
        || bad "  _nr_tar = ${_nr_tar:-<empty>}"
else
    bad "full payload did not resolve"
fi

mkpayload "$P" node_modules.tar.gz nodered.service
SA02M_NODERED_DIR="$P" nodered_payload_resolve \
    && bad "a bare node_modules tarball was accepted — the supported shape is a single node-red/ top level" \
    || ok "node_modules*.tar.* is not the supported payload shape"

mkpayload "$P" node-red-4.1.13.tar.gz nodered.service
SA02M_NODERED_DIR="relative/not/absolute" nodered_payload_resolve \
    && bad "a relative SA02M_NODERED_DIR was honoured" \
    || ok "a non-absolute SA02M_NODERED_DIR falls back to the fixed staging path"
[ "$(SA02M_NODERED_DIR=/nonexistent-xyz nodered_staging_dir)" = /opt/vendor-installers/nodered ] \
    && ok "an absent override falls back to /opt/vendor-installers/nodered" \
    || bad "override fallback broken"

echo
echo "── nodered_guard_major_upgrade (F3: no unattended cross-major) ────────"
clear_emit; mkinstalled "$ROOT_A" ''; mkinstalled "$ROOT_B" ''
nodered_guard_major_upgrade \
    && ok "nothing installed ⇒ clean install proceeds (today's behaviour, unchanged)" \
    || bad "a clean install was refused"
[ -z "$(last_emit)" ] && ok "  and emits nothing" || bad "  emitted $(last_emit)"

clear_emit; mkinstalled "$ROOT_A" '4.0.9'
nodered_guard_major_upgrade \
    && ok "same-major update proceeds (4.0.9 → 4.1.13)" \
    || bad "a same-major update was refused"

clear_emit; : >"$LOG"; mkinstalled "$ROOT_A" '3.1.15'
if nodered_guard_major_upgrade; then
    bad "a cross-major overwrite (3.1.15 → 4.1.13) was ALLOWED — flows and credentials migrate irreversibly"
else
    ok "cross-major overwrite refused"
    case "$(last_emit)" in
        *'"error":"major_upgrade_refused"'*'"installed":"3.1.15"'*'"target":"4.1.13"'*)
            ok "  the refusal names both versions" ;;
        *) bad "  refusal JSON = $(last_emit)" ;;
    esac
    grep -q 'what to do instead' "$LOG" \
        && ok "  the log tells a human what the supported path is" \
        || bad "  the refusal gives no next step in $LOG"
fi

# FAIL CLOSED: "I cannot read what is installed" is not "nothing is installed".
# A pre-release version stops the digits-and-dots capture dead, and treating
# that as a clean board would let the irreversible overwrite through.
for spec in 'RAW:{"name":"node-red","version":"5.0.0-beta.1"}' 'RAW:{"name":"node-red"' 'RAW:not json at all'; do
    clear_emit; mkinstalled "$ROOT_A" "$spec"
    if nodered_guard_major_upgrade; then
        bad "an UNREADABLE installed version was treated as a clean board: ${spec#RAW:}"
    else
        case "$(last_emit)" in
            *'"error":"major_upgrade_refused"'*'"installed":"unreadable"'*)
                ok "unreadable installed version ⇒ refused: ${spec#RAW:}" ;;
            *) bad "unreadable-version JSON = $(last_emit)" ;;
        esac
    fi
done

# The SECOND global root is weighed too: after the runbook's Node-22 step a
# board has /usr/lib/node_modules AND /usr/local/lib/node_modules, and a 3.x in
# the one we did not look at is exactly what must block.
clear_emit; mkinstalled "$ROOT_A" ''; mkinstalled "$ROOT_B" '3.1.15'
if nodered_guard_major_upgrade; then
    bad "a cross-major install in the second global root was missed"
else
    case "$(last_emit)" in
        *'"installed":"3.1.15"'*) ok "a 3.x in the SECOND global root still blocks (npm root -g divergence)" ;;
        *) bad "second-root JSON = $(last_emit)" ;;
    esac
fi
clear_emit; mkinstalled "$ROOT_A" '4.1.13'; mkinstalled "$ROOT_B" '3.1.15'
nodered_guard_major_upgrade \
    && bad "a 4.x in the first root masked a 3.x in the second — the guard stopped at the first hit" \
    || ok "a readable 4.x does not mask a 3.x in another root"
mkinstalled "$ROOT_A" ''; mkinstalled "$ROOT_B" ''

echo
echo "── the guard is WIRED into both install paths ─────────────────────────"
# The behavioural tests above call the guard directly, so they cannot see it
# being unwired from a caller. Assert the call sites statically — an unwired
# guard is exactly how this fix would silently disappear.
fnbody() { awk -v f="$1" 'index($0, f"() {")==1 {p=1} p; p && /^}$/ {exit}' "$T/fn.sh"; }
# These are SOURCE-TEXT pins on an extracted function body, so they must be
# COMMENT-BLIND: a `#` in front of a call leaves the text in the body and a raw
# `grep -q` counted a disabled call site as a live one (shape (a), found by the
# coverage sweep of review Q6, 1.0.6.24). Capturing then matching in-shell also
# retires the `producer | grep -q` early-exit pipe (shape (f)). lib_check.sh is
# the one home for both; an unavailable lib FAILS rather than skipping.
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F text_has >/dev/null; then
    body_has() { local _t; _t=$(strip_comments <<<"$1"); text_has "$_t" "$2"; }
else
    echo "FAIL  .ai-dev/quality/checks/lib_check.sh could not be sourced — the static wiring pins would be comment-blind; not skipping them"
    exit 1
fi
for caller in nodered_install_offline nodered_install_online; do
    body=$(fnbody "$caller")
    [ -n "$body" ] || { bad "could not read $caller() out of the extraction"; continue; }
    body_has "$body" 'nodered_guard_major_upgrade' \
        && ok "$caller() calls nodered_guard_major_upgrade" \
        || bad "$caller() no longer calls nodered_guard_major_upgrade — a cross-major overwrite would go through unattended"
done
# Same class: the tree is replaced, not merged, and the unit is stopped first.
body=$(fnbody nodered_install_offline)
body_has "$body" 'rm -rf /usr/lib/node_modules/node-red' \
    && ok "nodered_install_offline() removes the old tree before extracting (tar merges, it never deletes)" \
    || bad "nodered_install_offline() extracts over the existing tree again — the 3.x-leftovers franken-tree is back"
body_has "$body" 'sc_run_slow stop' \
    && ok "nodered_install_offline() stops the unit before touching the tree" \
    || bad "nodered_install_offline() replaces the tree under a running unit"
body_has "$body" 'nodered_payload_top_level_ok' \
    && ok "nodered_install_offline() asserts a single node-red/ top level before a root extraction" \
    || bad "the payload top-level assertion is gone — a root tar into /usr/lib with no shape check"
# nodered_global_roots is THIS harness's sandbox seam, so no behavioural case
# can see the shipped root list. Pin it statically instead — dropping a root is
# how a cross-major install hides from the guard on a board that carries two.
roots=$(fnbody nodered_global_roots)
for want in '/usr/lib/node_modules' '/usr/local/lib/node_modules' 'npm root -g'; do
    body_has "$roots" "$want" \
        && ok "nodered_global_roots() still consults $want" \
        || bad "nodered_global_roots() no longer consults $want — an install there would be invisible to the F3 guard"
done
# The empty-first-component mapping is the load-bearing half of the absolute-path
# defence: filtering empties out (the original `grep -v '^$'`) is exactly how an
# absolute member rides along beside a valid node-red/.
body_has "$(fnbody nodered_payload_top_level_ok)" "grep -v '^\$'" \
    && bad "nodered_payload_top_level_ok() filters empty components again — absolute-path members stop being seen" \
    || ok "nodered_payload_top_level_ok() does not discard empty path components"

echo
echo "── nodered_payload_top_level_ok (guards a root tar into /usr/lib) ─────"
A="$T/arch"; rm -rf "$A"; mkdir -p "$A/build/node-red/lib" "$A/build/other" "$A/abs/etc"
: >"$A/build/node-red/red.js"; : >"$A/build/node-red/lib/x.js"
: >"$A/build/other/pkg.js";    printf 'root:x:0:0\n' >"$A/abs/etc/passwd"
( cd "$A/build" && tar -czf "$A/good.tar.gz" node-red ) 2>/dev/null
( cd "$A/build" && tar -czf "$A/sibling.tar.gz" node-red other ) 2>/dev/null
( cd "$A/build" && tar -czf "$A/traversal.tar.gz" node-red -C "$A" --transform 's#^abs/etc/passwd#../evil#' abs/etc/passwd ) 2>/dev/null \
    || ( cd "$A/build" && tar -czf "$A/traversal.tar.gz" node-red ../abs 2>/dev/null )
( cd "$A/build" && tar -czPf "$A/absolute.tar.gz" node-red "$A/abs/etc/passwd" ) 2>/dev/null
printf 'not a tar at all\n' >"$A/garbage.tar.gz"

nodered_payload_top_level_ok "$A/good.tar.gz" \
    && ok "a clean single-top-level node-red/ archive passes" \
    || bad "the real payload shape was rejected"
nodered_payload_top_level_ok "$A/sibling.tar.gz" \
    && bad "an archive with a sibling top-level dir passed — it would splatter into /usr/lib/node_modules" \
    || ok "a sibling top-level directory is rejected"
if tar -tf "$A/absolute.tar.gz" 2>/dev/null | grep -q '^/'; then
    nodered_payload_top_level_ok "$A/absolute.tar.gz" \
        && bad "an ABSOLUTE-path member rode along beside node-red/ — the empty first component was dropped instead of flagged" \
        || ok "an absolute-path member is rejected (the empty-component case)"
else
    bad "could not build an absolute-path fixture — this tar lacks -P; the absolute case went unchecked"
fi
if tar -tf "$A/traversal.tar.gz" 2>/dev/null | grep -q '\.\.'; then
    nodered_payload_top_level_ok "$A/traversal.tar.gz" \
        && bad "a ../ traversal member passed" \
        || ok "a ../ traversal member is rejected"
else
    bad "could not build a traversal fixture — the traversal case went unchecked"
fi
nodered_payload_top_level_ok "$A/garbage.tar.gz" \
    && bad "an unreadable archive passed the shape check" \
    || ok "an unreadable archive is rejected (empty extraction never passes)"
nodered_payload_top_level_ok "$A/does-not-exist.tar.gz" \
    && bad "a missing archive passed the shape check" \
    || ok "a missing archive is rejected"

echo "── nodered_flows_healthy — ENGLISH journal ────────────────────────────"
printf '%s\n' '6 Aug 07:37:32 - [info] Started flows' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 0 ] && ok "'Started flows' ⇒ healthy (0)" || bad "healthy journal returned $rc"

printf '%s\n' '6 Aug 07:37:32 - [info] Waiting for missing types to be registered:' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 1 ] && ok "missing node types ⇒ unhealthy (1)" || bad "missing-types journal returned $rc"

printf '%s\n' '6 Aug 07:37:32 - [warn] Error loading credentials: bad secret' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 1 ] && ok "'Error loading credentials' ⇒ unhealthy (1)" || bad "credential-error journal returned $rc"

: >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 2 ] && ok "empty journal ⇒ no evidence at all (2)" || bad "empty journal returned $rc"

printf '%s\n' '6 Aug 07:37:32 - [info] Server now running at http://127.0.0.1:1880/' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 3 ] && ok "logging but no start marker ⇒ 3 (keep polling; a FAILURE at timeout, not 'no evidence')" \
                || bad "runtime-only journal returned $rc"

echo
echo "── nodered_flows_healthy — RUSSIAN journal (the bench board) ──────────"
# Verbatim from 192.168.1.136 (node-red logs in the system locale; these are
# the ru spellings in @node-red/runtime/locales/ru/runtime.json). An
# English-only verdict returns 3 here on a HEALTHY board and 3 on a BROKEN one
# — i.e. it cannot tell them apart, which is how a broken install went green.
printf '%s\n' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Сервер теперь работает на http://127.0.0.1:1880/' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Запуск потоков' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Запущены потоки' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 0 ] && ok "ru «Запущены потоки» ⇒ healthy (0)" \
                || bad "a HEALTHY ru-locale board returned $rc — the verdict is English-only again"

printf '%s\n' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Сервер теперь работает на http://127.0.0.1:1880/' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Ожидание регистрации отсутствующих типов:' \
  'Node-RED[7596]:  - my-missing-node' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 1 ] && ok "ru «Ожидание регистрации отсутствующих типов» ⇒ unhealthy (1)" \
                || bad "a BROKEN ru-locale board returned $rc — the false green is back"

printf '%s\n' 'Node-RED[7596]: [warn] Ошибка при загрузке учетных данных: bad secret' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 1 ] && ok "ru «Ошибка при загрузке учетных данных» ⇒ unhealthy (1)" || bad "ru credential error returned $rc"

printf '%s\n' 'Node-RED[7596]: [info] Потоки остановлены в безопасном режиме. Разверните, чтобы запустить.' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 1 ] && ok "ru safe-mode ⇒ unhealthy (1)" || bad "ru safe mode returned $rc"

echo
echo "── nodered_flows_healthy — the OTHER eight catalogues (the honest limit) ──"
# The ctl matches en and ru only. node-red ships eight more runtime catalogues,
# and a board running one of them is judged WRONGLY — but one-directionally:
# a HEALTHY de/ja board reports FAILURE (3 at the end of the poll window), never
# a false success. This pins that claim instead of leaving it as prose in the
# ctl's LANGUAGE note. Strings are verbatim from node-red 4.1.13
# @node-red/runtime/locales/{de,ja}/runtime.json, key nodes.flows.started-flows.
printf '%s\n' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] Flows sind gestartet' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 3 ] && ok "de «Flows sind gestartet» ⇒ 3 — a healthy de board reads as FAILURE, never as success" \
                || bad "a HEALTHY de-locale board returned $rc; only 3 (false FAILURE) is the accepted degradation — 0 would mean an unpinned marker slipped in, 1 a wrong verdict"

printf '%s\n' \
  'Node-RED[7596]: 6 Aug 07:37:32 - [info] フローを開始しました' >"$T/journal.txt"
nodered_flows_healthy nodered.service '-5 min'; rc=$?
[ "$rc" -eq 3 ] && ok "ja 「フローを開始しました」 ⇒ 3 — same one-directional degradation" \
                || bad "a HEALTHY ja-locale board returned $rc, want 3"

echo
# The locale pin is the other half of B1: our unit must fix the runtime's log
# language, or a board we installed inherits the system locale.
if grep -qE '^Environment=LC_ALL=' etc/systemd/system/nodered.service; then
    ok "etc/systemd/system/nodered.service pins LC_ALL (our own installs log deterministically)"
else
    bad "the unit no longer pins LC_ALL — the journal language becomes ambient again"
fi

echo
echo "── nodered_enable_start verdicts ──────────────────────────────────────"
clear_emit; STUB_PORT_LISTENING=0
printf '[info] Started flows\n' >"$T/journal.txt"
if nodered_enable_start ',"source":"offline"'; then
    case "$(last_emit)" in
        *'"ok":true'*'"verified":"flows"'*'"source":"offline"'*) ok "flows started ⇒ ok + verified:flows + the caller's extra fields" ;;
        *) bad "success JSON = $(last_emit)" ;;
    esac
else
    bad "a healthy start was reported as a failure"
fi

clear_emit; STUB_PORT_LISTENING=0
printf '%s\n' '[info] Waiting for missing types to be registered:' >"$T/journal.txt"
if nodered_enable_start; then
    bad "port 1880 open + every flow stopped was reported as SUCCESS — the false green is back"
else
    case "$(last_emit)" in
        *'"ok":false'*'"error":"install_failed"'*) ok "port open but flows stopped ⇒ install_failed (the headline fix)" ;;
        *) bad "failure JSON = $(last_emit)" ;;
    esac
fi

# THE B1 CASE at the emit surface: a ru-locale board whose flows never started.
# Before the fix this journal produced ok:true + verified:runtime_only.
clear_emit; STUB_PORT_LISTENING=0
printf '%s\n' \
  'Node-RED[7596]: [info] Сервер теперь работает на http://127.0.0.1:1880/' \
  'Node-RED[7596]: [info] Ожидание регистрации отсутствующих типов:' >"$T/journal.txt"
if nodered_enable_start; then
    bad "a ru-locale board with stopped flows reported SUCCESS — B1 has regressed"
else
    case "$(last_emit)" in
        *'"ok":false'*'"error":"install_failed"'*) ok "ru board, flows stopped ⇒ install_failed" ;;
        *) bad "ru failure JSON = $(last_emit)" ;;
    esac
fi
clear_emit; STUB_PORT_LISTENING=0
printf '%s\n' 'Node-RED[7596]: [info] Запущены потоки' >"$T/journal.txt"
if nodered_enable_start; then
    case "$(last_emit)" in
        *'"verified":"flows"'*) ok "ru board, flows started ⇒ ok + verified:flows (a correct install can pass on that board)" ;;
        *) bad "ru success JSON = $(last_emit)" ;;
    esac
else
    bad "a HEALTHY ru-locale board was reported as a failure — the runbook would be unsatisfiable there"
fi

# A runtime that logs but never says it started is a FAILURE at timeout, not
# "no evidence": we could read the journal, and it never said the one thing
# only a real start says.
clear_emit; STUB_PORT_LISTENING=0
printf '%s\n' '[info] Server now running at http://127.0.0.1:1880/' >"$T/journal.txt"
if nodered_enable_start; then
    bad "a unit that logged for the whole window without starting flows reported SUCCESS"
else
    case "$(last_emit)" in
        *'"error":"install_failed"'*) ok "logging but never started ⇒ install_failed at timeout (not a green)" ;;
        *) bad "timeout JSON = $(last_emit)" ;;
    esac
fi

clear_emit; STUB_PORT_LISTENING=0; : >"$T/journal.txt"; : >"$LOG"
if nodered_enable_start; then
    case "$(last_emit)" in
        *'"verified":"runtime_only"'*) ok "NO journal at all + port open ⇒ ok, labelled runtime_only (honest, not 'flows run')" ;;
        *) bad "no-evidence JSON = $(last_emit)" ;;
    esac
    grep -q 'no journal for' "$LOG" \
        && ok "  and the log says the flow check could not run" \
        || bad "  the degraded verdict is not recorded in the log"
else
    bad "no-evidence + running runtime was reported as a failure"
fi

clear_emit; STUB_PORT_LISTENING=1; : >"$T/journal.txt"
if nodered_enable_start; then
    bad "a dead runtime with no evidence was reported as success"
else
    ok "nothing listening + no evidence ⇒ install_failed"
fi

echo
echo "── nodered_install dispatch (payload beats the network) ───────────────"
mkpayload "$P" node-red-4.1.13.tar.gz nodered.service
: >"$T/chosen"; STUB_INTERNET=0
SA02M_NODERED_DIR="$P" nodered_install
[ "$(cat "$T/chosen")" = offline ] \
    && ok "payload staged + internet up ⇒ OFFLINE (the pin wins over the registry)" \
    || bad "chose $(cat "$T/chosen") with a payload present"

: >"$T/chosen"; STUB_INTERNET=0
SA02M_NODERED_DIR=/nonexistent-xyz nodered_install
[ "$(cat "$T/chosen")" = online ] \
    && ok "no payload + internet up ⇒ ONLINE (unchanged from today)" \
    || bad "chose $(cat "$T/chosen") with no payload and internet up"

clear_emit; : >"$T/chosen"; STUB_INTERNET=1
SA02M_NODERED_DIR=/nonexistent-xyz nodered_install
[ -s "$T/chosen" ] && bad "an installer ran with neither payload nor internet"
case "$(last_emit)" in
    *'"error":"no_internet"'*) ok "no payload dir + no internet ⇒ no_internet (unchanged from today)" ;;
    *) bad "expected no_internet, got $(last_emit)" ;;
esac

clear_emit; : >"$T/chosen"; STUB_INTERNET=1
mkpayload "$P" node-red-4.1.13.tar.gz          # dir present, unit missing
SA02M_NODERED_DIR="$P" nodered_install
case "$(last_emit)" in
    *'"error":"staging_missing"'*) ok "half-staged dir + no internet ⇒ staging_missing, not a network excuse" ;;
    *) bad "expected staging_missing, got $(last_emit)" ;;
esac

echo
if [ "$fails" -gt 0 ]; then
    printf 'test-nodered-ctl: %d FAILED\n' "$fails"
    exit 1
fi
printf 'test-nodered-ctl: all checks passed\n'
