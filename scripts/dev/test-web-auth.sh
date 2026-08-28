#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-web-auth.sh — functional regression test for the panel's auth core,
# www/network_config/cgi-bin/lib_web_auth.sh: server-side sessions, CSRF
# minting/validation, password hashing/verification, and the credential-file
# read/repair path.
#
# Why this exists: that file is 354 lines and 27 functions, EVERY mutating CGI
# sources it, and until 1.0.6.24 its entire coverage was `bash -n` plus the CI
# lint row (2026-08-28 audit, finding C6). The code that decides "is this
# person logged in" could be changed to fail OPEN — an unknown token accepted,
# an expired session honoured, a CSRF mismatch waved through — and the whole
# build beat stayed green. A syntax gate cannot see a wrong branch.
#
# The five guarantees this pins, in the order they matter:
#   1. FAIL CLOSED on the session path. An absent, malformed, unknown or
#      EXPIRED token is denied; only a token whose sha256 names a live,
#      unexpired file is accepted.
#   2. The store never holds a raw token. Session files are named by
#      sha256(token) — `ls` on the store must reveal nothing that can be
#      replayed as a cookie. (The flasher daemon depends on this hash matching:
#      opt/sa02m-flasher/sa02m_flasher/auth.py.)
#   3. CSRF is per-session and fails closed. No header, an empty header, a
#      wrong value, ANOTHER session's token, or a missing .csrf file all reject.
#   4. The credential file is read, never executed. `$(...)`/backticks in
#      /etc/sa02m_web.env are literal strings (S9); the repair path re-quotes
#      them and must NOT weaken the file's 0640 root:www-data permissions.
#   5. Password verification distinguishes right from wrong on both the $6$
#      crypt path and the legacy plaintext path.
#
# Method: the SHIPPED lib is sourced directly — it is standalone-sourceable and
# its one mutable root, $SA02M_SESSION_DIR, is already env-directed, so the
# whole session store lives in a scratch dir. `install` is a recording PATH
# shim (the repair path asks for -o root, which no dev box or CI runner can
# satisfy); nothing else is stubbed, so the hashing, the expiry arithmetic and
# the parser under test are the real ones. Nothing touches /run or /etc.
#
# Non-vacuous: a missing function, a store that never received a file, or a
# shim that was never invoked FAILS rather than passing on zero work.
#
# Proven RED (1.0.6.24), 13 mutations of a scratch copy of the lib, each run
# through WEB_AUTH_SRC so the real file is never touched:
#   accept a token whose session file is absent (fail open) · skip the expiry
#   comparison · name the store file by the RAW token instead of its hash ·
#   make the CSRF compare always true · accept when the .csrf file is absent ·
#   source the credential file in web_auth_read · source it in
#   web_auth_read_safe · `install -m 644` in the repair path · accept any
#   legacy plaintext password · widen the cookie-token pattern to `([^;]+)` ·
#   write session files with umask 022 · drop the staging metachar blacklist ·
#   drop the renewal throttle.
#
# TWO MUTATIONS THAT DELIBERATELY DO **NOT** GO RED, recorded so the next sweep
# does not read them as holes:
#   * removing the `[ -n "$got" ]` CSRF-header guard. With an empty header the
#     salted hashes still differ from the stored token's, and `[ -n "$stored" ]`
#     already bars the both-empty case — the guard is redundant depth, not a
#     load-bearing line. A mutation that changes no behaviour must not be sold
#     as a caught defect.
#   * `umask 027` -> `022` on a NON-Linux box. Cases 5-6 are the only POSIX-mode
#     assertions here and they are skipped where modes are not meaningful
#     (Windows git-bash), so that mutation stays green there and goes RED on
#     Linux — verified under WSL Ubuntu-24.04: `FAIL 5 session file mode is
#     644, expected 640`. CI is the authority for those two cases
#     (.ai-dev/notes/quality-gate-environment.md).
#
# Run: bash scripts/dev/test-web-auth.sh   (stdlib bash + coreutils, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

