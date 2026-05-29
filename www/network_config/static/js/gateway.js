/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  — Шлюз RS-485 → Ethernet
   gateway.js  v1.0.3.19
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

(function () {

// ── Constants ─────────────────────────────────────────────────────────────────
const PORTS   = ['COM1', 'COM2', 'COM3', 'COM4', 'COM5'];
const MODES   = [
  { value: 'disabled',     label: 'Отключён' },
  { value: 'modbus_tcp',   label: 'Modbus TCP' },
  { value: 'rtu_over_tcp', label: 'RTU over TCP' },
  { value: 'transparent',  label: 'Прозрачный' },
];
const BAUDS     = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];
const PARITIES  = [
  { value: 'none', label: 'Нет (N)' },
  { value: 'even', label: 'Чётный (E)' },
  { value: 'odd',  label: 'Нечётный (O)' },
];
const STOPBITS  = [{ value: 1, label: '1' }, { value: 2, label: '2' }];
const DATABITS  = [7, 8];
const DEFAULT_TCP_PORTS = { COM1: 502, COM2: 503, COM3: 504, COM4: 505, COM5: 506 };

// ── State ─────────────────────────────────────────────────────────────────────
let _config      = {};
let _status      = {};
let _activeSub   = 'device';
let _pollTimer   = null;
let _initialized = false;
let _dirty       = {};

// ── Public API ────────────────────────────────────────────────────────────────
window.gatewayInit = async function () {
  _openSubNav();
  if (_initialized) {
    _startPolling();
    _selectSub(_activeSub);
    return;
  }
  _initialized = true;
  _buildContentArea();
  _bindSubNavClicks();
  await _loadAll();
  _selectSub(_activeSub);
  _startPolling();
};

window.gatewayDestroy = function () {
  _stopPolling();
  _closeSubNav();
};

// ── Sidebar sub-nav management ────────────────────────────────────────────────
function _openSubNav() {
  const sub  = document.getElementById('nav-gateway-sub');
  const item = document.getElementById('nav-gateway');
  if (sub)  sub.classList.add('open');
  if (item) item.classList.add('expanded');
}

function _closeSubNav() {
  const sub  = document.getElementById('nav-gateway-sub');
  const item = document.getElementById('nav-gateway');
  if (sub)  sub.classList.remove('open');
  if (item) item.classList.remove('expanded');
}

function _bindSubNavClicks() {
  const sub = document.getElementById('nav-gateway-sub');
  if (!sub) return;
  sub.querySelectorAll('[data-gateway-sub]').forEach(el => {
    el.addEventListener('click', function (ev) {
      ev.stopPropagation();
      _selectSub(this.dataset.gatewaySub);
    });
  });
}

function _selectSub(key) {
  _activeSub = key;
  document.querySelectorAll('#nav-gateway-sub [data-gateway-sub]').forEach(el => {
    el.classList.toggle('active', el.dataset.gatewaySub === key);
  });
  const area = document.getElementById('gw-content');
  if (!area) return;
  if (key === 'device') {
    _renderDevicePanel(area);
  } else if (PORTS.includes(key)) {
    _renderPortPanel(area, key);
  }
}

// ── Content area skeleton ─────────────────────────────────────────────────────
function _buildContentArea() {
  const pane = document.getElementById('tab-gateway');
  if (!pane) return;
  pane.innerHTML = `
    <div class="page-header">
      <h2>Шлюз RS-485 → Ethernet</h2>
      <p>Преобразователь интерфейсов RS-485/TCP: Modbus TCP, RTU over TCP, прозрачный режим</p>
    </div>
    <div id="gw-content"></div>`;
}

// ── Device panel ──────────────────────────────────────────────────────────────
function _renderDevicePanel(area) {
  const active = _status.service_active;
  const svcClass = active ? 'badge badge-ok' : 'badge badge-err';
  const svcText  = active ? '● Сервис активен' : '● Сервис остановлен';

  const activePorts = PORTS.filter(p => (_config[p] || {}).enabled);
  const runningPorts = PORTS.filter(p => {
    const st = (_status.ports || {})[p];
    return st && st.running;
  });

  area.innerHTML = `
    <div class="widget" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px">
        <span id="gw-svc-badge" class="${svcClass}">${svcText}</span>
        <span style="color:var(--text-sec);font-size:.82rem">
          Включено портов: ${activePorts.length} / 5 &nbsp;|&nbsp; Работают: ${runningPorts.length}
        </span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button class="btn btn-sm" id="gw-btn-start">Запустить</button>
        <button class="btn btn-sm" id="gw-btn-reload">↺ Перезагрузить конфиг</button>
        <button class="btn btn-sm btn-warn" id="gw-btn-restart">⟳ Перезапустить</button>
        <button class="btn btn-sm btn-danger" id="gw-btn-stop">Остановить</button>
      </div>
    </div>

    <div class="widget">
      <h3 style="font-size:.95rem;margin:0 0 12px">Порты</h3>
      <table style="width:100%;border-collapse:collapse;font-size:.82rem">
        <thead>
          <tr style="color:var(--text-sec)">
            <th style="text-align:left;padding:4px 8px;font-weight:500">Порт</th>
            <th style="text-align:left;padding:4px 8px;font-weight:500">Режим</th>
            <th style="text-align:left;padding:4px 8px;font-weight:500">TCP</th>
            <th style="text-align:left;padding:4px 8px;font-weight:500">Бод</th>
            <th style="text-align:left;padding:4px 8px;font-weight:500">Статус</th>
          </tr>
        </thead>
        <tbody>
          ${PORTS.map(p => {
            const cfg = _config[p] || {};
            const st  = (_status.ports || {})[p] || {};
            const modeLabel = MODES.find(m => m.value === (cfg.mode || 'disabled'))?.label || '—';
            const running = st.running;
            const dot = cfg.enabled
              ? (running ? '<span style="color:var(--green)">●</span>' : '<span style="color:var(--red)">●</span>')
              : '<span style="color:var(--text-dim)">○</span>';
            return `<tr style="border-top:1px solid var(--border)">
              <td style="padding:6px 8px"><a href="#" class="gw-port-link" data-port="${p}" style="color:var(--cyan);text-decoration:none">${p}</a></td>
              <td style="padding:6px 8px;color:var(--text-sec)">${cfg.enabled ? modeLabel : '—'}</td>
              <td style="padding:6px 8px;color:var(--text-sec)">${cfg.enabled && cfg.mode !== 'disabled' ? (cfg.tcp_port || DEFAULT_TCP_PORTS[p]) : '—'}</td>
              <td style="padding:6px 8px;color:var(--text-sec)">${cfg.enabled ? (cfg.baudrate || 9600) : '—'}</td>
              <td style="padding:6px 8px">${dot}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;

  area.querySelectorAll('.gw-port-link').forEach(a => {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      _selectSub(this.dataset.port);
    });
  });
  area.querySelector('#gw-btn-start').onclick   = () => _svcCtrl('start');
  area.querySelector('#gw-btn-reload').onclick  = () => _svcCtrl('reload');
  area.querySelector('#gw-btn-restart').onclick = () => _svcCtrl('restart');
  area.querySelector('#gw-btn-stop').onclick    = () => _svcCtrl('stop');
}

