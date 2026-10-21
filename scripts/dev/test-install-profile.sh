#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-install-profile.sh — the board's install profile
# (docs/contracts/install-profile.md): a module the profile marks `skip` is
# never installed, refreshed or delivered again, install.sh removes it while
# keeping its data, and the file can never widen behaviour. Quality row
# `install-profile`.
#
# Every case runs the SHIPPED code: etc/sa02m-install-profile-lib.sh is sourced
# in a subshell against a sandbox (SA02M_PROFILE_CONF / SA02M_PROFILE_ROOT /
# SA02M_UPDATE_STATEDIR), with PATH shims for systemctl / flock / timeout that
# record every call. No root, no device.
#
#   P1  reader: no file / skip / garbage / unknown key / CRLF / an injection
#       line all read as the contract says; the injection never executes
#   P2  writer: exact bytes, equal value is a no-op (mtime kept), invalid ID or
#       value rc 2 with the file untouched, SA02M_ROOTFS_BUILD writes nothing,
#       no tmp left, unknown lines dropped on rewrite
#   P3  the runner's deploy loop (extracted apply_deploy_items + helpers, run
#       in cmd_apply's `if !` shape): with meta/profile.exclude the module's
#       destinations are neither installed, journalled nor counted, each is
#       logged, files_done == files_total on the kept count; without it every
#       item deploys (the over-filter tripwire)
#   P4  prepare_profile_excludes: no profile ⇒ no exclude file and no lib
#       needed; skip + lib ⇒ the module's patterns; skip + missing or broken
#       lib ⇒ die E_COMPAT, nothing written; keep + no lib ⇒ proceeds; the
#       call sits in cmd_apply before backing_up and the deploy loop
#   P5  install.sh: the argument loop extracted and run (repeatable flags, a
#       missing ID collects `-`); the profile call after lib.sh and the mode
#       check, before the first module; the removal call after the AGENT_API
#       block; the banner line; sa02m_profile_apply_cli refuses unknown /
#       non-[A-Z_] / overlapping IDs with nothing written, persists skip/keep,
#       exports SA02M_SKIP_<ID>=1, writes nothing under a rootfs build
#   P6  removal in a sandbox root: exactly the table paths go, the sudoers
#       grant before any systemctl call, every sa02m-user@ instance stopped and
#       disabled, kept data and sentinel neighbours survive, a second run makes
#       zero systemctl calls, a busy lock removes nothing, a `keep` profile or
#       the one-shot env never removes, a relative root is refused; P6k the
#       OTA runner's pid line in update.lock survives the removal and the
#       busy-lock path (the lock is opened for APPEND — review 1.0.7.2 F1,
#       RED on the `8>` redirect: «truncated update.lock: []»); P6l with a REAL
#       flock(1) holder the removal waits, then runs, the pid line intact
#       (SKIP where flock(1) is missing — git-bash; WSL / Linux CI run it)
#   P7  table completeness: every destination scripts/13-agent-api.sh writes
#       is an OTA pattern, an install-only path or kept data (parsed allow-list
#       style — an unrecognised absolute path FAILS; >= 14 parsed); validity —
#       no pattern names /var/www/, /usr/local/libexec/, /etc/nginx/,
#       /usr/local/lib/
#   P8  the lib ships on both OTA paths to /usr/local/lib/: the packer's
#       map_deploy_entry + allowlist and the runner's GitHub map_dst (both
#       executed, not grepped)
#   P9  the helper and sudoers loops of scripts/update-www-only.sh and the
#       helper loop of etc/sa02m-web-update-apply.sh, cut out and RUN with
#       stub installers: skip ⇒ none of the module's targets, each logged;
#       no profile (or a clone without the reader) ⇒ everything delivered;
#       update-www-only.sh installs the reader
#   P10 the contract's supported-ID table == sa02m_profile_ids
#   P11 sa02m_agent_api.cgi on a sandbox copy with a stub auth lib: without
#       the package GET/POST answer not_installed verbatim, the session and
#       CSRF checks still come first, with the package GET runs it from
#       SA02M_AGENT_API_ROOT
#
# RED on the 1.0.7.1 code (HEAD before this feature, via the *_SRC seams):
# install.sh — P5a-P5d (5 FAIL); the runner — P3a 13 FAIL lines (the 4 module
# destinations installed, journalled, counted 6/6) and P4 (no function);
# update-www-only.sh / the legacy path / the agent CGI — P9 and P11a/b/e
# (8 FAIL). P3b, P11c, P11d stay GREEN there: they are the tripwires.
#
# RED, measured 2026-10-14 on scratch copies of the lib (INSTALL_PROFILE_LIB):
# the reader's `skip)` value check widened to `?*)` — P1c garbage and P1f
# injection read skip, P6h/P6i remove on a keep profile; the sudoers step
# matching nothing (grant removed with the other files, after the units) — P6d
# «a systemctl call ran while the sudoers grant was still on disk»;
# sa02m-agent-journal.sh dropped from the AGENT_API row — P7 «written by
# scripts/13-agent-api.sh but in no table column»; /usr/local/lib/sa02m-x.sh
# added to the row — P7 «forbidden tree». Comment-out cases are registered in
# comment-mutation-proof.
#
# Run: bash scripts/dev/test-install-profile.sh   (bash + python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
# shellcheck source=../../.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

LIB=${INSTALL_PROFILE_LIB:-etc/sa02m-install-profile-lib.sh}
CONTRACT=docs/contracts/install-profile.md
MODULE_SRC=scripts/13-agent-api.sh
for f in "$LIB" "$CONTRACT" "$MODULE_SRC" scripts/pack-offline-update.py \
         scripts/offline-update-deploy-map.json etc/sa02m-update-runner.sh; do
    [ -f "$f" ] || { echo "FAIL  missing $f"; exit 1; }
done
LIB_ABS="$(cd "$(dirname "$LIB")" && pwd)/$(basename "$LIB")"
( . "$LIB_ABS" ) >/dev/null 2>&1 || { echo "FAIL  $LIB does not source cleanly"; exit 1; }

PY=""
for p in python3 python py; do
    command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys' >/dev/null 2>&1 && { PY=$p; break; }
