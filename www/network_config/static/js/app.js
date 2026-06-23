/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/** Версия веб-интерфейса — см. www/network_config/VERSION или scripts/sync-app-version.py */
const APP_VERSION = '1.0.3.34';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

/** Текущий вариант платы (sa02m-1eth / sa02m-2eth) для видимости Ethernet № 2. */
let _boardVariant = 'sa02m-1eth';

/* ── Auth guard ──────────────────────────────────────────────────────────── */
(function () {
  const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('session_token='));
  if (!hasCookie && !window.location.pathname.includes('login')) {
    window.location.replace('/login.html');
  }
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
    loadServicesControl(false);
    loadVariant();
  }
  if (tab === 'network') {
    applyVariantVisibility(_boardVariant);
    loadConfig();
  }
  if (tab === 'time') {
    loadConfig();
    refreshTimeReadouts();
  }
  if (tab === 'flasher' && window.flasherInit) window.flasherInit();
  if (tab === 'mqtt' && window.mqttTabInit) window.mqttTabInit();
  if (tab !== 'mqtt' && window.mqttTabDestroy) window.mqttTabDestroy();
  if (tab === 'gateway' && window.gatewayInit) window.gatewayInit();
  if (tab !== 'gateway' && window.gatewayDestroy) window.gatewayDestroy();
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
  const logo = document.querySelector('.topbar-logo-img');
  if (logo) {
    logo.addEventListener('click', () => switchTab('dashboard'));
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

/* ── Gauge helper (SVG stroke-dasharray arc) ───────────────────────────────
   Длина дуги M10 58 A45 45 0 0 1 100 58 ≈ π·45 ≈ 141.37, не 126 — иначе паттерн
   dash+gap короче пути и повторяется, справа появляется ложный «хвост». */
let _gaugeArcPathLen = null;
function gaugeArcPathLength() {
  if (_gaugeArcPathLen != null) return _gaugeArcPathLen;
  const el = document.getElementById('cpu-arc');
  if (el && typeof el.getTotalLength === 'function') {
    const L = el.getTotalLength();
    if (L > 1) {
      _gaugeArcPathLen = L;
      return L;
    }
  }
  _gaugeArcPathLen = Math.PI * 45;
  return _gaugeArcPathLen;
}
/** Сброс после смены разметки SVG дуг */
function invalidateGaugeArcCache() {
  _gaugeArcPathLen = null;
}

function arcDash(pct, pathLen) {
  const L = pathLen > 0 ? pathLen : gaugeArcPathLength();
  const fill = Math.min(1, Math.max(0, pct / 100)) * L;
  return fill + ' ' + (L - fill);
}

/** Дуга температуры: 30 °C = 0&nbsp;%, 100 °C = 100&nbsp;% */
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
  setText('proc-info', uiT('Процессов: 0 / 0'));
  setText('cpu-freq', '0 ' + uiT('МГц'));
  setText('disk-io', uiT('R ' + fmtBytes(0) + ' / W ' + fmtBytes(0)));
  ['end0-rx', 'end0-tx', 'end1-rx', 'end1-tx'].forEach(function (id) {
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
  if (svcIsInstalledFlag(d.svc_fcgiwrap_installed)) {
    pushRow('fcgiwrap', d.svc_fcgiwrap_uptime_s, d.svc_fcgiwrap);
  }
  if (svcIsInstalledFlag(d.svc_mosquitto_installed)) {
    pushRow('mosquitto', d.svc_mosquitto_uptime_s, d.svc_mosquitto);
  }
  if (svcIsInstalledFlag(d.svc_bridge_installed)) {
    pushRow('MQTT', d.svc_bridge_uptime_s, d.svc_bridge, {
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

  if (Array.isArray(d.optional_services) && rows.length < SVC_WIDGET_MAX_ROWS) {
    for (const s of d.optional_services) {
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

/* ══════════════════════════════════════════════════════════════════════════
   STATUS POLLING — приоритетные виджеты отдельно, остальное в фоне
   ══════════════════════════════════════════════════════════════════════════ */
const widgetBusy = { priority: false };
const backgroundBusy = {
  main: false,
  storage: false,
  time: false,
  uptime: false,
  network: false,
  load: false,
  system: false,
  services: false,
  hardware: false,
  rs485: false
};
/** Части дашборда, раньше собиравшиеся в одном part=main — rolling scheduler с фазовым сдвигом. */
const BACKGROUND_STATUS_PARTS = ['storage', 'time', 'uptime', 'network', 'load', 'system', 'services', 'hardware'];
/** Лёгкие части (кеш / быстрый CGI) vs тяжёлые (network, services, rs485 cold ~4 s). */
const LIGHT_STATUS_PARTS = ['storage', 'uptime', 'load', 'system'];
const HEAVY_STATUS_PARTS = ['network', 'services', 'hardware'];
const HEAVY_STATUS_PARTS_ALL = HEAVY_STATUS_PARTS.concat(['priority', 'rs485', 'main']);
/** Период light/heavy и фазы (мс): heavy реже и с большим зазором. */
const LIGHT_PART_PERIOD_MS = 6000;
const HEAVY_PART_PERIOD_MS = 12000;
const LIGHT_PART_PHASE_MS = {
  storage: 0,
  uptime: 750,
  load: 1500,
  system: 2250
};
const HEAVY_PART_PHASE_MS = {
  network: 3500,
  services: 6500,
  hardware: 9500
};
/** @deprecated — для fetchStatusMain stagger burst */
const BACKGROUND_PART_PERIOD_MS = LIGHT_PART_PERIOD_MS;
const BACKGROUND_PART_PHASE_MS = Object.assign({}, LIGHT_PART_PHASE_MS, HEAVY_PART_PHASE_MS, { time: 3000 });
const PRIORITY_POLL_PHASE_MS = 500;
/** RS-485 изолирован от light/heavy: 12 s, фаза ближе к концу цикла. */
const RS485_POLL_PHASE_MS = 10500;
/** Глобальная очередь status.cgi: не более одного запроса одновременно. */
const STATUS_GLOBAL_MIN_GAP_MS = 350;
const STATUS_HEAVY_MIN_GAP_MS = 2200;
/** После hw_set — повторный опрос; если раунд ещё в полёте — не терять повтор. */
let mainBundleRefreshQueued = false;
let statusMainRoundBusy = false;
let rs485RefreshQueued = false;
const backgroundLoaded = {
  main: false,
  storage: false,
  time: false,
  uptime: false,
  network: false,
  load: false,
  system: false,
  services: false,
  hardware: false,
  rs485: false
};
let _lastRs485Ports = null;
const statusFailures = {
  priority: 0,
  main: 0,
  rs485: 0
};
const statusPauseUntil = { main: 0, rs485: 0 };
const STATUS_TIMEOUT_MS = {
  priority: 3000,
  /** Координатор force-refresh (staggered burst всех part=*). */
  main: 6000,
  rs485: 10000,
  storage: 6000,
  time: 4000,
  uptime: 3000,
  network: 5000,
  load: 3000,
  system: 4000,
  services: 8000,
  hardware: 10000
};
/** Интервалы опроса и минимальный зазор между запросами одной части (клиентский rate-limit). */
const STATUS_POLL_INTERVAL_MS = { priority: 6000, main: 6000, rs485: 12000 };
const STATUS_MIN_GAP_MS = {
  priority: 800,
  main: 1500,
  rs485: 1500,
  storage: 1200,
  time: 1200,
  uptime: 1200,
  network: 1200,
  load: 1200,
  system: 1500,
  services: 1500,
  hardware: 1500
};

/** Карта SA02M_STATUS_ENABLE_* с status.cgi?part=blocks (1=опрос, 0=пропуск). */
let _statusBlocksConfig = null;

function isStatusBlockEnabled(part) {
  if (!_statusBlocksConfig) return part !== 'time';
  const v = _statusBlocksConfig[part];
  if (v === 0 || v === false) return false;
  if (v === 1 || v === true) return true;
  return part !== 'time';
}

/** GPIO-виджеты опрашиваем всегда: при hardware=0 CGI отдаёт TTL-кэш (hw_poll_disabled=1). */
function shouldFetchBackgroundPart(part) {
  if (part === 'hardware') return true;
  return isStatusBlockEnabled(part);
}

function activeBackgroundParts() {
  return BACKGROUND_STATUS_PARTS.filter(function (p) { return shouldFetchBackgroundPart(p); });
}

const STATUS_BLOCKS_FETCH_TIMEOUT_MS = 4000;

function fetchStatusBlocksConfig(onReady) {
  const ctrl = new AbortController();
  const timer = setTimeout(function () {
    try { ctrl.abort(); } catch (_) {}
  }, STATUS_BLOCKS_FETCH_TIMEOUT_MS);
  fetch('/cgi-bin/status.cgi?part=blocks', {
    cache: 'no-store',
    credentials: 'same-origin',
    signal: ctrl.signal
  })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (j && typeof j === 'object' && !j.error) _statusBlocksConfig = j;
    })
    .catch(function () { /* keep null → default enable all except time */ })
    .finally(function () {
      clearTimeout(timer);
      if (onReady) onReady();
    });
}

let _statusPollGeneration = 0;
let _statusPollTimers = { priority: null, rs485: null, bootstrap: null };
let _statusInitTimeouts = [];
let _statusFetchQueue = [];
let _statusFetchInFlight = false;
let _statusQueuePumpTimer = null;
let _statusLastGlobalFetchMs = 0;
let _statusLastHeavyStartMs = 0;

function isHeavyStatusPart(part) {
  return HEAVY_STATUS_PARTS_ALL.indexOf(part) >= 0;
}

function clearStatusFetchQueue() {
  _statusFetchQueue = [];
  if (_statusQueuePumpTimer) {
    clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = null;
  }
}

function scheduleStatusFetch(part, delayMs, runner) {
  const delay = Math.max(0, delayMs || 0);
  const at = Date.now() + delay;
  const dup = _statusFetchQueue.some(function (item) { return item.part === part; });
  if (dup) return;
  _statusFetchQueue.push({ part: part, at: at, queuedAt: Date.now(), runner: runner });
  _statusFetchQueue.sort(function (a, b) { return a.at - b.at; });
  pumpStatusFetchQueue();
}

function pumpStatusFetchQueue() {
  if (_statusFetchInFlight) return;
  const now = Date.now();
  if (_statusFetchQueue.length === 0) return;
  if (_statusFetchQueue[0].at > now) {
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, Math.max(50, _statusFetchQueue[0].at - now));
    return;
  }
  const sinceGlobal = now - _statusLastGlobalFetchMs;
  if (sinceGlobal < STATUS_GLOBAL_MIN_GAP_MS) {
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_GLOBAL_MIN_GAP_MS - sinceGlobal);
    return;
  }
  const item = _statusFetchQueue.shift();
  if (isHeavyStatusPart(item.part)) {
    const sinceHeavy = now - _statusLastHeavyStartMs;
    if (sinceHeavy < STATUS_HEAVY_MIN_GAP_MS) {
      item.at = now + (STATUS_HEAVY_MIN_GAP_MS - sinceHeavy);
      _statusFetchQueue.unshift(item);
      if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
      _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_HEAVY_MIN_GAP_MS - sinceHeavy);
      return;
    }
    _statusLastHeavyStartMs = now;
  }
  _statusFetchInFlight = true;
  _statusLastGlobalFetchMs = now;
  let released = false;
  const release = function () {
    if (released) return;
    released = true;
    _statusFetchInFlight = false;
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_GLOBAL_MIN_GAP_MS);
  };
  const queueWaitMs = Math.max(0, Date.now() - (item.queuedAt || item.at));
  try {
    item.runner(release, { queueWaitMs: queueWaitMs });
  } catch (_) {
    release();
  }
}
let _statusFetchAbort = { priority: null, main: null, rs485: null };
let _statusLastFetchMs = { priority: 0, main: 0, rs485: 0 };
BACKGROUND_STATUS_PARTS.forEach(function (p) {
  _statusFetchAbort[p] = null;
  _statusLastFetchMs[p] = 0;
});
let _statusLifecycleBound = false;

function statusRequestTimeout(part, queueWaitMs) {
  const base = STATUS_TIMEOUT_MS[part] || 3500;
  const extra = Math.max(0, queueWaitMs || 0);
  return Math.min(base + extra, base * 2);
}

function isBenignStatusFetchError(e, timedOut) {
  if (!e) return true;
  if (e.stale) return true;
  if (e.name === 'AbortError' && !timedOut) return true;
  return false;
}

function hideDashboardPollAlert() {
  const el = document.getElementById('dashboard-poll-alert');
  if (!el) return;
  el.style.display = 'none';
  el.textContent = '';
}

function abortStatusPart(part) {
  const ctrl = _statusFetchAbort[part];
  if (!ctrl) return;
  try { ctrl.abort(); } catch (_) {}
  _statusFetchAbort[part] = null;
}

function abortAllStatusFetches() {
  ['priority', 'main', 'rs485'].concat(BACKGROUND_STATUS_PARTS).forEach(abortStatusPart);
}

function clearStatusPollTimers() {
  Object.keys(_statusPollTimers).forEach(function (key) {
    if (_statusPollTimers[key]) {
      clearInterval(_statusPollTimers[key]);
      _statusPollTimers[key] = null;
    }
  });
  _statusInitTimeouts.forEach(function (id) { clearTimeout(id); });
  _statusInitTimeouts = [];
}

function resetBackgroundLoadedFlags() {
  Object.keys(backgroundLoaded).forEach(function (k) { backgroundLoaded[k] = false; });
}

function teardownStatusPolling() {
  _statusPollGeneration += 1;
  clearStatusPollTimers();
  clearStatusFetchQueue();
  abortAllStatusFetches();
  widgetBusy.priority = false;
  Object.keys(backgroundBusy).forEach(function (k) { backgroundBusy[k] = false; });
  mainBundleRefreshQueued = false;
  rs485RefreshQueued = false;
  statusMainRoundBusy = false;
  _statusFetchInFlight = false;
}

function canFetchStatusPart(part, force) {
  if (!force && isStatusPartPaused(part)) return false;
  if (!force && part !== 'main' && isStatusPartPaused('main')) return false;
  if (force) return true;
  const gap = STATUS_MIN_GAP_MS[part] || 1000;
  return (Date.now() - (_statusLastFetchMs[part] || 0)) >= gap;
}

/** Fetch status.cgi с AbortController, отменой предыдущего запроса той же части и проверкой поколения. */
function statusFetchJson(part, url, timeoutMs, gen, onTimeout) {
  abortStatusPart(part);
  const ctrl = new AbortController();
  _statusFetchAbort[part] = ctrl;
  const timer = setTimeout(function () {
    if (onTimeout) onTimeout();
    ctrl.abort();
  }, timeoutMs);
  return fetch(url, {
    cache: 'no-store',
    credentials: 'same-origin',
    signal: ctrl.signal
  }).finally(function () {
    clearTimeout(timer);
    if (_statusFetchAbort[part] === ctrl) _statusFetchAbort[part] = null;
  }).then(function (r) {
    if (gen !== _statusPollGeneration) {
      throw Object.assign(new DOMException('Stale poll', 'AbortError'), { stale: true });
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function bindStatusPollingLifecycle() {
  if (_statusLifecycleBound) return;
  _statusLifecycleBound = true;
  window.addEventListener('pagehide', teardownStatusPolling);
  window.addEventListener('beforeunload', teardownStatusPolling);
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) initStatusPolling();
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchPriorityPart('priority');
  });
}

function startPriorityPollTimer(gen) {
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchPriorityPart('priority');
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers.priority = setInterval(tick, STATUS_POLL_INTERVAL_MS.priority);
  }, PRIORITY_POLL_PHASE_MS));
}

