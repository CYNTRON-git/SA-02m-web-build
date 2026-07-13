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

