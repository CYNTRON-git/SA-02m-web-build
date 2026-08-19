#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-flasher-signatures — standalone Node test for flasher.js's
   module-signature recognition/caps/route functions.
   ───────────────────────────────────────────────────────────────────────────
   Regression coverage for two bugs fixed together (see
   .ai-dev/plans/flasher-signature-canonical-names.md):

   1) Missing signature-hint keys — a genuine MR-02m/MP-02m AI+AO module can
      report its Holding-290 signature as `6AO6AI` (legacy), `6AI6AO`
      (canonical, MR-02m firmware >=1.0.10.29), or `AO6AI6` (legacy
      letter-first) — all one board (MP02_AO6AI6), the bootloader's own
      bl_sig_match accepts all three. Before this fix, `6AI6AO`/`AO6AI6` were
      missing from `hintKeys`/`SIGNATURE_IO_HINTS`, so the module was
      misclassified as third-party Wiren Board hardware.
   2) Unsorted-iteration shadowing — `capsFromSignature` walked
      `SIGNATURE_IO_HINTS` in insertion order, so a short key (`6DO`) could
      match inside a longer compound signature (`6DO5DI2AO`) before the
      longer, correct key was tried, returning the wrong caps tuple.

   Mirrors the Python regression suite (test_module_profiles_policy.py
   TestCapsFromSignature, test_flash_route.py, test_module_line_profiles.py)
   for cross-language symmetry — this repo's flasher duplicates the
   recognition logic independently in Python (backend) and JS (frontend
   pre-flight check), so both must be fixed and tested together or the two
   layers can disagree.

   Following the js-unit precedent (test-clear-session-cookie.mjs): the
   functions/constants under test are extracted from the SHIPPED flasher.js
   by brace/bracket-matching and evaluated in a minimal sandbox, so the test
   exercises the shipped source, not a re-typed copy.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FLASHER_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'flasher.js');

/** Extract a top-level `function name(...) { ... }` declaration by brace-matching. */
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

/** Extract a top-level `const NAME = [...]` or `const NAME = {...}` statement. */
function extractConst(src, name) {
  const start = src.indexOf('const ' + name);
  if (start < 0) throw new Error('const ' + name + ' not found in flasher.js');
  const eq = src.indexOf('=', start);
  let openIdx = eq + 1;
  while (/\s/.test(src[openIdx])) openIdx++;
  const openCh = src[openIdx];
  const closeCh = openCh === '[' ? ']' : openCh === '{' ? '}' : null;
  if (!closeCh) throw new Error('const ' + name + ' is not an array/object literal');
  let depth = 0;
  for (let j = openIdx; j < src.length; j++) {
    if (src[j] === openCh) depth++;
    else if (src[j] === closeCh && --depth === 0) {
      const semi = src.indexOf(';', j);
      return src.slice(start, (semi >= 0 ? semi : j) + 1) + (semi >= 0 ? '' : ';');
    }
  }
  throw new Error('unbalanced brackets extracting const ' + name);
}

const src = readFileSync(FLASHER_JS, 'utf8');

const parts = [
  extractConst(src, 'WB_RELAY_SIG_PREFIXES'),
  extractConst(src, 'WB_MAO4_SIG_PREFIXES'),
  extractConst(src, 'SIGNATURE_IO_HINTS'),
  extractFn(src, 'stripBootloaderSignatureSuffix'),
  extractFn(src, 'isMpModuleSignatureForFirmwareHint'),
  extractFn(src, 'isWirenboardModuleSignature'),
  extractFn(src, 'resolveDeviceFlashRoute'),
  extractFn(src, 'normalizeModuleSignature'),
  extractFn(src, 'capsFromSignature'),
  extractFn(src, 'normalizeProductToken'),
  extractFn(src, 'tokenLooksLikeDtv'),
  extractFn(src, 'tokenLooksLikeCe'),
  extractFn(src, 'manifestDeviceForSignature'),
  'return { resolveDeviceFlashRoute, capsFromSignature, manifestDeviceForSignature };',
];

