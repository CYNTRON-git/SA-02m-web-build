#!/bin/bash
# Static gate: no hard-coded / retired web session token in shipped or dev code.
#
# The defect this closes, concretely: the fixed cookie `cyntron_session` was
# retired on 2026-07-12 when the panel moved to a server-side session store
# (www/network_config/cgi-bin/lib_web_auth.sh — the one home of the auth rule).
# Five call sites kept sending it. None of them broke loudly, because a dead
# token is syntactically perfect: the RS-485 port-lease probe answered "not
# busy" for 3.5 weeks, and nine dev tools silently received
# `{"error":"unauthorized"}` instead of data. No syntax, unit, or type check
# can see that class — only a convention gate can.
#
# PINS
#   A. The CLASS, not just the string: a `session_token=` whose value is a
#      hard-coded word. A live token is hex 32..128 (lib_web_auth.sh:52,
#      opt/sa02m-flasher/sa02m_flasher/auth.py), so any other bare word is a
#      credential frozen into the tree — dead on arrival or a real secret
#      committed. Both are defects; this pin does not care which.
#   B. The retired string itself, anywhere in the swept roots — broader than A,
#      so `CK='cyntron_session'` or a bare mention in code is caught even
#      without the `session_token=` prefix.
#   C. Non-vacuity, BOTH ways (the repo's ledger discipline): the roots exist
#      and were really swept, AND every allow-listed negative assertion is
#      still present and still negative. Deleting test_auth.py's rejection case
#      turns this gate RED rather than quietly green.
#
# ALLOW-LIST — two files, both of which name the retired token in order to
# assert it is REFUSED. They are the opposite of the defect and must survive:
#   * opt/sa02m-flasher/tests/test_auth.py — `cyntron_session` sits in the
#     rejected-token list of test_non_hex_token_rejected(). It pins that the
#     daemon refuses the old token; sweeping it away would delete the proof.
#   * scripts/dev/test-port-lease-gate.sh — its `curl` shim is handed the
#     retired token to prove the shim models the daemon's auth rule (401 +
#     exit 0 without -f) instead of returning a canned answer. Without that
#     control the port-lease harness would pass for the wrong reason.
#
# DELIBERATE NON-FLAGS
#   * Comment LINES are masked before matching (whole-line, the same idiom as
#     rtc-utc-convention.sh). The retired token is legitimately NAMED in prose
#     by the three scripts that used to send it and by lib_web_auth.sh's
#     history note — a gate that forbade explaining the defect would get the
#     explanations deleted, which is the opposite of what we want.
#     KNOWN LIMIT, stated rather than half-solved: only `#` and `//` comment
#     lines are masked. A Python DOCSTRING is prose the gate cannot tell from
#     code, so prose inside one must not spell the token — point at the auth
#     rule's one home (lib_web_auth.sh) instead. Parsing docstrings from bash
#     would be a fragile gate, and a gate that misfires gets switched off.
#   * An empty value (`session_token=` in a logout / cookie-clearing write:
#     logout.cgi, app.js clearSessionCookie) — that is a deletion, not a
#     credential.
#   * Expansions: `${TOKEN}`, `{token}`, `$_hex`, `%s`.
#   * Regex and prefix tests: `session_token=([a-f0-9]{32,128})`,
#     `startsWith('session_token=')`.
#   * docs/, CHANGELOG.md and .ai-dev/ are OUTSIDE the roots: BUGLOG, the
#     CHANGELOG, the backlog and the audit quote the retired token as the
#     history they record.
#
# Run: bash .ai-dev/quality/checks/no-retired-session-token.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

RETIRED='cyntron_session'
SWEEP_ROOTS="etc www opt tools scripts install.sh"
ALLOW="opt/sa02m-flasher/tests/test_auth.py scripts/dev/test-port-lease-gate.sh"

fails=0
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
ok()  { printf 'ok    %s\n' "$1"; }

# ── C1: the roots exist ────────────────────────────────────────────────────
for r in $SWEEP_ROOTS; do
    [ -e "$r" ] || bad "sweep root '$r' does not exist — the gate would pass vacuously"
done
[ "$fails" -eq 0 ] || { printf 'no-retired-session-token: %d FAILED\n' "$fails"; exit 1; }

# ── Collect the swept files (text only, no bytecode caches) ────────────────
FILES=$(grep -rIl '' $SWEEP_ROOTS --exclude-dir=__pycache__ --exclude-dir=node_modules 2>/dev/null)
n_files=$(printf '%s\n' "$FILES" | grep -c .)
if [ "$n_files" -lt 50 ]; then
    bad "only $n_files files swept — the sweep is broken, not the tree clean"
    printf 'no-retired-session-token: %d FAILED\n' "$fails"; exit 1
