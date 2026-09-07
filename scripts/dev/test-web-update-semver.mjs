#!/usr/bin/env node
/* Unit-test status.js internet-update gate: dotted-integer compare,
   webUpdResolveAvailable, and applyWebUpdateCheckUI button disable. */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app', 'status.js');
const src = fs.readFileSync(SRC, 'utf8');

function extractFn(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing function ' + name);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unclosed function ' + name);
}

function makeEls() {
  return {
    'web-upd-apply-btn': {
      hidden: false,
      disabled: true,
      textContent: 'Применить',
      title: '',
      attrs: {},
      setAttribute(k, v) { this.attrs[k] = v; }
    },
    'web-upd-status': {
      hidden: true,
      textContent: '',
      classList: { remove() {}, add() {} }
    },
    'web-upd-deployed-ver': { textContent: '—' },
    'web-upd-remote-ver': { textContent: '—' },
    'web-upd-checked': { textContent: '—' }
  };
}

function makeCtx(els) {
  const ctx = {
    _webUpdTxnActive: false,
    _webUpdLastCheck: null,
    _webUpdOnlineCanApply: false,
    uiT: (s) => s,
    setText: (id, v) => { if (els[id]) els[id].textContent = v; },
    document: { getElementById: (id) => els[id] || null }
  };
  const code = [
    'shortGitSha',
    'deployedRefDisplay',
    'fmtWebUpdChecked',
    'compareSemver',
    'webUpdResolveAvailable',
    'webUpdStatusSaysNone',
    'webUpdOnlineApplyAllowed',
    'webUpdSetOnlineApplyEnabled',
    'webUpdVersionDisplay',
    'applyWebUpdateCheckUI',
    'webUpdShouldPostApply'
  ].map(extractFn).join('\n\n');
  vm.runInNewContext(code, ctx, { filename: 'status.js-extract' });
  return ctx;
}

let fails = 0;
function ok(name) { process.stdout.write('  ok    ' + name + '\n'); }
function bad(name, detail) {
  process.stdout.write('  FAIL  ' + name + (detail ? ' — ' + detail : '') + '\n');
  fails += 1;
}
function eq(name, got, want) {
  if (got === want) ok(name);
  else bad(name, 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
}

const bare = makeCtx(makeEls());

process.stdout.write('A. compareSemver (dotted integers)\n');
eq('1.0.6.37 > 1.0.6.29', bare.compareSemver('1.0.6.37', '1.0.6.29'), 1);
eq('1.0.6.29 < 1.0.6.37', bare.compareSemver('1.0.6.29', '1.0.6.37'), -1);
eq('equal 1.0.6.37', bare.compareSemver('1.0.6.37', '1.0.6.37'), 0);
eq('1.0.6 == 1.0.6.0', bare.compareSemver('1.0.6', '1.0.6.0'), 0);
eq('1.0.6 > 1.0.5.99', bare.compareSemver('1.0.6', '1.0.5.99'), 1);
eq('invalid → null', bare.compareSemver('1.0.x', '1.0.6.29'), null);
eq('not string-compare 1.0.6.9 < 1.0.6.10', bare.compareSemver('1.0.6.9', '1.0.6.10'), -1);

process.stdout.write('B. webUpdResolveAvailable (internet Apply gate)\n');
eq(
  'current 1.0.6.37 > available 1.0.6.29 → false',
  bare.webUpdResolveAvailable({
    deployed_version: '1.0.6.37',
    remote_version: '1.0.6.29',
    update_available: true
  }),
  false
);
eq(
  'current == available → false',
  bare.webUpdResolveAvailable({
    deployed_version: '1.0.6.29',
    remote_version: '1.0.6.29',
    update_available: true
  }),
  false
);
eq(
  'current 1.0.6.29 < available 1.0.6.37 → true',
  bare.webUpdResolveAvailable({
    deployed_version: '1.0.6.29',
    remote_version: '1.0.6.37',
    update_available: false
  }),
  true
);
eq(
  'failed check (no versions, no remote) → null',
  bare.webUpdResolveAvailable({
    error: 'network_or_git_failed',
    update_available: null
  }),
  null
);
eq(
  'unparsable versions → null (do not enable)',
  bare.webUpdResolveAvailable({
    deployed_version: '1.0.x',
    remote_version: 'nope',
    update_available: true
  }),
  null
);
eq(
  'update_available false without versions → false',
  bare.webUpdResolveAvailable({ update_available: false }),
  false
);
eq(
  'null payload → null',
  bare.webUpdResolveAvailable(null),
  null
);

process.stdout.write('C. applyWebUpdateCheckUI (internet Apply disabled)\n');

function runCheck(payload) {
  const els = makeEls();
  const ctx = makeCtx(els);
  ctx.applyWebUpdateCheckUI(payload);
  return { els, ctx };
}

{
  const { els, ctx } = runCheck({
    deployed_version: '1.0.6.37',
    remote_version: '1.0.6.29',
    update_available: true,
    checked_at: '2026-09-07T12:34:03Z'
  });
  eq('operator case: status «Обновлений нет»', els['web-upd-status'].textContent, 'Обновлений нет');
  eq('operator case: Apply disabled', els['web-upd-apply-btn'].disabled, true);
  eq('operator case: aria-disabled', els['web-upd-apply-btn'].attrs['aria-disabled'], 'true');
  eq('operator case: no POST', ctx.webUpdShouldPostApply(), false);
  eq('operator case: versions shown', els['web-upd-deployed-ver'].textContent, '1.0.6.37');
  eq('operator case: remote shown', els['web-upd-remote-ver'].textContent, '1.0.6.29');
}

{
  const { els, ctx } = runCheck({
    deployed_version: '1.0.6.29',
    remote_version: '1.0.6.37',
    update_available: true
  });
  eq('older board: Apply enabled', els['web-upd-apply-btn'].disabled, false);
  eq('older board: POST allowed', ctx.webUpdShouldPostApply(), true);
}

{
  const { els, ctx } = runCheck({
    error: 'network_or_git_failed',
    update_available: null
  });
  eq('failed check: Apply disabled', els['web-upd-apply-btn'].disabled, true);
  eq('failed check: no POST', ctx.webUpdShouldPostApply(), false);
}

{
  const { els, ctx } = runCheck({ error: 'unauthorized' });
  eq('unauthorized: Apply disabled', els['web-upd-apply-btn'].disabled, true);
  eq('unauthorized: no POST', ctx.webUpdShouldPostApply(), false);
}

{
  const els = makeEls();
  els['web-upd-file-apply-btn'] = { disabled: false };
  const ctx = makeCtx(els);
  ctx.applyWebUpdateCheckUI({
    deployed_version: '1.0.6.37',
    remote_version: '1.0.6.29',
    update_available: true
  });
  eq('file Apply untouched when GitHub has nothing', els['web-upd-file-apply-btn'].disabled, false);
}

if (fails) {
  process.stdout.write('test-web-update-semver: ' + fails + ' FAIL\n');
  process.exit(1);
}
process.stdout.write('test-web-update-semver: ok\n');
