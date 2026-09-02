#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   html-id-contract — the mechanical half of the declared id-contract floor.

   THE FLOOR IT IMPLEMENTS. `docs/agent-rules/web-code-rigor.md` (Frontend
   floors): "every `getElementById` id in JS exists in `index.html` (and vice
   versa for dynamic content); a rename touches both plus any CSS hook". Until
   1.0.6.24 no registry row implemented it — a renamed id was caught only if a
   reviewer remembered to grep, and the DOM-skeleton half was nominally covered
   by `headless-smoke`, which never ran anywhere (2026-08-28 audit, C8 + C4).

   The defect it catches is silent by construction: `getElementById('typo')`
   returns null, the null-safe helpers (`setText`, `setHtml`) swallow it, and
   the widget simply never updates. No syntax error, no console error, no
   failing test — the exact «значение — / не обновляется» class the workflow
   doc has a standing trace recipe for.

   WHAT COUNTS AS "EXISTS", and why each source is legitimate. An id resolves
   when it is either declared in the served markup, or created by the served JS
   at runtime. Both are real — gateway.js and the RS-485 renderer build their
   own DOM, so a markup-only check would fire on correct code, and a gate that
   cries wolf is a gate the next person switches off. Runtime creation is read
   from four shapes:
       `id="x"` inside a template/HTML string (incl. the \" escaped form)
       `<el>.id = 'x'`
       `setAttribute('id', 'x')`
       `insertAdjacentHTML`/`innerHTML` bodies — covered by the first shape
   A DYNAMIC id (`card.id = 'rs485c-' + n`) is not resolvable statically and is
   not swept: only the literal `getElementById('...')` form is checked, so a
   computed lookup is out of scope by construction rather than by exception.

   WHAT IS SWEPT: every literal `document.getElementById('x')` /
   `getElementById("x")` in the served JS, plus the simple `#id` form of
   `querySelector`/`querySelectorAll` (a compound or descendant selector is
   skipped — it is a structural query, not an id contract).

   THE LEDGER. `ID_WHITELIST` is empty on purpose: measured 2026-08-28, the
   tree has ZERO unresolved ids, so there is no debt to accept. It exists so a
   future genuinely-unresolvable case is recorded with a reason rather than
   weakening the gate — and it is non-vacuous, so an entry that stops excusing
   anything FAILS.

   NON-VACUITY: zero swept JS files, zero markup ids, zero getElementById sites,
   or a served page that has gone missing all FAIL.

   PROVEN RED (1.0.6.24): renaming an id in index.html while JS still reads it ·
   renaming it in JS while the markup keeps the old one · deleting the markup
   line that declares it · a typo'd new getElementById · a `#id` querySelector
   with no home · a stale ledger row. Its comment-out case is registered in
   `comment-mutation-proof`.

   Run: node .ai-dev/quality/checks/html-id-contract.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stripJsComments, stripHtmlComments } from './lib_source.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const JS_DIR = join(ROOT, 'www', 'network_config', 'static', 'js');
const HTML = ['index.html', 'login.html', 'cloud.html']
  .map(f => join(ROOT, 'www', 'network_config', f));

/* Accepted debt: an id a reader legitimately cannot resolve statically.
   Empty today — the tree has none. Each future row: { id, reason }.          */
const ID_WHITELIST = [];

let fails = 0;
const ok = m => console.log(`html-id-contract: ok    ${m}`);
const bad = m => { console.log(`html-id-contract: FAIL  ${m}`); fails++; };

function jsFiles(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) jsFiles(p, out);
    else if (e.endsWith('.js')) out.push(p);
  }
  return out;
}
if (!existsSync(JS_DIR)) {
  console.log(`html-id-contract: FAIL  the served JS tree is missing: ${JS_DIR}`);
  process.exit(1);
}
const JS = jsFiles(JS_DIR);
if (JS.length < 5) bad(`only ${JS.length} JS file(s) swept — the sweep is broken, not the tree small`);