// ── Port panel ────────────────────────────────────────────────────────────────
function _renderPortPanel(area, port) {
  const cfg  = _config[port] || _defaultPortCfg(port);
  const st   = (_status.ports || {})[port] || {};
  const mode = cfg.mode || 'disabled';

  area.innerHTML = `
    <div class="widget">
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px">
        <h3 style="margin:0;font-size:1rem">${port}</h3>
        ${_statusBadgeHtml(st, cfg)}
        <div id="gw-counters-${port}" style="margin-left:auto">${_countersHtml(st)}</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px 24px">

        <div>
          <p class="field-label" style="margin-bottom:6px;font-size:.78rem;color:var(--text-sec);text-transform:uppercase;letter-spacing:.04em">Режим и TCP</p>

          <div class="field toggle-field-row" style="margin-bottom:10px">
            <span class="toggle-field-label">Включить порт</span>
            <label class="toggle-inline">
              <input type="checkbox" class="toggle toggle-wide" id="gw-en-${port}"
                     ${cfg.enabled ? 'checked' : ''}>
            </label>
          </div>

          <div class="field" style="margin-bottom:10px">
            <label style="font-size:.82rem;color:var(--text-sec)">Режим работы</label>
            <select id="gw-mode-${port}" style="margin-top:4px">
              ${MODES.map(m =>
                `<option value="${m.value}"${mode === m.value ? ' selected' : ''}>${m.label}</option>`
              ).join('')}
            </select>
            <p class="field-hint" id="gw-mode-hint-${port}" style="margin-top:4px;font-size:.77rem;color:var(--text-sec)">${_modeHint(mode)}</p>
          </div>

          <div id="gw-tcp-fields-${port}" ${mode === 'disabled' ? 'style="display:none"' : ''}>
            <div class="field" style="margin-bottom:10px">
              <label style="font-size:.82rem;color:var(--text-sec)">TCP-порт</label>
              <input type="number" id="gw-tcpport-${port}" style="margin-top:4px"
                     value="${cfg.tcp_port || DEFAULT_TCP_PORTS[port]}" min="1" max="65535">
            </div>
          </div>

          <div id="gw-fmb-field-${port}" ${mode === 'modbus_tcp' ? '' : 'style="display:none"'}>
            <div class="field toggle-field-row" style="margin-bottom:4px">
              <span class="toggle-field-label" style="font-size:.82rem">Fast Modbus probe (FC&nbsp;0x47)</span>
              <label class="toggle-inline">
                <input type="checkbox" class="toggle toggle-wide" id="gw-fmb-${port}"
                       ${cfg.fast_modbus_probe !== false ? 'checked' : ''}>
              </label>
            </div>
            <p class="field-hint" style="font-size:.77rem;color:var(--text-sec);margin-bottom:10px">Отвечать локально на WB Fast Modbus probe</p>
          </div>
        </div>

        <div>
          <p class="field-label" style="margin-bottom:6px;font-size:.78rem;color:var(--text-sec);text-transform:uppercase;letter-spacing:.04em">Параметры RS-485</p>

          <div class="field" style="margin-bottom:10px">
            <label style="font-size:.82rem;color:var(--text-sec)">Скорость (бод)</label>
            <select id="gw-baud-${port}" style="margin-top:4px">
              ${BAUDS.map(b =>
                `<option value="${b}"${parseInt(cfg.baudrate) === b ? ' selected' : ''}>${b}</option>`
              ).join('')}
            </select>
          </div>

          <div class="field" style="margin-bottom:10px">
            <label style="font-size:.82rem;color:var(--text-sec)">Чётность</label>
            <select id="gw-parity-${port}" style="margin-top:4px">
              ${PARITIES.map(p =>
                `<option value="${p.value}"${cfg.parity === p.value ? ' selected' : ''}>${p.label}</option>`
              ).join('')}
            </select>
          </div>

          <div class="field" style="margin-bottom:10px">
            <label style="font-size:.82rem;color:var(--text-sec)">Стоп-биты</label>
            <select id="gw-stop-${port}" style="margin-top:4px">
              ${STOPBITS.map(s =>
                `<option value="${s.value}"${parseInt(cfg.stopbits) === s.value ? ' selected' : ''}>${s.label}</option>`
              ).join('')}
            </select>
          </div>

          <div class="field" style="margin-bottom:10px">
            <label style="font-size:.82rem;color:var(--text-sec)">Биты данных</label>
            <select id="gw-data-${port}" style="margin-top:4px">
              ${DATABITS.map(d =>
                `<option value="${d}"${parseInt(cfg.databits) === d ? ' selected' : ''}>${d}</option>`
              ).join('')}
            </select>
          </div>
        </div>

      </div>

      <div style="display:flex;align-items:center;gap:10px;margin-top:16px;flex-wrap:wrap">
        <button class="btn btn-primary" id="gw-save-btn-${port}">Сохранить</button>
        <button class="btn btn-sm" id="gw-reset-btn-${port}">Сбросить</button>
        <span id="gw-save-status-${port}" style="font-size:.82rem"></span>
      </div>
    </div>`;

  const modeEl = area.querySelector(`#gw-mode-${port}`);
  modeEl.addEventListener('change', () => _onModeChange(port));

  area.querySelector(`#gw-save-btn-${port}`).addEventListener('click', () => _savePort(port));
  area.querySelector(`#gw-reset-btn-${port}`).addEventListener('click', () => {
    delete _dirty[port];
    const a = document.getElementById('gw-content');
    if (a) _renderPortPanel(a, port);
  });

  const fields = ['gw-en-', 'gw-tcpport-', 'gw-fmb-', 'gw-baud-', 'gw-parity-', 'gw-stop-', 'gw-data-'];
  fields.forEach(prefix => {
    const el = area.querySelector(`#${prefix}${port}`);
    if (el) el.addEventListener('change', () => { _dirty[port] = true; });
  });
}

