#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-web-creds-commit.sh — behavioural harness for the panel password change:
# www/network_config/cgi-bin/web_creds.cgi and the root helper it calls,
# etc/sa02m-commit-web-env.sh (installed as /usr/local/sbin/sa02m-commit-web-env).
# Quality row `web-creds-commit`.
#
# The guarantee: the new credential file reaches root ONLY on the helper's
# stdin, from the CGI that validated it. Before the fix the CGI wrote it to the
# fixed path /tmp/sa02m_web.env.new and root installed whatever sat there after
# a format-only check; /tmp is shared with every local service account, so any
# of them could pre-create that file and have root install ITS credentials on
# the Operator's next password change (audit 2026-10-30 F1; the threat-model
# row in docs/threat-model.md §4 points here).
#
# Method: the SHIPPED files run in a scratch tree. Each is copied with its
# absolute roots retargeted (/etc/sa02m_web.env, /tmp/, /usr/local/lib/ →
# under the sandbox; the copy is refused if any other absolute root survives),
# `install` is a recording PATH shim that copies (the helper asks for
# -o root, which no dev box or CI runner can satisfy), and for the CGI `sudo`
# is a PATH shim that records its argv and execs the retargeted helper on the
# same stdin — so the CGI cases run the real chain CGI → pipe → helper →
# install. The session and the CSRF token are minted through the shipped
# lib_web_auth.sh into a sandboxed SA02M_SESSION_DIR. None of the paths the
# subject itself uses on a board (/etc/sa02m_web.env, the /tmp staging and work
# dirs, /usr/local/lib, /run sessions) is touched on the host; the sandbox is
# one mktemp dir under $TMPDIR (or /tmp), removed on exit.
#
# Cases:
#   A   setup + non-vacuity: both copies retargeted (the install target and the
#       lib really moved; no un-retargeted absolute root left), the lib's
#       functions present
#   H1  messy but valid stdin (double quotes, CRLF, a junk line) → installed
#       NORMALISED, install argv `-m 640 -o root -g www-data`, no work dir left
#   H2  a well-formed $6$ hash on stdin → installed as SA02M_WEB_PASS_HASH
#   H3  an attacker's valid staging file planted at /tmp/sa02m_web.env.new +
#       EMPTY stdin → refused, target byte-unchanged, install never called
#   H4  the same planted file + a valid stdin → the STDIN credentials land,
#       never the planted ones
#   H5  empty stdin, nothing planted → refused, nothing installed
#   H6  malformed stdin (user charset, `$(` in the password, no password line,
#       a mis-shaped hash, a 3-char password) → each refused, nothing installed
#   H7  oversized stdin (valid lines + padding past the bound) → refused;
#       exactly the bound → installed (the bound is the bound, not a lower one)
#   C1  CGI valid change → ok:true, sudo argv exactly
#       `-n /usr/local/sbin/sa02m-commit-web-env`, the new user + password
#       verify against the installed file; the sandbox /tmp is EMPTY at the
#       moment sudo is called (the sudo shim snapshots it — the CGI wrote no
#       staging file) and nothing is left in it afterwards
#   C2  CGI with the commit failing → save_failed, never ok:true, target unchanged
#   C3  CGI with an attacker's staging file planted (mode 0444, so the CGI's
#       write cannot replace it) → the Operator's credentials land, the planted
#       file is byte-unchanged
#   C4  CGI with a wrong current password → wrong_password, sudo never called
#   C5  CGI with the commit hanging (the sudo shim sleeps; the copy's
#       `timeout 10` shortened to 2 s) → save_failed (timeout's 124 is a
#       non-zero PIPESTATUS), nothing installed, inside a 15 s outer bound
#       (web-code-rigor.md «Timeouts everywhere»; the margin is for a slow
#       runner — 2 s plus CGI start-up took 5-6 s on the Windows box)
#   S1  static: no live line of web_creds.cgi names /tmp, no live line of the
#       helper names the retired staging path (comment-stripped, lib_check.sh)
#
# RED, observed 2026-10-30 on the origin/main files (fdc006d, 1.0.7.20 — the
# tree before this fix; WEB_CREDS_HELPER/CGI_DIR pointed at copies of them).
# The failing classes (no count — it moves as cases are added): every
# valid-stdin case (H1, H2, H4, H7b) — the old helper ignores stdin and
# refuses for lack of a staging file; H3 — the planted `mallory` credentials
# INSTALLED from empty stdin; C3 — the CGI answers ok:true and root installs
# `mallory` (F1 end to end); C1 — sudo argv without -n, and a staging file in
# /tmp when sudo runs; C5 — no `timeout` on the commit (the outer bound kills
# the CGI); S1 — live /tmp lines in the CGI, a live staging path in the helper.
# Also RED, the review's skew run: the old helper with the new CGI — C1
# save_failed, C3 ok:true with `mallory` installed (why every panel-deploying
# path must ship the helper; gated by sudoers-pin-contract). GREEN on the fixed
# files: ALL OK.
#
# Comment-out cases registered in comment-mutation-proof, both measured RED:
# the helper's size bound (`-le "$MAX_BYTES"`) → H7a (4097 bytes installed);
# the CGI's pipe into the helper (`sudo -n /usr/local/sbin/sa02m-commit-web-env`)
# → C1/C3 (save_failed, nothing installed — the PIPESTATUS check fails closed
# on a missing pipeline).
#
# Windows/git-bash: C3's premise (a 0444 file the CGI cannot overwrite) holds
# for a non-root runner; as root the old CGI could overwrite it, but the FIXED
# CGI never writes there, so the assertion is the same on every host.
#
# Run: bash scripts/dev/test-web-creds-commit.sh
#      WEB_CREDS_HELPER=<file> / CGI_DIR=<dir> judge other copies (the RED run).
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

