#!/usr/bin/env bash
# websession-parity — the panel session/CSRF rule the two HTTP daemons read is ONE
# module shipped as TWO BYTE-IDENTICAL copies:
#     opt/sa02m-flasher/sa02m_flasher/websession.py
#     opt/sa02m-devices/sa02m_devices/websession.py
# and this row is what makes "byte-identical" a fact rather than a habit.
#
# WHY COPIES. The rule («sha256(token) file in the session dir, expiry in field 1,
# <hash>.csrf beside it, four refusal reasons») is owned by lib_web_auth.sh and
# was already mirrored once (the flasher's auth.py, documented «MUST match»).
# 1.0.6.65 gave sa02m-devices-api the same rule. A third shared package would cost
# installer edits on three deploy paths plus a shared-home gate and the
# «dependency lands before its consumer» ordering (the carel/led shape) for ~100
# lines; a cross-tree import breaks the moment update-www-only.sh refreshes one
# tree and not the other — the class the carel/led gates exist for. So: copies,
# pinned here (docs/decisions/selective-csrf-policy.md «Демоны», plan fork F2).
# Same idiom as firstboot-overlay-parity (PAIRS + cmp).
#
# PINS: (1) both files exist; (2) `cmp` byte-identical — FAIL prints the first
# differing lines; (3) NON-VACUITY: the module still defines check_csrf() and
# check_session_store() and is at least LINE_FLOOR lines long — two identical
# empty files, or a pair gutted in lockstep, FAIL instead of passing on "equal".
# The comment-mutation case (a `#` on `def check_csrf(` in the DEVICES copy — the
# bytes diverge) is registered in comment-mutation-proof.
#
# PROVEN RED (2026-09-28/29, 1.0.6.65): with the devices copy absent → FAIL (missing
# file); with one blank line appended to the flasher copy → FAIL naming the diff;
# GREEN once `cp` restored the pair.
#
# Run: bash .ai-dev/quality/checks/websession-parity.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

A=opt/sa02m-flasher/sa02m_flasher/websession.py
B=opt/sa02m-devices/sa02m_devices/websession.py
LINE_FLOOR=80
fails=0
ok()  { printf 'websession-parity: ok    %s\n' "$*"; }
bad() { printf 'websession-parity: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

for f in "$A" "$B"; do
    if [ -f "$f" ]; then ok "present: $f"
    else bad "missing: $f — the rule has one consumer without its copy (cp the other over it)"; fi
done
if [ "$fails" -eq 0 ]; then
    if cmp -s "$A" "$B"; then
        ok "byte-identical: $A == $B"
    else
        bad "the two copies differ — edit ONE, then cp it over the other:"
        diff -u "$A" "$B" | head -n 20 | sed 's/^/websession-parity:        /'
    fi
    for f in "$A" "$B"; do
        n=$(grep -c . "$f")
        if [ "$n" -ge "$LINE_FLOOR" ]; then ok "$f: $n non-empty lines (floor $LINE_FLOOR)"
        else bad "$f: only $n non-empty lines (floor $LINE_FLOOR) — the module was gutted, not just changed"; fi
        for fn in check_csrf check_session_store csrf_error_body session_token_from_cookie; do
            if grep -qE "^def ${fn}\(" "$f"; then ok "$f defines ${fn}()"
            else bad "$f no longer defines ${fn}() — a consumer's import breaks"; fi
        done
    done
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "websession-parity: ALL OK — the two websession.py copies are byte-identical and whole"
    exit 0
fi
echo "websession-parity: $fails FAILURE(S)"
exit 1