function startRs485PollTimer(gen) {
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchStatusRs485();
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers.rs485 = setInterval(tick, STATUS_POLL_INTERVAL_MS.rs485);
  }, RS485_POLL_PHASE_MS));
}

function startLightPartPollTimer(gen, part) {
  const phase = LIGHT_PART_PHASE_MS[part] || 0;
  const timerKey = 'part_' + part;
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchBackgroundPart(part, null, false, null);
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers[timerKey] = setInterval(tick, LIGHT_PART_PERIOD_MS);
  }, phase));
}

function startHeavyPartPollTimer(gen, part) {
  const phase = HEAVY_PART_PHASE_MS[part] || 0;
  const timerKey = 'part_' + part;
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchBackgroundPart(part, null, false, null);
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers[timerKey] = setInterval(tick, HEAVY_PART_PERIOD_MS);
  }, phase));
}

function startBackgroundPartPollTimer(gen, part) {
  if (LIGHT_STATUS_PARTS.indexOf(part) >= 0) {
    startLightPartPollTimer(gen, part);
  } else if (HEAVY_STATUS_PARTS.indexOf(part) >= 0) {
    startHeavyPartPollTimer(gen, part);
  } else {
    const phase = BACKGROUND_PART_PHASE_MS[part] || 0;
    const timerKey = 'part_' + part;
    const tick = function () {
      if (gen !== _statusPollGeneration) return;
      fetchBackgroundPart(part, null, false, null);
    };
    _statusInitTimeouts.push(setTimeout(function () {
      if (gen !== _statusPollGeneration) return;
      tick();
      _statusPollTimers[timerKey] = setInterval(tick, LIGHT_PART_PERIOD_MS);
    }, phase));
  }
}

function startAllBackgroundPartPollTimers(gen) {
  activeBackgroundParts().forEach(function (part) {
    startBackgroundPartPollTimer(gen, part);
  });
}

function initStatusPolling() {
  teardownStatusPolling();
  resetBackgroundLoadedFlags();
  clearHwHintPending();
  _statusBlocksConfig = null;
  const gen = _statusPollGeneration;
  let pollingStarted = false;

  const scheduleInitial = function () {
    if (pollingStarted || gen !== _statusPollGeneration) return;
    pollingStarted = true;
    BACKGROUND_STATUS_PARTS.forEach(function (p) {
      if (!shouldFetchBackgroundPart(p)) backgroundLoaded[p] = true;
    });
    if (!isStatusBlockEnabled('rs485')) backgroundLoaded.rs485 = true;
    startPriorityPollTimer(gen);
    startAllBackgroundPartPollTimers(gen);
    startRs485PollTimer(gen);
    bootstrapBackgroundWidgets(gen);
  };

  const deferScheduleInitial = function () {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(function () { requestAnimationFrame(scheduleInitial); });
    } else {
      _statusInitTimeouts.push(setTimeout(scheduleInitial, 0));
    }
  };

  fetchStatusBlocksConfig(deferScheduleInitial);
  _statusInitTimeouts.push(setTimeout(scheduleInitial, 600));
}

function isStatusPartPaused(part) {
  return (statusPauseUntil[part] || 0) > Date.now();
}

function setStatusPartPause(part, ms) {
  statusPauseUntil[part] = Date.now() + ms;
}

function noteStatusFailure(part, err) {
  statusFailures[part] = (statusFailures[part] || 0) + 1;
  const isTimeout = err && err.name === 'AbortError';
  const needPauseMain = part === 'main' && statusFailures[part] >= 5;
  const needPauseBg = BACKGROUND_STATUS_PARTS.indexOf(part) >= 0 && statusFailures[part] >= 5;
  const needPauseRs = part === 'rs485' && statusFailures[part] >= 3;
  if (needPauseMain || needPauseBg) {
    setStatusPartPause('main', 25000);
    BACKGROUND_STATUS_PARTS.forEach(function (p) { setStatusPartPause(p, 25000); });
    const el = document.getElementById('dashboard-poll-alert');
    if (el) {
      el.style.display = 'block';
      el.textContent = isTimeout
        ? 'Обновление основных виджетов приостановлено: несколько таймаутов ответа status.cgi. Проверьте нагрузку и nginx/fastcgi.'
        : 'Обновление основных виджетов приостановлено из-за ошибок ответа status.cgi.';
    }
  } else if (needPauseRs) {
    setStatusPartPause(part, 25000);
  }
}

