/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/** Версия веб-интерфейса — см. www/network_config/VERSION или scripts/sync-app-version.py */
const APP_VERSION = '1.0.6.14';

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
   caller, to avoid re-applying a non-idempotent action). */
(function () {
  if (window.location.pathname.includes('login')) return;
  const _fetch = window.fetch;
  let redirecting = false;
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
      // status===401 path below cannot catch it. A stale/absent CSRF token means
      // the session predates the sa02m_csrf cookie (upgraded device) or was
      // revoked — surface the SAME "session expired → re-login" path. Peek a
      // clone so the caller's body stays readable; only mutating methods can get
      // E_CSRF, so GET/HEAD polling skips the extra parse.
      if (res && res.ok && !redirecting && method !== 'GET' && method !== 'HEAD') {
        res.clone().json().then(function (j) {
          if (j && j.error_code === 'E_CSRF' && !redirecting) {
            redirecting = true;
            clearSessionCookie();
            window.location.replace('login.html');
          }
        }).catch(function () { /* non-JSON body (e.g. 504 HTML) — ignore */ });
      }
      if (!res || res.status !== 401 || redirecting) return res;
      // Re-check via the canonical auth endpoint — NOT status.cgi (it serves a
      // cached 200 that outlives the session) — and never from cache.
      return _fetch('cgi-bin/auth_check.cgi', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (chk) {
          if (chk && chk.status === 401) {
            redirecting = true;
            // Cookie can linger (10-day Max-Age) after the server session dies;
            // clear it so login.html doesn't bounce us back to the dashboard.
            // Clear at every path prefix — the cloud scopes it to /devcfg/<id>.
            clearSessionCookie();
            window.location.replace('login.html');
            return res;
          }
          return method === 'GET' ? _fetch.apply(self, args) : res;
        })
        .catch(function () { return res; });
    });
  };
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