fi
ok "sweep sees $n_files text files under: $SWEEP_ROOTS"

allowed() {
    for a in $ALLOW; do [ "$1" = "$a" ] && return 0; done
    return 1
}

# Strip whole comment lines (#-style and //-style) before matching. Reads
# stdin so it can filter a file OR an extracted function body — pin C needs
# the latter: the rejection test carries a COMMENT naming the retired token
# right above the assertion, and a pin that accepts the comment as proof would
# stay green after the assertion itself was deleted.
strip_comments() {
    sed -e 's#^[[:space:]]*//.*$##' -e 's#^[[:space:]]*\*.*$##' -e 's/^[[:space:]]*#.*$//'
}

# ── Pin A: a hard-coded, non-hex session_token= value ──────────────────────
hits_a=0
for f in $FILES; do
    allowed "$f" && continue
    while IFS= read -r val; do
        [ -n "$val" ] || continue
        # A real token is hex 32..128; anything else baked into the tree is the defect.
        if printf '%s' "$val" | grep -qE '^[a-f0-9]{32,128}$'; then
            bad "$f: a LIVE-shaped token literal is committed (${val:0:8}…) — credentials never belong in the tree"
            hits_a=$((hits_a + 1))
        else
            bad "$f: hard-coded session_token=$val — a frozen credential; use an env var or the session store"
            hits_a=$((hits_a + 1))
        fi
    done <<EOF
$(strip_comments < "$f" | grep -oE 'session_token=[A-Za-z0-9_-]+' | sed 's/^session_token=//')
EOF
done
[ "$hits_a" -eq 0 ] && ok "pin A: no hard-coded session_token= value in the swept roots"

# ── Pin B: the retired string itself, outside the allow-list ───────────────
hits_b=0
for f in $FILES; do
    allowed "$f" && continue
    if strip_comments < "$f" | grep -qF "$RETIRED"; then
        bad "$f: names the retired token '$RETIRED' in code — it was withdrawn 2026-07-12 and authorises nothing"
        hits_b=$((hits_b + 1))
    fi
done
[ "$hits_b" -eq 0 ] && ok "pin B: the retired token appears in no swept code path"

# ── Pin C: the allow-listed negative assertions are still negative ─────────
for a in $ALLOW; do
    [ -r "$a" ] || { bad "allow-listed file $a is gone — either restore it or drop the entry"; continue; }
    strip_comments < "$a" | grep -qF "$RETIRED" \
        || bad "$a no longer names '$RETIRED' — the proof that the retired token is REJECTED has been deleted; this entry now excuses nothing"
done

# test_auth.py's occurrence must sit inside the rejection test, not anywhere.
if [ -r opt/sa02m-flasher/tests/test_auth.py ]; then
    # Not a `/start/,/end/` range: the end pattern `^    def ` also matches the
    # START line, which collapses the range to one line and makes both pins
    # below fail on a perfectly healthy file.
    body=$(awk '/def test_non_hex_token_rejected/{p=1;next} p && (/^    def /||/^class /){exit} p' \
        opt/sa02m-flasher/tests/test_auth.py)
    printf '%s\n' "$body" | strip_comments | grep -qF "$RETIRED" \
        && ok "pin C: test_auth.py still asserts '$RETIRED' is REJECTED (inside test_non_hex_token_rejected)" \
        || bad "test_auth.py names '$RETIRED' but no longer inside test_non_hex_token_rejected — the assertion moved or turned positive"
    printf '%s\n' "$body" | grep -q 'assertFalse' \
        || bad "test_non_hex_token_rejected no longer asserts FALSE — a rejection test that stopped rejecting"
fi

# the harness's occurrence must still be its auth-rule control
if [ -r scripts/dev/test-port-lease-gate.sh ]; then
    grep -qF 'exit 22' scripts/dev/test-port-lease-gate.sh \
        && ok "pin C: the port-lease harness still uses the retired token as its 401/-f control" \
        || bad "scripts/dev/test-port-lease-gate.sh no longer checks the -f/401 outcome — its use of the retired token is no longer a control"
fi

echo
if [ "$fails" -gt 0 ]; then
    printf 'no-retired-session-token: %d FAILED\n' "$fails"
    exit 1
fi
printf 'no-retired-session-token: all checks passed\n'