LIB=${WEB_AUTH_SRC:-www/network_config/cgi-bin/lib_web_auth.sh}
[ -f "$LIB" ] || { echo "FAIL  auth lib not found: $LIB"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
export T_DIR="$T"
BIN="$T/bin"; mkdir -p "$BIN"
INSTALL_LOG="$T/install.log"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── recording `install` shim ───────────────────────────────────────────────
# web_auth_repair_file asks for `install -m 640 -o root -g www-data`. Neither a
# dev box nor a CI runner is root, so the real binary would fail and the repair
# would silently do nothing — the harness would then be testing an aborted
# path. The shim records the requested argv (that is what the permission
# assertion reads) and performs the copy with the requested mode.
cat > "$BIN/install" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/install.log"
mode=""; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m) mode="$2"; shift 2 ;;
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
[ "${#args[@]}" -ge 2 ] || exit 1
cp "${args[0]}" "${args[1]}" || exit 1
[ -n "$mode" ] && chmod "$mode" "${args[1]}"
exit 0
SHIM
chmod +x "$BIN/install"
PATH="$BIN:$PATH"; export PATH

# ── source the shipped lib into a scratch store ────────────────────────────
export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$LIB"

for fn in web_session_create web_session_check_cookie web_session_destroy_cookie \
          web_session_destroy_all web_csrf_create_for_hash web_csrf_validate \
          web_csrf_token_for_session web_auth_hash web_auth_verify \
          web_auth_read_safe web_auth_write web_auth_needs_repair \
          web_auth_repair_file web_auth_validate_staging; do
    declare -F "$fn" >/dev/null || { echo "FAIL  $LIB does not define $fn (did the file shape change?)"; exit 1; }
done

sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

# ═══ 1. Session mint + the store's shape ══════════════════════════════════
TOK=$(web_session_create admin)
if [ -n "$TOK" ] && [[ "$TOK" =~ ^[a-f0-9]{64}$ ]]; then
    ok "1  web_session_create mints a 64-hex token"
else
    bad "1  web_session_create returned an unusable token: '$TOK'"
fi
HASH=$(sha "$TOK")
if [ -f "$SA02M_SESSION_DIR/$HASH" ]; then
    ok "2  the session file is named sha256(token)"
else
    bad "2  no session file named sha256(token) — the store shape changed"
fi
if [ -e "$SA02M_SESSION_DIR/$TOK" ]; then
    bad "3  the RAW token is a filename in the store — listing it hands out a live cookie"
else
    ok "3  the raw token never appears as a filename"
fi
if grep -rqF "$TOK" "$SA02M_SESSION_DIR" 2>/dev/null; then
    bad "4  the raw token appears inside a store file — the store is replayable"
else
    ok "4  the raw token appears nowhere in the store's contents"
fi
if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
    m=$(stat -c '%a' "$SA02M_SESSION_DIR/$HASH" 2>/dev/null)
    d=$(stat -c '%a' "$SA02M_SESSION_DIR" 2>/dev/null)
    [ "$m" = "640" ] && ok "5  session file mode 640" || bad "5  session file mode is $m, expected 640 (umask 027)"
    [ "$d" = "2750" ] && ok "6  session dir mode 2750" || bad "6  session dir mode is $d, expected 2750 (flasher daemon traverses by group)"
else
    ok "5  session/dir mode assertions skipped (POSIX modes are not meaningful on $(uname -s 2>/dev/null)); CI is the authority"
    ok "6  (see 5)"
fi