function noteStatusSuccess(part) {
  statusFailures[part] = 0;
  if (part === 'main' || part === 'rs485' || BACKGROUND_STATUS_PARTS.indexOf(part) >= 0) {
    statusPauseUntil[part] = 0;
    if (part !== 'rs485') {
      statusFailures.main = 0;
      statusPauseUntil.main = 0;
      hideDashboardPollAlert();
    }
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function threshColor(val, warnAt, critAt) {
  return val >= critAt ? cssVar('--meter-red') : val >= warnAt ? cssVar('--meter-yellow') : cssVar('--meter-cyan');
}

/** USB / microSD: префикс полей в JSON — usb_* или sd_* */
function applyRemovableDisk(mounted, base, d) {
  const val = document.getElementById(base + '-val');
  const detail = document.getElementById(base + '-detail');
  if (!val || !detail) return;
  if (!mounted) {
    val.textContent = uiT('НЕ УСТАНОВЛЕН');
    val.classList.add('widget-val-removable-empty');
    detail.style.display = 'none';
    return;
  }
  val.classList.remove('widget-val-removable-empty');
  detail.style.display = '';
  const used = d[base + '_used_kb'];
  const total = d[base + '_total_kb'];
  const free = d[base + '_free_kb'];
  const pct = parseInt(d[base + '_pct'], 10) || 0;
  setText(base + '-val', fmtKB(used));
  setText(base + '-sub', uiT('из') + ' ' + fmtKB(total));
  setText(base + '-pct', pct + '%');
  setText(base + '-free', uiT('свободно') + ' ' + fmtKB(free));
  const bar = document.getElementById(base + '-bar');
  if (bar) {
    bar.style.width = pct + '%';
    bar.style.background = threshColor(pct, 70, 90);
  }
}

let _lastPriorityStatus = null;

function applyPriorityStatus(d) {
  _lastPriorityStatus = d;
  const arcLen = gaugeArcPathLength();

  /* CPU */
  if (d.cpu_usage !== undefined) {
    setText('cpu-val', d.cpu_usage + '%');
    const cpuArc = document.getElementById('cpu-arc');
    if (cpuArc) {
      cpuArc.style.strokeDasharray = arcDash(d.cpu_usage, arcLen);
      cpuArc.style.stroke = threshColor(d.cpu_usage, 60, 80);
    }
  }

  /* RAM */
  if (d.ram_used_kb !== undefined) {
    setText('ram-val', fmtKB(d.ram_used_kb));
    setText('ram-sub', uiT('из') + ' ' + fmtKB(d.ram_total_kb));
    setText('ram-pct', d.ram_pct + '%');
    setText('ram-free', uiT('свободно') + ' ' + fmtKB(d.ram_free_kb));
    const ramBar = document.getElementById('ram-bar');
    if (ramBar) {
      ramBar.style.width = d.ram_pct + '%';
      ramBar.style.background = threshColor(d.ram_pct, 70, 90);
    }
  }

  /* SWAP — слот зарезервирован до первого ответа priority; без swap после загрузки — убираем */
  const sb = document.getElementById('swap-block');
  if (d.swap_total_kb > 0) {
    if (sb) {
      sb.style.display = '';
      sb.classList.add('swap-active');
      sb.classList.remove('swap-block-collapsed');
      sb.removeAttribute('aria-hidden');
    }
    setText('swap-pct', d.swap_pct + '%');
    setText('swap-lbl', fmtKB(d.swap_used_kb) + ' / ' + fmtKB(d.swap_total_kb));
    const swapBar = document.getElementById('swap-bar');
    if (swapBar) {
      swapBar.style.width = d.swap_pct + '%';
      swapBar.style.background = d.swap_pct > 80 ? cssVar('--meter-red') : cssVar('--meter-orange');
    }
  } else if (sb) {
    sb.classList.remove('swap-active');
    sb.setAttribute('aria-hidden', 'true');
    sb.classList.add('swap-block-collapsed');
    sb.style.display = 'none';
  }

  /* Температура: дуга 30–100 °C; цвет <70 зелёный, 70–80 жёлтый, ≥80 красный */
  if (d.temp_c !== undefined) {
    setText('temp-val', d.temp_c + '°');
    const tempArc = document.getElementById('temp-arc');
    const tempHint = document.getElementById('temp-gauge-hint');
    if (tempArc) {
      tempArc.style.strokeDasharray = arcDash(tempToGaugePct(d.temp_c), arcLen);
      const tc = parseFloat(d.temp_c) || 0;
      const tempStroke = tc >= 80 ? cssVar('--meter-red') : tc >= 70 ? cssVar('--meter-yellow') : cssVar('--meter-green');
      tempArc.style.stroke = tempStroke;
      if (tempHint) {
        tempHint.textContent = uiT(tc >= 80
          ? 'Температура выше нормы'
          : 'Температура в норме');
      }
    }
  }

  /* Disk */
  if (d.disk_used_kb !== undefined) {
    setText('disk-val', fmtKB(d.disk_used_kb));
    setText('disk-sub', uiT('из') + ' ' + fmtKB(d.disk_total_kb));
    setText('disk-pct', d.disk_pct + '%');
    setText('disk-free', uiT('свободно') + ' ' + fmtKB(d.disk_free_kb));
    const diskBar = document.getElementById('disk-bar');
    if (diskBar) {
      diskBar.style.width = d.disk_pct + '%';
      diskBar.style.background = threshColor(d.disk_pct, 70, 90);
    }
  }
}

function applyStorageStatus(d) {
  if (d.disk_io_read_b !== undefined) {
    setText('disk-io', uiT('R ' + fmtTrafficBytes(d.disk_io_read_b) + ' / W ' + fmtTrafficBytes(d.disk_io_write_b)));
  }
  if (d.usb_modem_present) {
    applyUsbModem(d);
  } else {
    var sv = document.getElementById('usb-storage-view');
    var mv = document.getElementById('usb-modem-view');
    var tt = document.getElementById('usb-widget-title');
    if (sv) sv.style.display = '';
    if (mv) mv.style.display = 'none';
    if (tt) tt.textContent = uiT('USB-накопитель');
    applyRemovableDisk(!!d.usb_mounted, 'usb', d);
  }
  applyRemovableDisk(!!d.sd_mounted, 'sd', d);
}

function applyUsbModem(d) {
  var sv = document.getElementById('usb-storage-view');
  var mv = document.getElementById('usb-modem-view');
  var tt = document.getElementById('usb-widget-title');
  if (sv) sv.style.display = 'none';
  if (mv) mv.style.display = '';
  if (tt) tt.textContent = uiT('USB-модем');

  var stateEl = document.getElementById('usb-modem-state-val');
  if (stateEl) {
    var st = d.usb_modem_state || '';
    if (st === 'up') {
      stateEl.textContent = uiT('Подключён');
      stateEl.className = 'widget-val on';
    } else if (st === 'down' || st === 'unknown') {
      stateEl.textContent = uiT('Нет сети');
      stateEl.className = 'widget-val off';
    } else {
      stateEl.textContent = st;
      stateEl.className = 'widget-val';
    }
  }

  var parts = [d.usb_modem_vendor, d.usb_modem_model].filter(function(s) { return s && s.trim(); });
  setText('usb-modem-model', parts.join(' — ') || '');

  var iface = d.usb_modem_iface ? '[' + d.usb_modem_iface + ']' : '';
  setText('usb-modem-ip', d.usb_modem_ip ? d.usb_modem_ip + ' ' + iface : iface);

  var rx = typeof d.usb_modem_rx === 'number' ? fmtBytes(d.usb_modem_rx) : '—';
  var tx = typeof d.usb_modem_tx === 'number' ? fmtBytes(d.usb_modem_tx) : '—';
  setText('usb-modem-traffic', uiT('↓ ' + rx + ' / ↑ ' + tx));
}

function applyTimeStatus(d) {
  if (d.datetime_sys) setText('time-sys-disp', d.datetime_sys);
  else if (d.datetime) setText('time-sys-disp', d.datetime);
  if (document.getElementById('time-rtc-disp')) {
    const rtc = (d.rtc_datetime !== undefined && d.rtc_datetime !== null)
      ? String(d.rtc_datetime).trim()
      : '';
    setText('time-rtc-disp', rtc || '—');
  }
}

function refreshTimeReadouts() {
  fetch('/cgi-bin/config.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      applyTimeStatus({ datetime: d.datetime, rtc_datetime: d.rtc_datetime ?? '' });
    })
    .catch(() => {});
}

window.refreshTimeReadouts = refreshTimeReadouts;

function applyUptimeStatus(d) {
  setText('uptime-val', fmtUptime(d.uptime_sec ?? d.uptime_s));
}

/** Плейсхолдер pill (ширина «Нет линка») — резерв места до ответа status.cgi. */
function ethPillPlaceholderText() {
  return uiT('Нет линка');
}

/** Показывает только «Линк» / «Нет линка»; до ответа status.cgi и при absent — pill невидим, место зарезервировано. */
function applyEthIfaceState(spanId, operstate) {
  const el = document.getElementById(spanId);
  if (!el) return;
  const hide = () => {
    el.textContent = ethPillPlaceholderText();
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('hidden');
    el.className = 'eth-state eth-state-prominent eth-state-hidden';
  };
  if (operstate === undefined || operstate === null || operstate === '') {
    hide();
    return;
  }
  const s = String(operstate).trim().toLowerCase();
  if (s === 'absent' || s === 'unknown') {
    hide();
    return;
  }
  el.removeAttribute('hidden');
  el.removeAttribute('aria-hidden');
  if (s === 'up') {
    el.textContent = uiT('Линк');
    el.className = 'eth-state eth-state-prominent up';
  } else {
    el.textContent = uiT('Нет линка');
    el.className = 'eth-state eth-state-prominent down';
  }
}

/** Dashboard Ethernet widget: «Статический 1.2.3.4» или «DHCP: 1.2.3.4». */
function formatEthIpWidget(ipRaw, modeRaw) {
  const ip = (ipRaw !== undefined && ipRaw !== null) ? String(ipRaw).trim() : '';
  let mode = (modeRaw !== undefined && modeRaw !== null) ? String(modeRaw).trim().toLowerCase() : '';
  if (ip && mode !== 'dhcp' && mode !== 'static') mode = 'static';
  if (mode === 'dhcp') {
    const prefix = uiT('DHCP:');
    return ip ? `${prefix} ${ip}` : prefix;
  }
  if (mode === 'static') {
    const prefix = uiT('Статический');
    return ip ? `${prefix} ${ip}` : prefix;
  }
  return ip || '—';
}

function applyNetworkStatus(d) {
  applyEthIfaceState('end0-state', d.end0_operstate);
  applyEthIfaceState('end1-state', d.end1_operstate);
  setText('end0-rx', fmtTrafficBytes(d.net_rx_bytes || 0));
  setText('end0-tx', fmtTrafficBytes(d.net_tx_bytes || 0));
  setText('end1-rx', fmtTrafficBytes(d.net1_rx_bytes || 0));
  setText('end1-tx', fmtTrafficBytes(d.net1_tx_bytes || 0));
  setText('end0-ip', formatEthIpWidget(d.end0_ip, d.end0_mode));
  setText('end1-ip', formatEthIpWidget(d.end1_ip, d.end1_mode));
  if (d.ip) setText('tb-ip', d.ip);
}

function applyLoadStatus(d) {
  setText('load-1',  d.load_1  || '—');
  setText('load-5',  d.load_5  || '—');
  setText('load-15', d.load_15 || '—');
  setText('proc-info', uiT('Процессов: ' + (d.proc_running || 0) + ' / ' + (d.proc_total || 0)));
  if (d.cpu_freq_mhz) {
    const thr = d.cpu_throttle ? ' (' + d.cpu_throttle + '%)' : '';
    setText('cpu-freq', d.cpu_freq_mhz + ' ' + uiT('МГц') + thr);
  } else {
    setText('cpu-freq', '0 ' + uiT('МГц'));
  }
}

function applySystemStatus(d) {
  if (d.board)     setText('board-info',  d.board);
  if (d.cpu_model) setText('cpu-model',   d.cpu_model);
  else setText('cpu-model', '\u00a0');
  const armbianEl = document.getElementById('armbian-info');
  if (d.armbian_version) {
    setText('armbian-info', d.armbian_version);
    if (armbianEl) armbianEl.classList.remove('widget-sub-inert');
  } else if (armbianEl) {
    armbianEl.textContent = '\u00a0';
    armbianEl.classList.add('widget-sub-inert');
  }
  if (d.kernel)    setText('kernel-info', uiT('Ядро: ' + d.kernel));
  else setText('kernel-info', '\u00a0');

  const btn = document.getElementById('storage-format-toggle');
  const lbl = document.getElementById('storage-format-toggle-label');
  if (btn) {
    const installed = d.storage_mount_installed === 1;
    const on = d.storage_auto_format === 1;
    btn.dataset.storageOn = on ? '1' : '0';
    if (!installed) {
      btn.disabled = true;
      if (lbl) lbl.textContent = uiT('НЕ УСТАНОВЛЕНО');
      btn.className = 'btn btn-danger';
      btn.title = uiT('Нет storage-mount — выполните установку системы (install.sh)');
    } else {
      btn.disabled = false;
      btn.title = uiT('Нажмите, чтобы переключить: при выкл. раздел без ФС или NTFS не форматируется');
      if (lbl) lbl.textContent = uiT(on ? 'ВКЛЮЧЕНО' : 'ОТКЛЮЧЕНО');
      btn.className = 'btn btn-danger';
    }
  }
}

function toggleStorageAutoFormat() {
  const btn = document.getElementById('storage-format-toggle');
  if (!btn || btn.disabled) return;
  const on = btn.dataset.storageOn === '1';
  setStorageAutoFormat(on ? 0 : 1);
}

function fetchWithTimeout(url, options, timeoutMs) {
  const ms = timeoutMs || 12000;
  const c = new AbortController();
  const t = setTimeout(function () { c.abort(); }, ms);
  return fetch(url, Object.assign({}, options || {}, { signal: c.signal })).finally(function () {
    clearTimeout(t);
  });
}

function setStorageAutoFormat(enabled) {
  const body = 'enabled=' + encodeURIComponent(enabled ? '1' : '0');
  fetchWithTimeout('/cgi-bin/storage_format_set.cgi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
    credentials: 'same-origin'
  }, 12000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.ok) {
        fetchSystemWidget();
        toast(enabled ? 'ВКЛЮЧЕНО' : 'ОТКЛЮЧЕНО', 'success');
      } else if (j.error === 'storage_tools_not_installed') {
        toast('На устройстве не установлен storage-mount (запустите установщик)', 'error');
      } else {
        toast('Ошибка: ' + (j.error || 'unknown'), 'error');
      }
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError') {
        toast('Таймаут запроса — интерфейс не завис, повторите', 'error');
      } else {
        toast('Нет связи с сервером', 'error');
      }
    });
}

let _lastPartStatus = {};
let _lastServicesStatus = null;

function getBackgroundPartApply(part) {
  switch (part) {
    case 'storage': return applyStorageStatus;
    case 'time': return applyTimeStatus;
    case 'uptime': return applyUptimeStatus;
    case 'network': return applyNetworkStatus;
    case 'load': return applyLoadStatus;
    case 'system': return applySystemStatus;
    case 'services': return applyServicesStatus;
    case 'hardware': return applyHardwareStatus;
    default: return null;
  }
}

function allBackgroundPartsLoaded() {
  return activeBackgroundParts().every(function (p) { return backgroundLoaded[p]; });
}

function syncMainLoadedFlag() {
  backgroundLoaded.main = allBackgroundPartsLoaded();
}

function applyCachedBackgroundPartsI18n() {
  BACKGROUND_STATUS_PARTS.forEach(function (part) {
    const d = _lastPartStatus[part];
    const applyFn = getBackgroundPartApply(part);
    if (d && applyFn) applyFn(d);
  });
}

function applyServicesStatus(d) {
  _lastServicesStatus = d;
  renderServicesDynamic(d);
}

window.refreshMainStatusI18n = function () {
  applyCachedBackgroundPartsI18n();
};

window.refreshPriorityStatusI18n = function () {
  if (_lastPriorityStatus) applyPriorityStatus(_lastPriorityStatus);
};

window.refreshServicesDynamicI18n = function () {
  if (_lastServicesStatus) renderServicesDynamic(_lastServicesStatus);
};

function deviceTitleRu(variant) {
  return variant === 'sa02m-2eth'
    ? 'Сервер автоматизации СА-02м-2'
    : 'Сервер автоматизации СА-02м';
}

function applyDeviceTitle() {
  const title = document.getElementById('device-title');
  if (!title) return;
  const ru = deviceTitleRu(_boardVariant);
  title.textContent = window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
}

window.applyDeviceTitle = applyDeviceTitle;

function applyVariantVisibility(variant) {
  const v = variant || 'sa02m-1eth';
  _boardVariant = v;
  document.querySelectorAll('[data-hide-for]').forEach(function(el) {
    const hideFor = el.dataset.hideFor.split(/\s+/).filter(Boolean);
    el.style.display = hideFor.includes(v) ? 'none' : '';
  });
  const ethGrid = document.querySelector('.network-eth-grid');
  if (ethGrid) {
    ethGrid.classList.toggle('network-eth-grid-single', v !== 'sa02m-2eth');
  }
  const title = document.getElementById('device-title');
  if (title) applyDeviceTitle();
  const netDesc = document.getElementById('network-page-desc');
  if (netDesc) {
    netDesc.textContent = uiT(v === 'sa02m-2eth'
      ? 'Конфигурация Ethernet № 1 и № 2'
      : 'Конфигурация Ethernet № 1');
  }
  if (!backgroundLoaded.rs485) renderRs485Skeleton();
}

let _lastHwMetrics = null;

