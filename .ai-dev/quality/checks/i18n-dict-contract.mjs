#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   i18n-dict-contract — the mechanical half of the declared i18n floor.

   THE FLOOR IT IMPLEMENTS. `docs/agent-rules/web-code-rigor.md` (Frontend
   floors + Reviewer floor summary) states "every new visible Russian string
   needs a DICT entry; a shipped string with no EN translation is a finding".
   Until 1.0.6.24 no registry row implemented it: the floor was read as
   enforceable because it sits next to "the quality registry is green", and
   nothing checked it (2026-08-28 audit, finding C8).

   THE ORACLE IS THE SHIPPED RUNTIME, NOT A COPY OF IT. i18n.js is loaded into
   a vm sandbox with a minimal DOM and driven through its OWN
   `window.sa02mI18n.t()` after `setLang('en')`. So DICT, the REGEX fallback
   table and `normalize()` are the real ones — a string this gate calls
   translated is a string the browser will translate, and a DICT refactor
   cannot silently desynchronise a re-implementation here from the runtime.
   A string is "missing" exactly when `t(s) === s` while `s` carries Cyrillic.

   WHAT IS SWEPT, and why exactly this and no more:

   A. Every literal string passed to `uiT()` in the served JS. `uiT` is the
      documented path for a dynamic string (web-code-rigor.md), so a literal
      reaching it is by definition a visible string. Precise: no guessing about
      whether a string reaches the DOM. A `uiT(variable)` or a template literal
      is NOT swept — it cannot be resolved statically, and a gate that guessed
      would produce the false positives that get gates switched off.

   B. Cyrillic text nodes and the four translated attributes (`title`,
      `aria-label`, `placeholder`, `alt`) in the served HTML. The set matches
      what the runtime's `walk()` actually visits, so the gate cannot flag text
      the runtime never touches:
        * `<head>` is excluded — `apply()` starts at `document.body`.
        * `<script>` / `<style>` / `<textarea>` bodies are excluded — `walk()`
          returns early on those tags.
        * elements whose id is in the runtime's `isI18nControl()` list are
          excluded — `walk()` returns early on them too (the language and theme
          toggles own their own labels).

   THE LEDGER. `DICT_WHITELIST` below is the accepted-debt list, in the shape
   `ui-layout.mjs`'s CONTRAST_WHITELIST established: one entry per
   pre-existing untranslated string, each with a reason. It is NON-VACUOUS BOTH
   WAYS — an entry whose string now translates FAILS as stale, exactly as a
   stale contrast entry does. So the ledger can only shrink by accident, never
   grow silently.

   NON-VACUITY: zero swept JS files, zero swept HTML files, zero `uiT()` call
   sites, or an i18n runtime that fails to load all FAIL. A gate that finds
   nothing to check has not passed.

   PROVEN RED (1.0.6.24): deleting a DICT entry a `uiT()` site depends on ·
   deleting a DICT entry a markup string depends on · adding a new untranslated
   Russian `uiT()` literal · adding a new untranslated Russian widget title in
   index.html · a stale ledger entry (whitelisting a string that translates) ·
   an emptied DICT · a JS tree that stops being swept. Its comment-out case is
   registered in `comment-mutation-proof`.

   Run: node .ai-dev/quality/checks/i18n-dict-contract.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { stripJsComments, stripHtmlComments } from './lib_source.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const JS_DIR = join(ROOT, 'www', 'network_config', 'static', 'js');
const I18N = join(JS_DIR, 'i18n.js');
const HTML = ['index.html', 'login.html', 'cloud.html']
  .map(f => join(ROOT, 'www', 'network_config', f));

/* ── The accepted-debt ledger ───────────────────────────────────────────────
   Every entry is a string that is visible today and has no EN translation.
   `reason` says why it is not fixed here rather than leaving a bare exemption.
   Fixing one means DELETING its row — a row that no longer excuses anything
   FAILS the run (the stale-ledger check below).                              */
