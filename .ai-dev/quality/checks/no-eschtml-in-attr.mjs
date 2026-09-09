#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   no-eschtml-in-attr — served JS never builds a quoted HTML attribute from
   escHtml().

   THE DEFECT. `escHtml()` (app.js) escapes `& < >` and nothing else — it is a
   TEXT-context escaper. Interpolated into a double-quoted attribute it leaves
   `"` alive, so a value that carries a quote closes the attribute and the rest
   of the string becomes new attributes: `data-topic="…/x" onfocus="…"`. The
   1.0.6.38 channel picker did exactly that with inventory strings the board
   takes from the LAN broker's live cache (audit 2026-09-08, finding C4 — the
   injected handler EXECUTED in headless Chromium, with the operator's session).
   The attribute-safe helper is `escAttr()` (app.js, promoted from gateway.js),
   which also escapes `"` and `'`; docs/agent-rules/web-code-rigor.md
   «Escape before innerHTML» names it for attribute context.

   WHAT IS SWEPT. Every served bundle under www/network_config/static/js,
   comment-stripped (lib_source.mjs), for the two ends of an attribute built by
   string concatenation or a template literal:
     A. the OPENER — an attribute's opening quote whose value starts with
        escHtml():   ="' + escHtml(   /   ="prefix' + escHtml(   /   ="${escHtml(
        (and the quote-swapped and backslash-escaped spellings);
     B. the CLOSER — an escHtml() call whose NEXT string literal closes an
        attribute: escHtml(x) + '">'  /  escHtml(x) + '" title="'  — a literal
        that carries a `"` before any `<`.
   A and B together catch the first and the last escHtml() of an attribute
   value. KNOWN LIMIT, stated rather than half-solved: a value built from
   THREE or more escHtml() parts leaves the middle part(s) unseen — in this
   tree no attribute is built that way, and the sweep is over a shape, not a
   parse. Text-context escHtml() (the common, correct use) never matches: its
   neighbouring literal starts with `<` or carries no quote.

   NON-VACUITY (presence floors — these are what a comment-out turns RED):
     * app.js defines escAttr() and it really escapes `" ' < > &` — the gate
       loads the function from the shipped source and CALLS it; a helper that
       stops escaping quotes fails the run even with zero sites left;
     * at least 5 served JS files swept, at least 20 escHtml() call sites seen
       (the sweep is over the real tree, not an empty directory), and at least
       5 escAttr() attribute sites (the fix is in place, not the sites deleted).

   PROVEN RED (1.0.6.39, on the pre-fix bundles): 25 failures — 22 escHtml()
   attribute call sites (23 reported lines: an opener and a closer can name the
   same site) in smarthome.js and rs485.js, plus the two presence floors above
   (no escAttr() in app.js, zero escAttr() attribute sites). Its comment-out
   case (`function escAttr(` in app.js) is registered in comment-mutation-proof.

   Run: node .ai-dev/quality/checks/no-eschtml-in-attr.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { stripJsComments } from './lib_source.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const JS_DIR = join(ROOT, 'www', 'network_config', 'static', 'js');
const APP_JS = join(JS_DIR, 'app.js');

let fails = 0;
const ok = m => console.log(`no-eschtml-in-attr: ok    ${m}`);
const bad = m => { console.log(`no-eschtml-in-attr: FAIL  ${m}`); fails++; };

// ── The escaper it trusts — loaded from the shipped source and exercised ────
if (!existsSync(APP_JS)) {
  bad(`app.js is missing: ${APP_JS}`);
} else {
  const src = stripJsComments(readFileSync(APP_JS, 'utf8'));
  const m = /function escAttr\s*\([^)]*\)\s*\{[\s\S]*?\n\}/.exec(src);
  if (!m) {
    bad('app.js defines no escAttr() — the attribute-safe escaper has no home (comment it back in, or the fix is gone)');
  } else {
    const sandbox = {};
    vm.createContext(sandbox);
    try {
      vm.runInContext(m[0] + '\nthis.__escAttr = escAttr;', sandbox, { filename: 'app.js#escAttr' });
      const out = sandbox.__escAttr('"\'<>&');
      if (out === '&quot;&#39;&lt;&gt;&amp;') ok('app.js escAttr() escapes " \' < > & (called, not read)');
      else bad(`app.js escAttr() does not escape attribute context: got ${JSON.stringify(out)} for "'<>&`);
      if (sandbox.__escAttr(null) !== '' || sandbox.__escAttr(undefined) !== '') {
        bad('app.js escAttr() must render null/undefined as an empty string (a renderer passes absent fields)');
      }
    } catch (e) {
      bad(`app.js escAttr() could not be loaded: ${e.message}`);
    }
  }
}