function _onModeChange(port) {
  const area = document.getElementById('gw-content');
  if (!area) return;
  const mode = area.querySelector(`#gw-mode-${port}`)?.value || 'disabled';
  const tcpDiv = area.querySelector(`#gw-tcp-fields-${port}`);
  const fmbDiv = area.querySelector(`#gw-fmb-field-${port}`);
  const hint   = area.querySelector(`#gw-mode-hint-${port}`);
  if (tcpDiv) tcpDiv.style.display = mode === 'disabled' ? 'none' : '';
  if (fmbDiv) fmbDiv.style.display = mode === 'modbus_tcp' ? '' : 'none';
  if (hint)   hint.textContent = _modeHint(mode);
  _dirty[port] = true;
}

// ── Save port ─────────────────────────────────────────────────────────────────
async function _savePort(port) {
  const cfg = _readFormCfg(port);
  if (!cfg) return;

  _config[port] = cfg;
  const statusEl = document.getElementById(`gw-save-status-${port}`);
  if (statusEl) { statusEl.textContent = 'Сохранение…'; statusEl.style.color = 'var(--text-sec)'; }

  try {
    const resp = await fetch('/cgi-bin/gateway_config.cgi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ports: _config }),
    });
    const data = await resp.json();
    if (data.ok) {
      delete _dirty[port];
      _updateSubNavDots();
      if (statusEl) { statusEl.textContent = 'Сохранено'; statusEl.style.color = 'var(--green)'; }
      if (typeof toast === 'function') toast(`${port} сохранён`, 'ok');
    } else {
      if (statusEl) { statusEl.textContent = 'Ошибка: ' + (data.error || '?'); statusEl.style.color = 'var(--red)'; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Сетевая ошибка: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

function _readFormCfg(port) {
  const getVal = id => document.getElementById(id)?.value;
  const getChk = id => document.getElementById(id)?.checked ?? false;
  const mode = getVal(`gw-mode-${port}`) || 'disabled';
  return {
    enabled:           getChk(`gw-en-${port}`),
    mode,
    tcp_port:          parseInt(getVal(`gw-tcpport-${port}`)) || DEFAULT_TCP_PORTS[port],
    baudrate:          parseInt(getVal(`gw-baud-${port}`)) || 9600,
    parity:            getVal(`gw-parity-${port}`) || 'none',
    stopbits:          parseInt(getVal(`gw-stop-${port}`)) || 2,
    databits:          parseInt(getVal(`gw-data-${port}`)) || 8,
    fast_modbus_probe: getChk(`gw-fmb-${port}`),
  };
}

// ── Service control ────────────────────────────────────────────────────────────
async function _svcCtrl(action) {
  try {
    const resp = await fetch('/cgi-bin/gateway_ctrl.cgi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (typeof toast === 'function') toast(`Команда «${action}» выполнена`, 'ok');
      setTimeout(_loadStatus, 1200);
    } else {
      if (typeof toast === 'function') toast(`Ошибка: ${data.error || '?'}`, 'error');
    }
  } catch (e) {
    if (typeof toast === 'function') toast('Сетевая ошибка: ' + e.message, 'error');
  }
}

// ── Load / refresh ─────────────────────────────────────────────────────────────
async function _loadAll() {
  await Promise.all([_loadConfig(), _loadStatus()]);
}

async function _loadConfig() {
  try {
    const resp = await fetch('/cgi-bin/gateway_config.cgi');
    const data = await resp.json();
    if (data.ports) {
      _config = data.ports;
      for (const p of PORTS) {
        if (!_config[p]) _config[p] = _defaultPortCfg(p);
      }
    }
  } catch (e) {
    console.error('[gateway] loadConfig:', e);
  }
}

async function _loadStatus() {
  try {
    const resp = await fetch('/cgi-bin/gateway_status.cgi');
    _status = await resp.json();
    _updateSubNavDots();
    _refreshActivePanel();
  } catch (e) {
    console.error('[gateway] loadStatus:', e);
  }
}

function _refreshActivePanel() {
  const area = document.getElementById('gw-content');
  if (!area) return;
  if (_activeSub === 'device') {
    _renderDevicePanel(area);
  } else if (PORTS.includes(_activeSub)) {
    const countersEl = document.getElementById(`gw-counters-${_activeSub}`);
    const st = (_status.ports || {})[_activeSub] || {};
    if (countersEl) countersEl.innerHTML = _countersHtml(st);
  }
}

function _startPolling() {
  _stopPolling();
  _pollTimer = setInterval(_loadStatus, 5000);
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ── Sub-nav dot badges ─────────────────────────────────────────────────────────
function _updateSubNavDots() {
  const portStatus = _status.ports || {};
  for (const port of PORTS) {
    const dot = document.getElementById(`gwdot-${port}`);
    if (!dot) continue;
    const st  = portStatus[port];
    const cfg = _config[port];
    if (!cfg || !cfg.enabled) {
      dot.textContent = '';
      dot.className = 'gw-sub-dot';
    } else if (st && st.running) {
      dot.textContent = '●';
      dot.className = 'gw-sub-dot gw-sub-dot-ok';
    } else {
      dot.textContent = '●';
      dot.className = 'gw-sub-dot gw-sub-dot-err';
    }
  }
}

// ── Status helpers ────────────────────────────────────────────────────────────
function _statusBadgeHtml(st, cfg) {
  if (!cfg || !cfg.enabled) {
    return '<span class="badge badge-unk">Отключён</span>';
  }
  if (st && st.running) {
    const modeLabel = MODES.find(m => m.value === cfg.mode)?.label || cfg.mode;
    return `<span class="badge badge-ok">● Работает</span>
            <span class="badge" style="background:var(--bg-panel);color:var(--text-sec);border:1px solid var(--border)">
              ${modeLabel}&nbsp;:${cfg.tcp_port || '?'}</span>`;
  }
  const err = st && st.last_error ? ` — ${st.last_error}` : '';
  return `<span class="badge badge-err">● Остановлен${err}</span>`;
}

function _countersHtml(st) {
  if (!st || !st.running) return '';
  const fmtBytes = b => {
    b = parseInt(b) || 0;
    if (b >= 1048576) return (b / 1048576).toFixed(1) + ' МБ';
    if (b >= 1024)    return (b / 1024).toFixed(0) + ' КБ';
    return b + ' Б';
  };
  return `<span style="display:flex;gap:10px;font-size:.78rem;color:var(--text-sec);flex-wrap:wrap">
    <span>Клиентов: <b style="color:var(--text)">${st.tcp_clients ?? 0}</b></span>
    <span>TX: <b style="color:var(--text)">${fmtBytes(st.bytes_tx)}</b></span>
    <span>RX: <b style="color:var(--text)">${fmtBytes(st.bytes_rx)}</b></span>
    <span>Запросов: <b style="color:var(--text)">${st.requests ?? 0}</b></span>
    ${st.errors > 0 ? `<span style="color:var(--red)">Ошибок: <b>${st.errors}</b></span>` : ''}
  </span>`;
}

// ── Defaults ──────────────────────────────────────────────────────────────────
function _defaultPortCfg(port) {
  const idx = PORTS.indexOf(port);
  return {
    enabled: false,
    mode: 'modbus_tcp',
    tcp_port: 502 + idx,
    baudrate: 9600,
    parity: 'none',
    stopbits: 2,
    databits: 8,
    fast_modbus_probe: true,
  };
}

function _modeHint(mode) {
  const hints = {
    disabled:     'Порт не используется шлюзом.',
    modbus_tcp:   'Modbus TCP сервер (MBAP↔RTU). SCADA/ПЛК подключается как Modbus TCP мастер.',
    rtu_over_tcp: 'Сырые RTU-фреймы передаются через TCP без MBAP-обёртки.',
    transparent:  'Прозрачный мост: все байты RS-485 ↔ TCP.',
  };
  return hints[mode] || '';
}

})();