HELPER_SRC="${WEB_CREDS_HELPER:-etc/sa02m-commit-web-env.sh}"
HELPER_LIB_SRC="${WEB_CREDS_HELPER_LIB:-etc/sa02m-web-auth-lib.sh}"
CGI_DIR="${CGI_DIR:-www/network_config/cgi-bin}"
CGI_SRC="$CGI_DIR/web_creds.cgi"
CGI_LIB_SRC="$CGI_DIR/lib_web_auth.sh"
for f in "$HELPER_SRC" "$HELPER_LIB_SRC" "$CGI_SRC" "$CGI_LIB_SRC" .ai-dev/quality/checks/lib_check.sh; do
  [ -f "$f" ] || { echo "web-creds-commit: FAIL — not found: $f"; exit 1; }
done
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

SB="$(mktemp -d "${TMPDIR:-/tmp}/web-creds-commit.XXXXXX")" || { echo "web-creds-commit: FAIL — mktemp"; exit 1; }
trap 'chmod -R u+w "$SB" 2>/dev/null; rm -rf -- "$SB"' EXIT
mkdir -p "$SB/shim" "$SB/etc" "$SB/tmp" "$SB/lib" "$SB/cgi" "$SB/in"
TARGET="$SB/etc/sa02m_web.env"
PLANT="$SB/tmp/sa02m_web.env.new"
H="$SB/sa02m-commit-web-env"

# retarget SRC DST SED-EXPR… — every absolute root the expressions name goes
# under @SB@ first; any OTHER absolute root still on a live line refuses the
# copy (the harness must never touch the host), then @SB@ becomes the sandbox.
# $ALLOW is an ERE of absolute paths the copy may keep (the sudo argv).
retarget() {
  local src="$1" dst="$2" txt left
  shift 2
  txt=$(sed "$@" "$src") || return 1
  left=$(strip_comments_inline <<<"$txt" | sed -e 's#@SB@/[A-Za-z0-9_./-]*##g' ${ALLOW:+-e "s#$ALLOW##g"} \
         | grep -nE '(^|[^A-Za-z0-9_.$}@-])/(etc|tmp|run|var|usr|opt|home|root)/' || true)
  if [ -n "$left" ]; then
    printf '%s\n' "$left" | sed 's/^/        un-retargeted: /'
    return 2
  fi
  printf '%s\n' "${txt//@SB@/$SB}" > "$dst"
}

echo "A. setup"
ALLOW='' retarget "$HELPER_SRC" "$H" \
  -e 's#/etc/sa02m_web\.env#@SB@/etc/sa02m_web.env#g' \
  -e 's#/usr/local/lib/#@SB@/lib/#g' \
  -e 's#/tmp/#@SB@/tmp/#g'