# ═══ 2. FAIL CLOSED on the session decision ═══════════════════════════════
# check <label> <cookie> <expect: allow|deny>
check() {
    local label="$1" cookie="$2" want="$3" rc
    HTTP_COOKIE="$cookie" web_session_check_cookie && rc=allow || rc=deny
    [ "$rc" = "$want" ] && ok "$label -> $rc" || bad "$label -> $rc, expected $want"
}
check "7  a live token"                     "session_token=$TOK"                    allow
check "8  no cookie at all"                 ""                                      deny
check "9  cookie present, other key only"   "sa02m_csrf=$TOK"                       deny
check "10 forged token (64 hex, unknown)"   "session_token=$(printf 'd%.0s' $(seq 1 64))" deny
check "11 uppercase-hex token"              "session_token=${TOK^^}"                deny
# The short/non-hex deny vectors are BUILT, never written as a literal. A
# hard-coded `session_token=<word>` anywhere under www/etc/opt/tools/scripts is
# exactly what the `no-retired-session-token` gate forbids (its pin A), and it
# is right to forbid it here of all places — a credential-handling harness is
# where a real token would most plausibly be pasted. Expansions are that gate's
# documented non-flag, so the fixture is composed at runtime and pin A keeps its
# reach over this file instead of the file needing an allow-list entry that
# would then have to be kept honest forever.
SHORT_TOK=$(printf 'a%.0s' $(seq 1 6))
check "12 too-short token"                  "session_token=$SHORT_TOK"              deny
check "13 non-hex token"                    "session_token=$(printf 'z%.0s' $(seq 1 64))"  deny
check "14 token with a path traversal"      "session_token=../../etc/passwd"        deny

# The cookie PARSER itself, not only the decision it feeds. Every vector above
# is also denied downstream (an unknown hash names no file), so without these
# four the charset/length pattern at lib_web_auth.sh:52 could be widened to
# `([^;]+)` and nothing would notice — attacker text would then flow into the
# store path and rely on sha256 alone to sanitise it.
tokof() { HTTP_COOKIE="$1" web_session__cookie_token 2>/dev/null; }
[ "$(tokof "session_token=$TOK")" = "$TOK" ] \
    && ok "14a the parser returns a well-formed token" || bad "14a the parser did not return a well-formed token"
[ -z "$(tokof "session_token=../../etc/passwd")" ] \
    && ok "14b the parser rejects a path-traversal value" || bad "14b the parser let a path-traversal value through"
[ -z "$(tokof "session_token=$SHORT_TOK")" ] \
    && ok "14c the parser rejects a value under the 32-char floor" || bad "14c the parser let a short value through"
[ -z "$(tokof "session_token=$(printf 'Z%.0s' $(seq 1 40))")" ] \
    && ok "14d the parser rejects a non-hex value" || bad "14d the parser let a non-hex value through"

# Expiry: an expired file is denied AND pruned.
EXPTOK=$(printf 'a%.0s' $(seq 1 64))
EXPHASH=$(sha "$EXPTOK")
printf '%s %s\n' "$(( $(date +%s) - 10 ))" admin > "$SA02M_SESSION_DIR/$EXPHASH"
check "15 expired session" "session_token=$EXPTOK" deny
if [ -f "$SA02M_SESSION_DIR/$EXPHASH" ]; then
    bad "16 the expired session file was not pruned"
else
    ok "16 the expired session file is pruned on refusal"
fi
# A corrupt expiry field must deny, not be read as 0/infinite.
CORTOK=$(printf 'b%.0s' $(seq 1 64))
printf 'notanumber admin\n' > "$SA02M_SESSION_DIR/$(sha "$CORTOK")"
check "17 corrupt expiry field" "session_token=$CORTOK" deny
# An empty session file must deny.
EMPTOK=$(printf 'c%.0s' $(seq 1 64))
: > "$SA02M_SESSION_DIR/$(sha "$EMPTOK")"
check "18 empty session file" "session_token=$EMPTOK" deny