function hwMetricsPayloadValid(d) {
  return !!(d && (d.hw_configured !== undefined || d.hw_poll_disabled !== undefined));
}

function clearHwHintPending() {
  const hint = document.getElementById('hw-hint');
  if (!hint) return;
  hint.textContent = '';
  hint.style.display = 'none';
}

function applyHardwareStatus(d) {
  if (!hwMetricsPayloadValid(d)) return;
  _lastHwMetrics = d;
  const hint = document.getElementById('hw-hint');
  if (hint) {
    let msg = '';
    if (d.hw_i2c_busy === 1) {
      msg = 'ШИНА I2C ЗАНЯТА ДРУГОЙ СЛУЖБОЙ';
    } else if (d.hw_i2c_expander_absent === 1) {
      msg = 'НЕТ СВЯЗИ С МИКРОСХЕМОЙ РАСШИРЕНИЯ I2C';
    } else if (d.hw_configured === 0 && d.hw_poll_disabled !== 1) {
      msg = 'Каналы не заданы — отредактируйте /etc/sa02m_hw.conf';
    }
    hint.textContent = msg ? uiT(msg) : '';
    hint.style.display = msg ? '' : 'none';
  }
  if (d.hw_variant !== undefined) applyVariantVisibility(d.hw_variant);
  applyHwChannel('hw-do-st', 'do', d.hw_do);
  applyHwChannel('hw-beep-st', 'beeper', d.hw_beeper);
  applyHwChannel('hw-led-st', 'alarm_led', d.hw_alarm_led);
  let usbPl = d.hw_usb_power;
  clearPendingUsbPowerIfServerMatches(usbPl);
  if (pendingUsbPowerVal !== null && Date.now() < pendingUsbPowerUntil) {
    usbPl = pendingUsbPowerVal;
  }
  applyHwChannel('hw-usb-st', 'usb_power', usbPl);
  const pin = (k, legacy) => (d[k] !== undefined ? !!d[k] : !!legacy);
  const anyHw = !!d.hw_configured;
  setHwChannelBtns('do',        pin('hw_pin_do', anyHw));
  setHwChannelBtns('beeper',    pin('hw_pin_beeper', anyHw));
  setHwChannelBtns('alarm_led', pin('hw_pin_alarm_led', anyHw));
  setHwChannelBtns('usb_power', pin('hw_pin_usb_power', anyHw));
}

function applyRs485Status(d) {
  if (d.rs485 && d.rs485.length) renderRs485(d.rs485);
}

function applyStatus(d) {
  applyPriorityStatus(d);
  applyStorageStatus(d);
  applyTimeStatus(d);
  applyUptimeStatus(d);
  applyNetworkStatus(d);
  applyLoadStatus(d);
  applySystemStatus(d);
  applyServicesStatus(d);
  applyHardwareStatus(d);
  applyRs485Status(d);
}

function fetchPriorityPart(_part, persist = true) {
  if (widgetBusy.priority) return;
  if (!canFetchStatusPart('priority', false)) return;
  scheduleStatusFetch('priority', 0, function (release, meta) {
    const gen = _statusPollGeneration;
    if (widgetBusy.priority) { release(); return; }
    if (!canFetchStatusPart('priority', false)) { release(); return; }
    widgetBusy.priority = true;
    _statusLastFetchMs.priority = Date.now();
    let timedOut = false;
    const queueWaitMs = meta && meta.queueWaitMs ? meta.queueWaitMs : 0;
    statusFetchJson('priority', '/cgi-bin/status.cgi?part=priority', statusRequestTimeout('priority', queueWaitMs), gen, function () {
      timedOut = true;
    })
      .then(d => {
        if (d.error) return;
        applyPriorityStatus(d);
        if (persist) ['cpu', 'temp', 'ram', 'disk'].forEach((part) => writePriorityWarmupPart(part, d));
        noteStatusSuccess('priority');
      })
      .catch((e) => {
        if (gen !== _statusPollGeneration) return;
        if (isBenignStatusFetchError(e, timedOut)) return;
        noteStatusFailure('priority', e);
      })
      .finally(() => {
        widgetBusy.priority = false;
        release();
      });
  });
}

function fetchCpuWidget() {
  fetchPriorityPart('cpu');
}

function fetchTempWidget() {
  fetchPriorityPart('temp');
}

function fetchRamWidget() {
  fetchPriorityPart('ram');
}

function fetchDiskWidget() {
  fetchPriorityPart('disk');
}

function applyMainStatusBundle(d) {
  BACKGROUND_STATUS_PARTS.forEach(function (part) {
    _lastPartStatus[part] = d;
    const applyFn = getBackgroundPartApply(part);
    if (applyFn) applyFn(d);
  });
  syncMainLoadedFlag();
}

/** Поколение опроса HW: увеличивается после hw_set, чтобы отложенный JSON не затирал UI свежими кнопками. */
let mainStatusEpoch = 0;
function bumpMainStatusEpoch() {
  mainStatusEpoch++;
}

function fetchBackgroundPart(part, applyFn, force, onDone) {
  if (BACKGROUND_STATUS_PARTS.indexOf(part) >= 0 && !shouldFetchBackgroundPart(part)) {
    backgroundLoaded[part] = true;
    if (onDone) onDone(false);
    return;
  }
  if (backgroundBusy[part]) {
    if (part === 'rs485') rs485RefreshQueued = true;
    if (onDone) onDone(false);
    return;
  }
  if (!force && !canFetchStatusPart(part, false)) {
    if (onDone) onDone(false);
    return;
  }
  if (!applyFn && part !== 'rs485') applyFn = getBackgroundPartApply(part);
  if (part !== 'rs485' && !applyFn) {
    if (onDone) onDone(false);
    return;
  }
  scheduleStatusFetch(part, 0, function (release, meta) {
    const gen = _statusPollGeneration;
    if (backgroundBusy[part]) {
      if (part === 'rs485') rs485RefreshQueued = true;
      release();
      if (onDone) onDone(false);
      return;
    }
    if (!force && !canFetchStatusPart(part, false)) {
      release();
      if (onDone) onDone(false);
      return;
    }
    backgroundBusy[part] = true;
    _statusLastFetchMs[part] = Date.now();
    let timedOut = false;
    let reported = false;
    const reportDone = function (failed) {
      if (reported) return;
      reported = true;
      if (onDone) onDone(!!failed);
    };
    let url = '/cgi-bin/status.cgi?part=' + encodeURIComponent(part);
    if (force) url += '&no_cache=1';
    const epochAtStart = part === 'hardware' ? mainStatusEpoch : null;
    const apply = applyFn || applyRs485Status;
    const queueWaitMs = meta && meta.queueWaitMs ? meta.queueWaitMs : 0;
    statusFetchJson(part, url, statusRequestTimeout(part, queueWaitMs), gen, function () {
      timedOut = true;
    })
      .then(d => {
        if (epochAtStart !== null && epochAtStart !== mainStatusEpoch) return;
        if (d.error) return;
        if (part === 'rs485') {
          apply(d);
        } else {
          _lastPartStatus[part] = d;
          apply(d);
        }
        backgroundLoaded[part] = true;
        noteStatusSuccess(part);
      })
      .catch((e) => {
        if (gen !== _statusPollGeneration) return;
        if (isBenignStatusFetchError(e, timedOut)) return;
        noteStatusFailure(part, e);
        reportDone(true);
        if (gen === _statusPollGeneration && part === 'hardware' && !backgroundLoaded.hardware) {
          _statusInitTimeouts.push(setTimeout(function () {
            if (gen !== _statusPollGeneration) return;
            fetchBackgroundPart('hardware', applyHardwareStatus, true);
          }, 800));
        }
      })
      .finally(() => {
        backgroundBusy[part] = false;
        if (part === 'rs485' && rs485RefreshQueued) {
          rs485RefreshQueued = false;
          fetchBackgroundPart('rs485', applyRs485Status);
        }
        reportDone(false);
        release();
      });
  });
}

function fetchStatusMain(force) {
  if (statusMainRoundBusy) {
    if (force) mainBundleRefreshQueued = true;
    return;
  }
  if (!canFetchStatusPart('main', !!force)) return;
  statusMainRoundBusy = true;
  _statusLastFetchMs.main = Date.now();
  let pending = 0;
  let roundHadFailure = false;
  const finishPart = function (failed) {
    if (failed) roundHadFailure = true;
    pending -= 1;
    if (pending > 0) return;
    statusMainRoundBusy = false;
    syncMainLoadedFlag();
    if (!roundHadFailure) noteStatusSuccess('main');
    if (mainBundleRefreshQueued) {
      mainBundleRefreshQueued = false;
      fetchStatusMain(true);
    }
  };
  activeBackgroundParts().forEach(function (part) {
    pending += 1;
    const phase = BACKGROUND_PART_PHASE_MS[part] || 0;
    const launch = function () {
      fetchBackgroundPart(part, null, !!force, finishPart);
    };
    if (phase === 0) {
      launch();
    } else {
      _statusInitTimeouts.push(setTimeout(launch, phase));
    }
  });
  if (pending === 0) statusMainRoundBusy = false;
}

function fetchStorageWidget() {
  fetchBackgroundPart('storage', applyStorageStatus);
}

function fetchTimeWidget() {
  fetchBackgroundPart('time', applyTimeStatus);
}

function fetchUptimeWidget() {
  fetchBackgroundPart('uptime', applyUptimeStatus);
}

function fetchNetworkWidget() {
  fetchBackgroundPart('network', applyNetworkStatus);
}

function fetchLoadWidget() {
  fetchBackgroundPart('load', applyLoadStatus);
}

function fetchSystemWidget() {
  fetchBackgroundPart('system', applySystemStatus);
}

function shortGitSha(sha) {
  if (sha == null || typeof sha !== 'string') return '—';
  const s = sha.trim();
  if (!s) return '—';
  return s.length <= 7 ? s : s.slice(0, 7);
}

function deployedRefDisplay(j) {
  if (j && j.deployed_commit != null && typeof j.deployed_commit === 'string' && j.deployed_commit.trim()) {
    return shortGitSha(j.deployed_commit);
  }
  if (j && j.deployed_label != null && String(j.deployed_label).trim()) {
    return String(j.deployed_label).trim();
  }
  return '—';
}

/** ISO UTC → DD.MM.YY HH:mm:ss (например 22.05.26 12:09:35). */
function fmtWebUpdChecked(raw) {
  if (raw == null || raw === '') return '—';
  const s = String(raw).trim();
  if (!s || s === '—') return '—';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (m) {
    return m[3] + '.' + m[2] + '.' + m[1].slice(-2) + ' ' + m[4] + ':' + m[5] + ':' + m[6];
  }
  return s;
}

function webUpdResolveAvailable(j) {
  if (j.update_available === true) return true;
  if (j.update_available === false) return false;
  const depVer = j.deployed_version != null ? String(j.deployed_version).trim() : '';
  const remVer = j.remote_version != null ? String(j.remote_version).trim() : '';
  if (depVer && remVer) return depVer !== remVer;
  const rem = String(j.remote_commit || '').trim().toLowerCase();
  if (!rem) return null;
  const dep = String(j.deployed_commit || '').trim().toLowerCase();
  if (dep) {
    return dep !== rem && !rem.startsWith(dep) && !dep.startsWith(rem.slice(0, 7));
  }
  const lab = deployedRefDisplay(j).toLowerCase();
  if (lab !== '—' && /^[a-f0-9]{7,40}$/.test(lab)) {
    return lab !== rem && !rem.startsWith(lab);
  }
  return null;
}

function webUpdVersionDisplay(ver, commitOrLabel, fallbackLabel) {
  const v = ver != null && String(ver).trim() ? String(ver).trim() : '';
  if (v) return v;
  if (commitOrLabel != null && typeof commitOrLabel === 'string' && commitOrLabel.trim()) {
    return shortGitSha(commitOrLabel);
  }
  if (fallbackLabel != null && String(fallbackLabel).trim() && String(fallbackLabel).trim() !== '—') {
    return String(fallbackLabel).trim();
  }
  return '—';
}