rc=$?
cp "$HELPER_LIB_SRC" "$SB/lib/sa02m-web-auth-lib.sh"
if [ "$rc" -ne 0 ]; then
  bad "A1 helper copy refused (rc=$rc) — an absolute root the harness does not retarget"
elif ! text_has "$(stripped_text "$H")" "$TARGET"; then
  bad "A1 helper copy does not install into the sandbox target — the install line moved or the target path changed"
elif ! text_has "$(stripped_text "$H")" "$SB/lib/sa02m-web-auth-lib.sh"; then
  bad "A1 helper copy does not source the sandbox lib"
else
  ok "A1 helper retargeted (install target + lib under the sandbox, no host root left)"
fi
# The commit's timeout is shortened to 2 s in the copy so C5 can wait it out.
ALLOW='/usr/local/sbin/sa02m-commit-web-env' retarget "$CGI_SRC" "$SB/cgi/web_creds.cgi" \
  -e 's#/etc/sa02m_web\.env#@SB@/etc/sa02m_web.env#g' \
  -e 's#/tmp/#@SB@/tmp/#g' \
  -e 's#timeout 10 sudo -n /usr/local/sbin/sa02m-commit-web-env#timeout 2 sudo -n /usr/local/sbin/sa02m-commit-web-env#'
rc=$?
cp "$CGI_LIB_SRC" "$SB/cgi/lib_web_auth.sh"
if [ "$rc" -ne 0 ]; then
  bad "A2 CGI copy refused (rc=$rc)"
elif ! text_has "$(stripped_text "$SB/cgi/web_creds.cgi")" "$TARGET"; then
  bad "A2 CGI copy does not read the sandbox credential file"
else
  ok "A2 CGI retargeted (credential file under the sandbox, only the helper's sudo path kept)"
fi
[ "$fails" -eq 0 ] || { echo "web-creds-commit: $fails FAILURE(S) in setup — nothing below would be meaningful"; exit 1; }

# ── shims ───────────────────────────────────────────────────────────────────
cat > "$SB/shim/install" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$SB/install.calls"
n=\$#
src="\${@:n-1:1}"; dst="\${@:n:1}"
cp -- "\$src" "\$dst"
EOF
cat > "$SB/shim/sudo" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$SB/sudo.calls"
ls -A "$SB/tmp" > "$SB/tmp-at-sudo" 2>/dev/null
[ -e "$SB/sudo.fail" ] && exit 1
# exec: the process timeout(1) kills IS the sleeper, so no orphan keeps the
# CGI's stdout open.
[ -e "$SB/sudo.hang" ] && exec sleep 30
[ "\${1:-}" = -n ] && shift
[ "\${1:-}" = /usr/local/sbin/sa02m-commit-web-env ] || exit 1
shift
exec bash "$H" "\$@"
EOF
chmod +x "$SB/shim/install" "$SB/shim/sudo"
export PATH="$SB/shim:$PATH"

ORIG="SA02M_WEB_USER='admin'
SA02M_WEB_PASS='oldpass1'"
ATTACK="SA02M_WEB_USER='mallory'
SA02M_WEB_PASS='pwned123'"
HASH='$6$abcdEFGH12345678$'"$(printf 'a%.0s' $(seq 1 86))"

reset_target() {
  chmod -R u+w "$SB/tmp" 2>/dev/null
  rm -rf -- "$SB/tmp" "$SB/install.calls" "$SB/sudo.calls" "$SB/sudo.fail" "$SB/tmp-at-sudo"
  mkdir -p "$SB/tmp"
  printf '%s\n' "$ORIG" > "$TARGET"
  cp "$TARGET" "$SB/orig.env"
}
plant() { printf '%s\n' "$ATTACK" > "$PLANT"; cp "$PLANT" "$SB/plant.orig"; }
# run_helper INPUT-FILE → rc in $RC
run_helper() { bash "$H" < "$1" >/dev/null 2>"$SB/helper.err"; RC=$?; }
target_unchanged() { cmp -s "$TARGET" "$SB/orig.env"; }
installed() { [ -s "$SB/install.calls" ]; }
work_left() { local l; l=$(ls -A "$SB/tmp" 2>/dev/null | grep -v '^sa02m_web\.env\.new$' || true); [ -n "$l" ]; }
expect_refused() {  # label input
  reset_target; run_helper "$2"
  if [ "$RC" -ne 0 ] && target_unchanged && ! installed; then ok "$1 → refused (rc=$RC), nothing installed"
  else bad "$1 → want refusal; rc=$RC, installed=$(installed && echo yes || echo no), target: $(tr '\n' ';' < "$TARGET")"; fi
}