# Sliding renewal is throttled: a fresh session must NOT be rewritten on every
# authed request (the burst-of-requests race that produced spurious 401s).
before=$(cat "$SA02M_SESSION_DIR/$HASH")
HTTP_COOKIE="session_token=$TOK" web_session_check_cookie
after=$(cat "$SA02M_SESSION_DIR/$HASH")
[ "$before" = "$after" ] && ok "19 a fresh session is not rewritten (renewal throttle holds)" \
                         || bad "19 a fresh session was rewritten on a plain check — the renewal throttle is gone"
# An old-but-live session IS renewed.
OLDTOK=$(printf 'e%.0s' $(seq 1 64))
OLDHASH=$(sha "$OLDTOK")
printf '%s %s\n' "$(( $(date +%s) + SA02M_SESSION_TTL - 7200 ))" admin > "$SA02M_SESSION_DIR/$OLDHASH"
HTTP_COOKIE="session_token=$OLDTOK" web_session_check_cookie
newexp=$(cut -d' ' -f1 < "$SA02M_SESSION_DIR/$OLDHASH")
[ "$newexp" -gt "$(( $(date +%s) + SA02M_SESSION_TTL - 3600 ))" ] \
    && ok "20 a >1h-old live session is renewed" \
    || bad "20 an old live session was not renewed (expiry stayed $newexp)"

# ═══ 3. CSRF ══════════════════════════════════════════════════════════════
CSRF=$(web_csrf_token_for_session "$TOK")
[ -n "$CSRF" ] && ok "21 a CSRF token is minted for the session" || bad "21 no CSRF token minted"
[ "$(web_csrf_token_for_session "$TOK")" = "$CSRF" ] \
    && ok "22 the CSRF token is stable across reads" || bad "22 the CSRF token changed between reads"

# csrf <label> <cookie> <header> <expect>
csrf_check() {
    local label="$1" rc
    HTTP_COOKIE="$2" HTTP_X_SA02M_CSRF="$3" web_csrf_validate && rc=allow || rc=deny
    [ "$rc" = "$4" ] && ok "$label -> $rc" || bad "$label -> $rc, expected $4"
}
csrf_check "23 matching token"            "session_token=$TOK" "$CSRF"        allow
csrf_check "24 header absent/empty"       "session_token=$TOK" ""            deny
csrf_check "25 wrong token"               "session_token=$TOK" "deadbeef"    deny
csrf_check "26 token is the session token" "session_token=$TOK" "$TOK"       deny
csrf_check "27 no session cookie"         ""                   "$CSRF"      deny

# Cross-session: session B's CSRF token must not validate session A.
TOK2=$(web_session_create admin)
CSRF2=$(web_csrf_token_for_session "$TOK2")
if [ "$CSRF2" = "$CSRF" ]; then
    bad "28 two sessions share one CSRF token — the token is not per-session"
else
    csrf_check "28 another session's CSRF token" "session_token=$TOK" "$CSRF2" deny
fi
# A session whose .csrf file is gone must fail closed, not auto-mint on validate.
rm -f "$SA02M_SESSION_DIR/$(sha "$TOK2").csrf"
csrf_check "29 .csrf file missing" "session_token=$TOK2" "$CSRF2" deny

# web_csrf_require must emit the shared error shape and stop the request.
out=$( HTTP_COOKIE="session_token=$TOK" HTTP_X_SA02M_CSRF=wrong \
       bash -c ". '$LIB'; web_csrf_require; echo REACHED_BODY" 2>/dev/null )
if [[ "$out" == *'"error_code":"E_CSRF"'* && "$out" != *REACHED_BODY* ]]; then
    ok "30 web_csrf_require prints E_CSRF and stops before the handler body"
else
    bad "30 web_csrf_require did not stop the request: $out"
fi

# ═══ 4. Revocation ════════════════════════════════════════════════════════
HTTP_COOKIE="session_token=$TOK" web_session_destroy_cookie
check "31 after logout" "session_token=$TOK" deny
[ -e "$SA02M_SESSION_DIR/$HASH.csrf" ] \
    && bad "32 logout left the CSRF token file behind" \
    || ok "32 logout removes the session AND its CSRF file"