const DICT_WHITELIST = [
  // ── JS, uiT() call sites ────────────────────────────────────────────────
  { s: 'Не удалось завершить привязку',
    reason: 'app/alice.js pairing error toast; www/ is outside the 1.0.6.24 P3 bounds — reported for the follow-up package' },
  { s: 'Используйте su root для входа, затем обычные команды.\nПример: id -u -n',
    reason: 'app/misc.js root-console hint; multi-line, needs a copy decision for EN. Same bounds note as above' },

  // ── markup: units, tokens and short axis labels ─────────────────────────
  { s: '0 МГц', reason: 'CPU-frequency placeholder before the first poll; overwritten by applyPriorityStatus' },
  { s: '0 Б', reason: 'byte-count placeholder before the first poll; overwritten by the storage/traffic renderers' },
  { s: 'МР-02м', reason: 'product name; the EN form (MR-02m) is a product decision, not a translation' },
  { s: 'ДТВ-RS-485', reason: 'product name; see МР-02м' },
  { s: 'СЭ-02м-3', reason: 'product name; see МР-02м' },
  { s: '1 ч', reason: 'history range chip' },
  { s: '6 ч', reason: 'history range chip' },
  { s: '24 ч', reason: 'history range chip' },
  { s: '7 д', reason: 'history range chip' },
  { s: '30 д', reason: 'history range chip' },
  { s: 'с нач. мес.', reason: 'history range chip' },
  { s: 'за месяц', reason: 'history range chip' },
  { s: 'Pa ср.', reason: 'CE phase-power column header' },
  { s: 'Pb ср.', reason: 'CE phase-power column header' },
  { s: 'Pc ср.', reason: 'CE phase-power column header' },
  { s: 'P∑ ср.', reason: 'CE phase-power column header' },
  { s: 'кВт·ч', reason: 'energy unit' },
  { s: '₽/кВт·ч', reason: 'tariff unit' },
  { s: 'ΔE × ₽/кВт·ч', reason: 'cost formula label' },
  { s: 'Архив: …', reason: 'archive-status placeholder, rewritten by devices.js' },

  // ── markup: sentences ───────────────────────────────────────────────────
  { s: 'Нажмите кнопку — устройство получит код сопряжения. Затем войдите в личный кабинет на',
    reason: 'cloud pairing copy, split across an <a>; the EN half needs a copy decision' },
  { s: 'и введите этот код.', reason: 'tail of the cloud pairing sentence above' },
  { s: 'Введите этот код в личном кабинете на', reason: 'cloud pairing copy, split across an <a>' },
  { s: 'Перейти к настройкам облака', reason: 'cloud.html standalone page link' },
  { s: 'Сервер автоматизации ЦИНТРОН', reason: 'login.html tagline' },
  { s: 'Неверный логин или пароль', reason: 'login.html error line; login.html does not load i18n.js' },
  { s: 'Войти', reason: 'login.html submit button; see above' },

  // ── markup: attributes ──────────────────────────────────────────────────
  { s: 'Вставьте токен', reason: 'placeholder on the cloud token field' },
  { s: 'Остановить мост Modbus→MQTT', reason: 'services widget button title' },
  { s: 'Реле 1', reason: 'HW output button title' },
  { s: 'Режим графика', reason: 'devices chart-mode button title' },
  { s: 'Скачать Excel (.xlsx) за выбранный период', reason: 'devices export button title' },
  { s: '₽ за 1 кВт·ч', reason: 'tariff input title; currency + unit, see кВт·ч' },
  { s: 'ΔE (кВт·ч) × тариф (₽/кВт·ч)', reason: 'cost formula title; see ΔE × ₽/кВт·ч' },
];

const CYRILLIC = /[Ѐ-ӿ]/;
let fails = 0;
const ok = m => console.log(`i18n-dict-contract: ok    ${m}`);
const bad = m => { console.log(`i18n-dict-contract: FAIL  ${m}`); fails++; };

