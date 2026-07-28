#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-flasher-hints — standalone Node test for capsFromSignature() in
   flasher.js.
   ───────────────────────────────────────────────────────────────────────────
   Regression: SIGNATURE_IO_HINTS is scanned to map a device signature string
   to its (DO, DI, AO, AI) channel caps. Object insertion order used to decide
   the match, so a short key (e.g. "6DO") — itself a substring of a longer,
   more specific key's signature (e.g. "6DO5DI2AO") — could match first and
   silently drop the extra DI/AO channels. capsFromSignature() now sorts keys
   by length descending at lookup time so the most specific match always wins.

   Mutation proof: revert flasher.js's capsFromSignature() to iterate
   Object.entries(SIGNATURE_IO_HINTS) in insertion order (drop the
   length-descending sort) and test A goes red — "6DO" (inserted before
   "6DO5DI2AO") would match "6DO5DI2AO" first and return [6, 0, 0, 0].

   The object literal and function are extracted from flasher.js by
   brace-matching and evaluated directly, so the test exercises the SHIPPED
   source, not a copy. (Brace-matching is naive — it does not skip braces
   inside string literals; safe here because neither extracted block's string
   literals contain braces.)
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FLASHER_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'flasher.js');

function extractFn(src, name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) throw new Error('function ' + name + '() not found in flasher.js');
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no body brace for ' + name + '()');
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

function extractConstObject(src, name) {
  const marker = 'const ' + name + ' = ';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('const ' + name + ' not found in flasher.js');
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no opening brace for ' + name);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) {
      return src.slice(start, j + 1) + ';';
    }
  }
  throw new Error('unbalanced braces extracting ' + name);
}

function loadCapsFromSignature() {
  const src = readFileSync(FLASHER_JS, 'utf8');
  const hintsSrc = extractConstObject(src, 'SIGNATURE_IO_HINTS');
  const stripSrc = extractFn(src, 'stripBootloaderSignatureSuffix');
  const normalizeSrc = extractFn(src, 'normalizeModuleSignature');
  const capsSrc = extractFn(src, 'capsFromSignature');
  const combined = [hintsSrc, stripSrc, normalizeSrc, capsSrc,
    'return { capsFromSignature: capsFromSignature, SIGNATURE_IO_HINTS: SIGNATURE_IO_HINTS };'].join('\n');
  return new Function(combined)();
}

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

function deepEqualArr(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length
    && a.every((v, i) => v === b[i]);
}

const { capsFromSignature, SIGNATURE_IO_HINTS } = loadCapsFromSignature();

// Test A — regression: "6DO5DI2AO" must not be shadowed by the shorter "6DO".
const regression = capsFromSignature('6DO5DI2AO');
check(deepEqualArr(regression, [6, 5, 2, 0]),
  'capsFromSignature("6DO5DI2AO") === [6, 5, 2, 0] (got ' + JSON.stringify(regression) + ')');

// Test A2 — "10DI" is a real alternate signature for the 10-DI module,
// recorded independently in mqtt.js's inferModuleTypeFromName alias list and
// mqtt_bus_scan.py's alias table. Previously reached only via the dropped
// startswith(key.slice(0, 4)) fallback ('10DICON'.slice(0, 4) === '10DI');
// now an explicit dict key.
const shortAlias = capsFromSignature('10DI');
check(deepEqualArr(shortAlias, [0, 10, 0, 0]),
  'capsFromSignature("10DI") === [0, 10, 0, 0] (got ' + JSON.stringify(shortAlias) + ')');

// Test A3 — the longer '10DICON' key must still win over the shorter '10DI'
// alias for a full '10DICON...' signature (longest-key-first).
const fullSig = capsFromSignature('10DICON');
check(deepEqualArr(fullSig, [0, 10, 0, 0]),
  'capsFromSignature("10DICON") === [0, 10, 0, 0] (got ' + JSON.stringify(fullSig) + ')');

// Test B — self-consistency: every hint key must resolve to its own caps,
// guarding against the same shadowing bug class being reintroduced when a
// new key is added later.
for (const key of Object.keys(SIGNATURE_IO_HINTS)) {
  const expected = SIGNATURE_IO_HINTS[key];
  const got = capsFromSignature(key);
  check(deepEqualArr(got, expected),
    'capsFromSignature("' + key + '") === ' + JSON.stringify(expected) + ' (got ' + JSON.stringify(got) + ')');
}

if (failures) {
  console.error('js-unit-flasher-hints: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-flasher-hints: capsFromSignature ok');
process.exit(0);