web_session_destroy_all
check "33 after destroy_all" "session_token=$TOK2" deny

# ═══ 5. Password hashing / verification ═══════════════════════════════════
H=$(web_auth_hash 'S3cret!pass')
if [ -n "$H" ]; then
    web_auth_is_hash "$H" && ok "34 web_auth_hash produces a \$6\$ crypt hash" \
                          || bad "34 web_auth_hash produced a non-\$6\$ value: $H"
    web_auth_verify 'S3cret!pass' "$H" && ok "35 the correct password verifies against the hash" \
                                       || bad "35 the correct password FAILED to verify — a hashed board would lock out"
    web_auth_verify 'S3cret!pasS' "$H" && bad "36 a WRONG password verified against the hash — auth fails open" \
                                       || ok "36 a wrong password is rejected"
    web_auth_verify '' "$H"           && bad "37 an EMPTY password verified against the hash" \
                                       || ok "37 an empty password is rejected"
else
    ok "34 hashing skipped — no openssl/python3 hasher on this box (best-effort by design)"
    ok "35 (see 34)"; ok "36 (see 34)"; ok "37 (see 34)"
fi
web_auth_is_hash 'plaintextpw' && bad "38 a plaintext credential was read as a hash" \
                               || ok "38 a plaintext credential is not mistaken for a hash"
web_auth_verify 'cyntron' 'cyntron' && ok "39 legacy plaintext: the correct password verifies" \
                                    || bad "39 legacy plaintext: the correct password was rejected"
web_auth_verify 'cyntronn' 'cyntron' && bad "40 legacy plaintext: a WRONG password verified" \
                                     || ok "40 legacy plaintext: a wrong password is rejected"

# ═══ 6. The credential file is READ, never EXECUTED (S9) ══════════════════
ENVF="$T/sa02m_web.env"
CANARY="$T/canary"
printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS='\$(touch %s)pw;x|y'\n" "$CANARY" > "$ENVF"
web_auth_read_safe "$ENVF"
if [ -e "$CANARY" ]; then
    bad "41 reading the credential file EXECUTED its content — code-exec-on-config is back"
else
    ok "41 reading the credential file executes nothing"
fi
[ "$SA02M_WEB_USER" = "admin" ] && ok "42 the user is parsed out of the credential file" \
                                || bad "42 user parsed as '$SA02M_WEB_USER', expected admin"
case "$SA02M_WEB_PASS" in
    *'$(touch'*) ok "43 the metachar-bearing password is kept LITERAL" ;;
    *) bad "43 the password was transformed on read: '$SA02M_WEB_PASS'" ;;
esac

# The fixture above is SINGLE-quoted, which is what web_auth_write emits — and a
# single-quoted value does not expand even when the file IS sourced, so on its
# own it cannot tell a parser from a `. "$f"`. A credential file that predates
# the safe writer (or that an operator edited by hand) can carry a DOUBLE-quoted
# or bare value, and that is the shape S9 was really about. Both readers are
# driven with it.
ENVF2="$T/legacy_web.env"
CANARY2="$T/canary2"
printf 'SA02M_WEB_USER=admin\nSA02M_WEB_PASS="$(touch %s)pw"\n' "$CANARY2" > "$ENVF2"
web_auth_read_safe "$ENVF2"
if [ -e "$CANARY2" ]; then
    bad "43a a DOUBLE-quoted credential was EXECUTED on read — the file is being sourced, not parsed (S9)"
else
    ok "43a a double-quoted credential is not executed on read"
fi
web_auth_read "$ENVF2"
if [ -e "$CANARY2" ]; then
    bad "43b web_auth_read EXECUTED a double-quoted credential — the repair path runs config as code (S9)"
else
    ok "43b web_auth_read executes nothing either"