/* ── load the shipped runtime as the oracle ───────────────────────────────── */
function loadI18n() {
  if (!existsSync(I18N)) {
    console.log(`i18n-dict-contract: FAIL  the i18n runtime is missing: ${I18N}`);
    process.exit(1);
  }
  const sandbox = {
    localStorage: { getItem: () => null, setItem: () => {} },
    document: {
      readyState: 'complete',
      addEventListener: () => {},
      documentElement: {},
      body: null,
      getElementById: () => null,
      querySelectorAll: () => [],
      createTreeWalker: () => ({ nextNode: () => null }),
    },
    MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_NODE: 9 },
    NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1 },
    requestAnimationFrame: () => {},
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  try {
    vm.runInContext(readFileSync(I18N, 'utf8'), sandbox, { filename: 'i18n.js' });
  } catch (e) {
    console.log(`i18n-dict-contract: FAIL  the i18n runtime did not load: ${e.message}`);
    process.exit(1);
  }
  const api = sandbox.window.sa02mI18n;
  if (!api || typeof api.t !== 'function' || typeof api.setLang !== 'function') {
    console.log('i18n-dict-contract: FAIL  i18n.js defined no usable window.sa02mI18n — the gate has no oracle');
    process.exit(1);
  }
  api.setLang('en');
  if (api.lang !== 'en') {
    console.log('i18n-dict-contract: FAIL  setLang("en") did not switch the runtime — every string would read as untranslated');
    process.exit(1);
  }
  // Self-check the oracle before trusting it: a known pair must translate and
  // a nonsense string must not. Without this a broken load would report the
  // whole tree missing (noise) or the whole tree fine (a false green).
  if (api.t('Сеть') === 'Сеть') {
    console.log('i18n-dict-contract: FAIL  the oracle does not translate a known DICT entry — DICT is empty or unreachable');
    process.exit(1);
  }
  if (api.t('Зумбарабумбарашечка') !== 'Зумбарабумбарашечка') {
    console.log('i18n-dict-contract: FAIL  the oracle "translates" a nonsense string — it cannot tell missing from present');
    process.exit(1);
  }
  return api;
}

const i18n = loadI18n();
const translated = s => i18n.t(s) !== s;

/* ── half A: uiT() literals in the served JS ──────────────────────────────── */
function jsFiles(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) jsFiles(p, out);
    else if (e.endsWith('.js')) out.push(p);
  }
  return out;
}
if (!existsSync(JS_DIR)) {
  console.log(`i18n-dict-contract: FAIL  the served JS tree is missing: ${JS_DIR}`);
  process.exit(1);
}
const JS = jsFiles(JS_DIR).filter(f => f !== I18N);
if (JS.length < 5) {
  bad(`only ${JS.length} JS file(s) swept — the sweep is broken, not the tree small`);
}

