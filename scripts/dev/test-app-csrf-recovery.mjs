#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - runs the SHIPPED app.js fetch guard (the `401 → login` region, brace-extracted helpers) in a vm against a scripted fetch stub and reads what it DID (calls, headers, a logout, a toast); it pins no source line a comment token could satisfy, and its RED is recorded below against the pre-fix app.js.
/* test-app-csrf-recovery — the panel's reaction to an E_CSRF refusal
   (1.0.6.53; docs/decisions/selective-csrf-policy.md «Реакция панели»).

   WHY. Through cloud.cyntron.ru every mutating POST came back E_CSRF (the
   proxy dropped the X-SA02M-CSRF request header) and app.js treated E_CSRF as
   «session lost»: cookie cleared, login.html — and again after re-login, with
   no clue. The refusal now NAMES its reason (lib_web_auth.sh) and the panel
   reacts honestly: a header we sent that never arrived (`no_header` with a
   token in hand) is a TRANSIT problem — say so, keep the session, no retry;
   anything else → refresh the token once through GET csrf_token.cgi and
   re-issue the request exactly once; only a failed refresh (the session is
   really gone) or a second refusal that is not a transit strip logs out.

   WHAT RUNS. app.js is read from disk; clearSessionCookie / getSa02mCsrfToken /
   withCsrfHeaders are brace-extracted, and the whole `401 → login` region (the
   guard installer + the CSRF helpers) is evaluated in a vm with a fake
   window/document/location and a SCRIPTED `fetch` (one answer per call, in
   order). The observables are the calls the stub saw (url, method, the
   X-SA02M-CSRF header), whether location.replace('login.html') fired, the
   cookie jar, window.SA02M_CSRF and the toast text. Non-vacuous: the region
   must install a wrapper (window.fetch replaced), the helpers must exist, and
   the pre-fix tree is run through the SAME cases (the old IIFE lives in the
   same region), so the RED below is behaviour, not «function missing».

   PROVEN RED on 91157d5 (1.0.6.52, `git show` copy through APP_JS=<path>):
   22 FAIL — cases 1, 2, 4b, 6, 7 and the attempt counts of 3 and 4: immediate logout on every E_CSRF, no refresh
   GET, no retry, no bootstrap; the verdicts of 3, 4, 5, 8, 9 hold on both trees (a failed
   refresh and a second mismatch still log out; GET bodies were never
   inspected; the 401 path is untouched; a non-JSON body is returned as is).

   XHR UPLOADS (section 10, 1.0.6.63). The two file uploads in status.js
   (offline package — uploadOfflineUpdateFile; MPLC project — deployMplcProject)
   use XMLHttpRequest for upload progress, which the fetch wrapper never sees;
   until 1.0.6.63 an E_CSRF there was a bare «Ошибка: csrf» with no refresh and
   no re-send (backlog 2026-09-23). Both functions are brace-extracted from the
   SHIPPED status.js and run in the SAME world on top of the app.js region,
   against a fake XMLHttpRequest + FormData (scripted answers, one per send) and
   recording stubs for the widget helpers. Observables: the XHRs created (url,
   method, the X-SA02M-CSRF header and how many times it was set, the body
   object identity), the refresh GET on the fetch stub, the widget's status
   line / finish call / inspect call, the toast, a logout. The table is the
   wrapper's: no_header with a token in hand → transit toast, one XHR, no
   refresh; any other reason → one GET csrf_token.cgi, the SAME FormData
   instance re-sent once with the refreshed header (progress wired again), the
   caller sees the retry's answer; the retry's own E_CSRF → transit note or
   logout, never a third XHR; a failed refresh → logout; a network error is
   the caller's own path (no refresh).
   PROVEN RED on d66d7b6 (1.0.6.56, APP_JS= + STATUS_JS= `git show` copies):
   22 FAIL — 10a/10c/10d/10e/10f re-send counts and headers (one XHR, no
   refresh GET, no retry, no logout after a failed refresh or a second
   refusal), 10b/10g no transit toast / no SA02M_CSRF_BLOCKED, 10b widget line
   «Ошибка: csrf»; 10.0 and 10h hold on both trees (a plain answer and a
   network error were never the problem), as do 0–9.

   Run: node scripts/dev/test-app-csrf-recovery.mjs
        (APP_JS=<path> / STATUS_JS=<path> for other copies — the RED recipe) */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = process.env.APP_JS || path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');