done
[ -n "$PY" ] || { echo "FAIL  no working python interpreter"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

fails=0; passes=0
ok()  { printf 'ok    %s\n' "$1"; passes=$((passes + 1)); }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

CONF="$T/profile.conf"
# lib <fn> [args] — run one lib function in a fresh subshell with the sandbox
# seams; stdout is the function's, stderr goes to $T/lib.err.
lib() {
    ( SA02M_PROFILE_CONF="$CONF" SA02M_PROFILE_ROOT="${PROOT:-}" \
      SA02M_UPDATE_STATEDIR="$T/state" SA02M_PROFILE_LOCK_WAIT="${LOCK_WAIT:-1}" \
      SA02M_ROOTFS_BUILD="${ROOTFS:-}"
      export SA02M_PROFILE_CONF SA02M_PROFILE_ROOT SA02M_UPDATE_STATEDIR SA02M_PROFILE_LOCK_WAIT
      [ -n "$SA02M_ROOTFS_BUILD" ] && export SA02M_ROOTFS_BUILD || unset SA02M_ROOTFS_BUILD
      # shellcheck disable=SC1090
      . "$LIB_ABS" || exit 99
      "$@" ) 2>"$T/lib.err"
}
HEAD1='# SA-02m install profile: product modules this board does not carry. Written by install.sh --skip-module/--keep-module; hand-editable.'
HEAD2='# MODULE_<ID>=skip|keep   skip = never installed, refreshed or delivered by OTA; install.sh removes it (data kept).'

# ── P1 reader ───────────────────────────────────────────────────────────────
PROOT=""
rm -f "$CONF"
[ "$(lib sa02m_profile_get AGENT_API)" = keep ] && ok "P1a no file -> keep" || bad "P1a no file must read keep"
[ -z "$(lib sa02m_profile_skipped)" ] && ok "P1a no file -> nothing skipped" || bad "P1a no file must skip nothing"
printf 'MODULE_AGENT_API=skip\n' > "$CONF"
[ "$(lib sa02m_profile_get AGENT_API)" = skip ] && ok "P1b MODULE_AGENT_API=skip -> skip" || bad "P1b skip must read skip"
[ "$(lib sa02m_profile_skipped)" = AGENT_API ] && ok "P1b skipped lists AGENT_API" || bad "P1b skipped must list AGENT_API"
printf 'MODULE_AGENT_API=yes\n' > "$CONF"
[ "$(lib sa02m_profile_get AGENT_API)" = keep ] && ok "P1c a garbage value -> keep" || bad "P1c garbage must read keep"
printf 'MODULE_FOO=skip\n' > "$CONF"
[ "$(lib sa02m_profile_get AGENT_API)" = keep ] && ok "P1d an unknown key does not touch AGENT_API" || bad "P1d unknown key changed AGENT_API"
lib sa02m_profile_get FOO >/dev/null; rc=$?
[ "$rc" = 2 ] && ok "P1d an unknown ID -> rc 2" || bad "P1d unknown ID must be rc 2 (got $rc)"
printf 'MODULE_AGENT_API=skip\r\n' > "$CONF"
[ "$(lib sa02m_profile_get AGENT_API)" = skip ] && ok "P1e CRLF line -> skip" || bad "P1e CRLF skip must read skip"
printf 'MODULE_AGENT_API=skip; touch %s/pwned1\nMODULE_AGENT_API=$(touch %s/pwned2)\n' "$T" "$T" > "$CONF"
v=$(lib sa02m_profile_get AGENT_API); lib sa02m_profile_skipped >/dev/null
if [ "$v" = keep ] && [ ! -e "$T/pwned1" ] && [ ! -e "$T/pwned2" ]; then
    ok "P1f an injection line reads keep and never executes"
else
    bad "P1f injection: read '$v', pwned1=$([ -e "$T/pwned1" ] && echo yes || echo no), pwned2=$([ -e "$T/pwned2" ] && echo yes || echo no)"
fi

# ── P2 writer ───────────────────────────────────────────────────────────────
rm -f "$CONF"
lib sa02m_profile_set AGENT_API skip; rc=$?
printf '%s\n%s\nMODULE_AGENT_API=skip\n' "$HEAD1" "$HEAD2" > "$T/want"
if [ "$rc" = 0 ] && cmp -s "$CONF" "$T/want"; then ok "P2a set skip writes the exact file"; else bad "P2a set skip: rc=$rc, bytes differ: $(diff "$T/want" "$CONF" 2>&1 | tr '\n' ' ')"; fi
touch -d '2001-01-01 00:00:00' "$CONF"
m1=$(stat -c %Y "$CONF")
lib sa02m_profile_set AGENT_API skip; rc=$?
m2=$(stat -c %Y "$CONF")
[ "$rc" = 0 ] && [ "$m1" = "$m2" ] && ok "P2b the same value is a no-op (mtime kept)" || bad "P2b same value rewrote the file (rc=$rc, mtime $m1 -> $m2)"
cp "$CONF" "$T/before"
lib sa02m_profile_set NOPE skip; rc1=$?
lib sa02m_profile_set AGENT_API maybe; rc2=$?
[ "$rc1" = 2 ] && [ "$rc2" = 2 ] && cmp -s "$CONF" "$T/before" && ok "P2c invalid ID / value -> rc 2, file untouched" || bad "P2c invalid args: rc=$rc1/$rc2, file changed=$(cmp -s "$CONF" "$T/before" && echo no || echo yes)"
printf '# hand note\nMODULE_AGENT_API=skip\nJUNK=1\n' > "$CONF"
lib sa02m_profile_set AGENT_API keep; rc=$?
printf '%s\n%s\nMODULE_AGENT_API=keep\n' "$HEAD1" "$HEAD2" > "$T/want"
[ "$rc" = 0 ] && cmp -s "$CONF" "$T/want" && ok "P2d set keep rewrites the whole file, unknown lines dropped" || bad "P2d set keep: rc=$rc, got: $(tr '\n' '|' < "$CONF")"
leftover=$(find "$T" -maxdepth 1 -name 'profile.conf.tmp.*' | head -n 3)
[ -z "$leftover" ] && ok "P2e no tmp file left behind" || bad "P2e tmp left: $leftover"
rm -f "$CONF"
ROOTFS=1 lib sa02m_profile_set AGENT_API skip; rc=$?
[ "$rc" = 0 ] && [ ! -e "$CONF" ] && ok "P2f SA02M_ROOTFS_BUILD: nothing written" || bad "P2f a rootfs build wrote the profile (rc=$rc)"

# ── P6 removal ──────────────────────────────────────────────────────────────
BIN="$T/bin"; mkdir -p "$BIN"
CALLS="$T/calls"
R="$T/root"
cat > "$BIN/systemctl" <<EOF
#!/bin/bash
[ -e "$R/etc/sudoers.d/sa02m-agent-api" ] && printf 'GRANT-STILL-PRESENT\n' >> "$CALLS"
printf 'systemctl %s\n' "\$*" >> "$CALLS"
if [ "\${1:-}" = list-units ]; then
    printf 'sa02m-user@alpha.service loaded active running Agent program alpha\n'
    printf 'sa02m-user@beta.service loaded inactive dead Agent program beta\n'
fi
exit 0
EOF
cat > "$BIN/timeout" <<'EOF'
#!/bin/bash
shift
exec "$@"
EOF
cat > "$BIN/flock" <<EOF
#!/bin/bash
printf 'flock %s\n' "\$*" >> "$CALLS"
[ -e "$T/lock-busy" ] && exit 1
exit 0
EOF
chmod +x "$BIN"/*

PROOT="$R"
make_footprint() {
    rm -rf "$R"; mkdir -p "$R"
    local p
    for p in $(lib sa02m_profile_ota_patterns AGENT_API) $(lib sa02m_profile_install_only_paths AGENT_API); do
        case "$p" in
            */) mkdir -p "$p/sa02m_agent_api"; printf 'x' > "$p/sa02m_agent_api/panel.py" ;;
            *) mkdir -p "$(dirname "$p")"; printf 'x' > "$p" ;;
        esac
    done
    for p in $(lib sa02m_profile_kept_data AGENT_API); do
        mkdir -p "$p"; printf 'data' > "$p/keep.me"
    done
    # Sentinel neighbours: same directories, same name prefixes.
    mkdir -p "$R/opt/sa02m-agent-apix" "$R/usr/local/sbin" "$R/etc/sudoers.d" "$R/etc/systemd/system" "$R/usr/local/lib"
    printf 's' > "$R/opt/sa02m-agent-apix/keep"
    printf 's' > "$R/usr/local/sbin/sa02m-agent-api-ctl.sh.orig"
    printf 's' > "$R/etc/sudoers.d/sa02m-www"
    printf 's' > "$R/etc/systemd/system/sa02m-flasher.service"
    printf 's' > "$R/usr/local/lib/sa02m-install-profile-lib.sh"
}
run_enforce() {
    : > "$CALLS"
    ENF_OUT=$(PATH="$BIN:$PATH" lib sa02m_profile_enforce AGENT_API; echo "rc=$?")
    ENF_ERR=$(cat "$T/lib.err")
}
nsys() { local n=0 l; while IFS= read -r l; do case "$l" in systemctl*) n=$((n + 1)) ;; esac; done < "$CALLS"; echo "$n"; }

