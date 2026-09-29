#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it evaluates the shipped alice.js helpers and asserts their return values, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-alice-gateway — the «Яндекс Алиса» card's gateway pill and badge.

   Bench 1.135 (2026-09-28): the card flickered to «Шлюз недоступен» on 8 of 22
   polls while the client stayed connected, because the status endpoint
   probed the gateway on every poll and one slow ping read as «down». The
   backend now answers from the live session or a cached probe
   (docs/contracts/alice-mqtt-mapping.md §Gateway reachability) and says
   `gateway.state = "checking"` while it has no evidence yet. This test pins
   the card side of that contract:
     - `checking` renders a neutral «Проверка шлюза…» / «Проверяется», never
       «Шлюз недоступен» — a missing answer is not a negative one;
     - a connected client with `available: true` reads «Подключено»;
     - `unreachable`, and an older backend that sends only
       `available: false`, still read «Шлюз недоступен» (a TLS-trust failure
       keeps its own wording).
   Same extraction idiom as test-flasher-signature-hints.mjs: the helpers are
   cut out of the SHIPPED alice.js by brace matching and evaluated in a
   sandbox, so the test runs the served source, not a re-typed copy.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ALICE_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app', 'alice.js');

function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found in alice.js');
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

function extractConst(src, name) {
  const start = src.indexOf('const ' + name + ' =');
  if (start < 0) throw new Error('const ' + name + ' not found in alice.js');
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1) + ';';
  }
  throw new Error('unbalanced braces extracting const ' + name);
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

const src = readFileSync(ALICE_JS, 'utf8');
let api;
try {
  api = new Function([
    extractConst(src, 'ALICE_STATE_MAP'),
    extractFn(src, 'aliceCertUntrusted'),
    extractFn(src, 'aliceFriendlyStatus'),
    extractFn(src, 'aliceGatewayBadge'),
    'return { aliceFriendlyStatus, aliceGatewayBadge };',
  ].join('\n'))();
} catch (e) {
  console.error('  FAIL - the shipped alice.js helpers could not be extracted: ' + e.message);
  console.error('js-unit-alice-gateway: 1 assertion(s) failed');
  process.exit(1);
}
const { aliceFriendlyStatus, aliceGatewayBadge } = api;

const on = (gateway, state) => ({ client_enabled: true, gateway, status: { state } });

let r = aliceFriendlyStatus(on({ available: true, state: 'reachable', source: 'client' }, 'connected'));
check(r.text === 'Подключено' && r.kind === 'ok', 'connected client reads «Подключено» — got ' + JSON.stringify(r));

r = aliceFriendlyStatus(on({ available: false, state: 'checking', source: 'probe', probe: null }, 'missing_cert'));
check(r.text === 'Проверка шлюза…' && r.kind === 'unk',
  'no evidence yet reads the neutral «Проверка шлюза…», not «Шлюз недоступен» — got ' + JSON.stringify(r));
r = aliceGatewayBadge(on({ available: false, state: 'checking' }, 'missing_cert'));
check(r.text === 'Проверяется' && r.kind === 'unk', 'the gateway badge says «Проверяется» while checking — got ' + JSON.stringify(r));

r = aliceFriendlyStatus(on({ available: false, state: 'unreachable', probe: { message: 'The read operation timed out' } }, 'offline'));
check(r.text === 'Шлюз недоступен' && r.kind === 'err', 'unreachable reads «Шлюз недоступен» — got ' + JSON.stringify(r));
r = aliceGatewayBadge(on({ available: false, state: 'unreachable' }, 'offline'));
check(r.text === 'Недоступен' && r.kind === 'err', 'the badge reads «Недоступен» when unreachable — got ' + JSON.stringify(r));

r = aliceFriendlyStatus(on({ available: false, probe: { message: 'timed out' } }, 'offline'));
check(r.text === 'Шлюз недоступен', 'an older backend (no `state`) keeps «Шлюз недоступен» — got ' + JSON.stringify(r));
r = aliceGatewayBadge(on({ available: true }, 'connected'));
check(r.text === 'Доступен' && r.kind === 'ok', 'an older backend with available:true reads «Доступен» — got ' + JSON.stringify(r));

r = aliceFriendlyStatus(on({ available: false, state: 'unreachable', probe: { message: '[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate' } }, 'offline'));
check(r.text === 'сертификат шлюза не доверенный', 'a TLS-trust failure keeps its own wording — got ' + JSON.stringify(r));

r = aliceFriendlyStatus({ client_enabled: false, gateway: { available: false, state: 'checking' }, status: { state: 'disabled' } });
check(r.text === 'Отключено', 'a disabled client reads «Отключено» whatever the gateway — got ' + JSON.stringify(r));

if (failures) {
  console.error('js-unit-alice-gateway: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-alice-gateway: ok');