const STATUS_SRC = process.env.STATUS_JS || path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app', 'status.js');
const statusSrc = fs.readFileSync(STATUS_SRC, 'utf8');

let fails = 0;
function ok(name) { process.stdout.write('  ok    ' + name + '\n'); }
function bad(name, detail) { process.stdout.write('  FAIL  ' + name + (detail ? ' — ' + detail : '') + '\n'); fails += 1; }
function eq(name, got, want) {
  if (got === want) ok(name); else bad(name, 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
}

function extractFnFrom(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing function ' + name + ' in ' + (source === src ? SRC : STATUS_SRC));
  let i = source.indexOf('{', start), depth = 0;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') { depth--; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error('unclosed function ' + name);
}
function extractFn(name) { return extractFnFrom(src, name); }
// The two XHR upload sites, from the shipped status.js (section 10).
const UPLOADS = ['uploadOfflineUpdateFile', 'deployMplcProject'].map((n) => extractFnFrom(statusSrc, n)).join('\n\n');
// The guard region: from the `401 → login` banner to the Navigation banner.
// Both trees carry the banners; the region holds the old IIFE or the new
// installer + helpers, so the same cases run against either.
const REGION_START = src.indexOf('/* ── 401 → login');
const REGION_END = src.indexOf('/* ── Navigation');
if (REGION_START < 0 || REGION_END < 0 || REGION_END <= REGION_START) {
  process.stdout.write('test-app-csrf-recovery: FAIL — the `401 → login` … `Navigation` region is not found in ' + SRC + '\n');
  process.exit(1);
}
const REGION = src.slice(REGION_START, REGION_END);
const HELPERS = ['clearSessionCookie', 'getSa02mCsrfToken', 'withCsrfHeaders'].map(extractFn).join('\n\n');

const TOK = 'a'.repeat(64);
const NEWTOK = 'b'.repeat(64);
const E_CSRF = (reason) => ({ status: 200, body: Object.assign({ ok: false, error: 'csrf', error_code: 'E_CSRF' }, reason ? { reason } : {}) });
const OKBODY = { status: 200, body: { ok: true, applied: 1 } };
const TOKEN_OK = { status: 200, body: { ok: true, csrf: NEWTOK } };
const TOKEN_UNAUTH = { status: 200, body: { ok: false, error: 'unauthorized' } };

function makeRes(a) {
  if (a === undefined) return Promise.reject(new Error('unscripted fetch'));
  if (a === 'reject') return Promise.reject(new Error('network'));
  const res = {
    ok: a.status >= 200 && a.status < 300, status: a.status,
    clone() { return this; },
    json() { return a.body === null ? Promise.reject(new Error('non-json')) : Promise.resolve(a.body); }
  };
  return Promise.resolve(res);
}

// A world: fake browser globals + a scripted fetch, the guard region evaluated
// on top (installing the wrapper over window.fetch).
function makeWorld(script, opts) {
  opts = opts || {};
  const calls = [], replaced = [], toasts = [];
  const jar = new Map();
  for (const kv of (opts.cookie || '').split(';')) {
    const s = kv.trim(); if (!s) continue;
    const i = s.indexOf('='); jar.set(s.slice(0, i), s.slice(i + 1));
  }
  const document = {
    querySelector: () => null,
    get cookie() { return [...jar].map(([k, v]) => k + '=' + v).join('; '); },
    set cookie(v) {
      const parts = v.split(';').map((s) => s.trim());
      const [k] = parts[0].split('=');
      if (parts.some((p) => /^Max-Age=0$/i.test(p))) jar.delete(k);
      else jar.set(k, parts[0].slice(k.length + 1));
    }
  };
  const window = {
    location: { pathname: opts.pathname || '/', replace: (u) => replaced.push(u) },
    fetch: function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url);
      let method = (init && init.method) || (input && typeof input === 'object' && input.method) || 'GET';
      let hdr = null;
      const h = (init && init.headers) || (input && typeof input === 'object' && input.headers) || null;
      if (h) hdr = typeof h.get === 'function' ? h.get('X-SA02M-CSRF') : (h['X-SA02M-CSRF'] || null);
      calls.push({ url, method: String(method).toUpperCase(), csrf: hdr });
      return makeRes(script.shift());
    }
  };
  const rawFetch = window.fetch;
  const ctx = {
    window, document, console,
    uiT: (s) => s,
    toast: (m) => toasts.push(String(m)),
    setTimeout, clearTimeout, Promise, Object, String, decodeURIComponent, RegExp, Error
  };
  vm.createContext(ctx);
  vm.runInContext(HELPERS + '\n\n' + REGION, ctx, { filename: 'app.js-guard-extract' });
  if (window.fetch === rawFetch) throw new Error('the guard region installed no fetch wrapper — the region moved or is empty');
  const world = { ctx, window, document, calls, replaced, toasts, jar };
  if (opts.xhr) installUploadWorld(world, opts.xhr);
  return world;
}

