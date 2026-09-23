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
   cases 1, 2, 4b, 6, 7 FAIL — immediate logout on every E_CSRF, no refresh
   GET, no retry, no bootstrap; 3, 4, 5, 8, 9 hold on both trees (a failed
   refresh and a second mismatch still log out; GET bodies were never
   inspected; the 401 path is untouched; a non-JSON body is returned as is).

   Run: node scripts/dev/test-app-csrf-recovery.mjs   (APP_JS=<path> for another copy) */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = process.env.APP_JS || path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

let fails = 0;
function ok(name) { process.stdout.write('  ok    ' + name + '\n'); }
function bad(name, detail) { process.stdout.write('  FAIL  ' + name + (detail ? ' — ' + detail : '') + '\n'); fails += 1; }
function eq(name, got, want) {
  if (got === want) ok(name); else bad(name, 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
}

function extractFn(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing function ' + name);
  let i = src.indexOf('{', start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('unclosed function ' + name);
}
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
  vm.runInNewContext(HELPERS + '\n\n' + REGION, ctx, { filename: 'app.js-guard-extract' });
  if (window.fetch === rawFetch) throw new Error('the guard region installed no fetch wrapper — the region moved or is empty');
  return { ctx, window, document, calls, replaced, toasts, jar };
}

const settle = () => new Promise((r) => setTimeout(r, 40));

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

if (fails) {
  process.stdout.write('test-app-csrf-recovery: ' + fails + ' FAIL\n');
  process.exit(1);
}
process.stdout.write('test-app-csrf-recovery: ok\n');