function applyWebUpdateCheckUI(j) {
  if (!j || typeof j !== 'object') return;
  if (j.error === 'unauthorized') return;
  const depVer = webUpdVersionDisplay(j.deployed_version, j.deployed_commit, deployedRefDisplay(j));
  const remVer = webUpdVersionDisplay(j.remote_version, j.remote_commit, null);
  setText('web-upd-deployed-ver', depVer);
  setText('web-upd-remote-ver', remVer);
  setText('web-upd-deployed', deployedRefDisplay(j));
  setText('web-upd-remote', shortGitSha(j.remote_commit));
  setText('web-upd-checked', fmtWebUpdChecked(j.checked_at));
  const st = document.getElementById('web-upd-status');
  const applyBtn = document.getElementById('web-upd-apply-btn');
  if (!st) return;
  st.classList.remove('is-ok', 'is-warn', 'is-err');
  const emsg = j.error && j.error !== 'no_cache_yet' ? String(j.error) : '';
  const ua = webUpdResolveAvailable(j);
  if (ua === true) {
    st.textContent = 'Есть обновление: доступна ' + remVer + ', текущая ' + depVer + '.';
    st.classList.add('is-warn');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = false;
  } else if (ua === false) {
    st.textContent = 'Обновлений нет (версия ' + depVer + ').';
    st.classList.add('is-ok');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = true;
  } else if (emsg && !j.remote_commit) {
    st.textContent = 'Проверка не удалась.';
    st.classList.add('is-err');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = true;
  } else {
    st.textContent = '';
    st.hidden = true;
    if (applyBtn) applyBtn.hidden = true;
  }
}

function loadWebUpdateStatus() {
  fetchWithTimeout('/cgi-bin/web_update_check.cgi', {
    credentials: 'same-origin',
    cache: 'no-store'
  }, 8000)
    .then(function (r) { return r.json(); })
    .then(applyWebUpdateCheckUI)
    .catch(function () { /* вкладка открыта без бэкенда */ });
}

function checkWebUpdatesManual() {
  const btn = document.getElementById('web-upd-check-btn');
  if (btn) btn.disabled = true;
  fetchWithTimeout('/cgi-bin/web_update_check.cgi?force=1', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store'
  }, 25000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      applyWebUpdateCheckUI(j);
      if (j.error === 'unauthorized') {
        toast('Сессия истекла', 'error');
      } else if (j.error && j.error !== 'no_cache_yet' && !j.remote_commit) {
        toast('Проверка не удалась: ' + j.error, 'error');
      } else {
        const ua = webUpdResolveAvailable(j);
        if (ua === true) toast('Есть обновление', 'info');
        else if (ua === false) toast('Обновлений нет', 'success');
        else toast('Проверка выполнена', 'info');
      }
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError') {
        toast('Таймаут — повторите', 'error');
      } else {
        toast('Нет связи с сервером', 'error');
      }
    })
    .finally(function () { if (btn) btn.disabled = false; });
}

var _webUpdPollTimer = null;

function applyWebUpdate() {
  const applyBtn = document.getElementById('web-upd-apply-btn');
  const checkBtn = document.getElementById('web-upd-check-btn');
  const logEl = document.getElementById('web-upd-log');
  const st = document.getElementById('web-upd-status');
  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = 'Применяется…'; }
  if (checkBtn) checkBtn.disabled = true;
  if (logEl) { logEl.textContent = ''; logEl.hidden = false; }
  if (st) { st.textContent = 'Загрузка и установка обновления…'; st.className = 'web-upd-status is-warn'; st.hidden = false; }

  fetchWithTimeout('/cgi-bin/web_update_apply.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store'
  }, 10000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.status === 'running' || j.status === 'idle') {
        _webUpdStartPolling();
      } else {
        _webUpdFinish(j.status, j.log || '');
      }
    })
    .catch(function () {
      _webUpdFinish('error', 'Нет ответа от сервера');
    });
}

function _webUpdStartPolling() {
  if (_webUpdPollTimer) clearInterval(_webUpdPollTimer);
  _webUpdPollTimer = setInterval(function () {
    fetchWithTimeout('/cgi-bin/web_update_apply.cgi', {
      credentials: 'same-origin', cache: 'no-store'
    }, 8000)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var logEl = document.getElementById('web-upd-log');
        if (logEl && j.log) logEl.textContent = j.log;
        if (j.status !== 'running') {
          clearInterval(_webUpdPollTimer);
          _webUpdPollTimer = null;
          _webUpdFinish(j.status, j.log || '');
        }
      })
      .catch(function () {});
  }, 2000);
}

function _webUpdFinish(status, log) {
  var applyBtn = document.getElementById('web-upd-apply-btn');
  var checkBtn = document.getElementById('web-upd-check-btn');
  var st = document.getElementById('web-upd-status');
  var logEl = document.getElementById('web-upd-log');
  if (checkBtn) checkBtn.disabled = false;
  if (logEl && log) logEl.textContent = log;
  if (status === 'done') {
    if (st) { st.textContent = 'Обновление применено. Страница перезагрузится через 5 секунд…'; st.className = 'web-upd-status is-ok'; st.hidden = false; }
    if (applyBtn) applyBtn.hidden = true;
    toast('Обновление применено успешно', 'success');
    setTimeout(function () { location.reload(); }, 5000);
  } else {
    if (st) { st.textContent = 'Ошибка обновления. Проверьте лог ниже.'; st.className = 'web-upd-status is-err'; st.hidden = false; }
    if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = 'Применить обновление'; }
    toast('Ошибка обновления', 'error');
  }
}

function fetchServicesWidget() {
  fetchBackgroundPart('services', applyServicesStatus);
}

function fetchHardwareWidget() {
  fetchBackgroundPart('hardware', applyHardwareStatus);
}

function fetchStatusRs485(force) {
  fetchBackgroundPart('rs485', applyRs485Status, !!force);
}

function fetchStatus() {
  fetchPriorityPart('priority');
  fetchStatusMain();
  fetchStatusRs485();
}

function allBackgroundWidgetsLoaded() {
  return Object.values(backgroundLoaded).every(Boolean);
}

function bootstrapBackgroundWidgets(pollGen) {
  let attempts = 0;
  if (_statusPollTimers.bootstrap) {
    clearInterval(_statusPollTimers.bootstrap);
    _statusPollTimers.bootstrap = null;
  }
  _statusInitTimeouts.push(setTimeout(function () {
    if (pollGen !== _statusPollGeneration) return;
    _statusPollTimers.bootstrap = setInterval(function () {
      if (pollGen !== _statusPollGeneration) {
        clearInterval(_statusPollTimers.bootstrap);
        _statusPollTimers.bootstrap = null;
        return;
      }
      if (allBackgroundWidgetsLoaded() || attempts >= 8) {
        clearInterval(_statusPollTimers.bootstrap);
        _statusPollTimers.bootstrap = null;
        return;
      }
      const missing = activeBackgroundParts().filter(function (p) { return !backgroundLoaded[p]; });
      if (missing.length > 0) {
        fetchBackgroundPart(missing[0], null, false, null);
      } else if (!backgroundLoaded.rs485 && isStatusBlockEnabled('rs485')) {
        fetchStatusRs485();
      }
      attempts += 1;
    }, 2500);
  }, 4000));
}

/* ══════════════════════════════════════════════════════════════════════════
   HW GPIO CONTROL
   ══════════════════════════════════════════════════════════════════════════ */
const HW_STATUS_AUTO_CLEAR_MS = 3000;
let _hwBlockStatusTimer = null;
let _hwBlockStatusGen = 0;
let _lastHwBlockStatus = null;

function clearHwBlockStatusTimer() {
  if (_hwBlockStatusTimer) {
    clearTimeout(_hwBlockStatusTimer);
    _hwBlockStatusTimer = null;
  }
}

function setHwBlockStatus(msg, type, clearMs) {
  const el = document.getElementById('hw-block-status');
  if (!el) return;
  clearHwBlockStatusTimer();
  if (!msg) {
    _hwBlockStatusGen += 1;
    _lastHwBlockStatus = null;
    el.hidden = true;
    el.textContent = '';
    el.className = 'hw-block-status';
    return;
  }
  _lastHwBlockStatus = { msg: String(msg), type: type || '', clearMs: clearMs };
  el.hidden = false;
  el.textContent = uiT(_lastHwBlockStatus.msg);
  el.className = 'hw-block-status' + (type ? ' ' + type : '');
  const gen = ++_hwBlockStatusGen;
  const autoMs = (typeof clearMs === 'number' && clearMs > 0) ? clearMs : HW_STATUS_AUTO_CLEAR_MS;
  _hwBlockStatusTimer = setTimeout(() => {
    _hwBlockStatusTimer = null;
    if (_hwBlockStatusGen !== gen) return;
    setHwBlockStatus('');
  }, autoMs);
}

window.hwRerenderBlockStatus = function () {
  if (_lastHwBlockStatus) {
    setHwBlockStatus(_lastHwBlockStatus.msg, _lastHwBlockStatus.type, _lastHwBlockStatus.clearMs);
  }
};

const HW_STATE_WORDS = {
  do: ['ВЫКЛ', 'ВКЛ'],
  beeper: ['Тихо', 'Звук'],
  alarm_led: ['ВЫКЛ', 'ВКЛ'],
  usb_power: ['ВЫКЛ', 'ВКЛ'],
};
const HW_ST_BY_CH = {
  do: 'hw-do-st',
  beeper: 'hw-beep-st',
  alarm_led: 'hw-led-st',
  usb_power: 'hw-usb-st',
};

/** Пока status отдаёт старый hw_usb_power (gpiod/sudo), не откатывать подпись и кнопки. */
let pendingUsbPowerUntil = 0;
let pendingUsbPowerVal = /** @type {number|null} */ (null);

function clearPendingUsbPowerIfServerMatches(v) {
  if (pendingUsbPowerVal === null) return;
  if (hwLogicalFromPayload(v) === pendingUsbPowerVal) {
    pendingUsbPowerVal = null;
    pendingUsbPowerUntil = 0;
  }
}

/** 0/1 для GPIO/I2C в виджетах; строки из JSON и misfetches не ломают подсветку. */
function hwLogicalFromPayload(v) {
  if (v === null || v === undefined) return -1;
  if (v === -1) return -1;
  if (typeof v === 'string' && v.trim() === '') return -1;
  const n = Number(v);
  if (n === 0 || n === 1) return n;
  return -1;
}

function syncHwButtonStyles(channel, v) {
  if (channel === 'usb_power') {
    return;
  }
  const nv = hwLogicalFromPayload(v);
  const wrap = document.querySelector('.hw-btns[data-hw-ch="' + channel + '"]');
  if (!wrap) return;
  const b0 = wrap.querySelector('.hw-io-btn[data-hw-val="0"]');
  const b1 = wrap.querySelector('.hw-io-btn[data-hw-val="1"]');
  [b0, b1].forEach(function (b) {
    if (b) {
      b.classList.remove('hw-io-current', 'hw-io-to-on', 'hw-io-to-off');
    }
  });
  if (nv === -1) {
    if (b0) b0.classList.add('hw-io-current');
    if (b1) b1.classList.add('hw-io-current');
    return;
  }
  if (nv) {
    if (b0) b0.classList.add('hw-io-to-off');
    if (b1) b1.classList.add('hw-io-current');
  } else {
    if (b0) b0.classList.add('hw-io-current');
    if (b1) b1.classList.add('hw-io-to-on');
  }
}

function applyHwChannel(stId, channel, v) {
  const nv = hwLogicalFromPayload(v);
  const el = document.getElementById(stId);
  const words = HW_STATE_WORDS[channel] || ['ВЫКЛ', 'ВКЛ'];
  if (!el) {
    syncHwButtonStyles(channel, nv);
    return;
  }
  if (nv === -1) {
    el.textContent = uiT('н/д');
    el.className = 'hw-status-val na';
    syncHwButtonStyles(channel, -1);
    return;
  }
  el.textContent = uiT(nv ? words[1] : words[0]);
  el.className = 'hw-status-val ' + (nv ? 'on' : 'off');
  syncHwButtonStyles(channel, nv);
}

function setHwChannelBtns(channel, enabled) {
  document.querySelectorAll('.hw-btns[data-hw-ch="' + channel + '"] button').forEach(function (b) {
    b.disabled = !enabled;
  });
}

/** Сброс USB по умолчанию (test_fb.cpp: 100×100 ms), если сервер не вернул reset_sec. */
const USB_POWER_RESET_SEC_DEFAULT = 10;