make_footprint
printf 'MODULE_AGENT_API=skip\n' > "$CONF"
# The OTA runner keeps its pid in update.lock (opened for APPEND, read by
# lib_web_update.sh for `stale` and reboot.cgi): a removal waiting on the same
# lock must never truncate it (review 1.0.7.2 F1).
mkdir -p "$T/state"; printf '4242\n' > "$T/state/update.lock"
run_enforce
case "$ENF_OUT" in *rc=0) ok "P6a removal rc 0" ;; *) bad "P6a removal rc: $ENF_OUT; stderr: $ENF_ERR" ;; esac
grep -qx 4242 "$T/state/update.lock" && ok "P6k the runner's pid line in update.lock survives a removal" \
    || bad "P6k removal truncated update.lock: [$(tr '\n' ' ' < "$T/state/update.lock")]"
gone=1
for p in $(lib sa02m_profile_ota_patterns AGENT_API) $(lib sa02m_profile_install_only_paths AGENT_API); do
    [ -e "${p%/}" ] && { gone=0; bad "P6b still present after removal: ${p#"$R"}"; }
done
[ "$gone" = 1 ] && ok "P6b every OTA and install-only path of AGENT_API is gone"
kept=1
for p in $(lib sa02m_profile_kept_data AGENT_API); do
    [ -f "$p/keep.me" ] || { kept=0; bad "P6c kept data lost: ${p#"$R"}"; }
done
for p in opt/sa02m-agent-apix/keep usr/local/sbin/sa02m-agent-api-ctl.sh.orig etc/sudoers.d/sa02m-www \
         etc/systemd/system/sa02m-flasher.service usr/local/lib/sa02m-install-profile-lib.sh; do
    [ -f "$R/$p" ] || { kept=0; bad "P6c sentinel neighbour removed: /$p"; }
done
[ "$kept" = 1 ] && ok "P6c kept data and every sentinel neighbour survive"
calls=$(tr '\n' '|' < "$CALLS")
case "$calls" in
    *GRANT-STILL-PRESENT*) bad "P6d a systemctl call ran while the sudoers grant was still on disk: $calls" ;;
    *) ok "P6d the sudoers grant is removed before any systemctl call" ;;
esac
want_ok=1
for w in 'systemctl stop sa02m-agent-api.service' 'systemctl disable sa02m-agent-api.service' \
         'systemctl stop sa02m-user@alpha.service' 'systemctl disable sa02m-user@alpha.service' \
         'systemctl stop sa02m-user@beta.service' 'systemctl disable sa02m-user@beta.service' \
         'systemctl daemon-reload'; do
    case "|$calls" in *"|$w|"*) ;; *) want_ok=0; bad "P6e missing call: $w" ;; esac
done
case "$calls" in *"flock -w 1 8"*) ;; *) want_ok=0; bad "P6e removal did not take update.lock (flock -w <wait> 8): $calls" ;; esac
for w in start enable restart unmask; do
    case "$calls" in *"systemctl $w "*) want_ok=0; bad "P6e a widening verb ran: systemctl $w" ;; esac
done
[ "$want_ok" = 1 ] && ok "P6e stop+disable for the unit and every sa02m-user@ instance, daemon-reload, under update.lock, no widening verb"
run_enforce
n=$(nsys)
case "$ENF_OUT" in *rc=0) r0=1 ;; *) r0=0 ;; esac
if [ "$r0" = 1 ] && [ "$n" = 0 ] && text_has "$ENF_ERR" "удалять нечего"; then
    ok "P6f a second run: rc 0, zero systemctl calls, «удалять нечего»"
else
    bad "P6f second run: $ENF_OUT, $n systemctl call(s), stderr: $ENF_ERR"
fi
make_footprint; touch "$T/lock-busy"
printf '4242\n' > "$T/state/update.lock"
run_enforce
rm -f "$T/lock-busy"
grep -qx 4242 "$T/state/update.lock" && ok "P6k the runner's pid line survives a removal that gives up on a busy lock" \
    || bad "P6k busy-lock path truncated update.lock: [$(tr '\n' ' ' < "$T/state/update.lock")]"
if [ -f "$R/etc/sudoers.d/sa02m-agent-api" ] && [ -d "$R/opt/sa02m-agent-api" ] && [ "$(nsys)" = 0 ] \
   && text_has "$ENF_ERR" "отложено"; then
    case "$ENF_OUT" in *rc=0) bad "P6g a busy lock must return non-zero" ;; *) ok "P6g busy update.lock: nothing removed, WARN «отложено», rc != 0" ;; esac
else
    bad "P6g busy lock: removal ran anyway or no WARN ($ENF_OUT; stderr: $ENF_ERR)"
