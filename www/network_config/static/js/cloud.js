/* SA-02m — Облако (вкладка «Управление») */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

function cloudBadge(text, kind) {
  const cls = kind === 'ok' ? 'badge-ok' : kind === 'warn' ? 'badge-warn' : kind === 'err' ? 'badge-err' : 'badge-unk';
  return '<span class="badge ' + cls + '">' + escHtml(String(text)) + '</span>';
}

function cloudRelativeTime(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return diff + uiT('с назад');
  if (diff < 3600) return Math.floor(diff / 60) + uiT('м назад');
  return Math.floor(diff / 3600) + uiT('ч назад');
}

function cloudFmtRemaining(expiresAt) {
  const left = expiresAt - Math.floor(Date.now() / 1000);
  if (left <= 0) return uiT('истёк');
  const m = Math.floor(left / 60);
  const s = left % 60;
  return m > 0 ? (m + ' ' + uiT('мин') + ' ' + s + ' ' + uiT('с')) : (s + ' ' + uiT('с'));
}

const CLOUD_STATE_MAP = {
  active:          ['Подключено', 'ok'],
  standby:         ['Не подключено', 'unk'],
  pairing:         ['Ожидание', 'warn'],
  pair_expired:    ['Код истёк', 'warn'],
  already_claimed: ['Уже привязано', 'err'],
  claim_failed:    ['Облако недоступно', 'err'],
  enrolling:       ['Активация', 'warn'],
  enroll_failed:   ['Ошибка активации', 'err'],
  activating:      ['Активация', 'warn'],
  activation_failed: ['Ошибка активации', 'err'],
  unknown:         ['Нет данных', 'unk'],
};

const CLOUD_TUNNEL_MAP = {
  running:      ['Работает', 'ok'],
  failed:       ['Ошибка запуска', 'err'],
  frpc_missing: ['frpc не установлен', 'err'],
  no_config:    ['Нет конфигурации', 'unk'],
};

let _cloudPollTimer = null;
let _cloudCopyToastTimer = null;
// Last known cloud reachability (from status `server_reachable`); null until the
// first poll. Gates the online connect actions — see cloudApplyReachability.
let _cloudReachable = null;

function cloudSetMsg(text, ok) {
  const msg = $('cloud-msg');
  if (!msg) return;
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg ' + (ok ? 'is-ok' : 'is-err');
}

function cloudSetBadgeEl(el, text, kind) {
  if (!el) return;
  el.innerHTML = cloudBadge(uiT(text), kind);
}

function cloudShowRow(id, show) {
  const row = $(id);
  if (row) row.hidden = !show;
}

// Gate the online connect actions on server reachability. When the cloud is
// unreachable, hide the pair button and disable the enroll-token button (both
// paths need the WAN and fail identically offline), and surface the reserved
// «нет интернета» notes in their slot — no layout jump. Fail closed: anything
// other than an explicit `true` (undefined/probe error) counts as offline.
function cloudApplyReachability(d) {
  const reachable = d && d.server_reachable === true;
  _cloudReachable = reachable;
  const pairBtn = $('cloud-btn-pair');
  const offline = $('cloud-offline');
  const actBtn = $('cloud-btn-activate');
  const tokOffline = $('cloud-token-offline');
  if (pairBtn) pairBtn.hidden = !reachable;
  if (offline) offline.hidden = reachable;
  if (actBtn) actBtn.disabled = !reachable;
  if (tokOffline) tokOffline.hidden = reachable;
}

async function cloudPostAction(body) {
  const r = await fetch('cgi-bin/cloud.cgi', {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin',
    body: JSON.stringify(body),
  });
  return r.json();
}

function cloudAgentWantEnable(d) {
  // Off when stopped/disabled/masked — user can turn back on.
  if (!d) return true;
  if (d.service_active === 'active') return false;
  const en = String(d.service_enabled || '');
  if (en === 'disabled' || en === 'masked') return true;
  return true;
}

function cloudUpdateAgentBtn(d) {
  const btn = $('cloud-btn-agent');
  if (!btn) return;
  const wantOn = cloudAgentWantEnable(d);
  btn.disabled = false;
  btn.dataset.action = wantOn ? 'enable' : 'disable';
  if (wantOn) {
    btn.className = 'btn btn-sm btn-primary';
    btn.textContent = uiT('Включить агент');
  } else {
    btn.className = 'btn btn-sm btn-danger';
    btn.textContent = uiT('Отключить агент');
  }
}