echo "H. helper"
# H1 messy but valid → normalised
printf 'SA02M_WEB_USER="admin2"\r\nSA02M_WEB_PASS="newpass1"\r\n# junk line\n' > "$SB/in/h1"
reset_target; run_helper "$SB/in/h1"
want="SA02M_WEB_USER='admin2'
SA02M_WEB_PASS='newpass1'"
if [ "$RC" -eq 0 ] && [ "$(cat "$TARGET")" = "$want" ]; then ok "H1 valid messy stdin → installed normalised"
else bad "H1 valid stdin → want rc=0 + normalised file; rc=$RC, target: $(tr '\n' ';' < "$TARGET") err: $(head -c 300 "$SB/helper.err")"; fi
calls=$(cat "$SB/install.calls" 2>/dev/null || true)
if [[ "$calls" == "-m 640 -o root -g www-data "*" $TARGET" ]] && [ "$(grep -c . <<<"$calls")" -eq 1 ]; then
  ok "H1 install called once: -m 640 -o root -g www-data … <target>"
else bad "H1 install argv → got: ${calls:-<never called>}"; fi
if work_left; then bad "H1 the helper left work files in /tmp: $(ls -A "$SB/tmp" | tr '\n' ' ')"
else ok "H1 no work file left behind (trap cleanup)"; fi