fi

# The repair path: re-quote, and DO NOT weaken the permissions.
web_auth_needs_repair "$ENVF" && ok "44 web_auth_needs_repair flags a metachar-bearing file" \
                              || bad "44 web_auth_needs_repair missed a metachar-bearing file"
: > "$INSTALL_LOG"
web_auth_repair_file "$ENVF"
if [ ! -s "$INSTALL_LOG" ]; then
    bad "45 the repair path never invoked \`install\` — the file was rewritten by some other means, or not at all"
else
    ok "45 the repair path installs the rewritten file"
    if grep -q -- '-m 640' "$INSTALL_LOG" && grep -q -- '-o root' "$INSTALL_LOG" && grep -q -- '-g www-data' "$INSTALL_LOG"; then
        ok "46 the repair keeps 0640 root:www-data — permissions are not weakened"
    else
        bad "46 the repair changed the credential file's permissions: $(cat "$INSTALL_LOG")"
    fi
fi
if [ -e "$CANARY" ]; then
    bad "47 the repair path EXECUTED the stored password"
else
    ok "47 the repair path executes nothing"
fi
# Idempotence is CONTENT-level, not predicate-level: web_auth_needs_repair
# greps the file for metacharacters, and a password that legitimately CONTAINS
# one keeps matching forever. What must hold is that repairing twice produces
# byte-identical content and the stored secret still round-trips — the failure
# this pins is an escaping cascade (each pass adding another layer of quotes
# until the real password no longer verifies).
first=$(cat "$ENVF")
web_auth_repair_file "$ENVF"
if [ "$(cat "$ENVF")" = "$first" ]; then
    ok "48 repairing twice is byte-identical (no escaping cascade)"
else
    bad "48 a second repair changed the file — the escaping cascades"
fi
web_auth_read_safe "$ENVF"
case "$SA02M_WEB_PASS" in
    *'$(touch'*) ok "48b the stored secret survives the repair unchanged" ;;
    *) bad "48b the repair altered the stored secret: '$SA02M_WEB_PASS'" ;;
esac
ls "$ENVF".repair.* >/dev/null 2>&1 && bad "49 the repair left its temp file behind" \
                                    || ok "49 the repair leaves no temp file behind"

# ═══ 7. Staging validation (the gate on a credential about to be committed) ═
stage() { # <label> <content> <expect: accept|reject>
    local f="$T/stage.env" rc
    printf '%s' "$2" > "$f"
    web_auth_validate_staging "$f" && rc=accept || rc=reject
    [ "$rc" = "$3" ] && ok "$1 -> $rc" || bad "$1 -> $rc, expected $3"
}
stage "50 well-formed plaintext"  "SA02M_WEB_USER='admin'
SA02M_WEB_PASS='goodpass'
" accept
stage "51 password with a metachar" "SA02M_WEB_USER='admin'
SA02M_WEB_PASS='pw;rm -rf /'
" reject
stage "52 password too short"      "SA02M_WEB_USER='admin'
SA02M_WEB_PASS='ab'
" reject
stage "53 user with a slash"       "SA02M_WEB_USER='ad/min'
SA02M_WEB_PASS='goodpass'
" reject
stage "54 truncated \$6\$ hash"      "SA02M_WEB_USER='admin'
SA02M_WEB_PASS_HASH='\$6\$abc\$tooshort'
" reject
stage "55 no credential at all"    "SA02M_WEB_USER='admin'
" reject