// uiT('...') / uiT("...") — single-line, quote-matched, escape-aware.
const UIT = /\buiT\(\s*(['"])((?:\\.|(?!\1)[^\\\r\n])*)\1\s*\)/g;
const found = new Map();   // string -> "file:line"
function record(map, s, where) { if (!map.has(s)) map.set(s, where); }

for (const f of JS) {
  const lines = readFileSync(f, 'utf8').split('\n');
  lines.forEach((line, i) => {
    UIT.lastIndex = 0;
    let m;
    while ((m = UIT.exec(line))) {
      const raw = m[2];
      let s;
      try { s = JSON.parse('"' + raw.replace(/"/g, '\\"').replace(/\\'/g, "'") + '"'); }
      catch { s = raw; }
      record(found, s, `${relative(ROOT, f).split(sep).join('/')}:${i + 1}`);
    }
  });
}
const uitSites = found.size;
if (uitSites < 50) {
  bad(`only ${uitSites} distinct uiT() literal(s) found — the extraction is broken (expected >=50)`);
} else {
  ok(`${uitSites} distinct uiT() literal(s) across ${JS.length} served JS file(s)`);
}

/* ── half B: visible strings in the served markup ─────────────────────────── */
// The runtime returns early on these ids (isI18nControl), so their own text and
// attributes are never translated and must not be reported.
const I18N_CONTROL_IDS = ['lang-toggle', 'theme-toggle', 'lang-toggle-label',
  'theme-toggle-label', 'theme-toggle-icon', 'topbar-user-name',
  'time-sys-disp', 'time-rtc-disp'];
const TRANSLATED_ATTRS = ['title', 'aria-label', 'placeholder', 'alt'];

let htmlSwept = 0;
for (const f of HTML) {
  if (!existsSync(f)) { bad(`served page missing: ${relative(ROOT, f)}`); continue; }
  htmlSwept++;
  let html = stripHtmlComments(readFileSync(f, 'utf8'));
  // apply() starts at document.body — <head> is never walked.
  const bodyAt = html.search(/<body\b/i);
  if (bodyAt >= 0) html = html.slice(bodyAt);
  // walk() returns early on these tags.
  html = html.replace(/<(script|style|textarea)\b[^>]*>[\s\S]*?<\/\1>/gi, '');

  const rel = relative(ROOT, f).split(sep).join('/');
  // Text nodes.
  for (const chunk of html.split(/<[^>]*>/)) {
    const s = chunk.replace(/\s+/g, ' ').trim();
    if (s && CYRILLIC.test(s)) record(found, s, `${rel} (text)`);
  }
  // Attributes on tags the runtime does visit.
  for (const tag of html.match(/<[^>]+>/g) || []) {
    const idm = tag.match(/\bid\s*=\s*"([^"]*)"/);
    if (idm && I18N_CONTROL_IDS.includes(idm[1])) continue;
    for (const attr of TRANSLATED_ATTRS) {
      const re = new RegExp(`\\b${attr}\\s*=\\s*"([^"]*)"`, 'i');
      const m = tag.match(re);
      if (!m) continue;
      const s = m[1].replace(/\s+/g, ' ').trim();
      if (s && CYRILLIC.test(s)) record(found, s, `${rel} (@${attr})`);
    }
  }
}
if (htmlSwept !== HTML.length) {
  bad(`only ${htmlSwept} of ${HTML.length} served page(s) swept`);
} else {
  ok(`${htmlSwept} served page(s) swept for visible Russian strings`);
}

/* ── the verdict ──────────────────────────────────────────────────────────── */
const ledger = new Map(DICT_WHITELIST.map(e => [e.s, e.reason]));
const missing = [];
const ledgerHit = new Set();

for (const [s, where] of found) {
  if (!CYRILLIC.test(s)) continue;
  if (translated(s)) continue;
  if (ledger.has(s)) { ledgerHit.add(s); continue; }
  missing.push([s, where]);
}
if (missing.length === 0) {
  ok('every visible Russian string outside the ledger has an EN translation');
} else {
  for (const [s, where] of missing) {
    bad(`no EN translation for ${JSON.stringify(s)} — ${where}. Add a DICT entry in i18n.js (or a ledger row here with a reason).`);
  }
}

// Ledger non-vacuity, BOTH directions.
const stale = [];
for (const { s, reason } of DICT_WHITELIST) {
  if (translated(s)) { stale.push([s, 'it now HAS a translation — delete the row']); continue; }
  if (!ledgerHit.has(s)) { stale.push([s, `it matches no swept string any more (${reason})`]); }
}
if (stale.length === 0) {
  ok(`the ${DICT_WHITELIST.length}-row debt ledger is exact (every row still excuses a live string)`);
} else {
  for (const [s, why] of stale) bad(`stale ledger row ${JSON.stringify(s)}: ${why}`);
}

console.log('');
if (fails === 0) {
  console.log(`i18n-dict-contract: ALL OK — ${found.size} candidate string(s) checked against the shipped DICT; ${DICT_WHITELIST.length} accepted-debt row(s)`);
  process.exit(0);
}
console.log(`i18n-dict-contract: ${fails} FAILURE(S)`);
process.exit(1);
