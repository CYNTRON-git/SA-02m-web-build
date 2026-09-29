#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-post-csrf-headers — every `fetch` POST init in the served JS carries the
   panel's CSRF token (`withCsrfHeaders`).

   THE DEFECT. The CGI side of docs/decisions/selective-csrf-policy.md is held
   set-wide by cgi-csrf-policy; the PANEL side was held by habit. Audit
   2026-09-24 (M3) found the two HTTP daemons outside the policy, and the five
   fetch sites that call them (flasher.js apiPost / apiUpload / configApi,
   devices.js widgets add/remove) sent no token — a rule that is «every
   mutating POST» on the server needs its mirror on the client, or the next
   endpoint ships the same way. This row is that mirror.

   WHAT IS SWEPT. Every *.js under www/network_config/static/js (recursively),
   comment-stripped (lib_source.mjs). Each `method: 'POST'` / `method: "POST"`
   token is taken as a fetch-init object literal; the sweep walks back to the
   innermost unmatched `{` and forward to its matching `}` and requires a
   `withCsrfHeaders(` call INSIDE that init. A site that builds its headers
   elsewhere is a finding by design (the token then has to be traced by hand)
   — ledger it in EXEMPT below with the reason if that is ever right.

   WHAT IS NOT SWEPT, stated: XHR uploads (`xhr.open('POST', …)` +
   `setRequestHeader` in app/status.js — read the token at send time, their own
   pins live with 1.0.6.63's work), and login.html's inline login POST (no
   session yet; not under static/js). GET routes need no token by policy.

   NON-VACUITY: >= MIN_FILES bundles swept, >= MIN_SITES POST sites seen (a
   sweep that stops seeing the tree FAILS), and every EXEMPT row must match
   exactly one live site (a stale exemption FAILS — quality-gate-rigor (g)).

   RED, measured 2026-09-28 on d66d7b6 (1.0.6.56): 31 POST sites, 5 without the
   token — flasher.js:201 (apiPost), :361 (apiUpload), :1918 (configApi),
   devices.js:584 (removeWidget), :599 (addWidget); GREEN after the five gained
   `withCsrfHeaders`. The comment-mutation case (`//` before the devices.js
   `headers: withCsrfHeaders(` line inside fetchJson) is registered in
   comment-mutation-proof.

   Run: node .ai-dev/quality/checks/js-post-csrf-headers.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stripJsComments } from './lib_source.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const JS_ROOT = join(ROOT, 'www', 'network_config', 'static', 'js');
const MIN_FILES = 5;
const MIN_SITES = 30;

// file (relative to static/js) | literal on the `method: 'POST'` line | reason
// Empty today — the mechanism exists so a justified exception is recorded, not
// silently tolerated. Each row must match exactly one live site.
const EXEMPT = [];

let fails = 0;
const ok = (m) => console.log(`js-post-csrf-headers: ok    ${m}`);
const bad = (m) => { console.log(`js-post-csrf-headers: FAIL  ${m}`); fails++; };

function walk(dir, out) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

let files;
try { files = walk(JS_ROOT, []).sort(); } catch (e) { bad(`cannot read ${JS_ROOT}: ${e.message}`); }
if (!files || files.length < MIN_FILES) {
  bad(`only ${files ? files.length : 0} *.js under static/js (floor ${MIN_FILES}) — the sweep is dead, not green`);
  console.log(`js-post-csrf-headers: ${fails} FAILED`);
  process.exit(1);
}

// Innermost `{` enclosing `at`, scanning backward with brace depth; -1 when none.
function openerBefore(src, at) {
  let depth = 0;
  for (let i = at - 1; i >= 0; i--) {
    const c = src[i];
    if (c === '}') depth++;
    else if (c === '{') { if (depth === 0) return i; depth--; }
  }
  return -1;
}
function closerAfter(src, open) {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return i; }
  }
  return -1;
}
const lineOf = (src, idx) => src.slice(0, idx).split('\n').length;

const POST_RE = /method\s*:\s*(['"])POST\1/g;
let nSites = 0;
const exemptHits = new Map(EXEMPT.map((r) => [r.join('|'), 0]));

for (const file of files) {
  const rel = relative(JS_ROOT, file).split('\\').join('/');
  const src = stripJsComments(readFileSync(file, 'utf8'));
  for (const m of src.matchAll(POST_RE)) {
    nSites++;
    const line = lineOf(src, m.index);
    const open = openerBefore(src, m.index);
    const close = open >= 0 ? closerAfter(src, open) : -1;
    if (open < 0 || close < 0) { bad(`${rel}:${line} — POST token outside any brace-bounded init (parser could not bound it)`); continue; }
    const init = src.slice(open, close + 1);
    const lineText = src.split('\n')[line - 1];
    const ex = EXEMPT.find((r) => r[0] === rel && lineText.includes(r[1]));
    if (ex) { exemptHits.set(ex.join('|'), exemptHits.get(ex.join('|')) + 1); ok(`${rel}:${line} — exempt: ${ex[2]}`); continue; }
    if (/withCsrfHeaders\s*\(/.test(init)) ok(`${rel}:${line} — POST init carries withCsrfHeaders(`);
    else bad(`${rel}:${line} — POST init without withCsrfHeaders( — a mutating request the CSRF policy does not cover (selective-csrf-policy.md)`);
  }
}

if (nSites >= MIN_SITES) ok(`${files.length} bundles swept, ${nSites} POST sites (floor ${MIN_SITES})`);
else bad(`only ${nSites} POST sites across ${files.length} bundles (floor ${MIN_SITES}) — the sweep stopped seeing the tree`);
for (const [key, n] of exemptHits) {
  if (n !== 1) bad(`EXEMPT row '${key}' matched ${n} site(s), expected exactly 1 — stale or ambiguous ledger row`);
}

console.log('');
if (fails === 0) {
  console.log(`js-post-csrf-headers: ALL OK — ${nSites} POST inits carry the CSRF token`);
  process.exit(0);
}
console.log(`js-post-csrf-headers: ${fails} FAILED`);
process.exit(1);
