#!/usr/bin/env bash
# binding-reset-parity — the two copies of the binding-reset core stay ONE
# implementation.
#
# WHY COPIES AT ALL — one home, deliberately not restated here:
# docs/decisions/binding-reset-one-home.md (the install asymmetry, the rejected
# third install path, and the precedent this repo already set).
#
# WHAT THIS GATE ASSERTS, and why each half exists:
#
#   1. BYTE-IDENTITY of source and copy. Non-vacuous in every direction a
#      "compare two files" check can go hollow: a missing file FAILS, an empty
#      file FAILS, a file that lost its anchor definitions FAILS. "Nothing to
#      compare" is never a pass.
#   2. NO PER-DOOR BRANCH inside the core. This is the half that matters most.
#      The realistic drift is NOT two files growing apart — the byte compare
#      catches that on the next build. It is ONE file quietly growing
#      `if <source is that one>: …` and becoming two implementations sharing a
#      name, which a byte compare blesses happily. Every door difference must be
#      DATA on the injected descriptor. The ban is TEXTUAL over the whole file,
#      prose included — the core's own docstring is written so as never to quote
#      a forbidden form, and that is stated there.
#   3. STDLIB-ONLY imports. Byte-identity is only achievable because the core
#      imports nothing project-local: the two trees share no package, so one
#      door's import would break the other door. The allow-list is closed — a
#      module outside it FAILS and must be added here deliberately.
#
# The pattern matcher SELF-TESTS on a synthetic file carrying each banned form:
# a matcher that stopped matching would turn assertion 2 into decoration
# (docs/agent-rules/quality-gate-rigor.md — «an assertion that cannot fail is
# decoration»).
#
# Registered in comment-mutation-proof (case: comment a line of the source copy
# out → the copies differ → RED).
#
# RED battery, run by hand before this was called done (all four observed):
#   change one byte in either copy                -> byte-identity FAILS
#   truncate either copy to empty                 -> non-vacuity FAILS
#   delete either copy                            -> presence FAILS
#   add `if spec.name == "alice": pass` to the core -> drift ban FAILS
#   add `from . import constants` to the core     -> stdlib-only FAILS
#
# Run: bash .ai-dev/quality/checks/binding-reset-parity.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

SRC="$ROOT/opt/sa02m-cloud-agent/binding_core.py"
COPY="$ROOT/opt/sa02m-alice/sa02m_alice/common/binding_core.py"

