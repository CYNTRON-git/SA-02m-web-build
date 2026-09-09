#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - brace-extracts the SHIPPED status.js functions and RUNS them in a vm against a minimal DOM; every assertion is a computed result (a compare, a button state, a refusal text), never a source-line pin a comment token could satisfy. Its RED/GREEN evidence is the pre-fix tree (a7183c1~1 for the gate, 1f6f1a1 for the C9/C13 cases) recorded in the header below.
/* Unit-test status.js internet-update gate: dotted-integer compare,
   webUpdResolveAvailable, applyWebUpdateCheckUI button disable, and — since
   1.0.6.39 — (D) the Apply guard is DATA-derived only (audit C9: the old
   webUpdStatusSaysNone() regex over localized rendered text is gone, so a DICT
   wording change or a third locale can no longer re-open or jam a root-
   launching POST) and (E) the CGI's refusal codes are handled explicitly
   (audit C13: E_NO_UPDATE / E_CHECK_STALE map to their own status line and
   keep Apply disabled instead of the generic «Ошибка обновления»; contract:
   docs/contracts/web-update.md). PROVEN RED on 1f6f1a1: D — POST refused
   while the data said "update available" because the status text read
   «Обновлений нет», and webUpdStatusSaysNone still defined; E — no
   webUpdApplyRefusal in status.js. */
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
    'webUpdOnlineApplyAllowed',
    'webUpdSetOnlineApplyEnabled',
    'webUpdVersionDisplay',
    'applyWebUpdateCheckUI',
    'webUpdShouldPostApply'
  ].map(extractFn)
    // Present on one side of the 1.0.6.39 change only — extracted when found
    // so BOTH trees run to a verdict (section D/E) instead of throwing here.
    .concat(['webUpdStatusSaysNone', 'webUpdApplyRefusal'].map(extractFnOpt))
    .join('\n\n');
  vm.runInNewContext(code, ctx, { filename: 'status.js-extract' });
  return ctx;
}

function extractFnOpt(name) {
  return src.indexOf('function ' + name + '(') < 0 ? '' : extractFn(name);
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

process.stdout.write('D. the Apply guard is data-derived only (no localized-text probe)\n');
eq('webUpdStatusSaysNone is gone from status.js', typeof bare.webUpdStatusSaysNone, 'undefined');
{
  // The rendered status says «Обновлений нет» (a stale line, a renamed DICT
  // entry, a locale the regex never knew) while the DATA says an update is
  // available: the data decides. Before 1.0.6.39 the text probe vetoed it.
  const els = makeEls();
  const ctx = makeCtx(els);
  ctx.applyWebUpdateCheckUI({ deployed_version: '1.0.6.29', remote_version: '1.0.6.37', update_available: true });
  els['web-upd-status'].hidden = false;
  els['web-upd-status'].textContent = 'Обновлений нет';
  eq('status text «Обновлений нет» does not veto a data-available POST', ctx.webUpdShouldPostApply(), true);
  eq('webUpdOnlineApplyAllowed ignores the rendered text', ctx.webUpdOnlineApplyAllowed(ctx._webUpdLastCheck), true);
}
{
  // The inverse: the text says nothing at all, the data says "current is
  // newer" — still refused, from the data.
  const els = makeEls();
  const ctx = makeCtx(els);
  ctx.applyWebUpdateCheckUI({ deployed_version: '1.0.6.37', remote_version: '1.0.6.29', update_available: true });
  els['web-upd-status'].hidden = true;
  els['web-upd-status'].textContent = '';
  eq('an empty status line does not re-open a data-refused POST', ctx.webUpdShouldPostApply(), false);
}

process.stdout.write('E. the CGI refusal codes are handled explicitly (docs/contracts/web-update.md)\n');
if (typeof bare.webUpdApplyRefusal !== 'function') {
  bad('webUpdApplyRefusal is missing from status.js — E_NO_UPDATE / E_CHECK_STALE fall into the generic error path');
} else {
  const noUpd = bare.webUpdApplyRefusal({ ok: false, status: 'error', error: 'no_update', error_code: 'E_NO_UPDATE', log: 'Обновлений нет' });
  eq('E_NO_UPDATE → its own status line', noUpd && noUpd.status, 'Обновлений нет');
  eq('E_NO_UPDATE → Apply stays disabled', noUpd && noUpd.canApply, false);
  const stale = bare.webUpdApplyRefusal({ ok: false, status: 'error', error: 'check_stale', error_code: 'E_CHECK_STALE', log: 'x' });
  eq('E_CHECK_STALE → asks for a fresh check', stale && stale.status, 'Сведения об обновлении устарели — нажмите «Проверить»');
  eq('E_CHECK_STALE → Apply stays disabled', stale && stale.canApply, false);
  eq('a generic error is not a refusal (null → the error path)', bare.webUpdApplyRefusal({ ok: false, status: 'error', log: 'boom' }), null);
  eq('a running answer is not a refusal', bare.webUpdApplyRefusal({ ok: true, status: 'running' }), null);
  eq('a null payload is not a refusal', bare.webUpdApplyRefusal(null), null);
}

if (fails) {
  process.stdout.write('test-web-update-semver: ' + fails + ' FAIL\n');
  process.exit(1);
}
process.stdout.write('test-web-update-semver: ok\n');
