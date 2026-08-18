#!/usr/bin/env bash
# Quality row `js-syntax` (see .ai-dev/quality/tools.json). Syntax-checks every
# hand-written frontend script under www/network_config/static/js/ — recursively,
# so a module directory added later (static/js/mqtt/, static/js/flasher/, ...) is
# covered from its first file without a registry edit.
#
# Why a script and not `node --check "$f"` inline: `node --check` on a `.js` file
# that carries `import`/`export` SILENTLY EXITS 0 even when the file has a syntax
# error (Node 20/24 module-detection path — reproduced on v24.14.1, see
# docs/decisions/es-modules.md П1). So a module file is detected (a line that
# starts with `import`/`export`) and checked through
# `node --input-type=module --check < "$f"`, which does report the error;
# classic scripts keep the plain `node --check`. The heuristic is line-anchored on
# purpose: `import(` (dynamic import, legal in a classic script) and `import.meta`
# do not match, a commented `// import x` does not match.
#
# Usage: bash .ai-dev/quality/checks/js-syntax.sh [file ...]
#   no args → the shipped tree (non-vacuous: zero files found = FAIL)
#   args    → exactly those files (the self-test uses this to prove a broken
#             module FAILS and a valid one passes — scripts/dev/test-js-syntax-gate.sh)
set -u

JS_ROOT="www/network_config/static/js"
# `import`/`export` at line start followed by whitespace+specifier/binding, or
# directly by `{` `*` (or a quote for a bare `import"./x.js"`). The keyword must
# END there: `exportHistory()` / `exported_at:` in the shipped bundles must not
# read as modules, and `import(` / `import.meta` must not either.
MODULE_RE='^[[:space:]]*(import([[:space:]]+[{*"'"'"'A-Za-z_$]|[{*"'"'"'])|export([[:space:]]+[{*A-Za-z_$]|[{*]))'

files=()
if [ "$#" -gt 0 ]; then
  files=("$@")
else
  cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
  while IFS= read -r f; do files+=("$f"); done < <(find "$JS_ROOT" -type f -name '*.js' | sort)
  if [ "${#files[@]}" -eq 0 ]; then
    echo "js-syntax: FAIL — no *.js under $JS_ROOT (sweep is dead, not green)" >&2
    exit 1
  fi
fi

fails=0
n_mod=0
n_classic=0
for f in "${files[@]}"; do
  if [ ! -f "$f" ]; then
    echo "js-syntax: FAIL — missing file $f" >&2
    fails=$((fails + 1))
    continue
  fi
  if grep -Eq "$MODULE_RE" "$f"; then
    n_mod=$((n_mod + 1))
    # stdin form: the only `--check` path Node applies ESM parsing to for a `.js`
    # name; the error names [stdin], so we name the file ourselves.
    if ! node --input-type=module --check < "$f"; then
      echo "js-syntax: FAIL — $f (module)" >&2
      fails=$((fails + 1))
    fi
  else
    n_classic=$((n_classic + 1))
    if ! node --check "$f"; then
      echo "js-syntax: FAIL — $f (classic script)" >&2
      fails=$((fails + 1))
    fi
  fi
done

if [ "$fails" -ne 0 ]; then
  echo "js-syntax: $fails file(s) failed (checked $n_classic classic + $n_mod module)" >&2
  exit 1
fi
echo "js-syntax: OK — $n_classic classic script(s) + $n_mod module(s) parse"