/* Section 10's half of the world: a fake XMLHttpRequest (one scripted answer
   per send(), delivered from a timer like a real load event), a fake FormData,
   an always-present DOM, recording stubs for the status.js widget helpers the
   two upload functions call — then the shipped upload functions themselves. */
function installUploadWorld(world, xhrScript) {
  const ctx = world.ctx;
  const xhrs = world.xhrs = [];
  world.unscripted = 0;
  world.upd = { status: [], progress: [], enabled: [], inspect: [] };
  world.mplc = { status: [], progress: [], finish: [], polling: 0 };
  function FakeFormData() { this.parts = []; }
  FakeFormData.prototype.append = function (k, v, name) { this.parts.push({ k, v, name }); };
  function FakeXHR() {
    this.status = 0; this.responseText = ''; this.upload = {}; this.headers = {}; this.headerSets = 0;
    this.timeout = 0; this.withCredentials = false; this.sends = 0;
    xhrs.push(this);
  }
  FakeXHR.prototype.open = function (m, u) { this.method = String(m).toUpperCase(); this.url = u; };
  FakeXHR.prototype.setRequestHeader = function (k, v) { this.headerSets += 1; this.headers[k] = v; };
  FakeXHR.prototype.send = function (body) {
    const self = this;
    self.sends += 1;
    self.body = body;
    const a = xhrScript.shift();
    setTimeout(function () {
      if (a === undefined) { world.unscripted += 1; if (self.onerror) self.onerror(); return; }
      if (a === 'reject') { if (self.onerror) self.onerror(); return; }
      if (a === 'timeout') { if (self.ontimeout) self.ontimeout(); return; }
      if (self.upload.onprogress) self.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 });
      self.status = a.status;
      self.responseText = a.body === null ? 'not json' : JSON.stringify(a.body);
      if (self.onload) self.onload();
    }, 0);
  };
  world.document.getElementById = (id) => ({ id, disabled: false, hidden: false, textContent: '', checked: true });
  Object.assign(ctx, {
    XMLHttpRequest: FakeXHR, FormData: FakeFormData,
    // offline package upload
    _webUpdOfflineReady: true, _webUpdTxnActive: false,
    _webUpdSetStatus: (t, k) => world.upd.status.push({ t, k }),
    _webUpdSetProgress: (p, l) => world.upd.progress.push({ p, l }),
    setOfflineUpdateEnabled: (r) => world.upd.enabled.push(!!r),
    applyOfflineInspectUI: (i) => world.upd.inspect.push(i),
    // MPLC project deploy
    _mplcProjFile: { name: 'proj.zip' }, _mplcProjActive: false,
    _mplcProjFlasherBusy: () => false,
    _mplcProjSetStatus: (t, k) => world.mplc.status.push({ t, k }),
    _mplcProjSetProgress: (p, l) => world.mplc.progress.push({ p, l }),
    _mplcProjStagePct: () => 0,
    _mplcProjStartPolling: () => { world.mplc.polling += 1; },
    _mplcProjFinish: (r, j) => world.mplc.finish.push({ r, j })
  });
  vm.runInContext(UPLOADS, ctx, { filename: 'status.js-uploads-extract' });
  if (typeof ctx.uploadOfflineUpdateFile !== 'function' || typeof ctx.deployMplcProject !== 'function') {
    throw new Error('the status.js upload functions did not evaluate');
  }
}

