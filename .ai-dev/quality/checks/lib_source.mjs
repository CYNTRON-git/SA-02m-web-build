/* lib_source.mjs — comment stripping for the JS/HTML static gates. NOT a check
   row itself: it is imported by the gates in this directory.

   WHY IT EXISTS. `lib_check.sh` is the one home of this idea for the shell
   gates (`#` and `//` whole-line comments, blanked not deleted). The frontend
   gates need the same property over two grammars shell knows nothing about,
   and getting it wrong is blind in BOTH directions:

     * an id declaration inside an HTML comment must NOT count as declared —
       otherwise `<!-- <div id="x"> -->` satisfies html-id-contract while the
       element is gone and every `getElementById('x')` returns null;
     * a Russian string inside a commented-out block must NOT be reported as
       an untranslated visible string — a gate that flags dead code is a gate
       someone switches off (docs/agent-rules/quality-gate-rigor.md (g)).

   BLANKED, NOT DELETED: newlines are preserved so the gates' `file:line`
   reporting still points at the real line.

   WHAT IS DELIBERATELY NOT HANDLED, and why it is safe here:
   `stripJsComments` removes block comments and WHOLE-LINE `//` comments only.
   A trailing `// …` after code is left alone (removing it would need to know
   whether the `//` is inside a string or a regex literal — `'http://'` is the
   classic miss), and a block-comment opener inside a string would over-strip.
   Both risks are bounded by the gates' own non-vacuity floors: every consumer
   asserts a minimum number of swept files, ids and call sites, so an
   over-strip that eats real code FAILS the run instead of quietly shrinking
   the sweep. Measured on the 1.0.6.24 tree the strip changes no count.

   Coverage: this file has no separate self-test. It is exercised by the two
   gates that import it, and the comment-out mutations registered for those
   gates in `comment-mutation-proof` are what prove it does its job — a broken
   stripper turns those cases from RED to GREEN, which that row reports as a
   hollow gate. */

/** Blank `/* … *\/` blocks and whole-line `//` comments, preserving line count. */
export function stripJsComments(src) {
  const blocks = src.replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, ' '));
  return blocks
    .split('\n')
    .map(line => (/^\s*\/\//.test(line) ? '' : line))
    .join('\n');
}

/** Blank `<!-- … -->` comments, preserving line count. */
export function stripHtmlComments(src) {
  return src.replace(/<!--[\s\S]*?-->/g, m => m.replace(/[^\n]/g, ' '));
}
