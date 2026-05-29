/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/** Версия веб-интерфейса (синхронизируйте с install.sh). */
const APP_VERSION = '1.0.3.7';

/* ── Auth guard ──────────────────────────────────────────────────────────── */
(function () {
  const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('session_token='));
  if (!hasCookie && !window.location.pathname.includes('login')) {
    window.location.replace('/login.html');
  }
})();

/* ── Navigation ──────────────────────────────────────────────────────────── */
function initNav() {
  document.querySelectorAll('.nav-item[data-tab]').forEach(el => {
    el.addEventListener('click', () => {
      const tab = el.dataset.tab;
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      el.classList.add('active');
      const pane = document.getElementById('tab-' + tab);
      if (pane) pane.classList.add('active');
      if (tab === 'system') {
        loadLog();
        fetchSystemWidget();
        loadWebUpdateStatus();
        loadServicesControl(false);
      }
      if (tab === 'network' || tab === 'time') loadConfig();
      if (tab === 'flasher' && window.flasherInit) window.flasherInit();
      if (tab === 'mqtt' && window.mqttTabInit) window.mqttTabInit();
      if (tab !== 'mqtt' && window.mqttTabDestroy) window.mqttTabDestroy();
      if (tab === 'gateway' && window.gatewayInit) window.gatewayInit();
      if (tab !== 'gateway' && window.gatewayDestroy) window.gatewayDestroy();
    });
  });
}

/* ── Toast notifications ──────────────────────────────────────────────────── */
function toast(msg, type = 'info', ms = 4000) {
  let area = document.getElementById('toast-area');
  if (!area) {
    area = document.createElement('div');
    area.id = 'toast-area';
    area.className = 'toast-area';
    document.body.appendChild(area);
  }
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  area.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .4s'; setTimeout(() => t.remove(), 400); }, ms);
}

/* ── Utilities ────────────────────────────────────────────────────────────── */
function fmtKB(kb) {
  kb = parseInt(kb) || 0;
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + ' ГБ';
  if (kb >= 1024)    return (kb / 1024).toFixed(0) + ' МБ';
  return kb + ' КБ';
}
function fmtBytes(b) {
  b = parseInt(b) || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' ГБ';
  if (b >= 1048576)    return (b / 1048576).toFixed(1) + ' МБ';
  if (b >= 1024)       return (b / 1024).toFixed(1) + ' КБ';
  return b + ' Б';
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
  if (d) return d + 'д ' + h + 'ч ' + m + 'м';
  if (h) return h + 'ч ' + m + 'м';
  return m + 'м ' + (s % 60) + 'с';
}

/** Компактный аптайм для колонки «Службы» (короче, без секунд при наличии минут). */
function fmtUptimeSvc(s) {
  s = parseInt(s, 10) || 0;
  if (s <= 0) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'д\u00a0' + h + 'ч';
  if (h) return h + 'ч\u00a0' + m + 'м';
  if (m) return m + 'м';
  return s + 'с';
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
      el.textContent = '<1м';
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
  el.textContent = ok ? 'Активен' : 'Неактивен';
  el.className = 'badge ' + (ok ? 'badge-ok' : 'badge-err');
}

/** Короткое имя unit для подписи (mplc4 вместо mplc4.service). */
function unitUiLabel(name) {
  return String(name || '').replace(/\.(service|socket)$/i, '');
}

const SVC_WIDGET_MAX_ROWS = 6;

