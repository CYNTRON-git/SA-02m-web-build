/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/** Версия веб-интерфейса — см. www/network_config/VERSION или scripts/sync-app-version.py */
const APP_VERSION = '1.0.6.52';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

/** Текущий вариант платы (sa02m-1eth / sa02m-2eth) для видимости Ethernet № 2. */
let _boardVariant = 'sa02m-1eth';

/* ── Auth guard ──────────────────────────────────────────────────────────── */
(function () {
  const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('session_token='));
  if (!hasCookie && !window.location.pathname.includes('login')) {
    window.location.replace('login.html');
  }
})();

/* ── Session cookie eviction ───────────────────────────────────────────────
   The session cookie's Path is '/' on the LAN and '/devcfg/<id>' through the
   cloud proxy, which re-scopes every Set-Cookie into the device's own route.
   The device cannot know its cloud <id>, so a fixed 'Path=/' delete misses the
   real cookie behind the cloud and the 401 auto-logout loops (the cloud already
   evicted the broad cookie, the scoped one survives, login.html bounces back).
   Clear at every prefix of the current path — one of them is the real one, and
   clearing a path that holds no cookie is a harmless no-op. */
function clearSessionCookie() {
  const segs = window.location.pathname.split('/');
  let path = '';
  document.cookie = 'session_token=; Path=/; Max-Age=0; SameSite=Lax';
  for (let i = 1; i < segs.length; i++) {
    if (!segs[i]) continue;
    path += '/' + segs[i];
    document.cookie = 'session_token=; Path=' + path + '; Max-Age=0; SameSite=Lax';
  }
}

/* CSRF for mutating privileged CGI (X-SA02M-CSRF). Token is minted at login
   into a non-HttpOnly cookie / optional meta / window.SA02M_CSRF — missing token
   does not block legacy POSTs until the server enforces it. */
function getSa02mCsrfToken() {
  if (typeof window.SA02M_CSRF === 'string' && window.SA02M_CSRF) {
    return window.SA02M_CSRF;
  }
  try {
    const meta = document.querySelector('meta[name="sa02m-csrf"]');
    if (meta) {
      const mv = meta.getAttribute('content');
      if (mv) return mv;
    }
  } catch (e) { /* ignore */ }
  try {
    const parts = document.cookie.split(';');
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i].trim();
      if (p.indexOf('sa02m_csrf=') === 0) {
        return decodeURIComponent(p.slice('sa02m_csrf='.length));
      }
    }
  } catch (e) { /* ignore */ }
  return '';
}

function withCsrfHeaders(headers) {
  const out = Object.assign({}, headers || {});
  const tok = getSa02mCsrfToken();
  if (tok) out['X-SA02M-CSRF'] = tok;
  return out;
}

/* ── 401 → login ──────────────────────────────────────────────────────────
   A request that comes back 401 usually means the server-side session is gone
   (expired/revoked) — send the user to the login page instead of surfacing
   "HTTP 401: unauthorized" toasts from individual widgets. Wrap fetch once,
   centrally, so every caller (status polling, flasher, services…) is covered.

   But the board also emits the occasional *transient* 401 while the session is
   perfectly alive (a brief race in the session store). We must NOT log the user
   out over one of those. So on a 401 we re-check with one lightweight authed
   call: only a confirmed 401 clears the cookie and redirects; a transient one
   is swallowed by silently retrying the original GET (POSTs are left to their
   caller, to avoid re-applying a non-idempotent action).

   E_CSRF (1.0.6.53). The CGI layer answers a CSRF refusal as HTTP 200 with
   error_code "E_CSRF" and, since 1.0.6.53, a `reason`. Until then every E_CSRF
   meant «session lost → re-login» — which through cloud.cyntron.ru logged the
   user out on every mutating POST: the proxy dropped the X-SA02M-CSRF request
   header, a transit failure the panel misread as a session one. Now a header
   we SENT that the board reports absent (reason no_header, token in hand) is a
   transit strip → say so, keep the session, no retry; anything else → refresh
   the token once (GET cgi-bin/csrf_token.cgi, session-bound) and re-issue the
   request exactly once; a failed refresh (the session is really gone) or a
   second non-transit refusal → the login page, as before. Safe by
   construction: every ledgered CGI answers E_CSRF BEFORE its mutation
   (cgi-csrf-policy), so the refused request never ran. Harness:
   scripts/dev/test-app-csrf-recovery.mjs; policy:
   docs/decisions/selective-csrf-policy.md «Реакция панели». */