function cloudRenderStatus(d) {
  const card = $('cloud-card');
  if (!card) return;

  const svcActive = d.service_active === 'active';
  const en = String(d.service_enabled || '');
  const svcDisabled = en === 'disabled' || en === 'masked';
  if (svcActive) {
    cloudSetBadgeEl($('cloud-svc-state'), 'Работает', 'ok');
  } else if (svcDisabled) {
    cloudSetBadgeEl($('cloud-svc-state'), 'Отключен', 'err');
  } else {
    cloudSetBadgeEl($('cloud-svc-state'), d.service_active || 'не запущен', 'unk');
  }

  cloudUpdateAgentBtn(d);

  const stateEntry = CLOUD_STATE_MAP[d.state] || ['Неизвестно', 'unk'];
  if (!svcActive && svcDisabled) {
    cloudSetBadgeEl($('cloud-conn-state'), 'Отключен', 'unk');
  } else {
    cloudSetBadgeEl($('cloud-conn-state'), stateEntry[0], stateEntry[1]);
  }

  if (d.tunnel && svcActive) {
    cloudShowRow('cloud-row-tunnel', true);
    const tEntry = CLOUD_TUNNEL_MAP[d.tunnel] || [d.tunnel, 'unk'];
    cloudSetBadgeEl($('cloud-tunnel-state'), tEntry[0], tEntry[1]);
  } else {
    cloudShowRow('cloud-row-tunnel', false);
  }

  if (d.serial) {
    cloudShowRow('cloud-row-serial', true);
    const serial = $('cloud-dev-serial');
    if (serial) serial.textContent = d.serial;
  } else {
    cloudShowRow('cloud-row-serial', false);
  }

  if (d.last_heartbeat && svcActive) {
    cloudShowRow('cloud-row-ts', true);
    const ts = $('cloud-last-ts');
    if (ts) ts.textContent = cloudRelativeTime(d.last_heartbeat);
  } else {
    cloudShowRow('cloud-row-ts', false);
  }

  const idle = $('cloud-pair-idle');
  const active = $('cloud-pair-active');
  const fallback = $('cloud-token-fallback');
  const isConnected = d.state === 'active';
  const agentOff = !svcActive && svcDisabled;

  if (agentOff) {
    if (idle) idle.hidden = true;
    if (active) active.hidden = true;
    if (fallback) fallback.hidden = true;
    return;
  }

  if (d.state === 'pairing' && d.claim_code) {
    if (idle) idle.hidden = true;
    if (active) active.hidden = false;
    const codeText = $('cloud-pair-code-text');
    if (codeText) codeText.textContent = d.claim_code;
    const timer = $('cloud-pair-timer');
    if (timer) timer.textContent = d.expires_at ? cloudFmtRemaining(d.expires_at) : '—';
  } else {
    if (idle) idle.hidden = isConnected;
    if (active) active.hidden = true;
  }

  if (fallback) fallback.hidden = isConnected;
  if (idle && isConnected) idle.hidden = true;

  cloudApplyReachability(d);
}

async function cloudRefreshStatus() {
  if (!$('cloud-card')) return;
  try {
    const r = await fetch('cgi-bin/cloud.cgi', { cache: 'no-store', credentials: 'same-origin' });
    const d = await r.json();
    cloudRenderStatus(d);
  } catch (e) {
    cloudSetBadgeEl($('cloud-conn-state'), 'Ошибка', 'err');
  }
}