/** Виджет «Службы»: nginx, fcgiwrap, при наличии — MPLC/опрос, затем optional_services (всего ≤ 6). Без пустых строк. */
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

  pushRow('nginx', d.svc_nginx_uptime_s, d.svc_nginx);
  pushRow('fcgiwrap', d.svc_fcgiwrap_uptime_s, d.svc_fcgiwrap);
  if (d.svc_mosquitto && d.svc_mosquitto !== 'unknown')
    pushRow('mosquitto', d.svc_mosquitto_uptime_s, d.svc_mosquitto);
  if (d.svc_bridge && d.svc_bridge !== 'unknown')
    pushRow('MQTT', d.svc_bridge_uptime_s, d.svc_bridge, {
      title: 'Modbus→MQTT мост (sa02m-modbus-mqtt)',
      mono: true
    });

  const mu = (d.mplc_unit != null && String(d.mplc_unit).trim())
    ? unitUiLabel(String(d.mplc_unit).trim())
    : '';
  const mplcOn = svcStateIsActive(d.mplc_status);
  const mplcSt = normSvcState(d.mplc_status);
  /** Подпись строки: всегда MPLC4 для mplc/mplc4/неизвестного unit; иначе — имя unit (редкие варианты). */
  const mplcRowLabel =
    !mu || mu === 'mplc4' || mu === 'mplc' ? 'MPLC4' : mu;
  /** MPLC4: активна по systemd/pgrep mplc*, или неактивна (не скрывать «Неактивен»). */
  const showMplcRow = (mplcOn || !!mu || (mplcSt && mplcSt !== 'unknown')) && rows.length < SVC_WIDGET_MAX_ROWS;
  if (showMplcRow) {
    pushRow(mplcRowLabel, d.mplc_uptime_s, d.mplc_status, {
      mono: true,
      title: 'MPLC4 — опрос линии RS-485 (systemd)',
      tight: false
    });
  }

  if (Array.isArray(d.optional_services) && rows.length < SVC_WIDGET_MAX_ROWS) {
    for (const s of d.optional_services) {
      if (rows.length >= SVC_WIDGET_MAX_ROWS) break;
      const id = (s && s.id) ? unitUiLabel(String(s.id)) : '';
      if (!id) continue;
      const disp = (s && s.label && String(s.label).trim()) ? String(s.label).trim() : id;
      pushRow(disp, s.uptime_s, s.status, { mono: true });
    }
  }

  rows.forEach(function (row, i) {
    const r = document.createElement('div');
    r.className = 'svc-row' + (row.tight ? ' svc-row-tight' : '');
    const name = document.createElement('span');
    name.className = 'name' + (row.mono ? ' mono' : '');
    name.textContent = row.label;
    if (row.title) name.title = row.title;
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
/** После hw_set запрашиваем main снова; если предыдущий main ещё в полёте — не терять повтор. */
let mainBundleRefreshQueued = false;
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
const _prevRs = {};
const statusFailures = {
  priority: 0,
  main: 0,
  rs485: 0
};
const statusPauseUntil = { main: 0, rs485: 0 };
const STATUS_TIMEOUT_MS = {
  priority: 3000,
  /** Должен быть ≥ типичного времени status.cgi?part=main (I2C, RTC, сеть) и запас к fastcgi_read_timeout. */
  main: 14000,
  rs485: 4000
};

function statusRequestTimeout(part) {
  return STATUS_TIMEOUT_MS[part] || 3500;
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
  const needPauseRs = part === 'rs485' && statusFailures[part] >= 3;
  if (needPauseMain) {
    setStatusPartPause(part, 25000);
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
  if (part === 'main' || part === 'rs485') {
    statusPauseUntil[part] = 0;
  }
  if (part === 'main') {
    const el = document.getElementById('dashboard-poll-alert');
    if (el) {
      el.style.display = 'none';
      el.textContent = '';
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
    val.textContent = 'НЕ УСТАНОВЛЕН';
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
  setText(base + '-sub', 'из ' + fmtKB(total));
  setText(base + '-pct', pct + '%');
  setText(base + '-free', 'свободно ' + fmtKB(free));
  const bar = document.getElementById(base + '-bar');
  if (bar) {
    bar.style.width = pct + '%';
    bar.style.background = threshColor(pct, 70, 90);
  }
}

function applyPriorityStatus(d) {
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
    setText('ram-sub', 'из ' + fmtKB(d.ram_total_kb));
    setText('ram-pct', d.ram_pct + '%');
    setText('ram-free', 'свободно ' + fmtKB(d.ram_free_kb));
    const ramBar = document.getElementById('ram-bar');
    if (ramBar) {
      ramBar.style.width = d.ram_pct + '%';
      ramBar.style.background = threshColor(d.ram_pct, 70, 90);
    }
  }

  /* SWAP */
  if (d.swap_total_kb > 0) {
    const sb = document.getElementById('swap-block');
    if (sb) sb.style.display = 'block';
    setText('swap-pct', d.swap_pct + '%');
    setText('swap-lbl', fmtKB(d.swap_used_kb) + ' / ' + fmtKB(d.swap_total_kb));
    const swapBar = document.getElementById('swap-bar');
    if (swapBar) {
      swapBar.style.width = d.swap_pct + '%';
      swapBar.style.background = d.swap_pct > 80 ? cssVar('--meter-red') : cssVar('--meter-orange');
    }
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
        tempHint.textContent = tc >= 80
          ? 'Температура выше нормы'
          : 'Температура в норме';
      }
    }
  }

  /* Disk */
  if (d.disk_used_kb !== undefined) {
    setText('disk-val', fmtKB(d.disk_used_kb));
    setText('disk-sub', 'из ' + fmtKB(d.disk_total_kb));
    setText('disk-pct', d.disk_pct + '%');
    setText('disk-free', 'свободно ' + fmtKB(d.disk_free_kb));
    const diskBar = document.getElementById('disk-bar');
    if (diskBar) {
      diskBar.style.width = d.disk_pct + '%';
      diskBar.style.background = threshColor(d.disk_pct, 70, 90);
    }
  }
}

function applyStorageStatus(d) {
  if (d.disk_io_read_b !== undefined)
    setText('disk-io', 'R ' + fmtBytes(d.disk_io_read_b) + ' / W ' + fmtBytes(d.disk_io_write_b));
  if (d.usb_modem_present) {
    applyUsbModem(d);
  } else {
    var sv = document.getElementById('usb-storage-view');
    var mv = document.getElementById('usb-modem-view');
    var tt = document.getElementById('usb-widget-title');
    if (sv) sv.style.display = '';
    if (mv) mv.style.display = 'none';
    if (tt) tt.textContent = 'USB-накопитель';
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
  if (tt) tt.textContent = 'USB-модем';

  var stateEl = document.getElementById('usb-modem-state-val');
  if (stateEl) {
    var st = d.usb_modem_state || '';
    if (st === 'up') {
      stateEl.textContent = 'Подключён';
      stateEl.className = 'widget-val on';
    } else if (st === 'down' || st === 'unknown') {
      stateEl.textContent = 'Нет сети';
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
  setText('usb-modem-traffic', '↓ ' + rx + ' / ↑ ' + tx);
}

function applyTimeStatus(d) {
  if (d.datetime_sys) setText('time-sys-disp', d.datetime_sys);
  if (document.getElementById('time-rtc-disp') && d.rtc_datetime !== undefined) {
    const r = (d.rtc_datetime && String(d.rtc_datetime).trim()) ? String(d.rtc_datetime).trim() : '';
    setText('time-rtc-disp', r || '—');
  }
}

function applyUptimeStatus(d) {
  setText('uptime-val', d.uptime_str || fmtUptime(d.uptime_sec));
}

/** missingFallback — если operstate нет в JSON (старый status.cgi), не показывать «Нет адаптера» для реального eth0. */
function applyEthIfaceState(spanId, operstate, missingFallback) {
  const el = document.getElementById(spanId);
  if (!el) return;
  let v = operstate;
  if (v === undefined || v === '') {
    v = missingFallback !== undefined ? missingFallback : 'absent';
  }
  const s = String(v).trim().toLowerCase();
  let text; let cls;
  if (s === 'absent') {
    text = '● Нет адаптера';
    cls = 'absent';
  } else if (s === 'up') {
    text = '● Есть линк';
    cls = 'up';
  } else {
    text = '● Нет линка';
    cls = 'down';
  }
  el.textContent = text;
  el.className = 'eth-state eth-state-prominent ' + cls;
}

function applyNetworkStatus(d) {
  applyEthIfaceState('eth0-state', d.eth0_operstate, 'unknown');
  applyEthIfaceState('eth1-state', d.eth1_operstate);
  setText('eth0-traf', 'RX ' + fmtBytes(d.net_rx_bytes || 0) + '  TX ' + fmtBytes(d.net_tx_bytes || 0));
  setText('eth1-traf', 'RX ' + fmtBytes(d.net1_rx_bytes || 0) + '  TX ' + fmtBytes(d.net1_tx_bytes || 0));
  const ip0 = (d.eth0_ip !== undefined && d.eth0_ip !== null) ? String(d.eth0_ip).trim() : '';
  const ip1 = (d.eth1_ip !== undefined && d.eth1_ip !== null) ? String(d.eth1_ip).trim() : '';
  const m0 = (d.eth0_mode !== undefined && d.eth0_mode !== null) ? String(d.eth0_mode).trim().toLowerCase() : '';
  const m1 = (d.eth1_mode !== undefined && d.eth1_mode !== null) ? String(d.eth1_mode).trim().toLowerCase() : '';
  setText('eth0-ip', ip0 ? ip0 : (m0 === 'dhcp' ? 'DHCP' : '—'));
  setText('eth1-ip', ip1 ? ip1 : (m1 === 'dhcp' ? 'DHCP' : '—'));
  if (d.ip) setText('tb-ip', d.ip);
}

function applyLoadStatus(d) {
  setText('load-1',  d.load_1  || '—');
  setText('load-5',  d.load_5  || '—');
  setText('load-15', d.load_15 || '—');
  setText('proc-info', 'Процессов: ' + (d.proc_running || 0) + ' / ' + (d.proc_total || 0));
  if (d.cpu_freq_mhz) {
    const thr = d.cpu_throttle ? ' (' + d.cpu_throttle + '%)' : '';
    setText('cpu-freq', d.cpu_freq_mhz + ' МГц' + thr);
  }
}

function applySystemStatus(d) {
  if (d.board)     setText('board-info',  d.board);
  if (d.cpu_model) setText('cpu-model',   d.cpu_model);
  if (d.kernel)    setText('kernel-info', 'Ядро: ' + d.kernel);

  const btn = document.getElementById('storage-format-toggle');
  const lbl = document.getElementById('storage-format-toggle-label');
  if (btn) {
    const installed = d.storage_mount_installed === 1;
    const on = d.storage_auto_format === 1;
    btn.dataset.storageOn = on ? '1' : '0';
    if (!installed) {
      btn.disabled = true;
      if (lbl) lbl.textContent = 'НЕ УСТАНОВЛЕНО';
      btn.className = 'btn btn-danger';
      btn.title = 'Нет storage-mount — выполните установку системы (install.sh)';
    } else {
      btn.disabled = false;
      btn.title = 'Нажмите, чтобы переключить: при выкл. раздел без ФС или NTFS не форматируется';
      if (lbl) lbl.textContent = on ? 'ВКЛЮЧЕНО' : 'ОТКЛЮЧЕНО';
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

function applyServicesStatus(d) {
  renderServicesDynamic(d);
}

function applyHardwareStatus(d) {
  const hint = document.getElementById('hw-hint');
  if (hint) {
    if (d.hw_i2c_busy === 1) {
      hint.textContent = 'ШИНА I2C ЗАНЯТА ДРУГОЙ СЛУЖБОЙ';
    } else if (d.hw_i2c_expander_absent === 1) {
      hint.textContent = 'НЕТ СВЯЗИ С МИКРОСХЕМОЙ РАСШИРЕНИЯ I2C';
    } else if (d.hw_configured) {
      hint.textContent = 'Аппаратные каналы настроены (/etc/sa02m_hw.conf)';
    } else {
      hint.textContent = 'Каналы не заданы — отредактируйте /etc/sa02m_hw.conf';
    }
  }
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
  widgetBusy.priority = true;
  fetchWithTimeout('/cgi-bin/status.cgi?part=priority', {
    cache: 'no-store',
    credentials: 'same-origin'
  }, statusRequestTimeout('priority'))
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(d => {
      if (d.error) return;
      applyPriorityStatus(d);
      if (persist) ['cpu', 'temp', 'ram', 'disk'].forEach((part) => writePriorityWarmupPart(part, d));
      noteStatusSuccess('priority');
    })
    .catch((e) => { noteStatusFailure('priority', e); })
    .finally(() => { widgetBusy.priority = false; });
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
  applyStorageStatus(d);
  applyTimeStatus(d);
  applyUptimeStatus(d);
  applyNetworkStatus(d);
  applyLoadStatus(d);
  applySystemStatus(d);
  applyServicesStatus(d);
  applyHardwareStatus(d);
  ['main', 'storage', 'time', 'uptime', 'network', 'load', 'system', 'services', 'hardware'].forEach((part) => {
    backgroundLoaded[part] = true;
  });
}

/** Поколение опроса main: увеличивается после hw_set, чтобы отложенный JSON не затирал UI свежими кнопками. */
let mainStatusEpoch = 0;
function bumpMainStatusEpoch() {
  mainStatusEpoch++;
}

function fetchMainBundle(force) {
  if (backgroundBusy.main) {
    if (force) mainBundleRefreshQueued = true;
    return;
  }
  if (!force && isStatusPartPaused('main')) return;
  backgroundBusy.main = true;
  const epochAtStart = mainStatusEpoch;
  const mainUrl = '/cgi-bin/status.cgi?part=main' + (force ? '&no_cache=1' : '');
  fetchWithTimeout(mainUrl, {
    cache: 'no-store',
    credentials: 'same-origin'
  }, statusRequestTimeout('main'))
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(d => {
      if (epochAtStart !== mainStatusEpoch) return;
      if (d.error) return;
      applyMainStatusBundle(d);
      noteStatusSuccess('main');
    })
    .catch((e) => { noteStatusFailure('main', e); })
    .finally(() => {
      backgroundBusy.main = false;
      if (mainBundleRefreshQueued) {
        mainBundleRefreshQueued = false;
        fetchMainBundle(true);
      }
    });
}

function fetchBackgroundPart(part, applyFn) {
  if (part !== 'rs485') {
    fetchMainBundle();
    return;
  }
  if (backgroundBusy[part]) return;
  if (isStatusPartPaused(part)) return;
  backgroundBusy[part] = true;
  fetchWithTimeout('/cgi-bin/status.cgi?part=' + encodeURIComponent(part), {
    cache: 'no-store',
    credentials: 'same-origin'
  }, statusRequestTimeout(part))
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(d => {
      if (d.error) return;
      applyFn(d);
      backgroundLoaded[part] = true;
      noteStatusSuccess(part);
    })
    .catch((e) => { noteStatusFailure(part, e); })
    .finally(() => { backgroundBusy[part] = false; });
}

function fetchStorageWidget() {
  fetchMainBundle();
}

function fetchTimeWidget() {
  fetchMainBundle();
}

function fetchUptimeWidget() {
  fetchMainBundle();
}

function fetchNetworkWidget() {
  fetchMainBundle();
}

function fetchLoadWidget() {
  fetchMainBundle();
}

function fetchSystemWidget() {
  fetchMainBundle();
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

function applyWebUpdateCheckUI(j) {
  if (!j || typeof j !== 'object') return;
  if (j.error === 'unauthorized') return;
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
    st.textContent = 'Есть обновление.';
    st.classList.add('is-warn');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = false;
  } else if (ua === false) {
    st.textContent = 'Обновлений нет.';
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
  fetchMainBundle();
}

function fetchStatusMain() {
  fetchMainBundle();
}

function fetchStatusRs485() {
  fetchBackgroundPart('rs485', applyRs485Status);
}

function fetchStatus() {
  fetchPriorityPart('priority');
  fetchStatusMain();
  fetchStatusRs485();
}

function allBackgroundWidgetsLoaded() {
  return Object.values(backgroundLoaded).every(Boolean);
}

function bootstrapBackgroundWidgets() {
  let attempts = 0;
  const timer = setInterval(() => {
    if (allBackgroundWidgetsLoaded() || attempts >= 12) {
      clearInterval(timer);
      return;
    }
    fetchStatusMain();
    fetchStatusRs485();
    attempts += 1;
  }, 1200);
}

/* ══════════════════════════════════════════════════════════════════════════
   HW GPIO CONTROL
   ══════════════════════════════════════════════════════════════════════════ */
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
    el.textContent = 'н/д';
    el.className = 'hw-status-val na';
    syncHwButtonStyles(channel, -1);
    return;
  }
  el.textContent = nv ? words[1] : words[0];
  el.className = 'hw-status-val ' + (nv ? 'on' : 'off');
  syncHwButtonStyles(channel, nv);
}

function setHwChannelBtns(channel, enabled) {
  document.querySelectorAll('.hw-btns[data-hw-ch="' + channel + '"] button').forEach(function (b) {
    b.disabled = !enabled;
  });
}

/** Питание USB: удержание «Сброс» = 0 на линии, отпускание = 1 (без фиксации). */
let usbPowerResetHeld = false;

function bindUsbPowerResetButton() {
  const btn = document.getElementById('hw-usb-reset-btn');
  if (!btn || btn.dataset.usbResetBound === '1') return;
  btn.dataset.usbResetBound = '1';

  const restore = () => {
    if (!usbPowerResetHeld) return;
    usbPowerResetHeld = false;
    setHw('usb_power', 1, { quiet: true });
  };

  btn.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    usbPowerResetHeld = true;
    setHw('usb_power', 0, { quiet: true });
  });
  btn.addEventListener('pointerup', restore);
  btn.addEventListener('pointerleave', restore);
  btn.addEventListener('pointercancel', restore);

  if (!window.__sa02mUsbPowerBlurBound) {
    window.__sa02mUsbPowerBlurBound = true;
    window.addEventListener('blur', () => {
      if (!usbPowerResetHeld) return;
      usbPowerResetHeld = false;
      setHw('usb_power', 1, { quiet: true });
    });
  }
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
        fetchMainBundle(true);
        if (!quiet) toast('Применено', 'success');
      }
      else if (j.error === 'gpio_not_configured') toast('Канал не настроен в /etc/sa02m_hw.conf', 'error');
      else if (j.error === 'i2c_busy') toast('Шина I2C занята другой службой', 'error');
      else if (j.error === 'i2c_tools_missing') toast('На устройстве нет i2c-tools', 'error');
      else toast('Ошибка: ' + (j.error || 'unknown'), 'error');
    })
    .catch(() => toast('Нет связи с сервером', 'error'));
}

/* ══════════════════════════════════════════════════════════════════════════
   RS-485 CARDS
   ══════════════════════════════════════════════════════════════════════════ */
function renderRs485(ports) {
  const grid = document.getElementById('rs485-grid');
  if (!grid) return;
  const seen = new Set();
  ports.forEach(p => {
    seen.add('rs485c-' + p.n);
    const absent = p.st === 'absent';
    const prev   = _prevRs[p.n] || { tx: p.tx, rx: p.rx };
    const actNow = !absent && (p.tx !== prev.tx || p.rx !== prev.rx);
    _prevRs[p.n] = { tx: p.tx, rx: p.rx };

    let card = document.getElementById('rs485c-' + p.n);
    if (!card) {
      card = document.createElement('div');
      card.id = 'rs485c-' + p.n;
      grid.appendChild(card);
    }
    card.className = 'rs485-port' + (absent ? ' absent' : '');
    if (actNow) {
      card.classList.add('act');
      clearTimeout(card._actTimer);
      card._actTimer = setTimeout(() => card.classList.remove('act'), 1800);
    }

    const hasErr = !absent && !!(p.fe || p.pe || p.oe);
    let dotClass = 'idle';
    if (absent) dotClass = 'nopoll';
    else if (hasErr) dotClass = 'err';
    else if (p.open) dotClass = 'on';

    const tx   = actNow ? '<span class="rv act">' + fmtNum(p.tx) + '</span>' : '<span class="rv">' + fmtNum(p.tx) + '</span>';
    const rx   = actNow ? '<span class="rv act">' + fmtNum(p.rx) + '</span>' : '<span class="rv">' + fmtNum(p.rx) + '</span>';
    const err  = (p.fe || p.pe || p.oe) ? '<div class="rs485-err">Ош FE=' + p.fe + ' PE=' + p.pe + ' OE=' + p.oe + '</div>' : '';
    const stat = absent ? '' : (p.open ? '<div class="rs485-open">● активен</div>' : '<div class="rs485-closed">○ свободен</div>');

    card.innerHTML =
      '<div class="rs485-hdr"><span class="rs485-dot ' + dotClass + '"></span><span class="rs485-name">RS-485-' + p.n + '</span></div>' +
      '<div class="rs485-dev">' + (absent ? 'нет опроса' : p.dev) + '</div>' +
      '<div class="rs485-row"><span class="rl">TX</span>' + tx + '</div>' +
      '<div class="rs485-row"><span class="rl">RX</span>' + rx + '</div>' +
      stat + err;
  });
  Array.from(grid.children).forEach(card => {
    if (card.id && !seen.has(card.id)) card.remove();
  });
}

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
      /* eth0 */
      const eth0en = document.getElementById('eth0-en');
      if (eth0en) eth0en.checked = !!(d.eth0 && d.eth0.enabled);
      setVal('f-ip',   d.eth0?.ip || '');
      setVal('f-mask', d.eth0?.netmask || '');
      setVal('f-gw',   d.eth0?.gateway || '');
      setVal('f-dns',  d.eth0?.dns || '');
      toggleEth0Fields();
      /* eth1 */
      const eth1en = document.getElementById('eth1-en');
      if (eth1en) eth1en.checked = d.eth1?.enabled || false;
      setVal('f-ip1',   d.eth1?.ip || '');
      setVal('f-mask1', d.eth1?.netmask || '');
      setVal('f-gw1',   d.eth1?.gateway || '');
      setVal('f-dns1',  d.eth1?.dns || '');
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
  const en = document.getElementById('eth0-en');
  const wrap = document.getElementById('eth0-fields');
  if (en && wrap) {
    wrap.style.opacity = en.checked ? '1' : '.4';
    wrap.style.pointerEvents = en.checked ? '' : 'none';
  }
}

function toggleEth1Fields() {
  const en = document.getElementById('eth1-en');
  const wrap = document.getElementById('eth1-fields');
  if (en && wrap) {
    wrap.style.opacity = en.checked ? '1' : '.4';
    wrap.style.pointerEvents = en.checked ? '' : 'none';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   FORM SUBMISSION — network / time
   ══════════════════════════════════════════════════════════════════════════ */
function initForms() {
  /* eth0 */
  const f0 = document.getElementById('net-form');
  if (f0) f0.addEventListener('submit', e => {
    e.preventDefault();
    const en = document.getElementById('eth0-en')?.checked;
    if (en) {
      if (!validateNetForm(f0)) return;
      if (!document.getElementById('f-ip')?.value.trim() || !document.getElementById('f-mask')?.value.trim()) {
        toast('Укажите IP и маску для eth0', 'error');
        return;
      }
    }
    submitForm(f0, () => { configLoaded = false; toast('Настройки eth0 применены. Перезагрузите сеть.', 'success'); });
  });

  /* eth1 */
  const f1 = document.getElementById('net-form-eth1');
  if (f1) f1.addEventListener('submit', e => {
    e.preventDefault();
    const en = document.getElementById('eth1-en')?.checked;
    if (en && !document.getElementById('f-ip1')?.value.trim()) {
      toast('Укажите IP для eth1', 'error'); return;
    }
    submitForm(f1, () => { configLoaded = false; toast('Настройки eth1 применены.', 'success'); });
  });

  /* time */
  const ft = document.getElementById('time-form');
  if (ft) ft.addEventListener('submit', e => {
    e.preventDefault();
    submitForm(ft, () => toast('Время/таймзона применены', 'success'));
  });

  /* eth0 / eth1 toggles */
  const eth0en = document.getElementById('eth0-en');
  if (eth0en) eth0en.addEventListener('change', toggleEth0Fields);
  const eth1en = document.getElementById('eth1-en');
  if (eth1en) eth1en.addEventListener('change', toggleEth1Fields);
}

function validateNetForm(form) {
  let ok = true;
  const skipEth0Static =
    form.id === 'net-form' && !document.getElementById('eth0-en')?.checked;
  form.querySelectorAll('input[pattern]').forEach(inp => {
    if (skipEth0Static && inp.closest('#eth0-fields')) return;
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
  if (id === 'mqtt-bridge') return 'MQTT';
  if (id === 'mplc4' || lab.toLowerCase() === 'mplc4') return 'MPLC4';
  if (lab) return lab;
  return unitUiLabel(id);
}

function svcCtlRowState(svc) {
  if (svc.masked || svc.user_disabled) return 'inactive';
  return svc.active || 'inactive';
}

function renderServicesControl(data) {
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  const list = (data && data.services) || [];
  if (!list.length) {
    host.innerHTML = '<p class="field-hint">Нет управляемых служб</p>';
    return;
  }
  host.innerHTML = '';
  list.forEach(function (svc, i) {
    const off = !!(svc.masked || svc.user_disabled);
    const action = off ? 'start' : 'stop';
    const btnLabel = off ? 'Вкл' : 'Стоп';
    const btnClass = off ? 'btn btn-primary btn-sm svc-ctl-btn' : 'btn btn-warn btn-sm svc-ctl-btn';

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
      setTimeout(fetchStatus, 1500);
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
            bad_password_char: 'Пароль не может содержать символ \'',
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
   THEME (SVG toggle в шапке)
   ══════════════════════════════════════════════════════════════════════════ */
function syncThemeSwitcherVisual() {
  const obj = document.getElementById('theme-obj');
  if (!obj || !obj.contentDocument) return;
  const sw = obj.contentDocument.getElementById('switcher');
  if (!sw) return;
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  sw.classList.remove('Dark', 'Light', 'Stop', 'Start');
  sw.classList.add(light ? 'Light' : 'Dark', light ? 'Start' : 'Stop');
}

function initThemeToggle() {
  const obj = document.getElementById('theme-obj');
  if (!obj) return;
  const bind = () => {
    const doc = obj.contentDocument;
    if (!doc) return;
    const sw = doc.getElementById('switcher');
    if (!sw) return;
    sw.addEventListener('click', ev => {
      ev.preventDefault();
      const isLight = document.documentElement.getAttribute('data-theme') === 'light';
      if (isLight) {
        document.documentElement.removeAttribute('data-theme');
        try { localStorage.setItem('sa02m-theme', 'dark'); } catch (_) {}
      } else {
        document.documentElement.setAttribute('data-theme', 'light');
        try { localStorage.setItem('sa02m-theme', 'light'); } catch (_) {}
      }
      syncThemeSwitcherVisual();
    });
    syncThemeSwitcherVisual();
  };
  if (obj.contentDocument && obj.contentDocument.getElementById('switcher')) bind();
  else obj.addEventListener('load', bind, { once: true });
}

/* ══════════════════════════════════════════════════════════════════════════
   INIT
   ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  const verEl = document.getElementById('app-version');
  if (verEl) verEl.textContent = 'v' + APP_VERSION;

  initNav();
  initForms();
  initValidation();
  initWebCredsForm();
  initThemeToggle();
  handleUrlStatus();
  hydratePriorityWarmup();
  bindUsbPowerResetButton();

  /* Сначала отдельные первые виджеты, потом тяжелее блоки. */
  const scheduleStatus = () => {
    fetchPriorityPart('priority');
    setTimeout(fetchStatusMain, 180);
    setTimeout(fetchStatusRs485, 420);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => { requestAnimationFrame(scheduleStatus); });
  } else {
    setTimeout(scheduleStatus, 0);
  }
  bootstrapBackgroundWidgets();
  setInterval(() => fetchPriorityPart('priority'), 4000);
  setInterval(fetchStatusMain, 6000);
  setInterval(fetchStatusRs485, 8000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fetchStatus();
  });

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
});