function usbPowerReset() {
  const btn = document.getElementById('hw-usb-reset-btn');
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;

  fetch('/cgi-bin/hw_set.cgi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'channel=' + encodeURIComponent('usb_power') + '&value=' + encodeURIComponent('reset'),
    credentials: 'same-origin'
  })
    .then(r => r.json())
    .then(j => {
      if (j.ok && j.resetting) {
        const sec = (typeof j.reset_sec === 'number' && j.reset_sec > 0)
          ? j.reset_sec
          : USB_POWER_RESET_SEC_DEFAULT;
        setHwBlockStatus('Сброс питания на ' + sec + ' сек', 'success', (sec + 2) * 1000);
        applyHwChannel('hw-usb-st', 'usb_power', 0);
        pendingUsbPowerVal = 0;
        pendingUsbPowerUntil = Date.now() + (sec + 2) * 1000;
        bumpMainStatusEpoch();
        fetchStatusMain(true);
        window.setTimeout(function () {
          pendingUsbPowerVal = null;
          pendingUsbPowerUntil = 0;
          if (btn) btn.disabled = false;
          bumpMainStatusEpoch();
          fetchStatusMain(true);
        }, sec * 1000 + 500);
        return;
      }
      if (btn) btn.disabled = false;
      if (j.error === 'reset_busy') setHwBlockStatus('Сброс USB уже выполняется', 'warn');
      else if (j.error === 'gpio_not_configured') setHwBlockStatus('Канал не настроен в /etc/sa02m_hw.conf', 'error');
      else if (j.error === 'i2c_busy') setHwBlockStatus('Шина I2C занята другой службой', 'error');
      else if (j.error === 'i2c_tools_missing') setHwBlockStatus('На устройстве нет i2c-tools', 'error');
      else setHwBlockStatus('Ошибка: ' + (j.error || 'unknown'), 'error');
    })
    .catch(function () {
      if (btn) btn.disabled = false;
      setHwBlockStatus('Нет связи с сервером', 'error');
    });
}

function bindUsbPowerResetButton() {
  const btn = document.getElementById('hw-usb-reset-btn');
  if (!btn || btn.dataset.usbResetBound === '1') return;
  btn.dataset.usbResetBound = '1';
  btn.addEventListener('click', function (e) {
    e.preventDefault();
    usbPowerReset();
  });
}

function setHw(channel, value, opts) {
  const quiet = !!(opts && opts.quiet);
  const body = 'channel=' + encodeURIComponent(channel) + '&value=' + encodeURIComponent(value);
  fetch('/cgi-bin/hw_set.cgi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body, credentials: 'same-origin'
  })
    .then(r => r.json())
    .then(j => {
      if (j.ok) {
        const stId = HW_ST_BY_CH[channel];
        let vApplied = j.value;
        if (typeof vApplied !== 'number') vApplied = parseInt(String(j.value), 10);
        if (!Number.isFinite(vApplied)) vApplied = parseInt(String(value), 10);
        if (stId && (vApplied === 0 || vApplied === 1)) {
          applyHwChannel(stId, channel, vApplied);
        }
        if (channel === 'usb_power' && (vApplied === 0 || vApplied === 1)) {
          pendingUsbPowerVal = vApplied;
          pendingUsbPowerUntil = Date.now() + 15000;
        }
        bumpMainStatusEpoch();
        fetchStatusMain(true);
        if (!quiet) setHwBlockStatus('Применено', 'success');
      }
      else if (j.error === 'gpio_not_configured') setHwBlockStatus('Канал не настроен в /etc/sa02m_hw.conf', 'error');
      else if (j.error === 'i2c_busy') setHwBlockStatus('Шина I2C занята другой службой', 'error');
      else if (j.error === 'i2c_tools_missing') setHwBlockStatus('На устройстве нет i2c-tools', 'error');
      else setHwBlockStatus('Ошибка: ' + (j.error || 'unknown'), 'error');
    })
    .catch(() => setHwBlockStatus('Нет связи с сервером', 'error'));
}

/* ══════════════════════════════════════════════════════════════════════════
   RS-485 CARDS
   ══════════════════════════════════════════════════════════════════════════ */
function rs485PortCountForVariant(variant) {
  return variant === 'sa02m-2eth' ? 4 : 5;
}

/** RS-485-N ↔ COM(N+1): RS-485-0 = COM1, RS-485-3 = COM4 (см. /etc/sa02m_serial_map.conf). */
function rs485PortLabel(n) {
  return 'RS-485-' + n + ' (COM' + (n + 1) + ')';
}

function rs485ErrParts(fe, pe, oe) {
  const parts = [];
  if (fe) parts.push('FE=' + fe);
  if (pe) parts.push('PE=' + pe);
  if (oe) parts.push('OE=' + oe);
  return parts;
}

function rs485ErrSlotHtml(fe, pe, oe) {
  const parts = rs485ErrParts(fe, pe, oe);
  if (parts.length) {
    return '<div class="rs485-err">' + escHtml(uiT('Ош ' + parts.join(' '))) + '</div>';
  }
  return '<div class="rs485-err rs485-err-reserved" aria-hidden="true">\u00a0</div>';
}

function rs485ErrDelta(p) {
  const hasDelta = p.fe_d != null || p.pe_d != null || p.oe_d != null;
  if (hasDelta) {
    return {
      fe: p.fe_d | 0,
      pe: p.pe_d | 0,
      oe: p.oe_d | 0
    };
  }
  return { fe: p.fe | 0, pe: p.pe | 0, oe: p.oe | 0 };
}

function rs485SkeletonCardHtml(n) {
  return (
    '<div class="rs485-hdr"><span class="rs485-name">' + escHtml(uiT(rs485PortLabel(n))) + '</span><span class="rs485-dot idle" aria-hidden="true"></span></div>' +
    '<div class="rs485-dev rs485-dev-reserved" aria-hidden="true">\u00a0</div>' +
    '<div class="rs485-row"><span class="rl">TX</span><span class="rv">0</span></div>' +
    '<div class="rs485-row"><span class="rl">RX</span><span class="rv">0</span></div>' +
    rs485ErrSlotHtml(0, 0, 0)
  );
}

/** Карточки-заглушки до part=rs485 — высота виджета как после загрузки. */
function renderRs485Skeleton(portCount) {
  if (backgroundLoaded.rs485) return;
  const grid = document.getElementById('rs485-grid');
  if (!grid) return;
  const count = portCount || rs485PortCountForVariant(_boardVariant);
  for (let n = 0; n < count; n += 1) {
    let card = document.getElementById('rs485c-' + n);
    if (!card) {
      card = document.createElement('div');
      card.id = 'rs485c-' + n;
      grid.appendChild(card);
    }
    if (!card.classList.contains('rs485-port-skeleton')) {
      card.className = 'rs485-port rs485-port-skeleton';
      card.innerHTML = rs485SkeletonCardHtml(n);
    }
  }
  Array.from(grid.children).forEach(function (card) {
    const m = card.id && card.id.match(/^rs485c-(\d+)$/);
    if (m && parseInt(m[1], 10) >= count) card.remove();
  });
  Array.from(grid.children)
    .sort(function (a, b) {
      const na = parseInt((a.id || '').replace('rs485c-', ''), 10);
      const nb = parseInt((b.id || '').replace('rs485c-', ''), 10);
      return na - nb;
    })
    .forEach(function (card) { grid.appendChild(card); });
}

function renderRs485(ports) {
  _lastRs485Ports = ports;
  const grid = document.getElementById('rs485-grid');
  if (!grid) return;
  const seen = new Set();
  ports.slice().sort(function (a, b) { return (a.n | 0) - (b.n | 0); }).forEach(function (p) {
    seen.add('rs485c-' + p.n);
    const absent = p.st === 'absent';
    const disabled = p.st === 'disabled';
    let card = document.getElementById('rs485c-' + p.n);
    if (!card) {
      card = document.createElement('div');
      card.id = 'rs485c-' + p.n;
      grid.appendChild(card);
    }
    card.className = 'rs485-port' + (absent ? ' absent' : '') + (disabled ? ' disabled' : '');

    const errDelta = rs485ErrDelta(p);
    const hasErr = !absent && !disabled && !!(errDelta.fe || errDelta.pe || errDelta.oe);
    const hasTraffic = (p.tx | 0) > 0 || (p.rx | 0) > 0;
    const polling = !absent && !disabled && (!!p.open || hasTraffic);
    let dotClass = 'idle';
    let dotTitle = uiT('Порт свободен, опрос не выполняется');
    if (absent) {
      dotClass = 'nopoll';
      dotTitle = uiT('Интерфейс отсутствует');
    } else if (disabled) {
      dotClass = 'idle';
      dotTitle = uiT('Статистика RS-485 отключена (sa02m_status_blocks.conf)');
    } else if (!polling) {
      dotClass = 'idle';
    } else if (hasErr) {
      dotClass = 'warn';
      dotTitle = uiT('Опрос активен, ошибки линии (FE/PE/OE)');
    } else if ((p.rx | 0) > 0) {
      dotClass = 'on';
      dotTitle = uiT('Опрос активен, ответы устройств в норме');
    } else {
      dotClass = 'noresponse';
      dotTitle = uiT('Опрос активен, нет ответов устройств');
    }

    const tx   = '<span class="rv">' + fmtNum(p.tx) + '</span>';
    const rx   = '<span class="rv">' + fmtNum(p.rx) + '</span>';
    const err  = rs485ErrSlotHtml(errDelta.fe, errDelta.pe, errDelta.oe);
    const devLabel = absent ? uiT('нет опроса') : (disabled ? uiT('статистика отключена') : p.dev);

    card.innerHTML =
      '<div class="rs485-hdr"><span class="rs485-name">' + escHtml(uiT(rs485PortLabel(p.n))) + '</span><span class="rs485-dot ' + dotClass + '" title="' + escHtml(dotTitle) + '"></span></div>' +
      '<div class="rs485-dev">' + escHtml(devLabel) + '</div>' +
      '<div class="rs485-row"><span class="rl">TX</span>' + tx + '</div>' +
      '<div class="rs485-row"><span class="rl">RX</span>' + rx + '</div>' +
      err;
  });
  Array.from(grid.children).forEach(function (card) {
    if (card.id && !seen.has(card.id)) card.remove();
  });
  Array.from(grid.children)
    .sort(function (a, b) {
      const na = parseInt((a.id || '').replace('rs485c-', ''), 10);
      const nb = parseInt((b.id || '').replace('rs485c-', ''), 10);
      return na - nb;
    })
    .forEach(function (card) { grid.appendChild(card); });
}

window.refreshRs485I18n = function () {
  if (_lastRs485Ports && _lastRs485Ports.length) renderRs485(_lastRs485Ports);
};

/* ══════════════════════════════════════════════════════════════════════════
   CONFIG — load current network/time settings into forms
   ══════════════════════════════════════════════════════════════════════════ */
let configLoaded = false;

function browserIanaTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (_) {
    return '';
  }
}

/**
 * Выставить f-tz: сначала таймзона с устройства (если есть), иначе — текущий пояс браузера (ПК).
 * Неизвестные IANA добавляются в список option, чтобы значение можно было применить.
 */
function timeZoneSelectApplyFromDeviceOrBrowser(tzSel, deviceTzRaw) {
  if (!tzSel) return;
  const inList = function (tz) {
    return !!tz && Array.from(tzSel.options).some(function (o) { return o.value === tz; });
  };
  const ensureOpt = function (value, label) {
    if (!value || inList(value)) return;
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label || value;
    tzSel.appendChild(o);
  };

  const deviceTz = deviceTzRaw != null ? String(deviceTzRaw).trim() : '';
  const browserTz = browserIanaTz();

  if (deviceTz) {
    if (inList(deviceTz)) {
      tzSel.value = deviceTz;
      return;
    }
    ensureOpt(deviceTz, deviceTz);
    tzSel.value = deviceTz;
    return;
  }
  if (browserTz) {
    if (inList(browserTz)) {
      tzSel.value = browserTz;
      return;
    }
    ensureOpt(browserTz, browserTz);
    tzSel.value = browserTz;
  }
}

function loadConfig() {
  if (configLoaded) return;
  fetch('/cgi-bin/config.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      configLoaded = true;
      invalidateGaugeArcCache();
      /* end0 */
      const eth0en = document.getElementById('end0-en');
      if (eth0en) eth0en.checked = !!(d.end0 && d.end0.enabled);
      setVal('f-ip',   d.end0?.ip || '');
      setVal('f-mask', d.end0?.netmask || '');
      setVal('f-gw',   d.end0?.gateway || '');
      setVal('f-dns',  d.end0?.dns || '');
      toggleEth0Fields();
      /* end1 */
      const eth1en = document.getElementById('end1-en');
      if (eth1en) eth1en.checked = d.end1?.enabled || false;
      setVal('f-ip1',   d.end1?.ip || '');
      setVal('f-mask1', d.end1?.netmask || '');
      setVal('f-gw1',   d.end1?.gateway || '');
      setVal('f-dns1',  d.end1?.dns || '');
      toggleEth1Fields();
      /* time */
      timeZoneSelectApplyFromDeviceOrBrowser(document.getElementById('f-tz'), d.timezone);
      if (d.datetime) setVal('f-datetime', d.datetime);
      if (document.getElementById('time-sys-disp'))
        setText('time-sys-disp', d.datetime || '—');
      if (document.getElementById('time-rtc-disp')) {
        const r = (d.rtc_datetime && String(d.rtc_datetime).trim()) ? String(d.rtc_datetime).trim() : '';
        setText('time-rtc-disp', r || '—');
      }
    })
    .catch(() => {});
}