/* The X-SA02M-CSRF value a fetch() call carried — from its init headers or a
   Request object; '' when none. */
function sa02mSentCsrfToken(args) {
  try {
    const init = args[1];
    if (init && init.headers) {
      const h = init.headers;
      if (typeof h.get === 'function') return h.get('X-SA02M-CSRF') || '';
      return h['X-SA02M-CSRF'] || h['x-sa02m-csrf'] || '';
    }
    const a0 = args[0];
    if (a0 && typeof a0 === 'object' && a0.headers && typeof a0.headers.get === 'function') {
      return a0.headers.get('X-SA02M-CSRF') || '';
    }
  } catch (e) { /* no token */ }
  return '';
}

/* GET the session's own token. Resolves the hex token — also stored in
   window.SA02M_CSRF, where getSa02mCsrfToken reads first — or '' when the
   session is gone or the answer is not a token. `rawFetch` is the unwrapped
   fetch, so the refresh itself is never re-inspected by the guard. */
function sa02mRefreshCsrfToken(rawFetch) {
  const f = rawFetch || window.fetch;
  return f('cgi-bin/csrf_token.cgi', { credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { return r && r.ok ? r.json() : null; })
    .then(function (j) {
      if (j && j.ok === true && typeof j.csrf === 'string' && /^[a-f0-9]{64}$/.test(j.csrf)) {
        window.SA02M_CSRF = j.csrf;
        return j.csrf;
      }
      return '';
    })
    .catch(function () { return ''; });
}

/* Page load: a session older than the sa02m_csrf mirror cookie (the upgrade
   window) has no token in the browser — fetch it once, so the first mutating
   POST does not have to. Zero cost when the cookie is there. */
function sa02mBootstrapCsrfToken() {
  if (getSa02mCsrfToken()) return null;
  return sa02mRefreshCsrfToken();
}

function sa02mNoteCsrfTransitStrip() {
  window.SA02M_CSRF_BLOCKED = 'no_header';
  if (typeof toast === 'function') {
    toast(uiT('Прокси не пропускает заголовок X-SA02M-CSRF — действие через этот путь невозможно. Откройте панель по локальному адресу или обновите облачный сервис.'), 'error', 8000);
  }
}

/* The E_CSRF decision. `res` is the refused response, `body` its parsed JSON,
   `args` the original fetch arguments, `ctx` = { fetch: the raw fetch,
   logout: clear the cookie + login.html }. Resolves the response the caller
   should see — the original, or the retry's. */
function sa02mHandleCsrfRejection(res, body, args, ctx) {
  const reason = body && typeof body.reason === 'string' ? body.reason : '';
  const sent = sa02mSentCsrfToken(args);
  if (reason === 'no_header' && sent) {              // the proxy stripped it: transit, not session
    sa02mNoteCsrfTransitStrip();
    return Promise.resolve(res);
  }
  return sa02mRefreshCsrfToken(ctx.fetch).then(function (tok) {
    if (!tok) { ctx.logout(); return res; }            // the session is really gone
    const a0 = args[0];
    const init = args[1];
    const retryable = (typeof a0 === 'string' || (typeof URL !== 'undefined' && a0 instanceof URL)) &&
      !(init && init.body && typeof ReadableStream !== 'undefined' && init.body instanceof ReadableStream);
    if (!retryable) {                                   // a Request object cannot be re-read safely
      if (typeof toast === 'function') toast(uiT('Токен защиты сессии обновлён — повторите действие'), 'warn', 6000);
      return res;
    }
    const init2 = Object.assign({}, init || {});
    init2.headers = withCsrfHeaders(init2.headers);     // picks up the refreshed window.SA02M_CSRF
    // Exactly one re-issue, through the RAW fetch: the retry's own E_CSRF is
    // judged here and never handed back to the wrapper (no second refresh).
    return ctx.fetch(a0, init2).then(function (res2) {
      if (!res2 || !res2.ok) return res2;
      return res2.clone().json().then(function (j2) {
        if (j2 && j2.error_code === 'E_CSRF') {
          if (j2.reason === 'no_header') sa02mNoteCsrfTransitStrip();   // token in hand, still stripped
          else ctx.logout();
        }
        return res2;
      }, function () { return res2; });
    });
  });
}

function sa02mInstallFetchGuard() {
  const _fetch = window.fetch;
  let redirecting = false;
  const ctx = {
    fetch: function () { return _fetch.apply(window, arguments); },
    logout: function () {
      if (redirecting) return;
      redirecting = true;
      // Cookie can linger (10-day Max-Age) after the server session dies;
      // clear it so login.html doesn't bounce us back to the dashboard.
      // Clear at every path prefix — the cloud scopes it to /devcfg/<id>.
      clearSessionCookie();
      window.location.replace('login.html');
    }
  };
  window.fetch = function () {
    const self = this, args = arguments;
    let method = 'GET';
    try {
      const a0 = args[0];
      if (a0 && typeof a0 === 'object' && a0.method) method = a0.method;     // Request
      if (args[1] && args[1].method) method = args[1].method;                // init
    } catch (e) { /* keep GET */ }
    method = String(method).toUpperCase();

    return _fetch.apply(self, args).then(function (res) {
      // E_CSRF is returned as HTTP 200 with error_code:"E_CSRF" in the body
      // (project idiom — the CGI layer never uses a 401 status), so the
      // status===401 path below cannot catch it. Peek a clone so the caller's
      // body stays readable; only mutating methods can get E_CSRF, so GET/HEAD
      // polling skips the extra parse. The wrapper resolves to the FINAL
      // response — the retry's, when sa02mHandleCsrfRejection ran one.
      if (res && res.ok && !redirecting && method !== 'GET' && method !== 'HEAD') {
        return res.clone().json().then(function (j) {
          if (j && j.error_code === 'E_CSRF' && !redirecting) {
            return sa02mHandleCsrfRejection(res, j, args, ctx);
          }
          return res;
        }, function () { return res; });   // non-JSON body (e.g. 504 HTML) — as is
      }
      if (!res || res.status !== 401 || redirecting) return res;
      // Re-check via the canonical auth endpoint — NOT status.cgi (it serves a
      // cached 200 that outlives the session) — and never from cache.
      return _fetch('cgi-bin/auth_check.cgi', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (chk) {
          if (chk && chk.status === 401) {
            ctx.logout();
            return res;
          }
          return method === 'GET' ? _fetch.apply(self, args) : res;
        })
        .catch(function () { return res; });
    });
  };
}

(function () {
  if (window.location.pathname.includes('login')) return;
  sa02mInstallFetchGuard();
})();

/* ── Navigation ──────────────────────────────────────────────────────────── */
function switchTab(tab) {
  const navEl = document.querySelector('.nav-item[data-tab="' + tab + '"]');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  if (navEl) navEl.classList.add('active');
  const pane = document.getElementById('tab-' + tab);
  if (pane) pane.classList.add('active');
  if (tab === 'system') {
    loadLog();
    fetchSystemWidget();
    loadWebUpdateStatus();
    probeOfflineUpdateCapability();
    loadServicesControl(false);
    loadMplcProjectMeta();
    loadKernelControl(false);
    loadVariant();
    if (window.cloudTabInit) window.cloudTabInit();
  }
  if (tab !== 'system' && window.cloudTabDestroy) window.cloudTabDestroy();
  if (tab === 'network') {
    applyVariantVisibility(_boardVariant);
    loadConfig();
  }
  if (tab === 'time') {
    loadConfig();
    refreshTimeReadouts();
  }
  if (tab === 'flasher' && window.flasherInit) window.flasherInit();
  if (tab === 'devices' && window.devicesTabInit) window.devicesTabInit();
  if (tab !== 'devices' && window.devicesTabDestroy) window.devicesTabDestroy();
  if (tab === 'mqtt' && window.mqttTabInit) window.mqttTabInit();
  if (tab !== 'mqtt' && window.mqttTabDestroy) window.mqttTabDestroy();
  if (tab === 'gateway' && window.gatewayInit) window.gatewayInit();
  if (tab !== 'gateway' && window.gatewayDestroy) window.gatewayDestroy();
}

/* Deep-link a tab from the URL on load: `#system`/`#network` hash OR `?tab=system`
   query. Used by the cloud fleet "settings" button (/devcfg/<id>/#system). Tiny
   and defensive: validates the name and only switches to an existing tab, else
   leaves the default (dashboard). A fragment is never sent to the server, so it
   survives the cloud reverse proxy. */
function applyDeepLinkTab() {
  var tab = (location.hash || '').replace(/^#/, '');
  if (!tab) {
    var m = (location.search || '').match(/[?&]tab=([^&]+)/);
    if (m) { try { tab = decodeURIComponent(m[1]); } catch (e) { tab = m[1]; } }
  }
  if (tab === 'cloud') {
    switchTab('system');
    if (window.cloudScrollIntoView) {
      setTimeout(window.cloudScrollIntoView, 120);
    }
    return;
  }
  if (tab && /^[a-z0-9_-]+$/i.test(tab) &&
      document.querySelector('.nav-item[data-tab="' + tab + '"]')) {
    switchTab(tab);
  }
}

function initNav() {
  document.querySelectorAll('.nav-item[data-tab]').forEach(el => {
    el.addEventListener('click', () => {
      if (el.dataset.tab === 'gateway' && window.gatewayNavClick && window.gatewayNavClick()) {
        return;
      }
      switchTab(el.dataset.tab);
    });
  });
  const logo = document.querySelector('.topbar-logo');
  if (logo) {
    logo.addEventListener('click', () => {
      switchTab('dashboard');
      // Scroll back to the very top (Operator 2026-07-19): the main scroll
      // container on mobile, and the window as a fallback.
      const main = document.querySelector('.main');
      if (main) { if (main.scrollTo) main.scrollTo(0, 0); else main.scrollTop = 0; }
      if (window.scrollTo) window.scrollTo(0, 0);
    });
  }
}

/* ── Toast notifications ──────────────────────────────────────────────────── */
/** Integration-card notices (Alice / Cloud): viewport toast, 5.0 s. */
const CARD_NOTICE_MS = 5000;

function toast(msg, type = 'info', ms = 4000) {
  const text = uiT(msg);
  let area = document.getElementById('toast-area');
  if (!area) {
    area = document.createElement('div');
    area.id = 'toast-area';
    area.className = 'toast-area';
    document.body.appendChild(area);
  }
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = text;
  area.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .4s'; setTimeout(() => t.remove(), 400); }, ms);
}

/** Map the in-card ok/err flag onto the existing toast types. Empty text is a no-op. */
function cardNotice(msg, ok) {
  if (!msg) return;
  const type = ok === false ? 'error' : (ok === true ? 'success' : 'info');
  toast(msg, type, CARD_NOTICE_MS);
}
window.toast = toast;
window.cardNotice = cardNotice;

/* ── Utilities ────────────────────────────────────────────────────────────── */
function fmtKB(kb) {
  kb = parseInt(kb) || 0;
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + ' ' + uiT('ГБ');
  if (kb >= 1024)    return (kb / 1024).toFixed(0) + ' ' + uiT('МБ');
  return kb + ' ' + uiT('КБ');
}
function fmtBytes(b) {
  b = parseInt(b) || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' ' + uiT('ГБ');
  if (b >= 1048576)    return (b / 1048576).toFixed(1) + ' ' + uiT('МБ');
  if (b >= 1024)       return (b / 1024).toFixed(1) + ' ' + uiT('КБ');
  return b + ' ' + uiT('Б');
}
/** Ethernet TX/RX — 2 знака в МБ, чтобы малый прирост был виден между опросами. */
function fmtTrafficBytes(b) {
  b = parseInt(b) || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' ' + uiT('ГБ');
  if (b >= 1048576)    return (b / 1048576).toFixed(2) + ' ' + uiT('МБ');
  if (b >= 1024)       return (b / 1024).toFixed(1) + ' ' + uiT('КБ');
  return b + ' ' + uiT('Б');
}
function fmtNum(n) {
  n = parseInt(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + ' М';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' К';
  return n.toString();
}
function fmtUptime(s) {
  s = parseInt(s) || 0;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + ' ' + uiT('д') + ' ' + h + ' ' + uiT('ч') + ' ' + m + ' ' + uiT('м');
  if (h) return h + ' ' + uiT('ч') + ' ' + m + ' ' + uiT('м');
  return m + ' ' + uiT('м') + ' ' + (s % 60) + ' ' + uiT('с');
}

/** Uptime with only the TWO most significant units (mobile KPI tile, Operator
    2026-07-19): days+hours, else hours+minutes, else minutes+seconds. */
function fmtUptime2(s) {
  s = parseInt(s) || 0;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + ' ' + uiT('д') + ' ' + h + ' ' + uiT('ч');
  if (h) return h + ' ' + uiT('ч') + ' ' + m + ' ' + uiT('м');
  return m + ' ' + uiT('м') + ' ' + (s % 60) + ' ' + uiT('с');
}

/** Компактный аптайм для колонки «Службы» (короче, без секунд при наличии минут). */
function fmtUptimeSvc(s) {
  s = parseInt(s, 10) || 0;
  if (s <= 0) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + ' ' + uiT('д') + ' ' + h + ' ' + uiT('ч');
  if (h) return h + ' ' + uiT('ч') + ' ' + m + ' ' + uiT('м');
  if (m) return m + ' ' + uiT('м');
  return s + ' ' + uiT('с');
}

/** Колонка аптайма в строке «Службы»: при активной службе и 0 с показываем «<1м». */
function setSvcRowUptime(elOrId, sec, active) {
  const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
  if (!el) return;
  const s = parseInt(sec, 10);
  if (!Number.isFinite(s) || s < 0) {
    el.textContent = '—';
    el.removeAttribute('title');
    return;
  }
  if (s === 0) {
    if (active) {
      el.textContent = uiT('<1м');
      el.removeAttribute('title');
    } else {
      el.textContent = '—';
      el.removeAttribute('title');
    }
    return;
  }
  el.textContent = fmtUptimeSvc(s);
  el.title = fmtUptime(s);
}
function setText(id, val)  { const e = document.getElementById(id); if (e) e.textContent = val; }
function setHtml(id, val)  { const e = document.getElementById(id); if (e) e.innerHTML = val; }
// «Время с RTC» readout: show the device-local value (or '—'); carry the raw
// UTC the chip physically stores in the title tooltip. Setting .title via the
// DOM property is attribute-safe (not HTML-parsed). #time-rtc-disp is excluded
// from the i18n observer, so the literal "UTC:" prefix is left untranslated.
function setRtcReadout(local, utcRaw) {
  const e = document.getElementById('time-rtc-disp');
  if (!e) return;
  e.textContent = local || '—';
  if (utcRaw) e.title = 'UTC: ' + utcRaw;
  else e.removeAttribute('title');
}
function setStyle(id, prop, val) { const e = document.getElementById(id); if (e) e.style[prop] = val; }
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
// Attribute context (`title="…"`, `data-topic="…"`, `value="…"`): escHtml leaves
// `"` alive, so a quoted value closes the attribute and the rest of the string
// becomes new attributes (1.0.6.38 picker, audit C4 — an injected handler ran).
// Gate: .ai-dev/quality/checks/no-eschtml-in-attr.mjs. Null-tolerant on purpose:
// renderers pass absent fields.
function escAttr(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
const PRIORITY_WARMUP_KEY = 'sa02m-priority-warmup';
const PRIORITY_WARMUP_TTL_MS = 15000;

function readPriorityWarmupCache() {
  try {
    return JSON.parse(sessionStorage.getItem(PRIORITY_WARMUP_KEY) || '{}');
  } catch (_) {
    return {};
  }
}
function writePriorityWarmupPart(part, data) {
  try {
    const cache = readPriorityWarmupCache();
    cache[part] = { ts: Date.now(), data };
    sessionStorage.setItem(PRIORITY_WARMUP_KEY, JSON.stringify(cache));
  } catch (_) {}
}
function getPriorityWarmupPart(part) {
  const cache = readPriorityWarmupCache();
  const hit = cache[part];
  if (!hit || !hit.data || !hit.ts) return null;
  if (Date.now() - hit.ts > PRIORITY_WARMUP_TTL_MS) return null;
  return hit.data;
}
function hydratePriorityWarmup() {
  const applyMap = {
    cpu: applyPriorityStatus,
    temp: applyPriorityStatus,
    ram: applyPriorityStatus,
    disk: applyPriorityStatus
  };
  Object.entries(applyMap).forEach(([part, applyFn]) => {
    const data = getPriorityWarmupPart(part);
    if (data) applyFn(data);
  });
}

/** Шкала температуры: 30 °C = 0&nbsp;%, 100 °C = 100&nbsp;% */
function tempToGaugePct(celsius) {
  const t = parseFloat(celsius);
  if (Number.isNaN(t)) return 0;
  return Math.min(100, Math.max(0, ((t - 30) / (100 - 30)) * 100));
}

/* ── Service badge ────────────────────────────────────────────────────────── */
function normSvcState(v) {
  return String(v == null ? '' : v).trim().toLowerCase();
}
function svcStateIsActive(v) {
  const s = normSvcState(v);
  return s === 'active' || s === 'running' || s === 'activating';
}

function svcBadge(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  const s = normSvcState(state);
  if (!s || s === 'unknown') {
    el.textContent = '…';
    el.className = 'badge badge-unk';
    return;
  }
  if (s === 'disabled') {
    el.textContent = window.sa02mI18n ? window.sa02mI18n.t('Отключен') : 'Отключен';
    el.className = 'badge badge-err';
    return;
  }
  const ok = svcStateIsActive(state);
  const ru = ok ? 'Активен' : 'Неактивен';
  el.textContent = window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
  el.className = 'badge ' + (ok ? 'badge-ok' : 'badge-err');
}

/** Короткое имя unit для подписи (mplc4 вместо mplc4.service). */
function unitUiLabel(name) {
  return String(name || '').replace(/\.(service|socket)$/i, '');
}

/** Сравнение подписей служб для сортировки A→Z (без учёта регистра). */
function compareSvcDisplayName(a, b) {
  return String(a || '').trim().localeCompare(String(b || '').trim(), undefined, { sensitivity: 'base' });
}

const SVC_WIDGET_MAX_ROWS = 6;

/** Строки-заглушки до part=services — та же высота, что у badge-строк. */
function renderServicesSkeleton() {
  if (backgroundLoaded.services) return;
  const host = document.getElementById('svc-dynamic-list');
  if (!host) return;
  if (host.querySelector('.svc-row-skeleton')) return;
  host.innerHTML = '';
  for (let i = 0; i < SVC_WIDGET_MAX_ROWS; i += 1) {
    const r = document.createElement('div');
    r.className = 'svc-row svc-row-skeleton';
    r.setAttribute('aria-hidden', 'true');
    r.innerHTML =
      '<span class="name mono">CODESYS</span>' +
      '<span class="svc-uptime mono">&nbsp;</span>' +
      '<span class="badge badge-unk">&nbsp;</span>';
    host.appendChild(r);
  }
}

/** Плейсхолдеры дашборда до первого ответа status.cgi — совпадают с типичным loaded DOM. */
function initDashboardPlaceholders() {
  renderServicesSkeleton();
  renderRs485Skeleton();
  setText('proc-info', uiT('Проц.: 0 / 0'));
  setText('cpu-freq', '0 ' + uiT('МГц'));
  ['eth0-rx', 'eth0-tx', 'eth1-rx', 'eth1-tx'].forEach(function (id) {
    setText(id, fmtBytes(0));
  });
  ['cpu-model', 'armbian-info', 'kernel-info'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el && !String(el.textContent || '').trim()) el.textContent = '\u00a0';
  });
  ['ram-free', 'disk-free', 'usb-free', 'sd-free'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el && !String(el.textContent || '').trim()) el.textContent = '\u00a0';
  });
}