# H2 hash
printf "SA02M_WEB_USER='admin3'\nSA02M_WEB_PASS_HASH='%s'\n" "$HASH" > "$SB/in/h2"
reset_target; run_helper "$SB/in/h2"
if [ "$RC" -eq 0 ] && [ "$(cat "$TARGET")" = "SA02M_WEB_USER='admin3'
SA02M_WEB_PASS_HASH='$HASH'" ]; then ok "H2 \$6\$ hash on stdin → installed as SA02M_WEB_PASS_HASH"
else bad "H2 hash stdin → rc=$RC, target: $(tr '\n' ';' < "$TARGET")"; fi

# H3 planted + empty stdin
: > "$SB/in/empty"
reset_target; plant; run_helper "$SB/in/empty"
if [ "$RC" -ne 0 ] && target_unchanged && ! installed; then ok "H3 planted /tmp staging + empty stdin → refused, planted credentials NOT installed"
else bad "H3 planted /tmp staging + empty stdin → rc=$RC, target: $(tr '\n' ';' < "$TARGET") — the attacker's file reached root"; fi

# H4 planted + valid stdin → stdin wins
reset_target; plant; run_helper "$SB/in/h1"
if [ "$RC" -eq 0 ] && [ "$(cat "$TARGET")" = "$want" ]; then ok "H4 planted staging + valid stdin → the stdin credentials land"
else bad "H4 planted staging + valid stdin → rc=$RC, target: $(tr '\n' ';' < "$TARGET")"; fi

# H5 empty, nothing planted
expect_refused "H5 empty stdin, nothing planted" "$SB/in/empty"

# H6 malformed
printf "SA02M_WEB_USER='ad min;x'\nSA02M_WEB_PASS='newpass1'\n" > "$SB/in/h6a"
printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS='ab\$(id)cd'\n" > "$SB/in/h6b"
printf "SA02M_WEB_USER='admin'\n" > "$SB/in/h6c"
printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS_HASH='\$6\$salt\$short'\n" > "$SB/in/h6d"
printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS='abc'\n" > "$SB/in/h6e"
expect_refused "H6a username outside the charset" "$SB/in/h6a"
expect_refused "H6b \$( in the password" "$SB/in/h6b"
expect_refused "H6c no password line" "$SB/in/h6c"
expect_refused "H6d mis-shaped hash" "$SB/in/h6d"
expect_refused "H6e 3-char password" "$SB/in/h6e"

# H7 size bound: valid lines + padding. Over → refused; exactly the bound → installed.
mk_padded() {  # total-bytes out
  local head="SA02M_WEB_USER='admin4'
SA02M_WEB_PASS='newpass4'
# " n
  n=$(( $1 - ${#head} - 1 ))
  { printf '%s' "$head"; printf 'x%.0s' $(seq 1 "$n"); printf '\n'; } > "$2"
}
mk_padded 4097 "$SB/in/h7over"
mk_padded 4096 "$SB/in/h7exact"
sz_over=$(wc -c < "$SB/in/h7over"); sz_exact=$(wc -c < "$SB/in/h7exact")
if [ "${sz_over//[[:space:]]/}" -ne 4097 ] || [ "${sz_exact//[[:space:]]/}" -ne 4096 ]; then
  bad "H7 fixture sizes wrong (over=$sz_over exact=$sz_exact) — the case would test nothing"
else
  expect_refused "H7a 4097-byte stdin (valid lines + padding)" "$SB/in/h7over"
  reset_target; run_helper "$SB/in/h7exact"
  if [ "$RC" -eq 0 ] && [ "$(sed -n 1p "$TARGET")" = "SA02M_WEB_USER='admin4'" ]; then ok "H7b exactly 4096 bytes → installed (the bound is 4096)"
  else bad "H7b 4096-byte stdin → want installed; rc=$RC, target: $(tr '\n' ';' < "$TARGET")"; fi
fi

echo "C. web_creds.cgi → helper"
export SA02M_SESSION_DIR="$SB/sessions" SA02M_LOGIN_DIR="$SB/login"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$SB/cgi/lib_web_auth.sh"
for fn in web_session_create web_csrf_token_for_session web_auth_read web_auth_stored_secret web_auth_verify; do
  declare -F "$fn" >/dev/null || { bad "C0 lib_web_auth.sh does not define $fn"; }
done
run_cgi() {  # body → JSON tail on stdout
  local tok csrf body="$1" out
  tok=$(web_session_create admin); csrf=$(web_csrf_token_for_session "$tok")
  out=$(printf '%s' "$body" | REQUEST_METHOD=POST CONTENT_LENGTH="${#body}" \
        HTTP_COOKIE="session_token=$tok" HTTP_X_SA02M_CSRF="$csrf" \
        bash "$SB/cgi/web_creds.cgi" 2>/dev/null | tr -d '\r')
  printf '%s' "${out##*$'\n\n'}"
}
REQ='current_password=oldpass1&new_username=operator&new_password=Newpass77&new_password_confirm=Newpass77'
creds_are_new() {
  SA02M_WEB_USER=""; SA02M_WEB_PASS=""; SA02M_WEB_PASS_HASH=""
  web_auth_read "$TARGET" && [ "$SA02M_WEB_USER" = operator ] && web_auth_verify Newpass77 "$(web_auth_stored_secret)"
}

# C1
reset_target; body=$(run_cgi "$REQ")
sudo_argv=$(cat "$SB/sudo.calls" 2>/dev/null || true)
if [[ "$body" == *'"ok":true'* ]] && creds_are_new; then ok "C1 valid change → ok:true, the new user + password verify against the installed file"
else bad "C1 valid change → body: $body; target: $(tr '\n' ';' < "$TARGET")"; fi
if [ "$sudo_argv" = "-n /usr/local/sbin/sa02m-commit-web-env" ]; then ok "C1 sudo argv exactly: -n /usr/local/sbin/sa02m-commit-web-env"
else bad "C1 sudo argv → got: ${sudo_argv:-<never called>}"; fi
# The snapshot is the no-write proof: a helper that deletes its staging file
# afterwards (the old one did) would leave /tmp empty AFTER the run.
if [ ! -f "$SB/tmp-at-sudo" ]; then bad "C1 the sudo shim took no /tmp snapshot — sudo never ran, so 'nothing staged' would be vacuous"
elif [ -s "$SB/tmp-at-sudo" ]; then bad "C1 /tmp held a file when sudo was called: $(tr '\n' ' ' < "$SB/tmp-at-sudo")"
else ok "C1 /tmp empty at the moment sudo is called — the CGI staged nothing"; fi
if [ -n "$(ls -A "$SB/tmp" 2>/dev/null)" ]; then bad "C1 something was left in /tmp: $(ls -A "$SB/tmp" | tr '\n' ' ')"
else ok "C1 nothing left in /tmp afterwards"; fi

# C2 commit fails
reset_target; : > "$SB/sudo.fail"; body=$(run_cgi "$REQ")
if [[ "$body" == *'"error":"save_failed"'* ]] && [[ "$body" != *'"ok":true'* ]] && target_unchanged; then ok "C2 commit failing → save_failed, target unchanged"
else bad "C2 commit failing → body: $body"; fi

# C3 planted, unwritable staging
reset_target; plant; chmod 444 "$PLANT"; body=$(run_cgi "$REQ")
if [[ "$body" == *'"ok":true'* ]] && creds_are_new; then ok "C3 planted 0444 /tmp staging → the Operator's credentials land"
else bad "C3 planted 0444 /tmp staging → want ok:true + the Operator's credentials; body: $body; target: $(tr '\n' ';' < "$TARGET")"; fi
if cmp -s "$PLANT" "$SB/plant.orig"; then ok "C3 the planted file is byte-unchanged (the CGI never wrote there)"
else bad "C3 the planted file changed"; fi

# C4 wrong current password: refused before any commit
reset_target; body=$(run_cgi 'current_password=nope9999&new_username=operator&new_password=Newpass77&new_password_confirm=Newpass77')
if [[ "$body" == *'"error":"wrong_password"'* ]] && [ ! -e "$SB/sudo.calls" ] && target_unchanged; then ok "C4 wrong current password → wrong_password, sudo never called"
else bad "C4 wrong current password → body: $body; sudo: $(cat "$SB/sudo.calls" 2>/dev/null || echo none)"; fi

# C5 a hung commit is bounded: the copy's 2 s timeout must fire, answer
# save_failed and install nothing. The harness's own 15 s outer bound catches a
# CGI with no timeout (or one this copy failed to shorten — the count below).
n_bound=$(stripped_count "$SB/cgi/web_creds.cgi" 'timeout 2 sudo -n /usr/local/sbin/sa02m-commit-web-env')
if [ "$n_bound" -ne 1 ]; then
  bad "C5 the CGI copy carries $n_bound 'timeout 2 sudo -n …' line(s) after shortening, want 1 — the commit line lost its 'timeout 10' or changed shape (re-pin the sed above)"
fi
reset_target; : > "$SB/sudo.hang"
tok=$(web_session_create admin); csrf=$(web_csrf_token_for_session "$tok")
t0=$SECONDS
out=$(printf '%s' "$REQ" | REQUEST_METHOD=POST CONTENT_LENGTH="${#REQ}" \
      HTTP_COOKIE="session_token=$tok" HTTP_X_SA02M_CSRF="$csrf" \
      timeout 15 bash "$SB/cgi/web_creds.cgi" 2>/dev/null | tr -d '\r')
dt=$((SECONDS - t0)); body="${out##*$'\n\n'}"
if [[ "$body" == *'"error":"save_failed"'* ]] && [[ "$body" != *'"ok":true'* ]] && target_unchanged && ! installed && [ "$dt" -lt 15 ]; then
  ok "C5 hung commit → save_failed after the timeout (${dt}s), nothing installed"
elif [ "$dt" -ge 15 ]; then
  bad "C5 hung commit → the outer 15 s bound killed the CGI: the commit is not bounded by a timeout"
else
  bad "C5 hung commit → want save_failed + no install; ${dt}s, body: $body, installed=$(installed && echo yes || echo no)"
fi
rm -f "$SB/sudo.hang"

echo "S. static"
cgi_tmp=$(stripped_count "$CGI_SRC" '/tmp')
helper_stage=$(stripped_count "$HELPER_SRC" 'sa02m_web[.]env[.]new')
if [ "$cgi_tmp" -eq 0 ] && [ "$helper_stage" -eq 0 ]; then ok "S1 no live /tmp in web_creds.cgi, no live staging path in the helper"
else bad "S1 live /tmp lines in web_creds.cgi: $cgi_tmp; live sa02m_web.env.new lines in the helper: $helper_stage"; fi

if [ "$fails" -eq 0 ]; then echo "web-creds-commit: ALL OK"; exit 0; fi
echo "web-creds-commit: $fails FAILURE(S)"; exit 1
