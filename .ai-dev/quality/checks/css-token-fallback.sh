#!/usr/bin/env bash
# css-token-fallback — every `var(--token, <fallback>)` in the served stylesheet
# names a token that `:root` actually declares.
#
# WHY THIS EXISTS. `var(--x, #hex)` with an undefined `--x` is the quietest way
# to ship a hard-coded colour: the fallback wins in BOTH themes, the CSS/UI floor
# (docs/agent-rules/web-code-rigor.md — tokens only, both themes, AA ratios) is
# bypassed, and nothing looks wrong on the theme the author happened to check.
# Audit 2026-09-08 C5 found `var(--danger, #c0392b)` on the Carel alarm pill
# (every text token below 2.3:1 on it); the 1.0.6.39 ship review found two more
# (`--err`, `--accent`) and this gate's first run on that tree found three others
# (`--bg-code`, `--border-faint` x3). A class, not a typo — hence a gate.
#
# METHOD (python3, embedded — the file is CSS, lib_check.sh strips `#`/`//`
# lines and CSS comments are `/* */`):
#   1. Blank every `/* ... */` comment, keeping its newlines so reported line
#      numbers match the real file.
#   2. DEFINED = tokens declared inside a top-level `:root` block (the base
#      theme). A declaration counts only when it starts a statement — at line
#      start or after `;`/`{` — because `#  --cyan: ...` (the comment-out
#      mutation on a CSS file) is an INVALID declaration the browser drops, and
#      a parser that still read it would be blind to exactly that mutation.
#      A token declared ONLY in `html[data-theme="light"]` is undefined in the
#      dark theme, so the light block does not count as a definition.
#   3. Every `var(--x, ...)` whose `--x` is not DEFINED FAILS, named with its
#      line. The fallback form was the first gated class (1.0.6.40).
#   4. Every bare `var(--x)` whose `--x` is not DEFINED FAILS too, one FAILURE
#      per site (1.0.6.41 — report-only before). The defect is quieter than the
#      fallback form: the declaration is dropped at computed-value time, so an
#      inherited property (`color`, `font-family`) silently takes the parent's
#      value and a non-inherited one (`background`, `border`, `box-shadow`)
#      resets to its initial — the rule's intent renders in NEITHER theme (an
#      invisible spinner ring, a transparent alert box, sans where monospace
#      was meant).
#
# NON-VACUOUS: a missing stylesheet, a `:root` block that declares nothing, a
# sweep that finds zero `var(--x, ...)` fallbacks or zero bare `var(--x)`
# references, or an oracle that cannot see a known token (`--cyan`) FAILS the
# run.
#
# Proven RED (1.0.6.40): on the pre-fix main.css — 7 FAILURE(S), the five
# undefined tokens named with their lines (`--err`, `--accent` x2, `--bg-code`,
# `--border-faint` x3); ALL OK after the R2 fix.
# Proven RED (1.0.6.41, the bare class): on the 1.0.6.40 main.css — 16
# FAILURE(S), every site of the five undeclared tokens named with its line
# (`--accent` x3, `--font-mono` x5, `--muted` x2, `--panel`, `--text-muted`
# x5); ALL OK after the WP-CSS fix.
# Comment-out mutation: a `#` before the dark `--yellow: #ffd60a;` declaration
# → RED: the three `var(--yellow, ...)` fallbacks PLUS every bare
# `var(--yellow)` site (the count is whatever main.css carries at that
# revision — comment-mutation-proof asserts the RED, not a number); that case
# is registered there. (The dark `--cyan` line also trips the oracle self-check
# on top of its own sites — a noisier RED, so `--yellow` is the registered pin.)
#
# Run: bash .ai-dev/quality/checks/css-token-fallback.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

CSS=www/network_config/static/css/main.css
[ -f "$CSS" ] || { echo "css-token-fallback: FAIL — missing $CSS"; exit 1; }

py=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then py="$p"; break; fi
done
[ -n "$py" ] || { echo "css-token-fallback: FAIL — no working python interpreter"; exit 1; }

PYTHONIOENCODING=utf-8 "$py" - "$CSS" <<'PY'
import re, sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()
fails = 0

def ok(msg):  print(f"css-token-fallback: ok    {msg}")
def bad(msg):
    global fails
    fails += 1
    print(f"css-token-fallback: FAIL  {msg}")

# 1. blank comments, keep newlines (line numbers stay real)
text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)

# 2. DEFINED = statement-initial custom-property declarations inside :root blocks
decl_re = re.compile(r"(?:^|[;{])[ \t]*(--[A-Za-z0-9_-]+)[ \t]*:", re.M)
defined = set()
root_blocks = 0
# Every innermost `selector { body }` pair; a block nested in @media surfaces as
# its own match (the selector group cannot span the outer `{`).
for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", text, flags=re.S):
    selector, body = m.group(1), m.group(2)
    if re.search(r"(?<![\w-]):root(?![\w-])", selector):
        root_blocks += 1
        defined.update(decl_re.findall(body))
if root_blocks == 0 or not defined:
    bad("no :root block with custom-property declarations found — the parser stopped seeing the token home")
elif "--cyan" not in defined:
    bad("oracle self-check: `--cyan` not seen as a :root token — the declaration parser is broken")
else:
    ok(f"{len(defined)} tokens declared in {root_blocks} :root block(s)")

def line_of(pos):  # 1-based line of an offset in `text` (same line count as src)
    return text.count("\n", 0, pos) + 1

# 3. gated: var(--x, fallback) must name a declared token
fallbacks = list(re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)\s*,", text))
if not fallbacks:
    bad("zero var(--x, ...) fallbacks found — the sweep is vacuous")
missing = [(line_of(m.start()), m.group(1)) for m in fallbacks if m.group(1) not in defined]
if missing:
    for ln, tok in missing:
        bad(f"{path}:{ln}: var({tok}, ...) — `{tok}` is not declared in :root; the hard-coded fallback ships in BOTH themes")
elif fallbacks:
    ok(f"{len(fallbacks)} var(--x, ...) fallbacks all name :root tokens")

# 4. gated: bare var(--x) must name a declared token — one FAILURE per site
bare_refs = list(re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)\s*\)", text))
if not bare_refs:
    bad("zero bare var(--x) references found — the sweep is vacuous")
bare_missing = [(line_of(m.start()), m.group(1)) for m in bare_refs if m.group(1) not in defined]
if bare_missing:
    for ln, tok in bare_missing:
        bad(f"{path}:{ln}: var({tok}) — `{tok}` is not declared in :root; the declaration is dropped at computed-value time in BOTH themes")
elif bare_refs:
    ok(f"{len(bare_refs)} bare var(--x) references all name :root tokens")

if fails:
    print(f"css-token-fallback: {fails} FAILURE(S)")
    sys.exit(1)
print("css-token-fallback: ALL OK")
PY