function setVal(id, val) { const e = document.getElementById(id); if (e) e.value = val; }

function toggleEth0Fields() {
  const en = document.getElementById('end0-en');
  const wrap = document.getElementById('end0-fields');
  if (en && wrap) {
    wrap.style.opacity = en.checked ? '1' : '.4';
    wrap.style.pointerEvents = en.checked ? '' : 'none';
  }
}

function toggleEth1Fields() {
  const en = document.getElementById('end1-en');
  const wrap = document.getElementById('end1-fields');
  if (en && wrap) {
    wrap.style.opacity = en.checked ? '1' : '.4';
    wrap.style.pointerEvents = en.checked ? '' : 'none';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   FORM SUBMISSION — network / time
   ══════════════════════════════════════════════════════════════════════════ */
function initForms() {
  /* end0 */
  const f0 = document.getElementById('net-form');
  if (f0) f0.addEventListener('submit', e => {
    e.preventDefault();
    const en = document.getElementById('end0-en')?.checked;
    if (en) {
      if (!validateNetForm(f0)) return;
      if (!document.getElementById('f-ip')?.value.trim() || !document.getElementById('f-mask')?.value.trim()) {
        toast('Укажите IP и маску для Ethernet № 1', 'error');
        return;
      }
    }
    submitForm(f0, () => { configLoaded = false; toast('Настройки Ethernet № 1 применены. Перезагрузите сеть.', 'success'); });
  });

  /* end1 */
  const f1 = document.getElementById('net-form-end1');
  if (f1) f1.addEventListener('submit', e => {
    e.preventDefault();
    const en = document.getElementById('end1-en')?.checked;
    if (en && !document.getElementById('f-ip1')?.value.trim()) {
      toast('Укажите IP для Ethernet № 2', 'error'); return;
    }
    submitForm(f1, () => { configLoaded = false; toast('Настройки Ethernet № 2 применены.', 'success'); });
  });

  /* time */
  const ft = document.getElementById('time-form');
  if (ft) ft.addEventListener('submit', e => {
    e.preventDefault();
    submitForm(ft, () => toast('Время/таймзона применены', 'success'));
  });

  /* end0 / end1 toggles */
  const eth0en = document.getElementById('end0-en');
  if (eth0en) eth0en.addEventListener('change', toggleEth0Fields);
  const eth1en = document.getElementById('end1-en');
  if (eth1en) eth1en.addEventListener('change', toggleEth1Fields);
}

function validateNetForm(form) {
  let ok = true;
  const skipEth0Static =
    form.id === 'net-form' && !document.getElementById('end0-en')?.checked;
  form.querySelectorAll('input[pattern]').forEach(inp => {
    if (skipEth0Static && inp.closest('#end0-fields')) return;
    const v = inp.value.trim();
    if (v && !new RegExp('^' + inp.pattern + '$').test(v)) {
      inp.classList.add('invalid'); ok = false;
    } else inp.classList.remove('invalid');
  });
  return ok;
}

/** Разбор ответа apply.cgi (302 + Location: /?status=...) */
function parseApplyRedirect(response) {
  if (response.type === 'opaqueredirect') return { kind: 'unknown' };
  const loc = response.headers.get('Location') || '';
  if (loc.indexOf('error_time') !== -1) return { kind: 'error_time' };
  if (loc.indexOf('error_tz') !== -1) return { kind: 'error_tz' };
  if (loc.indexOf('applied_tz_failed') !== -1) return { kind: 'applied_tz_failed' };
  return { kind: 'ok' };
}

function submitForm(form, onSuccess) {
  const data = new URLSearchParams(new FormData(form));
  const btn = form.querySelector('button[type=submit]');
  if (btn) btn.disabled = true;
  fetch('/cgi-bin/apply.cgi', {
    method: 'POST',
    body: data,
    redirect: 'manual',
    credentials: 'same-origin'
  })
    .then((r) => {
      if (!r.ok && r.status !== 302 && r.status !== 301) {
        toast('Ошибка сервера: ' + r.status, 'error');
        return;
      }
      const pr = parseApplyRedirect(r);
      if (pr.kind === 'error_time') {
        toast(
          'Не удалось установить время. Проверьте формат и /var/log/sa02m_install.log на устройстве.',
          'error'
        );
        return;
      }
      if (pr.kind === 'error_tz') {
        toast('Таймзона не применена.', 'error');
        return;
      }
      if (pr.kind === 'applied_tz_failed') {
        configLoaded = false;
        toast('Настройки применены; таймзона не изменилась.', 'warn', 6000);
        onSuccess && onSuccess();
        return;
      }
      configLoaded = false;
      onSuccess && onSuccess();
    })
    .catch(() => toast('Ошибка отправки', 'error'))
    .finally(() => { if (btn) btn.disabled = false; });
}

/* ══════════════════════════════════════════════════════════════════════════
   APPLICATION SERVICES (Management tab)
   ══════════════════════════════════════════════════════════════════════════ */
function svcCtlDisplayLabel(svc) {
  const id = String((svc && svc.id) || '').trim();
  const lab = String((svc && svc.label) || '').trim();
  if (id === 'codesys') return 'CODESYS';
  if (id === 'mqtt-bridge') return 'MQTT';
  if (id === 'mqtt-telemetry') return 'MQTT мост';
  if (id === 'docker' || lab.toLowerCase() === 'docker') return 'Docker';
  if (id === 'mplc4' || lab.toLowerCase() === 'mplc4') return 'MPLC4';
  if (lab) return lab;
  return unitUiLabel(id);
}

function svcCtlRowState(svc) {
  if (svc.masked || svc.user_disabled) return 'inactive';
  return svc.active || 'inactive';
}

/** Кнопка «Пуск», если службу нужно включить (остановлена или отключена админом). */
function svcCtlWantsStart(svc) {
  if (!svc) return true;
  if (svc.masked || svc.user_disabled) return true;
  return !svcStateIsActive(svc.active);
}

let _lastSvcCtlData = null;

function renderServicesControl(data) {
  _lastSvcCtlData = data;
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  const list = ((data && data.services) || []).filter(function (svc) {
    return svcIsInstalledFlag(svc.installed);
  });
  if (!list.length) {
    host.innerHTML = '<p class="field-hint">' + (window.sa02mI18n ? window.sa02mI18n.t('Нет управляемых служб') : 'Нет управляемых служб') + '</p>';
    return;
  }
  const sorted = list.slice().sort(function (a, b) {
    return compareSvcDisplayName(svcCtlDisplayLabel(a), svcCtlDisplayLabel(b));
  });
  host.innerHTML = '';
  sorted.forEach(function (svc, i) {
    const wantStart = svcCtlWantsStart(svc);
    const action = wantStart ? 'start' : 'stop';
    const btnLabelRu = wantStart ? 'Пуск' : 'Стоп';
    const btnLabel = window.sa02mI18n ? window.sa02mI18n.t(btnLabelRu) : btnLabelRu;
    const btnClass = wantStart
      ? 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-on'
      : 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-off';

    const r = document.createElement('div');
    r.className = 'svc-row svc-ctl-row';
    r.setAttribute('role', 'listitem');

    const name = document.createElement('span');
    name.className = 'name mono';
    name.textContent = svcCtlDisplayLabel(svc);

    const mid = document.createElement('span');
    mid.className = 'svc-ctl-mid';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = btnClass;
    btn.textContent = btnLabel;
    btn.dataset.svcId = svc.id;
    btn.dataset.svcAction = action;
    btn.addEventListener('click', function () { serviceCtlAction(btn); });
    mid.appendChild(btn);

    const badge = document.createElement('span');
    badge.className = 'badge badge-unk';
    const bid = 'svc-ctl-badge-' + i;
    badge.id = bid;

    r.appendChild(name);
    r.appendChild(mid);
    r.appendChild(badge);
    host.appendChild(r);
    svcBadge(bid, svcCtlRowState(svc));
  });
}

window.refreshServicesControlI18n = function () {
  if (_lastSvcCtlData) renderServicesControl(_lastSvcCtlData);
};

function loadServicesControl(forceToast) {
  const host = document.getElementById('svc-ctl-list');
  const btn = document.getElementById('svc-ctl-refresh-btn');
  if (!host) return;
  if (btn) btn.disabled = true;
  if (!host.querySelector('.svc-row')) {
    host.innerHTML = '<p class="field-hint">Загрузка…</p>';
  }
  fetch('/cgi-bin/services_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error === 'unauthorized') throw new Error('нет доступа');
      if (j.error === 'ctl_missing') throw new Error('скрипт управления не установлен на устройстве');
      if (!j.ok && j.error) throw new Error(j.error);
      renderServicesControl(j);
      if (forceToast) toast('Список служб обновлён', 'success');
      setTimeout(function () {
        fetchBackgroundPart('services', applyServicesStatus, true);
        fetchPriorityPart('priority');
      }, 1500);
    })
    .catch((e) => {
      host.innerHTML = '<p class="field-hint log-err">' + escHtml(e && e.message ? e.message : String(e)) + '</p>';
      if (forceToast) toast('Службы: ' + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
}

function serviceCtlAction(btn) {
  const id = btn && btn.dataset ? btn.dataset.svcId : '';
  const action = btn && btn.dataset ? btn.dataset.svcAction : '';
  if (!id || !action) return;
  const label = btn.closest('.svc-row')?.querySelector('.name')?.textContent || id;
  const verb = action === 'stop' ? 'остановить' : 'включить';
  if (!confirm(verb.charAt(0).toUpperCase() + verb.slice(1) + ' «' + label + '»?')) return;
  btn.disabled = true;
  fetch('/cgi-bin/services_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify({ id, action }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
      toast(action === 'stop' ? 'Служба остановлена и отключена' : 'Служба включена', 'success');
      return loadServicesControl(false);
    })
    .catch((e) => {
      toast('Служба: ' + (e && e.message ? e.message : String(e)), 'error');
      loadServicesControl(false);
    })
    .finally(() => { btn.disabled = false; });
}

/* ══════════════════════════════════════════════════════════════════════════
   SYSTEM ACTIONS
   ══════════════════════════════════════════════════════════════════════════ */
function doRestart() {
  if (!confirm('Перезапустить службы nginx и fcgiwrap?')) return;
  fetch('/cgi-bin/restart.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: '{}',
  })
    .then(async (r) => {
      if (!r.ok) {
        const t = await r.text().catch(() => '');
        throw new Error((t || '').trim().slice(0, 120) || ('HTTP ' + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (j && j.ok === false) throw new Error(j.error || 'отклонено');
      return j;
    })
    .then(() => {
      toast('Команда перезапуска отправлена. Если systemd недоступен, смотрите /var/log/sa02m_install.log', 'success', 8000);
      setTimeout(fetchStatus, 2000);
    })
    .catch((e) => {
      toast('Перезапуск служб: ' + (e && e.message ? e.message : String(e)), 'error');
    });
}

function doReboot() {
  if (!confirm('Перезагрузить контроллер?')) return;
  fetch('/cgi-bin/reboot.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: '{}',
  })
    .then(async (r) => {
      if (!r.ok) {
        const t = await r.text().catch(() => '');
        throw new Error((t || '').trim().slice(0, 120) || ('HTTP ' + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (j && j.ok === false) throw new Error(j.error || 'отклонено');
      return j;
    })
    .then(() => {
      toast('Перезагрузка… страница обновится через 60 с', 'info', 65000);
      setTimeout(() => location.reload(), 60000);
    })
    .catch((e) => {
      toast('Перезагрузка не запущена: ' + (e && e.message ? e.message : String(e)), 'error');
    });
}

function doLogout() {
  window.location.href = '/cgi-bin/logout.cgi';
}

/* ══════════════════════════════════════════════════════════════════════════
   LOG
   ══════════════════════════════════════════════════════════════════════════ */
function renderLogText(box, text) {
  box.innerHTML = text.split('\n').map(line => {
    if (/error|ошибк|failed|timeout|timed out|broken pipe|banner exchange|reset|refused/i.test(line)) {
      return '<span class="log-err">' + escHtml(line) + '</span>';
    }
    if (/warn|degrad|inactive|missing|unavailable/i.test(line)) {
      return '<span class="log-warn">' + escHtml(line) + '</span>';
    }
    if (/ok|успешн|applied|reboot|started|active|listening/i.test(line)) {
      return '<span class="log-ok">' + escHtml(line) + '</span>';
    }
    return escHtml(line);
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}

function loadLog() {
  const box = document.getElementById('log-box');
  if (!box) return;
  box.classList.remove('log-box-ssh-debug');
  fetch('/cgi-bin/log.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.text())
    .then(t => renderLogText(box, t))
    .catch(() => { if (box) box.textContent = 'Не удалось загрузить журнал'; });
}

function loadSshDebug() {
  const box = document.getElementById('log-box');
  if (!box) return;
  box.classList.remove('log-box-ssh-debug');
  box.textContent = 'Загрузка SSH-диагностики… (до ~2 мин)';
  fetch('/cgi-bin/ssh_debug.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(t => {
      const trimmed = (t || '').replace(/^\uFEFF/, '');
      if (/^\s*</.test(trimmed)) {
        box.classList.add('log-box-ssh-debug');
        box.replaceChildren();
        const frame = document.createElement('iframe');
        frame.className = 'ssh-debug-iframe';
        frame.title = 'SSH-диагностика';
        frame.setAttribute('sandbox', 'allow-same-origin');
        frame.srcdoc = trimmed;
        box.appendChild(frame);
      } else {
        renderLogText(box, t);
      }
    })
    .catch(() => {
      if (!box) return;
      box.classList.remove('log-box-ssh-debug');
      box.textContent = 'Не удалось загрузить SSH-диагностику';
    });
}

/* ══════════════════════════════════════════════════════════════════════════
   IP INPUT VALIDATION (blur)
   ══════════════════════════════════════════════════════════════════════════ */
function initValidation() {
  document.querySelectorAll('input[pattern]').forEach(inp => {
    inp.addEventListener('blur', () => {
      const v = inp.value.trim();
      if (v && !new RegExp('^' + inp.pattern + '$').test(v))
        inp.classList.add('invalid');
      else
        inp.classList.remove('invalid');
    });
    inp.addEventListener('input', () => inp.classList.remove('invalid'));
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   STATUS URL TOAST (after form redirect)
   ══════════════════════════════════════════════════════════════════════════ */
function handleUrlStatus() {
  const params = new URLSearchParams(window.location.search);
  const s = params.get('status');
  if (!s) return;
  const map = {
    applied:             ['Настройки применены', 'success'],
    applied_tz_failed:   ['Время применено; таймзона не изменилась', 'warn'],
    error_tz:            ['Ошибка: неверная таймзона', 'error'],
    error_time:          ['Ошибка: не удалось установить время', 'error'],
    services:            ['Службы перезапущены', 'success'],
    reboot:              ['Перезагрузка запущена…', 'info'],
  };
  const [msg, type] = map[s] || ['Статус: ' + s, 'info'];
  toast(msg, type);
  history.replaceState(null, '', window.location.pathname);
}

/* ══════════════════════════════════════════════════════════════════════════
   TIME — синхронизация с браузером (ПК)
   ══════════════════════════════════════════════════════════════════════════ */
function pad2(n) { return n < 10 ? '0' + n : String(n); }

function fmtLocalDateTimeForDevice(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
    pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** @param {boolean} applyNow — если true, сразу POST в apply.cgi */
function syncTimeFromPC(applyNow) {
  const ft = document.getElementById('time-form');
  if (!ft) return;
  const tz = browserIanaTz();
  if (tz) {
    const sel = document.getElementById('f-tz');
    if (sel) {
      const known = Array.from(sel.options).some(o => o.value === tz);
      if (known) sel.value = tz;
      else {
        const o = document.createElement('option');
        o.value = tz;
        o.textContent = tz + ' (этот ПК)';
        sel.appendChild(o);
        sel.value = tz;
      }
    }
  }
  setVal('f-datetime', fmtLocalDateTimeForDevice(new Date()));
  if (!applyNow) {
    toast('Дата и время подставлены с этого ПК. При необходимости нажмите «Применить вручную».', 'success');
    return;
  }
  const data = new URLSearchParams(new FormData(ft));
  const btn = ft.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  fetch('/cgi-bin/apply.cgi', {
    method: 'POST',
    body: data,
    redirect: 'manual',
    credentials: 'same-origin'
  })
    .then((r) => {
      if (!r.ok && r.status !== 302 && r.status !== 301) {
        toast('Ошибка сервера: ' + r.status, 'error');
        return;
      }
      const pr = parseApplyRedirect(r);
      if (pr.kind === 'error_time') {
        toast('Не удалось установить время с этого ПК.', 'error');
        return;
      }
      if (pr.kind === 'error_tz') {
        toast('Таймзона не применена.', 'error');
        return;
      }
      if (pr.kind === 'applied_tz_failed') {
        configLoaded = false;
        toast('Время синхронизировано; таймзона не изменилась.', 'warn', 6000);
        setTimeout(loadConfig, 400);
        return;
      }
      configLoaded = false;
      toast('Время синхронизировано с этим ПК', 'success');
      setTimeout(loadConfig, 400);
    })
    .catch(() => toast('Ошибка отправки', 'error'))
    .finally(() => { if (btn) btn.disabled = false; });
}

function exportInstallLog() {
  window.location.href = '/cgi-bin/log_export.cgi';
}

/* ══════════════════════════════════════════════════════════════════════════
   WEB CREDENTIALS
   ══════════════════════════════════════════════════════════════════════════ */
function initWebCredsForm() {
  const form = document.getElementById('web-creds-form');
  if (!form) return;
  form.addEventListener('submit', e => {
    e.preventDefault();
    const body = new URLSearchParams(new FormData(form));
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    fetch('/cgi-bin/web_creds.cgi', {
      method: 'POST',
      body,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(r => r.json())
      .then(j => {
        if (j.ok) {
          toast('Сохранено. При следующем входе используйте новый логин и пароль.', 'success', 6500);
          const cur = document.getElementById('wc-cur');
          const p1 = document.getElementById('wc-p1');
          const p2 = document.getElementById('wc-p2');
          if (cur) cur.value = '';
          if (p1) p1.value = '';
          if (p2) p2.value = '';
        } else {
          const map = {
            unauthorized: 'Сессия истекла. Войдите снова.',
            wrong_password: 'Неверный текущий пароль',
            mismatch: 'Новый пароль и повтор не совпадают',
            bad_username: 'Недопустимый логин (латиница, цифры, . _ - , до 32 символов)',
            bad_password_len: 'Длина пароля 4–128 символов',
            bad_password_char: 'Пароль не может содержать символы \' $ ` ; | & < > ( )',
            no_password: 'Укажите новый пароль',
            no_user: 'Укажите логин',
            no_current: 'Укажите текущий пароль',
            no_auth_file: 'Файл учётных данных на устройстве недоступен',
            save_failed: 'Не удалось сохранить настройки',
          };
          toast(map[j.error] || ('Ошибка: ' + (j.error || 'unknown')), 'error');
        }
      })
      .catch(() => toast('Нет связи с сервером', 'error'))
      .finally(() => { if (btn) btn.disabled = false; });
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   THEME (кнопка в шапке, как gw-lwip)
   ══════════════════════════════════════════════════════════════════════════ */
const THEME_MOON_SVG = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 9A5.5 5.5 0 1 1 7 3a4 4 0 0 0 6 6z"/></svg>';
const THEME_SUN_SVG = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><line x1="8" y1="1" x2="8" y2="2.5"/><line x1="8" y1="13.5" x2="8" y2="15"/><line x1="1" y1="8" x2="2.5" y2="8"/><line x1="13.5" y1="8" x2="15" y2="8"/><line x1="3.1" y1="3.1" x2="4.1" y2="4.1"/><line x1="11.9" y1="11.9" x2="12.9" y2="12.9"/><line x1="12.9" y1="3.1" x2="11.9" y2="4.1"/><line x1="4.1" y1="11.9" x2="3.1" y2="12.9"/></svg>';

function themeTargetLabel(isDark) {
  const ru = isDark ? 'Светлая' : 'Тёмная';
  return window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
}

function updateThemeBtn() {
  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const icon = document.getElementById('theme-toggle-icon');
  const label = document.getElementById('theme-toggle-label');
  const btn = document.getElementById('theme-toggle');
  if (icon) icon.innerHTML = isDark ? THEME_MOON_SVG : THEME_SUN_SVG;
  if (label) label.textContent = themeTargetLabel(isDark);
  if (btn) {
    const en = window.sa02mI18n && window.sa02mI18n.lang === 'en';
    btn.title = en ? 'Toggle theme' : 'Переключить тему';
    btn.setAttribute('aria-label', en ? 'Toggle theme' : 'Переключить тему');
  }
}

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
    theme = 'dark';
  }
  try { localStorage.setItem('sa02m-theme', theme); } catch (_) {}
  updateThemeBtn();
}

function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  let stored = 'dark';
  try { stored = localStorage.getItem('sa02m-theme') || 'dark'; } catch (_) {}
  applyTheme(stored === 'light' ? 'light' : 'dark');
  btn.addEventListener('click', () => {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    applyTheme(isLight ? 'dark' : 'light');
  });
}

window.updateThemeBtn = updateThemeBtn;

/* ══════════════════════════════════════════════════════════════════════════
   HARDWARE VARIANT
   ══════════════════════════════════════════════════════════════════════════ */
const VARIANT_STATUS_AUTO_CLEAR_MS = 3000;
let _variantStatusTimer = null;
let _variantStatusGen = 0;

function cancelVariantStatusAutoClear() {
  if (_variantStatusTimer) {
    clearTimeout(_variantStatusTimer);
    _variantStatusTimer = null;
  }
  _variantStatusGen += 1;
}

function scheduleVariantStatusAutoClear(el) {
  cancelVariantStatusAutoClear();
  const gen = _variantStatusGen;
  _variantStatusTimer = setTimeout(function () {
    _variantStatusTimer = null;
    if (_variantStatusGen !== gen || !el) return;
    el.textContent = '';
  }, VARIANT_STATUS_AUTO_CLEAR_MS);
}

function variantDisplayLabel(variant) {
  const map = {
    'sa02m-1eth': 'СА-02м-1eth',
    'sa02m-2eth': 'СА-02м-2-2eth',
  };
  return map[variant] || String(variant || '').replace(/^sa02m-/i, 'СА-02м-');
}

function formatComPortCount(n) {
  n = Math.abs(parseInt(n, 10) || 0);
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return n + ' COM-порт';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return n + ' COM-порта';
  return n + ' COM-портов';
}

async function loadVariant() {
  try {
    const r = await fetch('/cgi-bin/variant.cgi');
    if (!r.ok) return;
    const d = await r.json();
    const sel = document.getElementById('hw-variant-select');
    if (sel) sel.value = d.variant || 'sa02m-1eth';
    applyVariantVisibility(d.variant || 'sa02m-1eth');
  } catch (_) {}
}

async function applyVariant() {
  const sel = document.getElementById('hw-variant-select');
  const status = document.getElementById('variant-status');
  if (!sel || !status) return;
  const variant = sel.value;
  cancelVariantStatusAutoClear();
  status.textContent = 'Применяю…';
  status.style.color = 'var(--text-sec)';
  try {
    const r = await fetch('/cgi-bin/variant.cgi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'variant=' + encodeURIComponent(variant)
    });
    const d = await r.json();
    if (d.ok) {
      status.textContent = '\u2713 Применено: ' + variantDisplayLabel(d.variant) + ', ' + formatComPortCount(d.serial_count);
      status.style.color = 'var(--green, #4caf50)';
      scheduleVariantStatusAutoClear(status);
      await loadVariant();
    } else {
      status.textContent = '\u2717 Ошибка: ' + (d.error || 'неизвестно');
      status.style.color = 'var(--red, #f44336)';
    }
  } catch (e) {
    status.textContent = '\u2717 ' + e.message;
    status.style.color = 'var(--red, #f44336)';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   INIT
   ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  const verEl = document.getElementById('app-version');
  if (verEl) verEl.textContent = 'v' + APP_VERSION;

  initNav();
  applyVariantVisibility('sa02m-1eth');
  initDashboardPlaceholders();
  initForms();
  initValidation();
  initWebCredsForm();
  initThemeToggle();
  handleUrlStatus();
  hydratePriorityWarmup();
  bindUsbPowerResetButton();
  loadVariant();
  bindStatusPollingLifecycle();
  initStatusPolling();

  /* Expose globals for inline onclick */
  window.setHw    = setHw;
  window.doRestart = doRestart;
  window.doReboot  = doReboot;
  window.loadServicesControl = loadServicesControl;
  window.serviceCtlAction = serviceCtlAction;
  window.doLogout  = doLogout;
  window.loadLog   = loadLog;
  window.loadSshDebug = loadSshDebug;
  window.syncTimeFromPC = syncTimeFromPC;
  window.exportInstallLog = exportInstallLog;
  window.toggleStorageAutoFormat = toggleStorageAutoFormat;

  document.getElementById('apply-variant-btn')?.addEventListener('click', applyVariant);
});