fails=0
ok()  { printf 'binding-reset-parity: ok    %s\n' "$*"; }
bad() { printf 'binding-reset-parity: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# Anchors: definitions the core must carry. A file gutted to a stub would still
# byte-match its equally gutted twin, so identity alone is not enough.
ANCHORS='def stand_down(
def finish_stand_down(
def wipe_binding(
def retry_wipe(
def restore_stand_down_status(
class RefusalTracker(
SourceSpec = '

# Every door difference is DATA on the descriptor — never a comparison of which
# source is being served. One alternation, applied to the whole file.
DRIFT_RE='(name[[:space:]]*(==|!=)|(==|!=)[[:space:]]*[A-Za-z_.]*name([^A-Za-z0-9_]|$)|name[[:space:]]+(not[[:space:]]+)?in[[:space:]]*[[({]|(==|!=)[[:space:]]*["'"'"'](alice|cloud)["'"'"']|["'"'"'](alice|cloud)["'"'"'][[:space:]]*(==|!=))'

# Closed allow-list: what the core is permitted to import. Widening it is a
# deliberate edit to THIS line, which is the point.
STDLIB_OK='collections|logging|os|subprocess|time'

# ── 1. presence + non-vacuity ────────────────────────────────────────────────
missing=0
for f in "$SRC" "$COPY"; do
    if [ ! -f "$f" ]; then
        bad "missing: ${f#"$ROOT"/} — a copy that is not there is not a match"
        missing=1
        continue
    fi
    bytes=$(wc -c < "$f")
    if [ "$bytes" -lt 2000 ]; then
        bad "${f#"$ROOT"/} is $bytes bytes — too small to be the core; refusing to compare"
        missing=1
    fi
done
if [ "$missing" -ne 0 ]; then
    echo "binding-reset-parity: $fails FAILURE(S)"
    exit 1
fi
ok "both copies present and non-empty"

# ── 2. byte-identity ─────────────────────────────────────────────────────────
if cmp -s "$SRC" "$COPY"; then
    ok "source and copy are byte-identical ($(wc -c < "$SRC") bytes)"
else
    bad "source and copy DIFFER — one home means one implementation:
  source: ${SRC#"$ROOT"/}
  copy:   ${COPY#"$ROOT"/}
  fix:    cp '${SRC#"$ROOT"/}' '${COPY#"$ROOT"/}'
$(diff "$SRC" "$COPY" | head -n 20)"
fi

# ── 3. anchors (a gutted twin must not pass) ─────────────────────────────────
anchor_fails=0
while IFS= read -r anchor; do
    [ -n "$anchor" ] || continue
    for f in "$SRC" "$COPY"; do
        # File argument, never a pipe: a producer feeding `grep -q` takes
        # SIGPIPE and turns "found" into a failure under pipefail
        # (docs/agent-rules/quality-gate-rigor.md shape (f)).
        if ! grep -qF -- "$anchor" "$f"; then
            bad "${f#"$ROOT"/} has lost the anchor '$anchor' — the core is not intact"
            anchor_fails=1
        fi
    done
done <<< "$ANCHORS"
[ "$anchor_fails" -eq 0 ] && ok "both copies carry every anchor definition"

# ── 4. the anti-drift pin ────────────────────────────────────────────────────
# Self-test FIRST: a matcher that no longer matches would make the pin below
# decoration rather than evidence.
probe="$(mktemp)"
{
    echo 'if spec.name == "alice":'
    echo 'if spec.name != "cloud":'
    echo 'if source == "alice":'
    echo 'if name in ("alice", "cloud"):'
    printf '%s\n' "if spec.name in ('alice',):"
} > "$probe"
probe_hits=$(grep -cE "$DRIFT_RE" "$probe")
rm -f "$probe"
if [ "${probe_hits:-0}" -ge 5 ]; then
    ok "the drift matcher fires on all $probe_hits synthetic per-door branches (self-test)"
else
    bad "the drift matcher only caught ${probe_hits:-0}/5 synthetic per-door branches — the pin below proves nothing"
fi

drift_fails=0
for f in "$SRC" "$COPY"; do
    hits="$(grep -nE "$DRIFT_RE" "$f")"
    if [ -n "$hits" ]; then
        bad "${f#"$ROOT"/} compares WHICH source it is serving — a per-door branch in the
  shared core is two implementations in one file, which a byte compare blesses.
  Put the difference on the descriptor instead:
$hits"
        drift_fails=1
    fi
done
[ "$drift_fails" -eq 0 ] && ok "no per-door branch in the core (every difference rides the descriptor)"

# ── 5. stdlib-only imports ───────────────────────────────────────────────────
imports="$(grep -nE '^[[:space:]]*(import|from)[[:space:]]+' "$SRC")"
if [ -z "$imports" ]; then
    bad "no import line found in ${SRC#"$ROOT"/} — the import scan is reading nothing"
else
    import_fails=0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        mod="$(printf '%s\n' "$line" | sed -E 's/^[0-9]+:[[:space:]]*(import|from)[[:space:]]+([A-Za-z0-9_.]*).*/\2/')"
        base="${mod%%.*}"
        if [ -z "$base" ] || ! printf '%s\n' "$base" | grep -qE "^($STDLIB_OK)$"; then
            bad "${SRC#"$ROOT"/} imports '$mod' — the core is stdlib-only (line: $line).
  A project-local import breaks the OTHER door, which has no such module, and
  byte-identity with it. Put the fact on the descriptor, or widen STDLIB_OK here."
            import_fails=1
        fi
    done <<< "$imports"
    [ "$import_fails" -eq 0 ] && ok "the core imports stdlib only ($(printf '%s\n' "$imports" | wc -l) import lines checked)"
fi

echo
[ "$fails" -eq 0 ] && { echo "binding-reset-parity: ALL OK"; exit 0; }
echo "binding-reset-parity: $fails FAILURE(S)"; exit 1
