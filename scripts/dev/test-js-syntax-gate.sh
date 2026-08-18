#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-js-syntax-gate.sh — non-vacuity test for the `js-syntax` quality row
# (.ai-dev/quality/checks/js-syntax.sh).
#
# Why this exists: `node --check` on a `.js` file that carries `import`/`export`
# exits 0 EVEN WITH A SYNTAX ERROR (Node 20/24 module-detection path). The gate
# used to be exactly that one call, so the day the first ES module landed
# (docs/decisions/es-modules.md) a broken module would have shipped GREEN and
# bricked the tab at load. This test pins the fix: a broken module must FAIL the
# gate, a valid module and a valid classic script must pass, a broken classic
# script must still fail, the module heuristic must not misread the shipped
# bundles' `exportHistory()` / `exported_at:` lines as modules, AND the
# false-classic direction is closed too: a broken file whose only ESM marker is
# mid-line (`… export { x };`) or `import.meta` is judged classic and must still
# FAIL (the classic branch is a pinned `--input-type=commonjs` parse, not the
# detection-prone plain `node --check`).
#
# §0 first re-proves the ORIGINAL defect on the local Node, so the test cannot
# rot into asserting something the platform no longer does: if a future Node
# makes `node --check` catch it, §0 says so loudly and the gate is still correct.
#
# Method: writes fixtures under mktemp -d and calls the SHIPPED check script with
# explicit file args. Nothing under www/ is touched.
#
# Run: bash scripts/dev/test-js-syntax-gate.sh   (bash + node, no other deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

GATE=".ai-dev/quality/checks/js-syntax.sh"
[ -f "$GATE" ] || { echo "FAIL  gate script missing: $GATE"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "FAIL  node not on PATH"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── fixtures ────────────────────────────────────────────────────────────────
printf 'import { a } from "./dep.js?v=1.0.0.0";\nexport function f( {\n' > "$tmp/broken-module.js"
printf 'import { a } from "./dep.js?v=1.0.0.0";\nexport function f() { return a; }\n' > "$tmp/ok-module.js"
printf 'export default 1;\n' > "$tmp/ok-export-only.js"
printf 'function f( {\n' > "$tmp/broken-classic.js"
printf '(function(){ var x = 1; window.x = x; })();\n' > "$tmp/ok-classic.js"
# A classic script whose lines START with the words the shipped bundles use —
# must be checked as CLASSIC (a non-strict body would fail a module parse).
printf 'function exportHistory(){ with(document){} }\nexportHistory();\nvar o = {\nexported_at: 1 };\n' > "$tmp/classic-export-word.js"
# A classic script using dynamic import() at line start — still classic.
printf 'function f( {\nimport("./x.js");\n' > "$tmp/broken-classic-dyn-import.js"
# The false-CLASSIC direction: the only ESM marker is mid-line / import.meta, so
# the heuristic says classic — plain `node --check` would flip to module
# detection and pass the syntax error; the pinned commonjs parse must FAIL.
printf 'const x = 1; export { x };\nfunction f( {\n' > "$tmp/broken-midline-export.js"
printf 'const u = import.meta.url;\nfunction f( {\n' > "$tmp/broken-import-meta.js"

# ── §0 the original defect, re-proven on this Node ─────────────────────────
if node --check "$tmp/broken-module.js" >/dev/null 2>&1; then
  ok "§0 node --check exits 0 on a broken .js module (the defect this gate closes) — node $(node --version)"
else
  echo "note  §0 this Node ($(node --version)) already fails node --check on a broken module; the gate stays correct, the historical premise no longer holds here"
fi
if node --input-type=module --check < "$tmp/broken-module.js" >/dev/null 2>&1; then
  bad "§0 node --input-type=module --check < broken module exited 0 — the fix's own primitive is dead on this Node"
else
  ok "§0 node --input-type=module --check < broken module exits non-zero"
fi

# ── §1 the gate itself ──────────────────────────────────────────────────────
run_gate() { bash "$GATE" "$@" >"$tmp/out" 2>&1; }

if run_gate "$tmp/broken-module.js"; then bad "§1 broken module passed the gate (vacuous)"; else ok "§1 broken module FAILS the gate"; fi
grep -q "(module)" "$tmp/out" && ok "§1 failure names the file as a module" || bad "§1 failure output does not name the module file: $(cat "$tmp/out")"

if run_gate "$tmp/ok-module.js" "$tmp/ok-export-only.js"; then ok "§1 valid modules pass"; else bad "§1 valid modules failed: $(cat "$tmp/out")"; fi
grep -q "0 classic script(s) + 2 module(s)" "$tmp/out" && ok "§1 both detected as modules" || bad "§1 module count wrong: $(cat "$tmp/out")"

if run_gate "$tmp/broken-classic.js"; then bad "§1 broken classic script passed"; else ok "§1 broken classic script FAILS"; fi
grep -q "(classic script)" "$tmp/out" && ok "§1 failure names the classic file" || bad "§1 classic failure not named: $(cat "$tmp/out")"

if run_gate "$tmp/ok-classic.js" "$tmp/classic-export-word.js"; then ok "§1 valid classic scripts pass"; else bad "§1 valid classic scripts failed: $(cat "$tmp/out")"; fi
grep -q "2 classic script(s) + 0 module(s)" "$tmp/out" && ok "§1 exportHistory()/exported_at: lines NOT misread as a module (non-strict body checked classic)" || bad "§1 heuristic misclassified: $(cat "$tmp/out")"

if run_gate "$tmp/broken-classic-dyn-import.js"; then bad "§1 broken classic with dynamic import() passed"; else ok "§1 broken classic with dynamic import() FAILS"; fi
grep -q "(classic script)" "$tmp/out" && ok "§1 dynamic import() does not flip a file to module" || bad "§1 dynamic import() misread as module: $(cat "$tmp/out")"

for fx in broken-midline-export broken-import-meta; do
  if node --check "$tmp/$fx.js" >/dev/null 2>&1; then
    ok "§1 premise: plain node --check passes $fx.js (the false-classic hole)"
  else
    echo "note  §1 this Node already fails plain node --check on $fx.js; the pinned parse below is still the gate"
  fi
  if run_gate "$tmp/$fx.js"; then bad "§1 $fx.js passed the gate (false-classic hole open)"; else ok "§1 $fx.js FAILS the gate (classic branch fail-closed)"; fi
  grep -q "(classic script)" "$tmp/out" && ok "§1 $fx.js was judged on the classic branch (heuristic unchanged, parse pinned)" || bad "§1 $fx.js branch label wrong: $(cat "$tmp/out")"
done

if run_gate "$tmp/ok-module.js" "$tmp/broken-classic.js"; then bad "§1 mixed list with one broken file passed"; else ok "§1 one broken file fails the whole list"; fi

if run_gate "$tmp/does-not-exist.js"; then bad "§1 missing file passed"; else ok "§1 missing file FAILS (no silent skip)"; fi

# ── §2 the shipped tree is green and non-empty ─────────────────────────────
if bash "$GATE" >"$tmp/out" 2>&1; then ok "§2 shipped tree passes: $(cat "$tmp/out")"; else bad "§2 shipped tree fails: $(cat "$tmp/out")"; fi
grep -Eq "OK — [1-9][0-9]* classic" "$tmp/out" && ok "§2 sweep is non-vacuous (>=1 file)" || bad "§2 sweep counted zero files"

echo
if [ "$fails" -ne 0 ]; then echo "test-js-syntax-gate: $fails FAILED"; exit 1; fi
echo "test-js-syntax-gate: all passed"