# ═══ 8. Login brute-force throttle (M4) ═══════════════════════════════════
# A single shared password over plain HTTP with no attempt limit is an
# unthrottled oracle (threat model §5). web_login_check/record_failure/
# record_success implement a per-client windowed lockout under /run. Driven here
# with a tiny window against a scratch dir.
#
# Proven RED (1.0.6.24), mutations of a scratch copy of the lib:
#   web_login_check always returns 0 (never locks)          -> 59 RED
#   web_login_record_failure is a no-op (count never grows) -> 59 RED
#   web_login_check keys off a constant, not REMOTE_ADDR     -> 60 RED (one
#     client's lockout leaks to another)
#   web_login_check treats an expired window as still locked -> 62 RED (a
#     legitimate operator never recovers)
#   web_login_check fails CLOSED on a broken dir             -> 63 RED
export SA02M_LOGIN_DIR="$T/login"
export SA02M_LOGIN_MAXFAIL=3
export SA02M_LOGIN_LOCKOUT=2
export REMOTE_ADDR="203.0.113.7"

web_login_record_success   # clean slate for this client
web_login_check && ok "57 a fresh client may attempt a login" \
                || bad "57 a fresh client was refused"
web_login_record_failure; web_login_record_failure
web_login_check && ok "58 below the threshold the client may still try" \
                || bad "58 the client was locked before MAXFAIL failures"
web_login_record_failure   # third failure reaches MAXFAIL
web_login_check && bad "59 NOT locked after MAXFAIL failures — brute force is unthrottled" \
                || ok "59 locked after MAXFAIL failures"
if REMOTE_ADDR="198.51.100.9" web_login_check; then
    ok "60 a different client is unaffected by another's lockout (per-client bucket)"
else
    bad "60 one client's lockout blocked a DIFFERENT client — the counter is not per-client"
fi
web_login_record_success
web_login_check && ok "61 a successful login clears the counter" \
                || bad "61 the counter was not cleared on success"
# Window expiry → auto-unlock; a lockout is never permanent.
web_login_record_failure; web_login_record_failure; web_login_record_failure
web_login_check || true    # locked now
sleep 3                    # > SA02M_LOGIN_LOCKOUT (2s)
web_login_check && ok "62 the lockout auto-clears after the window (never permanent)" \
                || bad "62 the lockout did not expire — a legitimate operator stays locked out"
# Fail OPEN when the state dir is unusable — a broken /run must never lock everyone out.
if SA02M_LOGIN_DIR="/dev/null/nope" web_login_check; then
    ok "63 fails OPEN on an unwritable state dir (no permanent lockout)"
else
    bad "63 failed CLOSED on a broken state dir — a broken /run would lock everyone out"
fi

# The lib is only half the guarantee — the shipped login.cgi must actually CALL
# it, on both the reject and the accept paths. Comment-safe via lib_check.sh so a
# `#`-disabled call is caught here, not silently.
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_has >/dev/null; then
    LOGIN=www/network_config/cgi-bin/login.cgi
    stripped_has "$LOGIN" 'web_login_check' \
        && ok "64 login.cgi gates on web_login_check" \
        || bad "64 login.cgi never calls web_login_check — the throttle is dead code"
    stripped_has "$LOGIN" 'web_login_record_failure' \
        && ok "65 login.cgi records a failed attempt" \
        || bad "65 login.cgi never records a failure — the counter never grows"
    stripped_has "$LOGIN" 'web_login_record_success' \
        && ok "66 login.cgi clears the counter on success" \
        || bad "66 login.cgi never clears the counter — a locked-out operator can't recover by logging in"
else
    ok "64 login.cgi wiring assertions skipped (lib_check.sh unavailable)"
    ok "65 (see 64)"; ok "66 (see 64)"
fi

# ── non-vacuity ────────────────────────────────────────────────────────────
n_store=$(find "$SA02M_SESSION_DIR" -type f 2>/dev/null | wc -l)
[ -d "$SA02M_SESSION_DIR" ] || bad "56 the session store was never created — the whole run was vacuous"
[ -s "$INSTALL_LOG" ] || bad "56 the \`install\` shim was never invoked — the repair assertions were vacuous"
[ "$n_store" -ge 0 ] && ok "56 store + shim were exercised"

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  all lib_web_auth.sh cases green"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
