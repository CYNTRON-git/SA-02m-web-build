/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  Web Interface — Application JS
   Single-Page Application: auth guard, dashboard polling, settings, GPIO
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

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

/* ── Gauge helper (SVG stroke-dasharray arc) ─────────────────────────────── */
function arcDash(pct, maxArc = 126) {
  const fill = Math.min(1, Math.max(0, pct / 100)) * maxArc;
  return fill + ' ' + (maxArc - fill);
}

/* ── Service badge ────────────────────────────────────────────────────────── */
function svcBadge(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = state === 'active' ? 'active' : (state || '?');
  el.className = 'badge ' + (state === 'active' ? 'badge-ok pulse' : 'badge-err');
}

/* ══════════════════════════════════════════════════════════════════════════
   STATUS POLLING
   ══════════════════════════════════════════════════════════════════════════ */
let fetchBusy = false;
const _prevRs = {};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function threshColor(val, warnAt, critAt) {
  return val >= critAt ? cssVar('--red') : val >= warnAt ? cssVar('--yellow') : cssVar('--cyan');
}

function applyStatus(d) {
  const ARC = 126;

  /* CPU */
  setText('cpu-val', d.cpu_usage + '%');
  const cpuArc = document.getElementById('cpu-arc');
  if (cpuArc) {
    cpuArc.style.strokeDasharray = arcDash(d.cpu_usage, ARC);
    cpuArc.style.stroke = threshColor(d.cpu_usage, 60, 80);
  }

  /* RAM */
  setText('ram-val', fmtKB(d.ram_used_kb));
  setText('ram-sub', 'из ' + fmtKB(d.ram_total_kb));
  setText('ram-pct', d.ram_pct + '%');
  setText('ram-free', 'свободно ' + fmtKB(d.ram_free_kb));
  const ramBar = document.getElementById('ram-bar');
  if (ramBar) {
    ramBar.style.width = d.ram_pct + '%';
    ramBar.style.background = threshColor(d.ram_pct, 70, 90);
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

  /* Temperature */
  setText('temp-val', d.temp_c + '°');
  const tempArc = document.getElementById('temp-arc');
  if (tempArc) {
    tempArc.style.strokeDasharray = arcDash(Math.min(d.temp_c, 100), ARC);
    tempArc.style.stroke = d.temp_c > 80 ? cssVar('--red') : d.temp_c > 60 ? cssVar('--yellow') : cssVar('--orange');
  }

  /* Disk */
  setText('disk-val', fmtKB(d.disk_used_kb));
  setText('disk-sub', 'из ' + fmtKB(d.disk_total_kb));
  setText('disk-pct', d.disk_pct + '%');
  setText('disk-free', 'свободно ' + fmtKB(d.disk_free_kb));
  const diskBar = document.getElementById('disk-bar');
  if (diskBar) {
    diskBar.style.width = d.disk_pct + '%';
    diskBar.style.background = threshColor(d.disk_pct, 70, 90);
  }
  if (d.disk_io_read_b !== undefined)
    setText('disk-io', 'R ' + fmtBytes(d.disk_io_read_b) + ' / W ' + fmtBytes(d.disk_io_write_b));

  /* Uptime */
  setText('uptime-val', d.uptime_str || fmtUptime(d.uptime_sec));

  /* Network eth0 */
  setText('net-rx', fmtBytes(d.net_rx_bytes));
  setText('net-tx', fmtBytes(d.net_tx_bytes));

  /* eth1 */
  const st = d.eth1_operstate || 'absent';
  const ethEl = document.getElementById('eth1-state');
  if (ethEl) {
    ethEl.textContent = st === 'up' ? '● UP' : st === 'down' ? '● DOWN' : '● не найден';
    ethEl.className = 'eth-state ' + (st === 'up' ? 'up' : st === 'down' ? 'down' : 'absent');
  }
  setText('eth1-traf', 'RX ' + fmtBytes(d.net1_rx_bytes || 0) + '  TX ' + fmtBytes(d.net1_tx_bytes || 0));

  /* Load */
  setText('load-1',  d.load_1  || '—');
  setText('load-5',  d.load_5  || '—');
  setText('load-15', d.load_15 || '—');
  setText('proc-info', 'Процессов: ' + (d.proc_running || 0) + ' / ' + (d.proc_total || 0));
  if (d.cpu_freq_mhz) {
    const thr = d.cpu_throttle ? ' (' + d.cpu_throttle + '%)' : '';
    setText('cpu-freq', d.cpu_freq_mhz + ' МГц' + thr);
  }

  /* System info */
  if (d.board)     setText('board-info',  d.board);
  if (d.cpu_model) setText('cpu-model',   d.cpu_model);
  if (d.kernel)    setText('kernel-info', 'Ядро: ' + d.kernel);

  /* Services */
  svcBadge('svc-nginx',    d.svc_nginx);
  svcBadge('svc-fcgi',     d.svc_fcgiwrap);
  svcBadge('svc-mplc',     d.mplc_status);
  if (d.mplc_status === 'active' && d.mplc_uptime_s > 0)
    setText('mplc-uptime', fmtUptime(d.mplc_uptime_s));
  else setText('mplc-uptime', '');

  /* HW GPIO */
  const hint = document.getElementById('hw-hint');
  if (hint) hint.textContent = d.hw_configured
    ? 'GPIO настроены (/etc/sa02m_hw.conf)'
    : 'GPIO не заданы — отредактируйте /etc/sa02m_hw.conf';
  setHwRow('hw-do-st',   d.hw_do);
  setHwRow('hw-beep-st', d.hw_beeper);
  setHwRow('hw-led-st',  d.hw_alarm_led);
  document.querySelectorAll('.hw-btns .btn').forEach(b => {
    b.disabled = !d.hw_configured;
  });

  /* RS-485 */
  if (d.rs485 && d.rs485.length) renderRs485(d.rs485);

  /* Topbar IP */
  if (d.ip) setText('tb-ip', d.ip);
}

function fetchStatus() {
  if (fetchBusy) return;
  fetchBusy = true;
  fetch('/cgi-bin/status.cgi', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => { if (!d.error) applyStatus(d); })
    .catch(() => {})
    .finally(() => { fetchBusy = false; });
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

    const dot  = absent ? 'absent' : (p.open ? 'on' : 'idle');
    const tx   = actNow ? '<span class="rv act">' + fmtNum(p.tx) + '</span>' : '<span class="rv">' + fmtNum(p.tx) + '</span>';
    const rx   = actNow ? '<span class="rv act">' + fmtNum(p.rx) + '</span>' : '<span class="rv">' + fmtNum(p.rx) + '</span>';
    const err  = (p.fe || p.pe || p.oe) ? '<div class="rs485-err">Ош FE=' + p.fe + ' PE=' + p.pe + ' OE=' + p.oe + '</div>' : '';
    const stat = absent ? '' : (p.open ? '<div class="rs485-open">● активен</div>' : '<div class="rs485-closed">○ свободен</div>');

    card.innerHTML =
      '<div class="rs485-hdr"><span class="rs485-dot ' + dot + '"></span><span class="rs485-name">RS-485-' + p.n + '</span></div>' +
      '<div class="rs485-dev">' + (absent ? 'не найден' : p.dev) + '</div>' +
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
      /* eth0 */
      setVal('f-ip',   d.eth0?.ip || '');
      setVal('f-mask', d.eth0?.netmask || '');
      setVal('f-gw',   d.eth0?.gateway || '');
      setVal('f-dns',  d.eth0?.dns || '');
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
    })
    .catch(() => {});
}