fi
# P6l the reviewer's repro with a REAL flock(1): a holder (the OTA runner's
# idiom: append + flock, pid written) keeps the lock 2 s; the removal waits,
# then runs — and the pid line the holder wrote is still there afterwards.
REALFLOCK=$(command -v flock 2>/dev/null || true)
case "$REALFLOCK" in
    "$BIN"/*|'') REALFLOCK="" ;;
esac
if [ -z "$REALFLOCK" ]; then
    SKIPS="P6l"; printf 'SKIP  P6l real-flock wait: no flock(1) on this host (git-bash) — WSL / Linux CI run it\n'
else
    BIN2="$T/bin2"; mkdir -p "$BIN2"; cp "$BIN/systemctl" "$BIN/timeout" "$BIN2/"
    make_footprint
    printf 'MODULE_AGENT_API=skip\n' > "$CONF"
    : > "$T/state/update.lock"
    ( exec 9>>"$T/state/update.lock"; flock 9; printf '4242\n' >&9; sleep 2 ) &
    holder=$!
    sleep 0.5
    : > "$CALLS"
    l_out=$( ( LOCK_WAIT=10 PATH="$BIN2:$PATH" lib sa02m_profile_enforce AGENT_API ); echo "rc=$?")
    mid=$(cat "$T/state/update.lock")
    wait "$holder"
    if [ "$mid" = 4242 ] && [ ! -e "$R/etc/sudoers.d/sa02m-agent-api" ]; then
        ok "P6l real flock: the removal waited for the holder, then ran; the holder's pid line survived ($l_out)"
    else
        bad "P6l real flock: lock content '$mid', grant removed=$([ -e "$R/etc/sudoers.d/sa02m-agent-api" ] && echo no || echo yes), $l_out"
    fi
fi
make_footprint
printf 'MODULE_AGENT_API=keep\n' > "$CONF"
run_enforce
[ -f "$R/etc/sudoers.d/sa02m-agent-api" ] && [ "$(nsys)" = 0 ] && ok "P6h a keep profile removes nothing" || bad "P6h keep profile removed or called systemctl"
rm -f "$CONF"
: > "$CALLS"
( export SA02M_SKIP_AGENT_API=1; run_enforce; [ -f "$R/etc/sudoers.d/sa02m-agent-api" ] && [ "$(nsys)" = 0 ] ) \
    && ok "P6i the one-shot SA02M_SKIP_AGENT_API=1 without a profile removes nothing" \
    || bad "P6i the one-shot env removed the module"
printf 'MODULE_AGENT_API=skip\n' > "$CONF"
PROOT="relative/root"
: > "$CALLS"
out=$(PATH="$BIN:$PATH" lib sa02m_profile_remove_module AGENT_API; echo "rc=$?")
err=$(cat "$T/lib.err")
case "$out" in *rc=0) bad "P6j a relative root must be refused" ;; *) text_has "$err" "не абсолютный" && ok "P6j a relative root is refused" || bad "P6j relative root: no refusal message ($err)" ;; esac
PROOT="$R"

# ── P5 installer: CLI, profile block, export, enforce ───────────────────────
PROOT=""
INST=${INSTALL_SH_SRC:-install.sh}
# P5a the argument loop, extracted from the shipped install.sh and RUN.
awk 'index($0,"while [[ $# -gt 0 ]]; do")==1{f=1} f{print} f&&/^done/{exit}' "$INST" > "$T/argloop.sh"
if ! grep -q '^done' "$T/argloop.sh"; then
    bad "P5a could not extract the argument loop from $INST"
else
    p5a=$(bash -c 'D=$1; _PROFILE_SKIP=""; _PROFILE_KEEP=""; set -- --refresh --skip-module AGENT_API --keep-module FOO --skip-module BAR --pass x
        . "$D/argloop.sh"; printf "skip=[%s] keep=[%s] mode=[%s]" "$_PROFILE_SKIP" "$_PROFILE_KEEP" "${SA02M_INSTALL_MODE:-}"' _ "$T" 2>&1) || true
    case "$p5a" in
        "skip=[ AGENT_API BAR] keep=[ FOO] mode=[refresh]") ok "P5a --skip-module/--keep-module are repeatable and collected ($p5a)" ;;
        *) bad "P5a argument loop: got '$p5a'" ;;
    esac
    p5t=$(bash -c 'D=$1; _PROFILE_SKIP=""; set -- --skip-module; . "$D/argloop.sh"; echo "end skip=[$_PROFILE_SKIP]"' _ "$T" 2>&1) || true
    case "$p5t" in
        *"end skip=[ -]"*) ok "P5a a trailing --skip-module without an ID ends the loop and collects «-» (refused later)" ;;
        *) bad "P5a trailing --skip-module: '$p5t'" ;;
    esac
fi
# P5b-d the order pins (comment-stripped line numbers).
l_src=$(stripped_first_line "$INST" '^source "\$SCRIPT_DIR/scripts/lib\.sh"')
l_mode=$(stripped_first_line "$INST" '^case "\$SA02M_INSTALL_MODE" in')
l_cli=$(stripped_first_line "$INST" '^sa02m_profile_apply_cli "\$_PROFILE_SKIP" "\$_PROFILE_KEEP" \|\| exit 2$')
l_ban=$(stripped_first_line "$INST" 'Профиль: \$\{SA02M_PROFILE_SUMMARY')
l_m01=$(stripped_first_line "$INST" '^sa02m_run_module 01-system\.sh')
l_agent=$(stripped_first_line "$INST" 'sa02m_run_module 13-agent-api\.sh')
l_enf=$(stripped_first_line "$INST" '^sa02m_profile_enforce AGENT_API \|\| log WARN ')
l_hk=$(stripped_first_line "$INST" 'sa02m_run_module 06c-homekit\.sh')
if [ -n "$l_cli" ] && [ -n "$l_src" ] && [ -n "$l_mode" ] && [ -n "$l_m01" ] \
   && [ "$l_cli" -gt "$l_src" ] && [ "$l_cli" -gt "$l_mode" ] && [ "$l_cli" -lt "$l_m01" ]; then
    ok "P5b the profile call (line $l_cli) runs after lib.sh and the mode check, before the first module"
else
    bad "P5b profile call 'sa02m_profile_apply_cli \"\$_PROFILE_SKIP\" \"\$_PROFILE_KEEP\" || exit 2' missing or misplaced (cli=${l_cli:-none} src=${l_src:-none} mode=${l_mode:-none} 01=${l_m01:-none})"
fi
if [ -n "$l_enf" ] && [ -n "$l_agent" ] && [ -n "$l_hk" ] && [ "$l_enf" -gt "$l_agent" ] && [ "$l_enf" -lt "$l_hk" ]; then
    ok "P5c the removal call (line $l_enf) follows the AGENT_API module block"
else
    bad "P5c 'sa02m_profile_enforce AGENT_API || log WARN …' missing or misplaced (enforce=${l_enf:-none} agent=${l_agent:-none} homekit=${l_hk:-none})"
fi
if [ -n "$l_ban" ] && [ -n "$l_m01" ] && [ "$l_ban" -lt "$l_m01" ]; then
    ok "P5d the banner prints the profile summary"
else
    bad "P5d the banner does not print 'Профиль: \${SA02M_PROFILE_SUMMARY…}'"
fi
# P5e the CLI half in the lib, run in the shell it exports into.
cli() {  # $1 skip  $2 keep → prints rc, export, file
    ( SA02M_PROFILE_CONF="$CONF"; SA02M_ROOTFS_BUILD="${ROOTFS:-}"
      export SA02M_PROFILE_CONF
      [ -n "$SA02M_ROOTFS_BUILD" ] && export SA02M_ROOTFS_BUILD || unset SA02M_ROOTFS_BUILD
      unset SA02M_SKIP_AGENT_API
      # shellcheck disable=SC1090
      . "$LIB_ABS"
      sa02m_profile_apply_cli "$1" "$2"; rc=$?
      printf 'rc=%s env=%s file=%s sum=%s' "$rc" "${SA02M_SKIP_AGENT_API:-}" \
          "$( [ -f "$CONF" ] && grep '^MODULE_' "$CONF" | tr '\n' ' ' || echo none)" "${SA02M_PROFILE_SUMMARY:-}" ) 2>"$T/cli.err"
}
rm -f "$CONF"; r=$(cli " NOPE" "")
case "$r" in "rc=2 env= file=none"*) text_has "$(cat "$T/cli.err")" "неизвестный модуль" && ok "P5e an unknown ID: rc 2, nothing written, «неизвестный модуль»" || bad "P5e unknown ID: no ERR line" ;; *) bad "P5e unknown ID: $r" ;; esac
rm -f "$CONF"; : > "$T/AGENT_API"; r=$(cd "$T" && cli " -" "" ; echo; cli " A*" "")
case "$r" in "rc=2 env= file=none"*"rc=2 env= file=none"*) text_has "$(cat "$T/cli.err")" "недопустимый ID" && ok "P5e a missing ID («-») and a glob («A*», with a file named AGENT_API in cwd) are refused, nothing written" || bad "P5e bad ID: no ERR line" ;; *) bad "P5e missing/glob ID: $r" ;; esac
rm -f "$CONF" "$T/AGENT_API"; r=$(cli " AGENT_API" " AGENT_API")
case "$r" in "rc=2 env= file=none"*) ok "P5e one ID in both lists: rc 2, nothing written" ;; *) bad "P5e overlap: $r" ;; esac
rm -f "$CONF"; r=$(cli " AGENT_API" "")
case "$r" in "rc=0 env=1 file=MODULE_AGENT_API=skip  sum=без AGENT_API") ok "P5e --skip-module AGENT_API: file skip, SA02M_SKIP_AGENT_API=1 exported" ;; *) bad "P5e skip: $r" ;; esac
r=$(cli "" "")
case "$r" in "rc=0 env=1 file=MODULE_AGENT_API=skip "*) ok "P5e a later run without flags still skips (from the file)" ;; *) bad "P5e file-only skip: $r" ;; esac
r=$(cli "" " AGENT_API")
case "$r" in "rc=0 env= file=MODULE_AGENT_API=keep  sum=все модули") ok "P5e --keep-module AGENT_API: file keep, nothing exported" ;; *) bad "P5e keep: $r" ;; esac
rm -f "$CONF"; r=$(ROOTFS=1 cli " AGENT_API" "")
case "$r" in "rc=0 env=1 file=none"*) text_has "$(cat "$T/cli.err")" "не записывается" && ok "P5e rootfs build: nothing written, this run still skips, WARN" || bad "P5e rootfs: no WARN" ;; *) bad "P5e rootfs: $r" ;; esac

# ── P3/P4 the OTA filter in the runner ──────────────────────────────────────
# The SHIPPED functions, cut out by literal prefix (portable awk), sourced into
# a subshell with file-backed txn/log stubs — the update-deploy-skip idiom.
RUNNER=${INSTALL_PROFILE_RUNNER:-etc/sa02m-update-runner.sh}
cat "$RUNNER" > "$T/runner.sh" 2>/dev/null || { echo "FAIL  cannot read runner: $RUNNER"; exit 1; }
rx() { awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$T/runner.sh"; }
: > "$T/rfn.sh"
for fn in apply_deploy_items is_unchanged journal_append atomic_install_file die; do
    rx "$fn" >> "$T/rfn.sh"
    grep -q "^$fn() {" "$T/rfn.sh" || { bad "P3 could not extract $fn() from $RUNNER"; }
done
HAS_PREP=0
if grep -q '^prepare_profile_excludes() {' "$T/runner.sh"; then
    rx prepare_profile_excludes >> "$T/rfn.sh"; HAS_PREP=1
fi
# Runner harness: $1 = scenario dir; stubs + the extracted functions.
cat > "$T/rstub.sh" <<'EOF'
log()                  { printf '%s\n' "$*" >> "$SC/runner.log"; }
utc_now()              { echo 1970-01-01T00:00:00Z; }
cleanup_imaging_lock() { :; }
manifest_path()        { printf '%s\n' "$STATEDIR/staging/$1/meta/manifest.json"; }
txn_patch() { local kv; for kv in "$@"; do printf '%s\n' "$kv" >> "$SC/txnvars"; done; }
txn_get()   { local v; v=$(grep "^$1=" "$SC/txnvars" 2>/dev/null | tail -n1); printf '%s\n' "${v#*=}"; }
# Durability (fdatasync order) is update-deploy-skip's subject, not this one's;
# a no-op keeps P3 runnable where `sync -d FILE` is refused (git-bash).
sync() { :; }
EOF
R3="$T/p3root"
p3_fixture() {  # $1 = scenario dir
    SC=$1; rm -rf "$SC" "$R3"; mkdir -p "$SC/state/staging/TXN/meta" "$SC/state/staging/TXN/overlay" "$SC/state/staging/TXN/backups" "$R3"
    : > "$SC/txnvars"; : > "$SC/runner.log"
    local ov="$SC/state/staging/TXN/overlay" i=0 dst
    {
        printf '{"schema_version":1,"version":"9.9.9.9","deploy":['
        for dst in /var/www/network_config/x.txt /opt/sa02m-agent-api/sa02m_agent_api/a.py \
                   /usr/local/sbin/sa02m-agent-api-ctl.sh /etc/sudoers.d/sa02m-agent-api \
                   /opt/sa02m-agent-apix/b.py /etc/systemd/system/sa02m-user@.service; do
            i=$((i + 1)); printf 'item %s\n' "$i" > "$ov/f$i"
            [ "$i" -gt 1 ] && printf ','
            printf '{"src":"f%s","dst":"%s%s","mode":"0644","owner":""}' "$i" "$R3" "$dst"
        done
        printf ']}\n'
    } > "$SC/state/staging/TXN/meta/manifest.json"
}
p3_run() {  # runs the shipped deploy loop in cmd_apply's `if !` shape
    ( set -uo pipefail
      SC=$1; STATEDIR="$SC/state"; export SA02M_UPDATE_PROGRESS_S=0
      # shellcheck disable=SC1090,SC1091
      . "$T/rstub.sh"; . "$T/rfn.sh"
      if ! apply_deploy_items TXN; then exit 1; fi ) >/dev/null 2>"$1/stderr"
}
EXCLUDED_DSTS="/opt/sa02m-agent-api/sa02m_agent_api/a.py /usr/local/sbin/sa02m-agent-api-ctl.sh /etc/sudoers.d/sa02m-agent-api /etc/systemd/system/sa02m-user@.service"
KEPT_DSTS="/var/www/network_config/x.txt /opt/sa02m-agent-apix/b.py"
# P3a profile skip: the exclude file the runner reads (pattern column of the lib's excludes).
p3_fixture "$T/p3a"
printf 'MODULE_AGENT_API=skip\n' > "$CONF"
PROOT="$R3" lib sa02m_profile_ota_excludes | cut -f2 > "$T/p3a/state/staging/TXN/meta/profile.exclude"
p3_run "$T/p3a"; rc=$?
p3ok=1
[ "$rc" = 0 ] || { p3ok=0; bad "P3a apply rc=$rc ($(head -c 300 "$T/p3a/stderr"))"; }
for d in $EXCLUDED_DSTS; do
    [ -e "$R3$d" ] && { p3ok=0; bad "P3a an excluded destination was installed: $d"; }
    grep -qxF "apply: profile skip $R3$d" "$T/p3a/runner.log" || { p3ok=0; bad "P3a no 'apply: profile skip' log line for $d"; }
    grep -qF "$R3$d" "$T/p3a/state/staging/TXN/journal.jsonl" 2>/dev/null && { p3ok=0; bad "P3a an excluded destination was journalled: $d"; }
done
for d in $KEPT_DSTS; do
    [ -f "$R3$d" ] || { p3ok=0; bad "P3a a kept destination was not installed: $d"; }
done
ft=$(SC="$T/p3a"; . "$T/rstub.sh"; txn_get files_total); fd=$(SC="$T/p3a"; . "$T/rstub.sh"; txn_get files_done)
[ "$ft" = 2 ] && [ "$fd" = 2 ] || { p3ok=0; bad "P3a files_total/files_done = $ft/$fd, want 2/2 (the kept count — recover-verify invariant)"; }
[ "$p3ok" = 1 ] && ok "P3a profile skip: the 4 module destinations are neither installed, journalled nor counted; each is logged; the 2 others deploy; files_done == files_total == 2"
# P3b no exclude file: byte-identical behaviour — everything deploys.
p3_fixture "$T/p3b"
p3_run "$T/p3b"; rc=$?
ft=$(SC="$T/p3b"; . "$T/rstub.sh"; txn_get files_total)
n_inst=0; for d in $EXCLUDED_DSTS $KEPT_DSTS; do [ -f "$R3$d" ] && n_inst=$((n_inst + 1)); done
if [ "$rc" = 0 ] && [ "$ft" = 6 ] && [ "$n_inst" = 6 ] && ! grep -q 'profile skip' "$T/p3b/runner.log" \
   && [ ! -e "$T/p3b/state/staging/TXN/deploy.excluded" ]; then
    ok "P3b no profile.exclude: all 6 items deploy, files_total 6, no skip line, no deploy.excluded"
else
    bad "P3b no exclude file: rc=$rc files_total=$ft installed=$n_inst/6"
fi
# P4 prepare_profile_excludes: when the runner computes the exclude file.
if [ "$HAS_PREP" = 0 ]; then
    bad "P4 $RUNNER has no prepare_profile_excludes() — the OTA filter is never computed"
else
    p4() {  # $1 conf (or none)  $2 lib path → "rc=N excl=<lines> code=<error_code>"
        local sc="$T/p4"; rm -rf "$sc"; mkdir -p "$sc/state/staging/TX4/meta"; : > "$sc/txnvars"; : > "$sc/runner.log"
        ( SC=$sc; STATEDIR="$sc/state"
          SA02M_PROFILE_CONF=$1; SA02M_INSTALL_PROFILE_LIB=$2; SA02M_PROFILE_ROOT=$R3
          export SA02M_PROFILE_CONF SA02M_INSTALL_PROFILE_LIB SA02M_PROFILE_ROOT
          # shellcheck disable=SC1090,SC1091
          . "$T/rstub.sh"; . "$T/rfn.sh"
          prepare_profile_excludes TX4 ) >/dev/null 2>&1
        local rc=$? x="$sc/state/staging/TX4/meta/profile.exclude" n=none
        [ -f "$x" ] && n=$(grep -c . "$x")
        printf 'rc=%s excl=%s code=%s' "$rc" "$n" "$(SC=$sc; . "$T/rstub.sh"; txn_get error_code)"
    }
    printf 'MODULE_AGENT_API=skip\n' > "$T/p4.conf"
    printf 'MODULE_AGENT_API=keep\n' > "$T/p4keep.conf"
    printf 'exit 3\n' > "$T/broken-lib.sh"
    npat=$(PROOT="$R3" lib sa02m_profile_ota_patterns AGENT_API | grep -c .)
    r=$(p4 "$T/no-such.conf" "$T/no-such-lib.sh"); [ "$r" = "rc=0 excl=none code=" ] && ok "P4a no profile file: no exclude file, no lib needed" || bad "P4a no profile: $r"
    r=$(p4 "$T/p4.conf" "$LIB_ABS"); [ "$r" = "rc=0 excl=$npat code=" ] && ok "P4b profile skip + lib: exclude file with the module's $npat patterns" || bad "P4b skip+lib: $r (want excl=$npat)"
    r=$(p4 "$T/p4.conf" "$T/no-such-lib.sh"); [ "$r" = "rc=1 excl=none code=E_COMPAT" ] && ok "P4c profile skip, reader missing: die E_COMPAT before any deploy, no exclude file" || bad "P4c skip without reader: $r"
    r=$(p4 "$T/p4.conf" "$T/broken-lib.sh"); [ "$r" = "rc=1 excl=none code=E_COMPAT" ] && ok "P4d profile skip, reader broken: E_COMPAT" || bad "P4d broken reader: $r"
    r=$(p4 "$T/p4keep.conf" "$T/no-such-lib.sh"); [ "$r" = "rc=0 excl=none code=" ] && ok "P4e profile keep, reader missing: proceeds (nothing to filter)" || bad "P4e keep without reader: $r"
    # P4f the call site: cmd_apply computes the filter before backing up / deploying.
    l_prep=$(stripped_first_line "$RUNNER" '^[[:space:]]+prepare_profile_excludes "\$txn"$')
    l_back=$(stripped_first_line "$RUNNER" 'txn_patch "stage=backing_up"')
    l_dep=$(stripped_first_line "$RUNNER" 'if ! apply_deploy_items "\$txn"; then')
    if [ -n "$l_prep" ] && [ -n "$l_back" ] && [ -n "$l_dep" ] && [ "$l_prep" -lt "$l_back" ] && [ "$l_prep" -lt "$l_dep" ]; then
        ok "P4f cmd_apply calls prepare_profile_excludes (line $l_prep) before backing_up and the deploy loop"
    else
        bad "P4f prepare_profile_excludes \"\$txn\" call missing or after backing_up/deploy (prep=${l_prep:-none} backup=${l_back:-none} deploy=${l_dep:-none})"
    fi
fi

# ── P9 the shell delivery paths: partial refresh and mandatory shared handoff ─
# The shipped loops are cut out and RUN with stubbed installers; the lib is the
# real one, the profile a sandbox file.
WWW_ONLY=${WWW_ONLY_SRC:-scripts/update-www-only.sh}
LEGACY=${LEGACY_APPLY_SRC:-etc/sa02m-web-update-apply.sh}
P9="$T/p9"; mkdir -p "$P9/sbin" "$P9/etc/sudoers.d" "$P9/clone/repo/usr/local/sbin" "$P9/clone/repo/etc"
AGENT_HELPERS="sa02m-agent-api-ctl.sh sa02m-agent-token-store.sh sa02m-agent-root-cap.sh sa02m-agent-root-exec.sh sa02m-rules-store-apply.sh sa02m-user-unit.sh sa02m-agent-journal.sh"
for h in sa02m-gateway-config-apply.sh sa02m-mqtt-config-apply.sh sa02m-cloud-web-trigger.sh $AGENT_HELPERS; do
    : > "$P9/sbin/$h"; : > "$P9/clone/repo/usr/local/sbin/$h"
done
for s in sa02m-cloud sa02m-flasher sa02m-mqtt sa02m-gateway sa02m-alice sa02m-agent-api; do : > "$P9/etc/sudoers.d/$s"; done
cp "$LIB_ABS" "$P9/clone/repo/etc/sa02m-install-profile-lib.sh"
cut_block() {  # $1 file  $2 literal first-line prefix (after indentation) → to the first `done` at col 0
    awk -v start="$2" 'index($0,start)==1{f=1} f{print} f&&/^done/{exit}' "$1"
}
cut_block "$WWW_ONLY" 'for _h in sa02m-gateway-config-apply.sh' > "$P9/www-helpers.sh"
cut_block "$WWW_ONLY" 'for _sud in sa02m-cloud ' > "$P9/www-sudoers.sh"
p9_run() {  # $1 block  $2 conf → prints installed targets, one per line
    ( SA02M_PROFILE_CONF=$2; export SA02M_PROFILE_CONF
      REPO_SBIN="$P9/sbin"; REPO_ETC="$P9/etc"; CLONE_TMP="$P9/clone"; LOGFILE=/dev/null
      log() { printf 'LOG %s\n' "$*"; }
      sed() { :; }
      visudo() { return 0; }
      sa02m_atomic_install() { printf 'INSTALL %s\n' "${*: -1}"; }
      install() { printf 'INSTALL %s\n' "${*: -1}"; }
      atomic_install_script() { printf 'INSTALL %s\n' "${*: -1}"; }
      # shellcheck disable=SC1090
      . "$LIB_ABS"
      # shellcheck disable=SC1090
      . "$1" ) 2>&1
}
printf 'MODULE_AGENT_API=skip\n' > "$T/p9.conf"
for blk in www-helpers www-sudoers; do
    if ! grep -q '^done' "$P9/$blk.sh"; then
        bad "P9 could not extract the $blk loop (the guarded block moved or is gone)"
        continue
    fi
    on=$(p9_run "$P9/$blk.sh" "$T/p9.conf"); off=$(p9_run "$P9/$blk.sh" "$T/no-such.conf")
    case "$blk" in
        www-sudoers) agent_t="/etc/sudoers.d/sa02m-agent-api"; other_t="/etc/sudoers.d/sa02m-gateway"; n_agent=1 ;;
        *) agent_t="/usr/local/sbin/sa02m-agent-api-ctl.sh"; other_t="/usr/local/sbin/sa02m-gateway-config-apply.sh"; n_agent=7 ;;
    esac
    n_skip=$(grep -c 'модуль пропущен профилем установки' <<<"$on")
    if text_has "$on" "INSTALL $agent_t" || ! text_has "$on" "INSTALL $other_t" || [ "$n_skip" != "$n_agent" ]; then
        bad "P9 $blk with MODULE_AGENT_API=skip: agent target installed or others not, or $n_skip/$n_agent skip lines: $(tr '\n' '|' <<<"$on" | head -c 400)"
    elif ! text_has "$off" "INSTALL $agent_t" || ! text_has "$off" "INSTALL $other_t"; then
        bad "P9 $blk without a profile must deliver everything: $(tr '\n' '|' <<<"$off" | head -c 400)"
    else
        ok "P9 $blk: profile skip ⇒ none of the module's $n_agent target(s) delivered, each logged, the rest delivered; no profile ⇒ everything delivered"
    fi
done
# Online updates use the same profile-aware runner tested by P3/P4. The old
# absent-reader fallback was deliberately removed: missing preparation refuses.
stripped_has "$LEGACY" 'exec "$RUNNER" apply' \
    && ok "P9 online delivery delegates to the shared profile-aware runner" \
    || bad "P9 online delivery bypasses the shared profile-aware runner"
awk '/^if handoff_to_shared_runner; then/{f=1} f{print}' "$LEGACY" > "$P9/handoff.sh"
(
    STATUS_FILE="$P9/status"
    handoff_to_shared_runner() { return 1; }
    log() { printf '%s\n' "$*"; }
    rsync() { echo UNSAFE_FALLBACK; }
    install() { echo UNSAFE_FALLBACK; }
    . "$P9/handoff.sh"
) > "$P9/handoff.out" 2>&1
handoff_rc=$?
if [ "$handoff_rc" = 1 ] && grep -q 'E_BOOTSTRAP_REQUIRED' "$P9/handoff.out" \
   && [ "$(cat "$P9/status" 2>/dev/null)" = error ] && ! grep -q UNSAFE_FALLBACK "$P9/handoff.out"; then
    ok "P9 unavailable shared runner refuses before any legacy copy (missing-reader refusal covered by P4)"
else
    bad "P9 unavailable shared runner did not refuse safely: rc=$handoff_rc"
fi
P9C='sa02m_atomic_install -m 644 "$SCRIPT_DIR/../etc/sa02m-install-profile-lib.sh" /usr/local/lib/sa02m-install-profile-lib.sh'
stripped_has "$WWW_ONLY" "$P9C" && ok "P9 update-www-only.sh installs the reader to /usr/local/lib/" || bad "P9 update-www-only.sh does not install the reader"

# ── P11 the agent card's CGI on a board without the module ─────────────────
CGI_SRC=${AGENT_CGI_SRC:-www/network_config/cgi-bin/sa02m_agent_api.cgi}
P11="$T/p11"; mkdir -p "$P11/cgi-bin" "$P11/empty" "$P11/agent/sa02m_agent_api"
cp "$CGI_SRC" "$P11/cgi-bin/sa02m_agent_api.cgi"
cat > "$P11/cgi-bin/lib_web_auth.sh" <<'EOF'
web_session_check_cookie() { [ -z "${P11_NOSESSION:-}" ]; }
web_csrf_validate() { [ -z "${P11_NOCSRF:-}" ]; }
web_csrf_error_body() { echo '{"ok":false,"error":"E_CSRF"}'; }
EOF
cgi() {  # $1 method  $2 root → the body (after the header block)
    printf '{}' | REQUEST_METHOD=$1 SA02M_AGENT_API_ROOT=$2 bash "$P11/cgi-bin/sa02m_agent_api.cgi" 2>/dev/null \
        | awk 'f{print} /^\r?$/{f=1}' | tr -d '\r'
}
WANT_GET='{"ok":true,"state":"not_installed","active":"inactive","tokens":[],"message":"Agent API module is not installed on this board (install profile)"}'
WANT_POST='{"ok":false,"error":"not_installed","state":"not_installed"}'
r=$(cgi GET "$P11/empty"); [ "$r" = "$WANT_GET" ] && ok "P11a GET without the package: the not_installed state, verbatim" || bad "P11a GET: '$r'"
r=$(cgi POST "$P11/empty"); [ "$r" = "$WANT_POST" ] && ok "P11b POST without the package: not_installed, verbatim" || bad "P11b POST: '$r'"
r=$(P11_NOSESSION=1 cgi GET "$P11/empty"); [ "$r" = '{"ok":false,"error":"unauthorized"}' ] && ok "P11c no session: unauthorized comes first" || bad "P11c no session: '$r'"
r=$(P11_NOCSRF=1 cgi POST "$P11/empty"); [ "$r" = '{"ok":false,"error":"E_CSRF"}' ] && ok "P11d POST without CSRF: E_CSRF comes before not_installed" || bad "P11d no CSRF: '$r'"
: > "$P11/agent/sa02m_agent_api/__init__.py"
printf 'print("{\\"ok\\":true,\\"probe\\":\\"installed\\"}")\n' > "$P11/agent/sa02m_agent_api/panel.py"
r=$(cgi GET "$P11/agent")
case "$r" in
    *'"probe":"installed"'*) ok "P11e with the package: GET runs the panel from SA02M_AGENT_API_ROOT" ;;
    *) bad "P11e installed GET did not run the package from the root: '$r'" ;;
esac

# ── P7 table completeness + validity ────────────────────────────────────────
PROOT=""
lib sa02m_profile_ota_patterns AGENT_API > "$T/pat.ota"
lib sa02m_profile_install_only_paths AGENT_API > "$T/pat.only"
lib sa02m_profile_kept_data AGENT_API > "$T/pat.kept"
"$PY" - "$MODULE_SRC" "$T/pat.ota" "$T/pat.only" "$T/pat.kept" > "$T/p7.out" 2>"$T/p7.err" <<'PY'
import re, sys
src, ota, only, kept = sys.argv[1:5]
pats = [l.strip() for l in open(ota) if l.strip()]
only_p = [l.strip() for l in open(only) if l.strip()]
kept_p = [l.strip() for l in open(kept) if l.strip()]
fail = []
# Validity: a module row never names a shared or foreign tree.
for p in pats + only_p + kept_p:
    if not p.startswith("/"):
        fail.append("pattern not absolute: %s" % p)
    for bad in ("/var/www/", "/usr/local/libexec/", "/etc/nginx/", "/usr/local/lib/"):
        if bad in p or p.rstrip("/") == bad.rstrip("/"):
            fail.append("pattern names a forbidden tree %s: %s" % (bad, p))
# Allow-list parse of every absolute path the module writes.
NOT_DEST = {"/usr/sbin/nologin"}
dests = []
for raw in open(src, encoding="utf-8"):
    line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
    if not line.strip():
        continue
    m = re.match(r"\s*install_helper\s+(\S+)\s*$", line)
    if m:
        dests.append("/usr/local/sbin/" + m.group(1))
        continue
    for t in re.finditer(r"(?<![\w$}])(/(?:etc|opt|usr|var|lib|run)/[A-Za-z0-9_.@/-]*)(\$?)", line):
        if t.group(2):          # variable-suffixed (install_helper body, $1)
            continue
        p = t.group(1)
        if p in NOT_DEST:
            continue
        dests.append(p)
def covered(p):
    for q in pats + kept_p:
        if q.endswith("/"):
            if p == q.rstrip("/") or p.startswith(q):
                return True
        elif p == q:
            return True
    return p in only_p
dests = sorted(set(d.rstrip("/") for d in dests))
for d in dests:
    if not covered(d):
        fail.append("written by %s but in no table column: %s" % (src, d))
print("PARSED %d" % len(dests))
for f in fail:
    print("FAIL " + f)
PY
n7=$(grep -m1 '^PARSED ' "$T/p7.out"); n7=${n7#PARSED }; n7=${n7%$'\r'}
p7f=$(grep '^FAIL ' "$T/p7.out")
if [ -z "$p7f" ] && [ "${n7:-0}" -ge 14 ]; then
    ok "P7 every one of $n7 destinations in $MODULE_SRC is in the AGENT_API row; no forbidden tree"
else
    bad "P7 table: parsed ${n7:-0} (need >= 14); ${p7f//$'\n'/; }"
fi

# ── P8 the lib ships on both OTA paths ──────────────────────────────────────
"$PY" - > "$T/p8.out" 2>&1 <<'PY'
import importlib.util, json, re, sys
from pathlib import Path
REL = "etc/sa02m-install-profile-lib.sh"
WANT = "/usr/local/lib/sa02m-install-profile-lib.sh"
spec = importlib.util.spec_from_file_location("packer", "scripts/pack-offline-update.py")
pk = importlib.util.module_from_spec(spec); spec.loader.exec_module(pk)
dm = json.load(open("scripts/offline-update-deploy-map.json", encoding="utf-8"))
e = pk.map_deploy_entry(REL, dm)
print("PACKER", e and e["dst"], e and e["mode"])
pats = pk.load_allowlist_patterns()
print("ALLOW", any(pk.path_matches(REL, p) for p in pats))
src = open("etc/sa02m-update-runner.sh", encoding="utf-8").read()
a = src.index("MPLC_OTA_PLUGINS = frozenset(")
b = src.index("def render_nginx_template(", a)
ns = {"Path": Path, "re": re}
exec(src[a:b], ns)
print("RUNNER", ns["map_dst"](REL))
PY
p8=$(tr -d '\r' < "$T/p8.out" | tr '\n' '|')
P8_WANT=/usr/local/lib/sa02m-install-profile-lib.sh
case "$p8" in
    "PACKER $P8_WANT 0644|ALLOW True|RUNNER $P8_WANT|")
        ok "P8 the lib reaches /usr/local/lib/ by the signed package (map + allowlist) and by GitHub-OTA (map_dst)" ;;
    *) bad "P8 lib delivery: $p8" ;;
esac

P8B='sa02m_atomic_install -m 644 "$SCRIPT_DIR/../etc/sa02m-install-profile-lib.sh" /usr/local/lib/sa02m-install-profile-lib.sh'
if stripped_has scripts/03-webserver.sh "$P8B"; then
    ok "P8b the installer (03-webserver.sh) installs the lib atomically to /usr/local/lib/"
else
    bad "P8b scripts/03-webserver.sh no longer installs the lib: '$P8B'"
fi

# ── P10 contract table == code ──────────────────────────────────────────────
code_ids=$(lib sa02m_profile_ids | sort | tr '\n' ' ')
doc_ids=$(awk '/^## 2\./{f=1; next} /^Остальные/{f=0} /^## /{f=0} f && /^\| `[A-Z_]+` \|/{ s=$0; sub(/^\| `/, "", s); sub(/`.*/, "", s); print s }' "$CONTRACT" | sort | tr '\n' ' ')
if [ -n "${code_ids// /}" ] && [ "$code_ids" = "$doc_ids" ]; then
    ok "P10 contract §2 supported IDs == sa02m_profile_ids ($code_ids)"
else
    bad "P10 contract IDs '$doc_ids' != code IDs '$code_ids'"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "install-profile: ALL OK — $passes case(s) green${SKIPS:+, SKIPPED here: $SKIPS (not a pass)}"
    exit 0
fi
echo "install-profile: $fails FAIL(s)"
exit 1
