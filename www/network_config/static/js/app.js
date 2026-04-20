/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

/** Версия веб-интерфейса (синхронизируйте с install.sh). */
const APP_VERSION = '1.0.2';

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
      if (tab === 'system') loadLog();
      if (tab === 'network' || tab === 'time') loadConfig();
      if (tab === 'flasher' && window.flasherInit) window.flasherInit();
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
function svcBadge(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  const ok = state === 'active';
  el.textContent = ok ? 'Активен' : 'Неактивен';
  el.className = 'badge ' + (ok ? 'badge-ok pulse' : 'badge-err');
}

/* ══════════════════════════════════════════════════════════════════════════
   STATUS POLLING — приоритетные виджеты отдельно, остальное в фоне
   ══════════════════════════════════════════════════════════════════════════ */
const widgetBusy = { cpu: false, temp: false, ram: false, disk: false };
const backgroundBusy = {
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
const backgroundLoaded = {
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

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function threshColor(val, warnAt, critAt) {
  return val >= critAt ? cssVar('--red') : val >= warnAt ? cssVar('--yellow') : cssVar('--cyan');
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
      swapBar.style.background = d.swap_pct > 80 ? cssVar('--red') : cssVar('--orange');
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
      const tempStroke = tc >= 80 ? cssVar('--red') : tc >= 70 ? cssVar('--yellow') : cssVar('--green');
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
  applyRemovableDisk(!!d.usb_mounted, 'usb', d);
  applyRemovableDisk(!!d.sd_mounted, 'sd', d);
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

function applyNetworkStatus(d) {
  setText('net-rx', fmtBytes(d.net_rx_bytes));
  setText('net-tx', fmtBytes(d.net_tx_bytes));
  const st = d.eth1_operstate || 'absent';
  const ethEl = document.getElementById('eth1-state');
  if (ethEl) {
    ethEl.textContent = st === 'up' ? '● В сети' : st === 'down' ? '● Нет линка' : '● Нет адаптера';
    ethEl.className = 'eth-state ' + (st === 'up' ? 'up' : st === 'down' ? 'down' : 'absent');
  }
  setText('eth1-traf', 'RX ' + fmtBytes(d.net1_rx_bytes || 0) + '  TX ' + fmtBytes(d.net1_tx_bytes || 0));
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
}

function applyServicesStatus(d) {
  svcBadge('svc-nginx',    d.svc_nginx);
  svcBadge('svc-fcgi',     d.svc_fcgiwrap);
  svcBadge('svc-mplc',     d.mplc_status);
  if (d.mplc_status === 'active' && d.mplc_uptime_s > 0)
    setText('mplc-uptime', fmtUptime(d.mplc_uptime_s));
  else setText('mplc-uptime', '');
}

function applyHardwareStatus(d) {
  const hint = document.getElementById('hw-hint');
  if (hint) {
    if (d.hw_i2c_expander_absent === 1) {
      hint.textContent = 'НЕТ СВЯЗИ С МИКРОСХЕМОЙ РАСШИРЕНИЯ I2C';
    } else if (d.hw_configured) {
      hint.textContent = 'GPIO настроены (/etc/sa02m_hw.conf)';
    } else {
      hint.textContent = 'GPIO не заданы — отредактируйте /etc/sa02m_hw.conf';
    }
  }
  setHwRow('hw-do-st',   d.hw_do);
  setHwRow('hw-beep-st', d.hw_beeper);
  setHwRow('hw-led-st',  d.hw_alarm_led);
  setHwRow('hw-usb-st',  d.hw_usb_power);
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

function fetchPriorityPart(part, persist = true) {
  if (widgetBusy[part]) return;
  widgetBusy[part] = true;
  fetch('/cgi-bin/status.cgi?part=' + encodeURIComponent(part), { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      if (d.error) return;
      applyPriorityStatus(d);
      if (persist) writePriorityWarmupPart(part, d);
    })
    .catch(() => {})
    .finally(() => { widgetBusy[part] = false; });
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

function fetchBackgroundPart(part, applyFn) {
  if (backgroundBusy[part]) return;
  backgroundBusy[part] = true;
  fetch('/cgi-bin/status.cgi?part=' + encodeURIComponent(part), { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      if (d.error) return;
      applyFn(d);
      backgroundLoaded[part] = true;
    })
    .catch(() => {})
    .finally(() => { backgroundBusy[part] = false; });
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

function fetchServicesWidget() {
  fetchBackgroundPart('services', applyServicesStatus);
}

function fetchHardwareWidget() {
  fetchBackgroundPart('hardware', applyHardwareStatus);
}

function fetchStatusMain() {
  fetchStorageWidget();
  fetchTimeWidget();
  fetchUptimeWidget();
  fetchNetworkWidget();
  fetchLoadWidget();
  fetchSystemWidget();
  fetchServicesWidget();
  fetchHardwareWidget();
}

function fetchStatusRs485() {
  fetchBackgroundPart('rs485', applyRs485Status);
}

function fetchStatus() {
  fetchCpuWidget();
  fetchTempWidget();
  fetchRamWidget();
  fetchDiskWidget();
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
function setHwRow(id, v) {
  const el = document.getElementById(id);
  if (!el) return;
  if (v === -1 || v === undefined || v === null) {
    el.textContent = 'н/д'; el.className = 'hw-state na'; return;
  }
  el.textContent = v ? 'ВКЛ' : 'ВЫКЛ';
  el.className = 'hw-state ' + (v ? 'on' : 'off');
}

function setHwChannelBtns(channel, enabled) {
  document.querySelectorAll('.hw-btns[data-hw-ch="' + channel + '"] .btn').forEach(b => {
    b.disabled = !enabled;
  });
}

function setHw(channel, value) {
  const body = 'channel=' + encodeURIComponent(channel) + '&value=' + encodeURIComponent(value);
  fetch('/cgi-bin/hw_set.cgi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body, credentials: 'same-origin'
  })
    .then(r => r.json())
    .then(j => {
      if (j.ok) { fetchStatus(); toast('Применено', 'success'); }
      else if (j.error === 'gpio_not_configured') toast('GPIO не настроен в /etc/sa02m_hw.conf', 'error');
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
  ports.forEach(p => {
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
}

/* ══════════════════════════════════════════════════════════════════════════
   CONFIG — load current network/time settings into forms
   ══════════════════════════════════════════════════════════════════════════ */
let configLoaded = false;

function loadConfig() {
  if (configLoaded) return;
  fetch('/cgi-bin/config.cgi', { cache: 'no-store' })
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
      const tzSel = document.getElementById('f-tz');
      if (tzSel && d.timezone) tzSel.value = d.timezone;
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

function submitForm(form, onSuccess) {
  const data = new URLSearchParams(new FormData(form));
  const btn = form.querySelector('button[type=submit]');
  if (btn) btn.disabled = true;
  fetch('/cgi-bin/apply.cgi', { method: 'POST', body: data, redirect: 'manual' })
    .then(() => { onSuccess && onSuccess(); })
    .catch(() => toast('Ошибка отправки', 'error'))
    .finally(() => { if (btn) btn.disabled = false; });
}

/* ══════════════════════════════════════════════════════════════════════════
   SYSTEM ACTIONS
   ══════════════════════════════════════════════════════════════════════════ */
function doRestart() {
  if (!confirm('Перезапустить службы nginx и fcgiwrap?')) return;
  fetch('/cgi-bin/restart.cgi', { method: 'POST', redirect: 'manual' })
    .then(() => { toast('Службы перезапущены', 'success'); setTimeout(fetchStatus, 2000); })
    .catch(() => toast('Ошибка', 'error'));
}

function doReboot() {
  if (!confirm('Перезагрузить контроллер?')) return;
  fetch('/cgi-bin/reboot.cgi', { method: 'POST', redirect: 'manual' })
    .then(() => toast('Перезагрузка… подождите 30с', 'info', 30000))
    .catch(() => {});
}

function doLogout() {
  window.location.href = '/cgi-bin/logout.cgi';
}

/* ══════════════════════════════════════════════════════════════════════════
   LOG
   ══════════════════════════════════════════════════════════════════════════ */
function loadLog() {
  const box = document.getElementById('log-box');
  if (!box) return;
  fetch('/cgi-bin/log.cgi', { cache: 'no-store' })
    .then(r => r.text())
    .then(t => {
      box.innerHTML = t.split('\n').map(line => {
        if (/error|ошибк/i.test(line)) return '<span class="log-err">' + escHtml(line) + '</span>';
        if (/warn/i.test(line))         return '<span class="log-warn">' + escHtml(line) + '</span>';
        if (/ok|успешн|applied|reboot|started/i.test(line)) return '<span class="log-ok">' + escHtml(line) + '</span>';
        return escHtml(line);
      }).join('\n');
      box.scrollTop = box.scrollHeight;
    })
    .catch(() => { if (box) box.textContent = 'Не удалось загрузить журнал'; });
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
    applied:      ['Настройки применены', 'success'],
    error_tz:     ['Ошибка: неверная таймзона', 'error'],
    error_time:   ['Ошибка: не удалось установить время', 'error'],
    services:     ['Службы перезапущены', 'success'],
    reboot:       ['Перезагрузка запущена…', 'info'],
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

function browserIanaTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (_) {
    return '';
  }
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
      else toast('Часовой пояс ПК (' + tz + ') не в списке — выберите вручную', 'info', 5500);
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
  fetch('/cgi-bin/apply.cgi', { method: 'POST', body: data, redirect: 'manual' })
    .then(() => {
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

  /* Сначала отдельные первые виджеты, потом тяжелее блоки. */
  const scheduleStatus = () => {
    fetchCpuWidget();
    setTimeout(fetchTempWidget, 50);
    setTimeout(fetchRamWidget, 100);
    setTimeout(fetchDiskWidget, 150);
    setTimeout(fetchUptimeWidget, 220);
    setTimeout(fetchNetworkWidget, 320);
    setTimeout(fetchLoadWidget, 420);
    setTimeout(fetchServicesWidget, 520);
    setTimeout(fetchStorageWidget, 620);
    setTimeout(fetchHardwareWidget, 760);
    setTimeout(fetchSystemWidget, 900);
    setTimeout(fetchTimeWidget, 1040);
    setTimeout(fetchStatusRs485, 1180);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => { requestAnimationFrame(scheduleStatus); });
  } else {
    setTimeout(scheduleStatus, 0);
  }
  bootstrapBackgroundWidgets();
  setInterval(fetchCpuWidget, 4000);
  setInterval(fetchTempWidget, 5000);
  setInterval(fetchRamWidget, 5000);
  setInterval(fetchDiskWidget, 7000);
  setInterval(fetchUptimeWidget, 4000);
  setInterval(fetchNetworkWidget, 5000);
  setInterval(fetchLoadWidget, 5000);
  setInterval(fetchServicesWidget, 7000);
  setInterval(fetchStorageWidget, 10000);
  setInterval(fetchHardwareWidget, 10000);
  setInterval(fetchTimeWidget, 5000);
  setInterval(fetchSystemWidget, 30000);
  setInterval(fetchStatusRs485, 8000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fetchStatus();
  });

  /* Expose globals for inline onclick */
  window.setHw    = setHw;
  window.doRestart = doRestart;
  window.doReboot  = doReboot;
  window.doLogout  = doLogout;
  window.loadLog   = loadLog;
  window.syncTimeFromPC = syncTimeFromPC;
  window.exportInstallLog = exportInstallLog;
});
