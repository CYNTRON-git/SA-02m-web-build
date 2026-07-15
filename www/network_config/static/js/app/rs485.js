/* SA-02m Web Interface -- RS-485 CARDS (RS-485 device cards on the dashboard).
   Extracted from app.js (F10 decomposition). Plain classic script sharing the
   global scope; original load order preserved. See index.html for the ordered
   <script> tags. */
'use strict';

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

/** TX and RX on ONE row (was two). Each side is a label+value pair spread apart. */
function rs485TxRxRowHtml(txVal, rxVal) {
  return (
    '<div class="rs485-row rs485-txrx">' +
      '<span class="rs485-txrx-item"><span class="rl">TX</span><span class="rv">' + txVal + '</span></span>' +
      '<span class="rs485-txrx-item"><span class="rl">RX</span><span class="rv">' + rxVal + '</span></span>' +
    '</div>'
  );
}

/** One module chip, coloured from its OWN tri-state (never the port-level flag):
    online===true → cyan live-online; online===false → red live-offline;
    online==null (source "scan") → grey last-scan chip, NEVER red/green (honesty, R2).
    A scan-only module mixed onto a bridge-owned port thus stays grey, not a false red. */
function rs485ModuleChipHtml(m) {
  const online = m ? m.online : null;
  const isScan = (online == null) || (m && m.source === 'scan');
  let state;
  let titleAttr = '';
  if (!isScan && online === true) {
    state = 'is-on';
  } else if (!isScan && online === false) {
    state = 'is-off';
  } else {
    state = 'is-scan';
    titleAttr = ' title="' + escHtml(uiT('по последнему сканированию')) + '"';
  }
  const addr = (m && m.addr != null) ? (m.addr + '\u00b7') : '';
  const label = addr + ((m && m.model) ? m.model : uiT('модуль'));
  return '<span class="rs485-chip ' + state + '"' + titleAttr + '>' + escHtml(label) + '</span>';
}

/** Date-only string for a scan timestamp (epoch seconds); '' when unknown. */
function rs485ScanDateStr(ts) {
  const n = Number(ts);
  if (!isFinite(n) || n <= 0) return '';
  const d = new Date(n * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/** Module sub-block below TX/RX: our-model chips + third-party summary + scan badge. */
function rs485ModulesHtml(mods) {
  if (!mods) return '';
  const ours = mods.ours || [];
  const tp = mods.third_party || {};
  const live = !!mods.live;
  const total = tp.total | 0;
  if (!ours.length && total <= 0) return '';
  let html = '';
  if (ours.length) {
    let chips = '';
    for (let i = 0; i < ours.length; i += 1) chips += rs485ModuleChipHtml(ours[i]);
    html += '<div class="rs485-chips">' + chips + '</div>';
  }
  if (!live) {
    const dateStr = rs485ScanDateStr(mods.ts);
    const titleAttr = dateStr ? (' title="' + escHtml(uiT('ростер по сканированию от') + ' ' + dateStr) + '"') : '';
    html += '<div class="rs485-mods-note"' + titleAttr + '>' + escHtml(uiT('по последнему сканированию')) + '</div>';
  }
  if (total > 0) {
    let line = escHtml(uiT('сторонних')) + ': ' + total;
    if (tp.online != null) line += ', ' + escHtml(uiT('онлайн')) + ' ' + (tp.online | 0);
    html += '<div class="rs485-mods-tp">' + line + '</div>';
  }
  return '<div class="rs485-mods">' + html + '</div>';
}

function rs485SkeletonCardHtml(n) {
  return (
    '<div class="rs485-hdr"><span class="rs485-name">' + escHtml(uiT(rs485PortLabel(n))) + '</span><span class="rs485-dot idle" aria-hidden="true"></span></div>' +
    '<div class="rs485-dev rs485-dev-reserved" aria-hidden="true">\u00a0</div>' +
    rs485TxRxRowHtml('0', '0') +
    rs485ErrSlotHtml(0, 0, 0) +
    '<div class="rs485-mods rs485-mods-reserved" aria-hidden="true"> </div>'
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

    const err  = rs485ErrSlotHtml(errDelta.fe, errDelta.pe, errDelta.oe);
    const devLabel = absent ? uiT('нет опроса') : (disabled ? uiT('статистика отключена') : p.dev);
    const mods = (!absent && !disabled) ? rs485ModulesHtml(p.modules) : '';

    card.innerHTML =
      '<div class="rs485-hdr"><span class="rs485-name">' + escHtml(uiT(rs485PortLabel(p.n))) + '</span><span class="rs485-dot ' + dotClass + '" title="' + escHtml(dotTitle) + '"></span></div>' +
      '<div class="rs485-dev">' + escHtml(devLabel) + '</div>' +
      rs485TxRxRowHtml(fmtNum(p.tx), fmtNum(p.rx)) +
      err +
      mods;
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