const { resolveDeviceFlashRoute, capsFromSignature, manifestDeviceForSignature } =
  new Function(parts.join('\n'))();

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

function arraysEqual(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length &&
    a.every((v, i) => v === b[i]);
}

// --- resolveDeviceFlashRoute: canonical + legacy AI+AO signature forms ---
check(resolveDeviceFlashRoute('6AI6AO') === 'mp_mr',
  "resolveDeviceFlashRoute('6AI6AO') === 'mp_mr' (canonical count-first)");
check(resolveDeviceFlashRoute('AO6AI6') === 'mp_mr',
  "resolveDeviceFlashRoute('AO6AI6') === 'mp_mr' (legacy letter-first)");
check(resolveDeviceFlashRoute('6AO6AI') === 'mp_mr',
  "resolveDeviceFlashRoute('6AO6AI') === 'mp_mr' (legacy — regression guard)");

// --- capsFromSignature: same three forms resolve identical caps ---
check(arraysEqual(capsFromSignature('6AI6AO'), [0, 0, 6, 6]),
  "capsFromSignature('6AI6AO') deep-equals [0, 0, 6, 6]");
check(arraysEqual(capsFromSignature('AO6AI6'), [0, 0, 6, 6]),
  "capsFromSignature('AO6AI6') deep-equals [0, 0, 6, 6]");
check(arraysEqual(capsFromSignature('6AO6AI'), [0, 0, 6, 6]),
  "capsFromSignature('6AO6AI') deep-equals [0, 0, 6, 6] (legacy — regression guard)");

// --- capsFromSignature: longest-key-first fixes the shadowing bug ---
check(arraysEqual(capsFromSignature('6DO5DI2AO'), [6, 5, 2, 0]),
  "capsFromSignature('6DO5DI2AO') deep-equals [6, 5, 2, 0] (longest match, not the shorter '6DO' shadow)");

// --- capsFromSignature: "10DI" short alias (mqtt.js's own alias list uses
// it) must resolve on its own, and must not shadow the longer "10DICON". ---
check(arraysEqual(capsFromSignature('10DI'), [0, 10, 0, 0]),
  "capsFromSignature('10DI') deep-equals [0, 10, 0, 0] (short alias for '10DICON')");
check(arraysEqual(capsFromSignature('10DICON'), [0, 10, 0, 0]),
  "capsFromSignature('10DICON') deep-equals [0, 10, 0, 0] (longer key still wins)");

// --- manifestDeviceForSignature: firmware family per scanned signature.
// MUST mirror Python module_profiles.manifest_device_for_signature so the
// per-family "свежая прошивка" badge never offers a DTV/СЭ an MR-02m version.
// The ENMETER alias is the drift the reviewer caught: Python SPECIAL_SIG_CODES
// maps EN_METER/ENMETER → MP02_CE02M3, so JS must return 'CE-02m-3', not 'MR-02m'.
const MANIFEST_DEVICE_CASES = [
  ['Sens.', 'RTU-Sensor'],   // DTV default EEPROM signature (type 17)
  ['SENSOR', 'RTU-Sensor'],
  ['12AI', 'MR-02m'],        // MR/MP-02m I/O variant
  ['6AI6AO', 'MR-02m'],
  ['CE02M3', 'CE-02m-3'],    // CE-02m-3 (type 100)
  ['CE-02M3', 'CE-02m-3'],
  ['EN_METER', 'CE-02m-3'],  // ENMETER mezzanine → CE-02m-3 family (parity)
  ['ENMETER', 'CE-02m-3'],
  ['MR2M-01', ''],           // third-party Wiren Board relay → no CYNTRON family
];
for (const [sig, want] of MANIFEST_DEVICE_CASES) {
  const got = manifestDeviceForSignature(sig);
  check(got === want,
    `manifestDeviceForSignature('${sig}') === '${want}' (got '${got}')`);
}

if (failures) {
  console.error('js-unit-flasher-signatures: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-flasher-signatures: ok');
process.exit(0);