function setVal(id, val) { const e = document.getElementById(id); if (e) e.value = val; }

function toggleEth1Fields() {
  const en = document.getElementById('eth1-en');
  const wrap = document.getElementById('eth1-fields');
  if (en && wrap) wrap.style.opacity = en.checked ? '1' : '.4';
}

/* ══════════════════════════════════════════════════════════════════════════
   FORM SUBMISSION — network / time
   ══════════════════════════════════════════════════════════════════════════ */
function initForms() {
  /* eth0 */
  const f0 = document.getElementById('net-form');
  if (f0) f0.addEventListener('submit', e => {
    e.preventDefault();
    if (!validateNetForm(f0)) return;
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

  /* eth1 toggle */
  const eth1en = document.getElementById('eth1-en');
  if (eth1en) eth1en.addEventListener('change', toggleEth1Fields);
}

function validateNetForm(form) {
  let ok = true;
  form.querySelectorAll('input[pattern]').forEach(inp => {
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
   INIT
   ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  initNav();
  initForms();
  initValidation();
  handleUrlStatus();

  fetchStatus();
  setInterval(fetchStatus, 4000);

  /* Expose globals for inline onclick */
  window.setHw    = setHw;
  window.doRestart = doRestart;
  window.doReboot  = doReboot;
  window.doLogout  = doLogout;
  window.loadLog   = loadLog;
});