/** true, если backend пометил службу как установленную на устройстве. */
function svcIsInstalledFlag(v) {
  return v === true || v === 1;
}

/** Виджет «Службы»: собирает строки из status.cgi, сортировка по имени A→Z (≤ 6). Без пустых строк. */
function renderServicesDynamic(d) {
  const host = document.getElementById('svc-dynamic-list');
  if (!host) return;
  host.innerHTML = '';

  const rows = [];
  const seen = new Set();

  function pushRow(label, uptimeS, state, opts) {
    if (rows.length >= SVC_WIDGET_MAX_ROWS) return false;
    const lab = String(label || '').trim();
    if (!lab) return false;
    const key = lab.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    rows.push({
      label: lab,
      uptimeS: uptimeS,
      state: state,
      mono: !!(opts && opts.mono),
      title: opts && opts.title ? String(opts.title) : '',
      tight: !!(opts && opts.tight)
    });
    return true;
  }

  if (svcIsInstalledFlag(d.svc_codesys_installed)) {
    pushRow('CODESYS', d.svc_codesys_uptime_s, d.svc_codesys, {
      mono: true,
      title: 'CODESYS Control runtime'
    });
  }
  const optionalServices = Array.isArray(d.optional_services) ? d.optional_services : [];
  const klogicService = optionalServices.find(function (s) {
    if (!s || s.installed === false || s.installed === 0) return false;
    const id = s.id ? unitUiLabel(String(s.id)).toLowerCase() : '';
    const label = s.label ? String(s.label).trim().toLowerCase() : '';
    return id === 'klogic' || id === 'klogicd' || label === 'klogic';
  });
  if (klogicService) {
    pushRow('KLogic', klogicService.uptime_s, klogicService.status, {
      mono: true,
      title: 'KLogic runtime'
    });
  }
  if (svcIsInstalledFlag(d.svc_mosquitto_installed)) {
    pushRow('mosquitto', d.svc_mosquitto_uptime_s, d.svc_mosquitto);
  }
  if (svcIsInstalledFlag(d.svc_bridge_installed)) {
    pushRow('MQTT мост', d.svc_bridge_uptime_s, d.svc_bridge, {
      title: 'Modbus→MQTT мост (sa02m-modbus-mqtt)',
      mono: true
    });
  }

  if (svcIsInstalledFlag(d.mplc_installed)) {
    const mu = (d.mplc_unit != null && String(d.mplc_unit).trim())
      ? unitUiLabel(String(d.mplc_unit).trim())
      : '';
    const mplcRowLabel =
      !mu || mu === 'mplc4' || mu === 'mplc' ? 'MPLC4' : mu;
    pushRow(mplcRowLabel, d.mplc_uptime_s, d.mplc_status, {
      mono: true,
      title: 'MPLC4 — опрос линии RS-485 (systemd)',
      tight: false
    });
  }

  if (rows.length < SVC_WIDGET_MAX_ROWS) {
    for (const s of optionalServices) {
      if (rows.length >= SVC_WIDGET_MAX_ROWS) break;
      if (s && s.installed === false) continue;
      if (s && s.installed === 0) continue;
      const id = (s && s.id) ? unitUiLabel(String(s.id)) : '';
      if (!id) continue;
      const disp = (s && s.label && String(s.label).trim()) ? String(s.label).trim() : id;
      pushRow(disp, s.uptime_s, s.status, { mono: true });
    }
  }

  rows.sort(function (a, b) {
    return compareSvcDisplayName(a.label, b.label);
  });

  rows.forEach(function (row, i) {
    const r = document.createElement('div');
    r.className = 'svc-row' + (row.tight ? ' svc-row-tight' : '');
    const name = document.createElement('span');
    name.className = 'name' + (row.mono ? ' mono' : '');
    name.textContent = row.label;
    if (row.title) name.title = uiT(row.title);
    const up = document.createElement('span');
    up.className = 'svc-uptime mono';
    const on = svcStateIsActive(row.state);
    setSvcRowUptime(up, row.uptimeS, on);
    const badge = document.createElement('span');
    badge.className = 'badge badge-unk';
    const bid = 'svc-dyn-' + i;
    badge.id = bid;
    r.appendChild(name);
    r.appendChild(up);
    r.appendChild(badge);
    host.appendChild(r);
    svcBadge(bid, row.state);
  });
}