const settle = () => new Promise((r) => setTimeout(r, 40));
// An XHR case: two timer-delivered answers with a promise chain between them.
const settleXhr = () => new Promise((r) => setTimeout(r, 120));

async function post(world, url, extraInit) {
  const init = Object.assign({ method: 'POST', credentials: 'same-origin', headers: world.ctx.withCsrfHeaders() }, extraInit || {});
  let body = null;
  try { const res = await world.window.fetch(url, init); body = await res.json(); } catch (e) { body = { thrown: String(e && e.message) }; }
  await settle();
  return body;
}

process.stdout.write('test-app-csrf-recovery (' + SRC + ')\n');
process.stdout.write('0. non-vacuity\n');
eq('0 the guard region is non-empty', REGION.length > 200, true);
{
  const w = makeWorld([], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  eq('0 the wrapper is installed over window.fetch', typeof w.window.fetch, 'function');
  eq('0 getSa02mCsrfToken reads the mirror cookie', w.ctx.getSa02mCsrfToken(), TOK);
}

process.stdout.write('1. transit strip: no_header with a token in hand → message, session kept, no retry\n');
{
  const w = makeWorld([E_CSRF('no_header')], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  const body = await post(w, 'cgi-bin/services_ctrl.cgi');
  eq('1 the original E_CSRF body is returned to the caller', body && body.error_code, 'E_CSRF');
  eq('1 exactly one fetch (no refresh, no retry)', w.calls.length, 1);
  eq('1 no logout', w.replaced.length, 0);
  eq('1 the session cookie survives', w.jar.has('session_token'), true);
  eq('1 the toast names the stripped header', w.toasts.some((t) => t.indexOf('X-SA02M-CSRF') >= 0), true);
  eq('1 window.SA02M_CSRF_BLOCKED records the transit strip', w.window.SA02M_CSRF_BLOCKED, 'no_header');
}

process.stdout.write('2. mismatch → refresh once → retry once with the NEW token → the retry\'s body\n');
{
  const w = makeWorld([E_CSRF('mismatch'), TOKEN_OK, OKBODY], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  const body = await post(w, 'cgi-bin/mqtt_set.cgi');
  eq('2 three fetches: POST, token GET, retried POST', w.calls.length, 3);
  eq('2 the refresh is GET cgi-bin/csrf_token.cgi', w.calls[1] && w.calls[1].method + ' ' + w.calls[1].url, 'GET cgi-bin/csrf_token.cgi');
  eq('2 the retry hits the same URL with POST', w.calls[2] && w.calls[2].method + ' ' + w.calls[2].url, 'POST cgi-bin/mqtt_set.cgi');
  eq('2 the retry carries the refreshed token', w.calls[2] && w.calls[2].csrf, NEWTOK);
  eq('2 the caller sees the retry\'s body', body && body.applied, 1);
  eq('2 no logout', w.replaced.length, 0);
  eq('2 window.SA02M_CSRF holds the refreshed token', w.window.SA02M_CSRF, NEWTOK);
}

process.stdout.write('3. refresh answers unauthorized → the session is really gone → logout once\n');
{
  const w = makeWorld([E_CSRF('mismatch'), TOKEN_UNAUTH], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  await post(w, 'cgi-bin/reboot.cgi');
  eq('3 login.html once', w.replaced.join(','), 'login.html');
  eq('3 the session cookie is cleared', w.jar.has('session_token'), false);
  eq('3 no retry after a failed refresh', w.calls.length, 2);
}

process.stdout.write('4. the retry is refused again\n');
{
  const w = makeWorld([E_CSRF('mismatch'), TOKEN_OK, E_CSRF('mismatch')], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  await post(w, 'cgi-bin/mqtt_set.cgi');
  eq('4 mismatch after a fresh token → logout', w.replaced.join(','), 'login.html');
  eq('4 no third attempt', w.calls.length, 3);
}
{
  // The pre-mortem case: a session predating the cookie AND a stripping proxy —
  // the refresh succeeds, the retry (now with a token) still says no_header.
  const w = makeWorld([E_CSRF('no_token_file'), TOKEN_OK, E_CSRF('no_header')], { cookie: 'session_token=' + TOK });
  await post(w, 'cgi-bin/mqtt_set.cgi');
  eq('4b no_header on the retry → the transit message, NOT a logout', w.replaced.length, 0);
  eq('4b the toast names the stripped header', w.toasts.some((t) => t.indexOf('X-SA02M-CSRF') >= 0), true);
  eq('4b no third attempt', w.calls.length, 3);
}

process.stdout.write('5. a GET is never inspected\n');
{
  const w = makeWorld([E_CSRF('mismatch')], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  try { const r = await w.window.fetch('cgi-bin/status.cgi?part=main', { credentials: 'same-origin' }); await r.json(); } catch (e) { /* ignore */ }
  await settle();
  eq('5 one fetch, no refresh', w.calls.length, 1);
  eq('5 no logout, no toast', w.replaced.length + w.toasts.length, 0);
}

process.stdout.write('6. bootstrap: the token is fetched only when the cookie is absent\n');
{
  const w = makeWorld([TOKEN_OK], { cookie: 'session_token=' + TOK });
  eq('6 sa02mBootstrapCsrfToken exists', typeof w.ctx.sa02mBootstrapCsrfToken, 'function');
  if (typeof w.ctx.sa02mBootstrapCsrfToken === 'function') {
    w.ctx.sa02mBootstrapCsrfToken(); await settle();
    eq('6 no mirror cookie → one token GET', w.calls.length === 1 && w.calls[0].url, 'cgi-bin/csrf_token.cgi');
    eq('6 the fetched token is now the one withCsrfHeaders sends', w.ctx.withCsrfHeaders()['X-SA02M-CSRF'], NEWTOK);
    const w2 = makeWorld([TOKEN_OK], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
    w2.ctx.sa02mBootstrapCsrfToken(); await settle();
    eq('6 mirror cookie present → no fetch at all', w2.calls.length, 0);
  } else {
    bad('6 no mirror cookie → one token GET (bootstrap missing)');
    bad('6 mirror cookie present → no fetch at all (bootstrap missing)');
  }
}

process.stdout.write('7. a Request object is not re-issued: refresh, then ask the user to repeat\n');
{
  const w = makeWorld([E_CSRF('mismatch'), TOKEN_OK], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  const req = { url: 'cgi-bin/mqtt_set.cgi', method: 'POST', headers: { get: (k) => (k === 'X-SA02M-CSRF' ? TOK : null) } };
  let body = null;
  try { const res = await w.window.fetch(req); body = await res.json(); } catch (e) { body = { thrown: String(e.message) }; }
  await settle();
  eq('7 the token was refreshed', w.calls.length === 2 && w.calls[1].url, 'cgi-bin/csrf_token.cgi');
  eq('7 no logout', w.replaced.length, 0);
  eq('7 the caller gets the E_CSRF body (its own error line)', body && body.error_code, 'E_CSRF');
  eq('7 the user is told to repeat the action', w.toasts.some((t) => /повторите/i.test(t)), true);
}

process.stdout.write('8. the 401 path keeps its semantics\n');
{
  const w = makeWorld([{ status: 401, body: {} }, { status: 401, body: {} }], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  await post(w, 'cgi-bin/mqtt_set.cgi');
  eq('8 a confirmed 401 → auth_check then login.html', w.calls.length === 2 && w.calls[1].url === 'cgi-bin/auth_check.cgi' && w.replaced.join(','), 'login.html');
}

process.stdout.write('9. a non-JSON 200 body on a POST is returned untouched\n');
{
  const w = makeWorld([{ status: 200, body: null }], { cookie: 'session_token=' + TOK + '; sa02m_csrf=' + TOK });
  let status = null;
  try { const res = await w.window.fetch('cgi-bin/x.cgi', { method: 'POST' }); status = res.status; } catch (e) { status = 'thrown'; }
  await settle();
  eq('9 the response resolves (status 200), nothing else happens', status === 200 && w.calls.length === 1 && w.replaced.length, 0);
}

process.stdout.write('10. XHR uploads (status.js) follow the same table (' + STATUS_SRC + ')\n');
const COOKIES = 'session_token=' + TOK + '; sa02m_csrf=' + TOK;
const UPLOAD_OK = { status: 200, body: { ok: true, inspect: { version: '1.0.6.63', signature_ok: true, compatible: true } } };
const DEPLOY_OK = { status: 200, body: { ok: true, pending: true, stage: 'validate' } };
const E_CSRF_WIDGET_LINE = 'Ошибка защиты сессии — повторите действие';
const lastStatus = (arr) => (arr.length ? arr[arr.length - 1].t : null);
{
  // Non-vacuity: a plain answer → exactly one XHR, header set once, the body handed on.
  const w = makeWorld([], { cookie: COOKIES, xhr: [UPLOAD_OK] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10.0 one XHR POST cgi-bin/web_update_upload.cgi', w.xhrs.length === 1 && w.xhrs[0].method + ' ' + w.xhrs[0].url, 'POST cgi-bin/web_update_upload.cgi');
  eq('10.0 X-SA02M-CSRF set exactly once with the token in hand', w.xhrs[0] && w.xhrs[0].headerSets === 1 && w.xhrs[0].headers['X-SA02M-CSRF'], TOK);
  eq('10.0 the caller sees the answer (inspect applied, «Пакет загружен»)', w.upd.inspect.length === 1 && w.toasts.indexOf('Пакет загружен') >= 0, true);
  eq('10.0 no refresh, no logout', w.calls.length + w.replaced.length, 0);
}
{
  const w = makeWorld([TOKEN_OK], { cookie: COOKIES, xhr: [E_CSRF('mismatch'), UPLOAD_OK] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10a mismatch → two XHRs to the same endpoint', w.xhrs.length === 2 && w.xhrs[1].method + ' ' + w.xhrs[1].url, 'POST cgi-bin/web_update_upload.cgi');
  eq('10a one refresh GET cgi-bin/csrf_token.cgi between them', w.calls.length === 1 && w.calls[0].method + ' ' + w.calls[0].url, 'GET cgi-bin/csrf_token.cgi');
  eq('10a the retry carries the refreshed token, set once', w.xhrs[1] && w.xhrs[1].headerSets === 1 && w.xhrs[1].headers['X-SA02M-CSRF'], NEWTOK);
  eq('10a the SAME FormData instance is re-sent (the body is not rebuilt)', w.xhrs[1] && w.xhrs[0].body === w.xhrs[1].body && w.xhrs[0].body.parts[0].name, 'pkg.sa02m');
  eq('10a each XHR object is sent once', w.xhrs.map((x) => x.sends).join(','), '1,1');
  eq('10a progress is wired on the retry too', w.xhrs[1] && typeof w.xhrs[1].upload.onprogress, 'function');
  eq('10a the caller sees the retry\'s answer once (inspect applied once)', w.upd.inspect.length === 1 && w.toasts.filter((t) => t === 'Пакет загружен').length, 1);
  eq('10a no logout, no transit toast', w.replaced.length + w.toasts.filter((t) => t.indexOf('X-SA02M-CSRF') >= 0).length, 0);
}
{
  const w = makeWorld([], { cookie: COOKIES, xhr: [E_CSRF('no_header')] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10b no_header with a token in hand → one XHR, no refresh, no retry', w.xhrs.length + ',' + w.calls.length, '1,0');
  eq('10b the toast names the stripped header', w.toasts.some((t) => t.indexOf('X-SA02M-CSRF') >= 0), true);
  eq('10b window.SA02M_CSRF_BLOCKED records the transit strip', w.window.SA02M_CSRF_BLOCKED, 'no_header');
  eq('10b no logout, the session cookie survives', w.replaced.length === 0 && w.jar.has('session_token'), true);
  eq('10b the widget shows its own E_CSRF line, not the raw code', lastStatus(w.upd.status), E_CSRF_WIDGET_LINE);
  eq('10b the widget line is not toasted a second time', w.toasts.indexOf(E_CSRF_WIDGET_LINE), -1);
  eq('10b nothing was applied', w.upd.inspect.length, 0);
}
{
  const w = makeWorld([TOKEN_OK], { cookie: COOKIES, xhr: [E_CSRF('mismatch'), E_CSRF('mismatch')] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10c the retry refused again (mismatch) → logout once', w.replaced.join(','), 'login.html');
  eq('10c the session cookie is cleared', w.jar.has('session_token'), false);
  eq('10c exactly two XHRs and one refresh — no third attempt', w.xhrs.length + ',' + w.calls.length, '2,1');
}
{
  // A session older than the mirror cookie AND a stripping proxy: the first XHR
  // goes out with no header (no token in hand) → no_token_file → refresh OK →
  // the retry (now with a token) still says no_header → transit, not logout.
  const w = makeWorld([TOKEN_OK], { cookie: 'session_token=' + TOK, xhr: [E_CSRF('no_token_file'), E_CSRF('no_header')] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10d the first XHR carried no header (no token in hand)', w.xhrs[0] && w.xhrs[0].headerSets, 0);
  eq('10d the retry carried the refreshed token', w.xhrs[1] && w.xhrs[1].headers['X-SA02M-CSRF'], NEWTOK);
  eq('10d no_header on the retry → the transit message, NOT a logout', w.replaced.length === 0 && w.toasts.some((t) => t.indexOf('X-SA02M-CSRF') >= 0), true);
  eq('10d no third attempt', w.xhrs.length, 2);
}
{
  const w = makeWorld([TOKEN_UNAUTH], { cookie: COOKIES, xhr: [E_CSRF('mismatch')] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10e refresh answers unauthorized → logout once, no retry', w.replaced.join(',') + ' ' + w.xhrs.length, 'login.html 1');
}
{
  const w = makeWorld([TOKEN_OK], { cookie: COOKIES, xhr: [E_CSRF('mismatch'), DEPLOY_OK] });
  w.ctx.deployMplcProject();
  await settleXhr();
  eq('10f MPLC deploy: mismatch → two XHRs to cgi-bin/mplc_project_deploy.cgi', w.xhrs.length === 2 && w.xhrs.every((x) => x.method + ' ' + x.url === 'POST cgi-bin/mplc_project_deploy.cgi'), true);
  eq('10f the retry carries the refreshed token, set once', w.xhrs[1] && w.xhrs[1].headerSets === 1 && w.xhrs[1].headers['X-SA02M-CSRF'], NEWTOK);
  eq('10f the SAME FormData instance is re-sent', w.xhrs[1] && w.xhrs[0].body === w.xhrs[1].body && w.xhrs[0].body.parts[0].name, 'proj.zip');
  eq('10f the accepted retry hands over to the stage poll, no finish(error)', w.mplc.polling + ',' + w.mplc.finish.length, '1,0');
  eq('10f no logout', w.replaced.length, 0);
}
{
  const w = makeWorld([], { cookie: COOKIES, xhr: [E_CSRF('no_header')] });
  w.ctx.deployMplcProject();
  await settleXhr();
  eq('10g MPLC deploy: no_header → one XHR, no refresh', w.xhrs.length + ',' + w.calls.length, '1,0');
  eq('10g the toast names the stripped header, no logout', w.replaced.length === 0 && w.toasts.some((t) => t.indexOf('X-SA02M-CSRF') >= 0), true);
  eq('10g the widget finishes with the E_CSRF body (its own error line)', w.mplc.finish.length === 1 && w.mplc.finish[0].r === 'error' && w.mplc.finish[0].j && w.mplc.finish[0].j.error_code, 'E_CSRF');
  eq('10g the poll never starts', w.mplc.polling, 0);
}
{
  const w = makeWorld([], { cookie: COOKIES, xhr: ['reject'] });
  w.ctx.uploadOfflineUpdateFile({ name: 'pkg.sa02m' });
  await settleXhr();
  eq('10h a network error is the caller\'s own path: «Ошибка загрузки файла», no refresh', lastStatus(w.upd.status) + ' ' + w.calls.length, 'Ошибка загрузки файла 0');
  eq('10h one XHR, no logout', w.xhrs.length + ',' + w.replaced.length, '1,0');
}

if (fails) {
  process.stdout.write('test-app-csrf-recovery: ' + fails + ' FAIL\n');
  process.exit(1);
}
process.stdout.write('test-app-csrf-recovery: ok\n');