window.cloudCopyPairCode = function cloudCopyPairCode() {
  const codeEl = $('cloud-pair-code-text');
  if (!codeEl) return;
  const code = codeEl.textContent.trim();
  if (!code || code.startsWith('·')) return;

  const done = function () {
    const t = $('cloud-copy-toast');
    if (!t) return;
    t.classList.add('show');
    clearTimeout(_cloudCopyToastTimer);
    _cloudCopyToastTimer = setTimeout(function () { t.classList.remove('show'); }, 1500);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(code).then(done).catch(function () {});
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = code;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { if (document.execCommand('copy')) done(); } catch (err) {}
  document.body.removeChild(ta);
};

window.cloudRecheck = async function cloudRecheck() {
  // Force a fresh reachability probe (busts the 60 s CGI cache).
  cloudSetMsg(uiT('Проверка соединения'), true);
  try {
    const r = await fetch('cgi-bin/cloud.cgi?recheck=1', { cache: 'no-store', credentials: 'same-origin' });
    const d = await r.json();
    cloudRenderStatus(d);
    cloudSetMsg('', true);
  } catch (e) {
    cloudSetBadgeEl($('cloud-conn-state'), 'Ошибка', 'err');
  }
};

window.cloudStartPairing = async function cloudStartPairing() {
  // Defensive: the pair button is hidden offline, but never fire a pairing
  // request (and its "code arrives shortly" message) when we know the WAN is down.
  if (_cloudReachable === false) {
    cloudSetMsg(uiT('нет интернета'), false);
    return;
  }
  const btn = $('cloud-btn-pair');
  if (btn) {
    btn.disabled = true;
    btn.textContent = uiT('Запрос кода');
  }
  try {
    const d = await cloudPostAction({ action: 'pair' });
    if (d.ok) {
      cloudSetMsg(uiT('Запрошен код сопряжения — появится здесь через несколько секунд'), true);
      setTimeout(cloudRefreshStatus, 2000);
      setTimeout(cloudRefreshStatus, 5000);
    } else {
      cloudSetMsg(d.error || uiT('Ошибка'), false);
    }
  } catch (e) {
    cloudSetMsg(uiT('Ошибка соединения с устройством'), false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = uiT('Подключить к облаку');
    }
  }
};

window.cloudCancelPairing = async function cloudCancelPairing() {
  try {
    await cloudPostAction({ action: 'cancel' });
    cloudSetMsg(uiT('Сопряжение отменено'), true);
  } catch (e) { /* ignore */ }
  setTimeout(cloudRefreshStatus, 1500);
};

window.cloudToggleAgent = async function cloudToggleAgent() {
  const btn = $('cloud-btn-agent');
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action === 'enable' ? 'enable' : 'disable';
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = uiT('…');
  try {
    const d = await cloudPostAction({ action: action });
    if (d.ok) {
      cloudSetMsg(
        action === 'disable'
          ? uiT('Агент отключён — запросы в облако остановлены')
          : uiT('Агент включён'),
        true
      );
      setTimeout(cloudRefreshStatus, 800);
      setTimeout(cloudRefreshStatus, 2500);
    } else {
      cloudSetMsg(d.error || uiT('Ошибка'), false);
      btn.disabled = false;
      btn.textContent = prev;
    }
  } catch (e) {
    cloudSetMsg(uiT('Ошибка соединения с устройством'), false);
    btn.disabled = false;
    btn.textContent = prev;
  }
};

window.cloudActivate = async function cloudActivate() {
  const tokenEl = $('cloud-token');
  const serverEl = $('cloud-server');
  const btn = $('cloud-btn-activate');
  const token = tokenEl ? tokenEl.value.trim() : '';
  const server = serverEl ? serverEl.value.trim() : '';

  if (!token) {
    cloudSetMsg(uiT('Введите токен активации'), false);
    return;
  }

  // The enroll-token path needs the WAN too — fail the same way offline.
  if (_cloudReachable === false) {
    cloudSetMsg(uiT('нет доступа к серверу'), false);
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = uiT('Подключаю');
  }

  try {
    const d = await cloudPostAction({ token: token, server: server });
    if (d.ok) {
      cloudSetMsg(d.message || uiT('Активация запущена'), true);
      if (tokenEl) tokenEl.value = '';
      setTimeout(cloudRefreshStatus, 3000);
      setTimeout(cloudRefreshStatus, 8000);
      setTimeout(cloudRefreshStatus, 15000);
    } else {
      cloudSetMsg(d.error || uiT('Ошибка'), false);
    }
  } catch (e) {
    cloudSetMsg(uiT('Ошибка соединения с устройством'), false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = uiT('Подключить по токену');
    }
  }
};

window.cloudTabInit = function cloudTabInit() {
  cloudRefreshStatus();
  if (_cloudPollTimer) clearInterval(_cloudPollTimer);
  _cloudPollTimer = setInterval(cloudRefreshStatus, 5000);
};

window.cloudTabDestroy = function cloudTabDestroy() {
  if (_cloudPollTimer) {
    clearInterval(_cloudPollTimer);
    _cloudPollTimer = null;
  }
};

window.cloudScrollIntoView = function cloudScrollIntoView() {
  const card = $('cloud-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
};

})();
