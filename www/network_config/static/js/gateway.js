/* ═══════════════════════════════════════════════════════════════════════════
   СА-02м  — Шлюз RS-485 → Ethernet
   gateway.js  v1.0.0
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

(function () {

// ── Constants ─────────────────────────────────────────────────────────────────
const PORTS   = ['COM1', 'COM2', 'COM3', 'COM4', 'COM5'];
const MODES   = [
  { value: 'disabled',     label: 'Отключён' },
  { value: 'modbus_tcp',   label: 'Modbus TCP (MBAP↔RTU)' },
  { value: 'rtu_over_tcp', label: 'RTU over TCP (raw RTU)' },
  { value: 'transparent',  label: 'Прозрачный (raw bytes)' },
];
const BAUDS   = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];
const PARITIES   = [
  { value: 'none', label: 'Нет (N)' },
  { value: 'even', label: 'Чётный (E)' },
  { value: 'odd',  label: 'Нечётный (O)' },
];
const STOPBITS   = [{ value: 1, label: '1' }, { value: 2, label: '2' }];
const DATABITS   = [7, 8];

// Default TCP ports per port index (502–506, 8502–8506, 9502–9506)
const DEFAULT_TCP_PORTS = { COM1: 502, COM2: 503, COM3: 504, COM4: 505, COM5: 506 };

// ── State ─────────────────────────────────────────────────────────────────────
let _config        = {};     // { COM1: {...}, COM2: {...}, ... }
let _status        = {};     // from gateway_status.cgi
let _activePort    = PORTS[0];
let _pollTimer     = null;
let _initialized   = false;
let _dirty         = {};     // ports with unsaved changes

// ── Init ──────────────────────────────────────────────────────────────────────
window.gatewayInit = async function gatewayInit() {
  if (_initialized) {
    _startPolling();
    return;
  }
  _initialized = true;
  _buildUI();
  await _loadAll();
  _startPolling();
};

window.gatewayDestroy = function gatewayDestroy() {
  _stopPolling();
};

// ── UI builder ────────────────────────────────────────────────────────────────
function _buildUI() {
  const pane = document.getElementById('tab-gateway');
  if (!pane) return;
  pane.innerHTML = `
    <div class="page-header">
      <h2>Шлюз RS-485 → Ethernet</h2>
      <p>Преобразователь интерфейсов RS-485/TCP: Modbus TCP, RTU over TCP, прозрачный режим</p>
    </div>

    <div id="gw-svc-bar" class="widget gw-svc-bar">
      <div class="gw-svc-row">
        <span id="gw-svc-badge" class="badge badge-unk">● Сервис —</span>
        <div style="margin-left:auto;display:flex;gap:6px">
          <button class="btn btn-sm" onclick="gatewayCtrl('start')">Запустить</button>
          <button class="btn btn-sm" onclick="gatewayCtrl('reload')">↺ Перезагрузить конфиг</button>
          <button class="btn btn-sm btn-warn" onclick="gatewayCtrl('restart')">⟳ Перезапустить</button>
          <button class="btn btn-sm btn-danger" onclick="gatewayCtrl('stop')">Остановить</button>
        </div>
      </div>
    </div>

    <div class="gw-layout">
      <div class="gw-port-tabs" id="gw-port-tabs">
        ${PORTS.map(p => `
          <button class="gw-port-tab${p === _activePort ? ' active' : ''}"
                  data-port="${p}" onclick="gatewaySelectPort('${p}')" type="button">
            <span class="gw-port-tab-name">${p}</span>
            <span class="gw-port-tab-badge" id="gw-tab-badge-${p}"></span>
          </button>`).join('')}
      </div>

      <div class="gw-port-panel widget" id="gw-port-panel">
        <div id="gw-form-area"><!-- filled by _renderPortForm --></div>
      </div>
    </div>`;
}

// ── Port tab switch ────────────────────────────────────────────────────────────
window.gatewaySelectPort = function(port) {
  _activePort = port;
  document.querySelectorAll('.gw-port-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.port === port);
  });
  _renderPortForm(port);
};

// ── Form renderer ─────────────────────────────────────────────────────────────
function _renderPortForm(port) {
  const area = document.getElementById('gw-form-area');
  if (!area) return;

  const cfg = _config[port] || _defaultPortCfg(port);
  const st  = (_status.ports || {})[port] || {};
  const mode = cfg.mode || 'disabled';
  const running = st.running || false;

  const dirty = _dirty[port] ? ' <span class="gw-dirty-dot" title="Несохранённые изменения">●</span>' : '';

  area.innerHTML = `
    <div class="gw-port-form-head">
      <div>
        <h3 style="margin:0 0 4px;font-size:1rem">${port}${dirty}</h3>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          ${_statusBadgeHtml(st, cfg)}
        </div>
      </div>
      <div class="gw-port-counters" id="gw-counters-${port}">
        ${_countersHtml(st)}
      </div>
    </div>

    <div class="gw-form-grid">
      <div class="form-section gw-section">
        <h4>Режим и TCP</h4>

        <div class="field toggle-field-row">
          <span class="toggle-field-label">Включить порт</span>
          <label class="toggle-inline">
            <input type="checkbox" class="toggle toggle-wide" id="gw-en-${port}"
                   ${cfg.enabled ? 'checked' : ''}
                   onchange="gatewayFieldChange('${port}')">
          </label>
        </div>

        <div class="field">
          <label>Режим работы</label>
          <select id="gw-mode-${port}" onchange="gatewayModeChange('${port}')">
            ${MODES.map(m =>
              `<option value="${m.value}"${mode === m.value ? ' selected' : ''}>${m.label}</option>`
            ).join('')}
          </select>
          <p class="field-hint" id="gw-mode-hint-${port}">${_modeHint(mode)}</p>
        </div>

        <div id="gw-tcp-fields-${port}" ${mode === 'disabled' ? 'style="display:none"' : ''}>
          <div class="field">
            <label>TCP-порт</label>
            <input type="number" id="gw-tcpport-${port}"
                   value="${cfg.tcp_port || DEFAULT_TCP_PORTS[port]}"
                   min="1" max="65535"
                   onchange="gatewayFieldChange('${port}')">
          </div>
        </div>

        <div id="gw-fmb-field-${port}"
             ${mode === 'modbus_tcp' ? '' : 'style="display:none"'}>
          <div class="field toggle-field-row">
            <span class="toggle-field-label">Fast Modbus probe (FC 0x47)</span>
            <label class="toggle-inline">
              <input type="checkbox" class="toggle toggle-wide" id="gw-fmb-${port}"
                     ${cfg.fast_modbus_probe !== false ? 'checked' : ''}
                     onchange="gatewayFieldChange('${port}')">
            </label>
          </div>
          <p class="field-hint">Отвечать локально на WB Fast Modbus probe без обращения к RS-485</p>
        </div>
      </div>

      <div class="form-section gw-section">
        <h4>Параметры RS-485</h4>

        <div class="field">
          <label>Скорость (бод)</label>
          <select id="gw-baud-${port}" onchange="gatewayFieldChange('${port}')">
            ${BAUDS.map(b =>
              `<option value="${b}"${parseInt(cfg.baudrate) === b ? ' selected' : ''}>${b}</option>`
            ).join('')}
          </select>
        </div>

        <div class="field">
          <label>Чётность</label>
          <select id="gw-parity-${port}" onchange="gatewayFieldChange('${port}')">
            ${PARITIES.map(p =>
              `<option value="${p.value}"${cfg.parity === p.value ? ' selected' : ''}>${p.label}</option>`
            ).join('')}
          </select>
        </div>

        <div class="field">
          <label>Стоп-биты</label>
          <select id="gw-stop-${port}" onchange="gatewayFieldChange('${port}')">
            ${STOPBITS.map(s =>
              `<option value="${s.value}"${parseInt(cfg.stopbits) === s.value ? ' selected' : ''}>${s.label}</option>`
            ).join('')}
          </select>
        </div>

        <div class="field">
          <label>Биты данных</label>
          <select id="gw-data-${port}" onchange="gatewayFieldChange('${port}')">
            ${DATABITS.map(d =>
              `<option value="${d}"${parseInt(cfg.databits) === d ? ' selected' : ''}>${d}</option>`
            ).join('')}
          </select>
        </div>
      </div>
    </div>

    <div class="btn-group gw-save-row">
      <button class="btn btn-primary" onclick="gatewaySavePort('${port}')">
        Сохранить ${port}
      </button>
      <button class="btn btn-sm" onclick="gatewayResetPort('${port}')">Сбросить</button>
    </div>
    <div class="gw-save-status" id="gw-save-status-${port}"></div>
  `;
}

// ── Mode change: show/hide conditional fields ─────────────────────────────────
window.gatewayModeChange = function(port) {
  const mode = document.getElementById(`gw-mode-${port}`)?.value || 'disabled';
  const tcpDiv = document.getElementById(`gw-tcp-fields-${port}`);
  const fmbDiv = document.getElementById(`gw-fmb-field-${port}`);
  const hint   = document.getElementById(`gw-mode-hint-${port}`);
  if (tcpDiv) tcpDiv.style.display = mode === 'disabled' ? 'none' : '';
  if (fmbDiv) fmbDiv.style.display = mode === 'modbus_tcp' ? '' : 'none';
  if (hint)   hint.textContent = _modeHint(mode);
  gatewayFieldChange(port);
};

window.gatewayFieldChange = function(port) {
  _dirty[port] = true;
  const dot = document.querySelector(`#gw-form-area .gw-dirty-dot`);
  if (!dot) {
    const h3 = document.querySelector(`#gw-form-area h3`);
    if (h3) h3.innerHTML += ' <span class="gw-dirty-dot" title="Несохранённые изменения">●</span>';
  }
};

// ── Save port ─────────────────────────────────────────────────────────────────
window.gatewaySavePort = async function(port) {
  const cfg = _readFormCfg(port);
  if (!cfg) return;

  _config[port] = cfg;
  const statusEl = document.getElementById(`gw-save-status-${port}`);
  if (statusEl) { statusEl.textContent = 'Сохранение…'; statusEl.className = 'gw-save-status'; }

  try {
    const resp = await fetch('/cgi-bin/gateway_config.cgi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ports: _config }),
    });
    const data = await resp.json();
    if (data.ok) {
      delete _dirty[port];
      _renderPortForm(port);   // re-render clears dirty dot
      _updateTabBadges();
      if (statusEl) { statusEl.textContent = ''; }
      if (typeof toast === 'function') toast(`${port} сохранён`, 'ok');
    } else {
      if (statusEl) { statusEl.textContent = 'Ошибка: ' + (data.error || '?'); statusEl.className = 'gw-save-status gw-save-err'; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Сетевая ошибка: ' + e.message; statusEl.className = 'gw-save-status gw-save-err'; }
  }
};

window.gatewayResetPort = function(port) {
  delete _dirty[port];
  _renderPortForm(port);
};

function _readFormCfg(port) {
  const getVal  = id => document.getElementById(id)?.value;
  const getChk  = id => document.getElementById(id)?.checked ?? false;
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
window.gatewayCtrl = async function(action) {
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
};

// ── Load / refresh ────────────────────────────────────────────────────────────
async function _loadAll() {
  await Promise.all([_loadConfig(), _loadStatus()]);
}

async function _loadConfig() {
  try {
    const resp = await fetch('/cgi-bin/gateway_config.cgi');
    const data = await resp.json();
    if (data.ports) {
      _config = data.ports;
      // Fill defaults for any missing port
      for (const p of PORTS) {
        if (!_config[p]) _config[p] = _defaultPortCfg(p);
      }
    }
    _renderPortForm(_activePort);
    _updateTabBadges();
  } catch (e) {
    console.error('[gateway] loadConfig:', e);
  }
}

async function _loadStatus() {
  try {
    const resp = await fetch('/cgi-bin/gateway_status.cgi');
    _status = await resp.json();
    _updateServiceBadge();
    _updateTabBadges();
    _updateCounters();
  } catch (e) {
    console.error('[gateway] loadStatus:', e);
  }
}

function _startPolling() {
  _stopPolling();
  _pollTimer = setInterval(_loadStatus, 5000);
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ── UI updaters ────────────────────────────────────────────────────────────────
function _updateServiceBadge() {
  const badge = document.getElementById('gw-svc-badge');
  if (!badge) return;
  const active = _status.service_active;
  badge.className = 'badge ' + (active ? 'badge-ok' : 'badge-err');
  badge.textContent = '● Сервис ' + (active ? 'активен' : 'остановлен');
}

function _updateTabBadges() {
  const portStatus = (_status.ports) || {};
  for (const port of PORTS) {
    const el  = document.getElementById(`gw-tab-badge-${port}`);
    if (!el) continue;
    const st  = portStatus[port];
    const cfg = _config[port];
    if (!cfg || !cfg.enabled) {
      el.textContent = '';
      el.className   = 'gw-port-tab-badge';
    } else if (st && st.running) {
      el.textContent = '●';
      el.className   = 'gw-port-tab-badge gw-badge-ok';
    } else {
      el.textContent = '●';
      el.className   = 'gw-port-tab-badge gw-badge-err';
    }
  }
}

function _updateCounters() {
  const portStatus = (_status.ports) || {};
  const el = document.getElementById(`gw-counters-${_activePort}`);
  if (!el) return;
  const st = portStatus[_activePort];
  if (!st) return;
  el.innerHTML = _countersHtml(st);
  // also refresh status badge inside active port form
  const formBadgeEl = document.querySelector('#gw-form-area .gw-port-form-head .badge');
  if (formBadgeEl) {
    const cfg = _config[_activePort] || {};
    formBadgeEl.outerHTML = _statusBadgeHtml(st, cfg);
  }
}

function _countersHtml(st) {
  if (!st || !st.running) return '';
  const fmtBytes = b => {
    b = parseInt(b) || 0;
    if (b >= 1048576) return (b/1048576).toFixed(1) + ' МБ';
    if (b >= 1024)    return (b/1024).toFixed(0) + ' КБ';
    return b + ' Б';
  };
  return `<div class="gw-counters">
    <span title="TCP-клиентов">Клиентов: <b>${st.tcp_clients ?? 0}</b></span>
    <span title="Байт TX">TX: <b>${fmtBytes(st.bytes_tx)}</b></span>
    <span title="Байт RX">RX: <b>${fmtBytes(st.bytes_rx)}</b></span>
    <span title="Запросов">Запросов: <b>${st.requests ?? 0}</b></span>
    ${st.errors > 0 ? `<span class="gw-counter-err" title="Ошибок">Ошибок: <b>${st.errors}</b></span>` : ''}
  </div>`;
}

function _statusBadgeHtml(st, cfg) {
  if (!cfg || !cfg.enabled) {
    return '<span class="badge badge-unk">Отключён</span>';
  }
  if (st && st.running) {
    const modeLabel = MODES.find(m => m.value === cfg.mode)?.label || cfg.mode;
    return `<span class="badge badge-ok">● Работает</span>
            <span class="badge" style="background:var(--bg-panel);color:var(--text-sec);border:1px solid var(--border)">
              ${modeLabel} :${cfg.tcp_port || '?'}</span>`;
  }
  const err = (st && st.last_error) ? ` — ${st.last_error}` : '';
  return `<span class="badge badge-err">● Остановлен${err}</span>`;
}

// ── Helpers ────────────────────────────────────────────────────────────────────
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
    modbus_tcp:   'Modbus TCP сервер (MBAP↔RTU). SCADA/ПЛК подключается как Modbus TCP мастер на указанный TCP-порт.',
    rtu_over_tcp: 'Сырые RTU-фреймы передаются через TCP без MBAP-обёртки. TCP-порт 8502+N по умолчанию.',
    transparent:  'Прозрачный мост: все байты RS-485 ↔ TCP. Подходит для любого протокола поверх RS-485.',
  };
  return hints[mode] || '';
}

})();