// ── The sweep ───────────────────────────────────────────────────────────────
function jsFiles(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) jsFiles(p, out);
    else if (e.endsWith('.js')) out.push(p);
  }
  return out;
}
if (!existsSync(JS_DIR)) {
  bad(`the served JS tree is missing: ${JS_DIR}`);
  console.log(`no-eschtml-in-attr: ${fails} FAILURE(S)`);
  process.exit(1);
}
const FILES = jsFiles(JS_DIR);

// A. attribute OPENER followed by escHtml( — the value's first part.
const OPENERS = [
  /=\s*"[^"'\n]*'\s*\+\s*escHtml\(/g,     // ="' + escHtml(   /  ="#i-' + escHtml(
  /=\s*'[^"'\n]*"\s*\+\s*escHtml\(/g,     // ='" + escHtml(
  /=\s*\\"[^"\n]*"\s*\+\s*escHtml\(/g,    // =\"" + escHtml(   (double-quoted JS string)
  /=\s*"\$\{\s*escHtml\(/g,               // ="${escHtml(      (template literal)
  /=\s*'\$\{\s*escHtml\(/g,               // ='${escHtml(
];
// B. escHtml(...) whose NEXT literal closes an attribute: a `"` before any `<`.
//    The call's argument may carry one nested pair of parentheses (uiT('…')).
const CLOSERS = [
  /escHtml\((?:[^()]|\([^()]*\))*\)\s*\+\s*'[^'<]*"/g,        // + '">'  /  + '" title="'
  /escHtml\((?:[^()]|\([^()]*\))*\)\s*\+\s*"[^"<]*\\"/g,      // + "\">"
  /escHtml\((?:[^()]|\([^()]*\))*\)\s*\}[^`<$]*"/g,           // ${escHtml(x)}"   (template literal)
];

let escHtmlSites = 0;
let escAttrAttrSites = 0;
const hits = [];   // [file:line, snippet]
for (const f of FILES) {
  const text = stripJsComments(readFileSync(f, 'utf8'));
  const rel = relative(ROOT, f).split(sep).join('/');
  escHtmlSites += (text.match(/\bescHtml\(/g) || []).length;
  escAttrAttrSites += (text.match(/=\s*"[^"'\n]*'\s*\+\s*escAttr\(/g) || []).length;
  const lineAt = idx => text.slice(0, idx).split('\n').length;
  const seen = new Set();
  for (const re of [...OPENERS, ...CLOSERS]) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text))) {
      const line = lineAt(m.index);
      const key = `${rel}:${line}`;
      if (seen.has(key)) continue;
      seen.add(key);
      hits.push([key, m[0].replace(/\s+/g, ' ').slice(0, 60)]);
    }
  }
}

if (FILES.length < 5) bad(`only ${FILES.length} served JS file(s) swept — the sweep is broken, not the tree small`);
else ok(`${FILES.length} served JS file(s) swept`);
if (escHtmlSites < 20) bad(`only ${escHtmlSites} escHtml() call site(s) seen — the sweep is broken (expected >=20)`);
else ok(`${escHtmlSites} escHtml() call site(s) seen`);
if (escAttrAttrSites < 5) bad(`only ${escAttrAttrSites} escAttr() attribute site(s) seen — the fix is not in place (expected >=5)`);
else ok(`${escAttrAttrSites} escAttr() attribute site(s) seen`);

if (hits.length === 0) {
  ok('no quoted attribute is built from escHtml() in the served JS');
} else {
  for (const [where, snip] of hits) {
    bad(`attribute built from escHtml() at ${where}: ${snip} — use escAttr() (app.js) in attribute context`);
  }
}

console.log('');
if (fails === 0) {
  console.log(`no-eschtml-in-attr: ALL OK — ${FILES.length} file(s), ${escHtmlSites} escHtml() sites, none in attribute context`);
  process.exit(0);
}
console.log(`no-eschtml-in-attr: ${fails} FAILURE(S)`);
process.exit(1);
