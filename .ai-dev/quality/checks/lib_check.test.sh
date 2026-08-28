#!/usr/bin/env bash
# check-lib self-test — lib_check.sh is sourced by most static gates, so a bug
# in it breaks every one of them at once and each would fail SILENTLY GREEN (a
# stripper that returns nothing makes every presence pin report "missing", a
# stripper that strips nothing restores the comment-blindness the lib exists to
# remove). This test is the condition on which the shared lib was allowed to
# exist at all (plan decision L, 1.0.6.24).
#
# Run: bash .ai-dev/quality/checks/lib_check.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$HERE/lib_check.sh" || { echo "check-lib: cannot source lib_check.sh"; exit 1; }

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── Fixture: every shape a gate meets in this repo ──────────────────────────
FIX="$TMP/fixture.sh"
cat > "$FIX" <<'FIXTURE'
#!/bin/bash
# a whole-line comment naming web_csrf_require in prose
web_csrf_require "$TOKEN"
#web_csrf_require "$TOKEN"
    # indented comment naming sd_notify("READY=1")
sd_notify("READY=1")
value=1  # trailing comment naming systemctl enable
case "$v" in
    *sa02m*|6.1.0-rc6*) echo match ;;
esac
// a JS-style comment naming import_marker
FIXTURE

text="$(stripped_text "$FIX")"

# 1. Whole-line comments are gone, in both syntaxes.
text_matches "$text" '^[[:space:]]*#' && bad "1a: a whole-line # comment survived stripping" \
    || ok "1a: whole-line # comments are stripped"
text_has "$text" '// a JS-style comment' && bad "1b: a whole-line // comment survived stripping" \
    || ok "1b: whole-line // comments are stripped"

# 2. Real code survives — the stripper must not eat the thing being pinned.
text_has "$text" 'web_csrf_require "$TOKEN"' \
    && ok "2a: the real call site survives stripping" \
    || bad "2a: the real call site was eaten — every presence pin would report missing"
text_has "$text" 'sd_notify("READY=1")' \
    && ok "2b: a needle carrying quotes and parens survives" \
    || bad "2b: sd_notify(\"READY=1\") was eaten"

# 3. A shell `case` label starting with `*` is NOT a comment (kernel-policy
#    reads exactly such a label out of etc/sa02m-kernel-select.sh).
text_has "$text" '*sa02m*|6.1.0-rc6*)' \
    && ok "3: a leading-* case label is preserved (not treated as a block comment)" \
    || bad "3: a leading-* case label was stripped — kernel-policy's §8 arm read would go empty"

# 4. Line numbers are preserved: comments are BLANKED, not deleted. Every
#    ordering pin (READY before ctor, backup before delete) rests on this.
n_in=$(grep -c '' "$FIX")
# Measured on the stripper's own output, not on `text`: a command substitution
# eats TRAILING newlines, so a fixture whose last line is a comment would lose
# its blank tail through the capture and read as a stripper defect it is not.
n_out=$(strip_comments < "$FIX" | grep -c '')
[ "$n_in" = "$n_out" ] \
    && ok "4: line count preserved ($n_in) — ordering pins keep their line numbers" \
    || bad "4: line count changed ($n_in -> $n_out) — comments are being deleted, not blanked"
sd_line=$(stripped_first_line "$FIX" 'sd_notify')
real_line=$(grep -n 'sd_notify' "$FIX" | grep -v '#' | head -1 | cut -d: -f1)
[ -n "$sd_line" ] && [ "$sd_line" = "$real_line" ] \
    && ok "5: stripped_first_line() returns the REAL file line number ($sd_line)" \
    || bad "5: stripped_first_line() gave '$sd_line', the real code line is '$real_line'"

# ── The class this lib exists to close ─────────────────────────────────────
# 6. A needle that exists ONLY inside a comment must read as absent.
grep -q 'systemctl enable' "$FIX" \
    && ok "6a: the raw file does contain 'systemctl enable' (fixture is honest)" \
    || bad "6a: fixture lost its trailing-comment line — the next check would be vacuous"
stripped_has_inline "$FIX" 'systemctl enable' \
    && bad "6b: a needle living only in a TRAILING comment still reads as present" \
    || ok "6b: a trailing-comment-only needle reads as absent"
# The commented-out duplicate must not keep the pin alive once the real call
# goes: strip, drop the real line, and the needle must be gone.
GUT="$TMP/gutted.sh"
grep -v '^web_csrf_require' "$FIX" > "$GUT"
grep -q 'web_csrf_require' "$GUT" \
    && ok "7a: the gutted file still CONTAINS web_csrf_require (in a comment) — the exact hollow-gate setup" \
    || bad "7a: fixture mutation removed the comment too — check 7b would be vacuous"
stripped_has "$GUT" 'web_csrf_require "$TOKEN"' \
    && bad "7b: a commented-out call still satisfies stripped_has — the lib does not close the class" \
    || ok "7b: with the real call commented out, stripped_has reports it missing (RED)"
stripped_has "$FIX" 'web_csrf_require "$TOKEN"' \
    && ok "7c: with the real call present, stripped_has reports it found (GREEN)" \
    || bad "7c: stripped_has misses a real call — the lib would make every gate fail"

# 8. Counting and matching agree with the code-only view.
c=$(stripped_count "$FIX" 'web_csrf_require')
[ "$c" = "1" ] \
    && ok "8a: stripped_count() counts the code line only (1)" \
    || bad "8a: stripped_count() returned '$c' — expected 1 (the commented twin must not count)"
stripped_matches "$FIX" '^sd_notify\("READY=1"\)$' \
    && ok "8b: stripped_matches() applies the ERE to the stripped text" \
    || bad "8b: stripped_matches() failed on a line that is present in code"

# ── The SIGPIPE trap (.ai-dev/notes/quality-gate-environment.md) ────────────
# 9. Under `set -o pipefail`, `strip_comments < f | grep -q needle` reports a
#    FOUND needle as absent when grep quits early and SIGPIPEs the sed still
#    writing the rest of a large file. The helpers must be immune. The needle
#    sits on line 1 with ~200k lines behind it, so grep -q exits at once while
#    sed is far from done.
BIG="$TMP/big.sh"
{ echo 'needle_marker=1'; for _ in $(seq 1 200000); do echo '# filler'; done; } > "$BIG"
stripped_has "$BIG" 'needle_marker=1' \
    && ok "9a: stripped_has() finds a match on a huge file under pipefail (no SIGPIPE poisoning)" \
    || bad "9a: stripped_has() lost a present needle on a huge file — the SIGPIPE trap is back"
stripped_matches "$BIG" '^needle_marker=1$' \
    && ok "9b: stripped_matches() likewise" \
    || bad "9b: stripped_matches() lost a present needle on a huge file"

# 10. Non-vacuity: an unreadable file must never read as "everything present".
stripped_has "$TMP/does-not-exist" 'anything' \
    && bad "10a: stripped_has() reported a needle in a nonexistent file" \
    || ok "10a: a missing file yields no matches"
[ "$(stripped_count "$TMP/does-not-exist" 'x')" = "0" ] \
    && ok "10b: stripped_count() on a missing file is 0" \
    || bad "10b: stripped_count() on a missing file is not 0"

echo
if [ "$fails" -eq 0 ]; then
    echo "check-lib: ALL OK"
    exit 0
fi
echo "check-lib: $fails FAILURE(S)"
exit 1