/* ── ids declared in the served markup ────────────────────────────────────── */
const declared = new Set();
let pagesSwept = 0;
for (const f of HTML) {
  if (!existsSync(f)) { bad(`served page missing: ${relative(ROOT, f)}`); continue; }
  pagesSwept++;
  // Comment-stripped: an id inside <!-- --> is NOT declared. Without this the
  // gate is comment-blind in the worst direction — commenting an element out
  // would keep satisfying every getElementById that reads it (lib_source.mjs).
  const t = stripHtmlComments(readFileSync(f, 'utf8'));
  for (const m of t.matchAll(/\bid\s*=\s*"([^"]+)"/g)) declared.add(m[1]);
  for (const m of t.matchAll(/\bid\s*=\s*'([^']+)'/g)) declared.add(m[1]);
}
if (pagesSwept !== HTML.length) bad(`only ${pagesSwept} of ${HTML.length} served page(s) swept`);
if (declared.size < 100) {
  bad(`only ${declared.size} id(s) found in the served markup — the extraction is broken (expected >=100)`);
} else {
  ok(`${declared.size} id(s) declared across ${pagesSwept} served page(s)`);
}

/* ── ids the served JS creates at runtime ─────────────────────────────────── */
const created = new Set();
for (const f of JS) {
  const t = stripJsComments(readFileSync(f, 'utf8'));
  for (const m of t.matchAll(/\bid\s*=\s*\\?["']([A-Za-z][\w:.-]*)\\?["']/g)) created.add(m[1]);
  for (const m of t.matchAll(/\.id\s*=\s*["'`]([A-Za-z][\w:.-]*)["'`]\s*;/g)) created.add(m[1]);
  for (const m of t.matchAll(/setAttribute\(\s*["']id["']\s*,\s*["']([^"']+)["']/g)) created.add(m[1]);
}
ok(`${created.size} id(s) created at runtime by the served JS`);

/* ── ids the served JS reads ──────────────────────────────────────────────── */
const BY_ID = /\bgetElementById\(\s*(['"])([^'"\r\n]+)\1\s*\)/g;
// Only the SIMPLE `#id` form: a compound/descendant selector is a structural
// query, not an id contract, and guessing at one produces false positives.
const BY_SEL = /\bquerySelector(?:All)?\(\s*(['"])#([A-Za-z][\w:.-]*)\1\s*\)/g;
const reads = new Map();  // id -> [where, ...]
let sites = 0;
for (const f of JS) {
  const rel = relative(ROOT, f).split(sep).join('/');
  stripJsComments(readFileSync(f, 'utf8')).split('\n').forEach((line, i) => {
    for (const re of [BY_ID, BY_SEL]) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line))) {
        sites++;
        const id = m[2];
        if (!reads.has(id)) reads.set(id, []);
        reads.get(id).push(`${rel}:${i + 1}`);
      }
    }
  });
}
if (sites < 100) {
  bad(`only ${sites} id lookup(s) found — the extraction is broken (expected >=100)`);
} else {
  ok(`${sites} literal id lookup(s), ${reads.size} distinct id(s)`);
}

/* ── the verdict ──────────────────────────────────────────────────────────── */
const ledger = new Map(ID_WHITELIST.map(e => [e.id, e.reason]));
const ledgerHit = new Set();
const unresolved = [];
for (const [id, where] of reads) {
  if (declared.has(id) || created.has(id)) continue;
  if (ledger.has(id)) { ledgerHit.add(id); continue; }
  unresolved.push([id, where]);
}
if (unresolved.length === 0) {
  ok('every id the served JS reads exists in the markup or is created by the JS itself');
} else {
  for (const [id, where] of unresolved) {
    bad(`id "${id}" is read but never declared: ${where.slice(0, 3).join(', ')}${where.length > 3 ? ` (+${where.length - 3} more)` : ''} — the lookup returns null and the widget silently never updates`);
  }
}

const stale = ID_WHITELIST.filter(e => !ledgerHit.has(e.id));
if (stale.length === 0) {
  ok(`the ${ID_WHITELIST.length}-row ledger is exact`);
} else {
  for (const e of stale) bad(`stale ledger row "${e.id}": it resolves now, or matches no lookup any more (${e.reason})`);
}

console.log('');
if (fails === 0) {
  console.log(`html-id-contract: ALL OK — ${reads.size} distinct id(s) read by the served JS all resolve`);
  process.exit(0);
}
console.log(`html-id-contract: ${fails} FAILURE(S)`);
process.exit(1);
