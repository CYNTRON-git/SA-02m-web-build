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
  if (diff < 60) return diff + ' ' + uiT('сек. назад');
  if (diff < 3600) return Math.floor(diff / 60) + ' ' + uiT('мин. назад');
  return Math.floor(diff / 3600) + ' ' + uiT('ч. назад');
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
  // Stand-down after a confirmed cloud refusal (agent contract §4): the board
  // erased its binding and waits for «Привязать заново».
  revoked:         ['Доступ отозван', 'err'],
  unlinked:        ['Отвязано в облаке', 'warn'],
  // The cloud refused the board but its binding could NOT be erased (a wipe
  // error) — the agent keeps retrying; never «Подключено», never «отвязано».
  unlink_failed:   ['Ошибка отвязки', 'err'],
  unknown:         ['Нет данных', 'unk'],
};

// Reason class written by the agent's stand-down → human RU (DICT-translated).
const CLOUD_REASON_MAP = {
  revoked:  'доступ отозван владельцем',
  unlinked: 'устройство отвязано в облаке',
  unknown:  'облако не признаёт устройство',
  // unlink_failed: the agent writes the code, the errno stays in `detail`/journal.
  wipe_failed: 'не удалось стереть файлы привязки в /etc/sa02m-cloud — подробности в журнале агента; попытка повторяется',
};

// ISO stamp from the agent → «N мин. назад»; a stamp that does not parse is
// shown as-is rather than as NaN.
function cloudRelativeStamp(iso) {
  const ms = Date.parse(String(iso || ''));
  return isNaN(ms) ? String(iso || '') : cloudRelativeTime(Math.floor(ms / 1000));
}
// The «Причина» line for the 409 state (the claim endpoint answers 409 until
// the owner detaches the device) — explains «Уже привязано» without the journal.
const CLOUD_ALREADY_CLAIMED_REASON = 'в облаке доступ отозван или устройство числится за владельцем; нажмите «Отвязать» в облаке';
// The «Туннель» / «Последний отчёт» rows describe a LIVE binding and nothing
// else: they render only in `active`. Hidden rows are also emptied, so no
// value from a previous state can survive a transition (bench 1.135: a card
// in already_claimed still read «Туннель: Работает / 19 сек. назад»).
const CLOUD_LIVE_STATE = 'active';

function cloudIsStandDown(d) {
  return !!d && (d.state === 'revoked' || d.state === 'unlinked');
}

function cloudPairLabel(d) {
  return uiT(cloudIsStandDown(d) ? 'Привязать заново' : 'Подключить к облаку');
}

let _cloudStandDown = false;

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

/* -- «Управление из облака» (the sa02m-cloud-control unit) -----------------
   Moved here from the «Умный дом» card in 1.0.6.29 (markup: #cloud-ctrl-*).
   Its data does NOT come from cloud.cgi: the `cloud_control` block rides the
   Alice-API status poll (app/alice.js -> window.sa02mAliceOnData), and its
   actions go to the same sa02m_alice_api.cgi endpoint - no second CGI poll is
   added, and the payload here is the ALICE one, never cloudRenderStatus's. */
const CLOUD_CTRL_STATE_MAP = {
  disabled: ['Отключено', 'unk'],
  offline: ['Шлюз недоступен', 'err'],
  connecting: ['Подключение', 'warn'],
  connected: ['Подключено', 'ok'],
  error: ['Ошибка', 'err'],
  missing_deps: ['Нет зависимостей', 'err'],
  missing_identity: ['Нет идентификации', 'warn'],
  unknown: ['Нет данных', 'unk'],
};

// Error tokens the cloud-profile client writes (`error` in status-cloud.json)
// to RU phrases (DICT-translated). An unknown token still shows raw, as the
// Alice card does for the gateway's own reasons.
const CLOUD_CTRL_ERROR_MAP = {
  file_not_found: 'файл не найден',
  gateway_unreachable: 'сервер недоступен',
  gateway_disconnected: 'соединение потеряно',
  revoked: 'доступ отозван',
  'invalid credential': 'неверные учётные данные',
  'too many requests': 'слишком много запросов',
  'cloud control not enabled on the host': 'управление не включено на сервере',
  'cloud identity missing': 'нет облачной идентификации',
  missing_identity: 'нет облачной идентификации',
};

function cloudCtrlErrorText(token) {
  const ru = CLOUD_CTRL_ERROR_MAP[String(token)];
  return ru ? uiT(ru) : String(token);
}

// Its own line (#cloud-ctrl-msg), never the card's #cloud-msg: the pairing
// actions own that one and the two would overwrite each other. Transient
// success notices auto-clear, errors stay until the state changes, `ok === null`
// is a neutral hint - the behaviour that came with the control.
const CLOUD_CTRL_MSG_TTL_MS = 5000;
let _cloudCtrlMsgTimer = null;

function cloudCtrlSetMsg(text, ok) {
  const msg = $('cloud-ctrl-msg');
  if (!msg) return;
  if (_cloudCtrlMsgTimer) { clearTimeout(_cloudCtrlMsgTimer); _cloudCtrlMsgTimer = null; }
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg' + (ok === null ? '' : (ok ? ' is-ok' : ' is-err'));
  if (ok === true) {
    _cloudCtrlMsgTimer = setTimeout(function () {
      _cloudCtrlMsgTimer = null;
      const el = $('cloud-ctrl-msg');
      // Only clear what is still this notice - a newer message owns itself.
      if (el && !el.hidden && el.textContent === text) cloudCtrlSetMsg('', true);
    }, CLOUD_CTRL_MSG_TTL_MS);
  }
}

function cloudRenderControl(d) {
  if (!$('cloud-card')) return;
  const cc = d && d.cloud_control;
  const badge = $('cloud-ctrl-state');
  const btn = $('cloud-btn-ctrl');
  if (!cc) {
    // Older backend without the block (cached CGI): say so, do not guess.
    cloudSetBadgeEl(badge, 'Нет данных', 'unk');
    if (btn) btn.disabled = true;
    return;
  }
  const enabled = !!cc.enabled;
  const st = enabled ? (cc.state || 'unknown') : 'disabled';
  const entry = CLOUD_CTRL_STATE_MAP[st] || CLOUD_CTRL_STATE_MAP.unknown;
  cloudSetBadgeEl(badge, entry[0], entry[1]);
  // cloud_enrolled is tri-state (true / false / null = unknowable): only an
  // explicit false locks the button - an unknown lets the operator try, and
  // the client then reports `missing_identity` honestly.
  const notEnrolled = cc.cloud_enrolled === false;
  if (btn) {
    btn.dataset.action = enabled ? 'cloud_control_disable' : 'cloud_control_enable';
    btn.className = 'btn btn-sm ' + (enabled ? 'btn-danger' : 'btn-primary');
    btn.textContent = uiT(enabled ? 'Выключить' : 'Включить');
    btn.disabled = notEnrolled && !enabled;
    btn.title = notEnrolled ? uiT('Сначала привяжите устройство к облаку') : '';
  }
  if (notEnrolled && !enabled) {
    cloudCtrlSetMsg(uiT('Сначала привяжите устройство к облаку'), null);
  } else if (enabled && entry[1] === 'err' && cc.error) {
    cloudCtrlSetMsg(uiT(entry[0]) + ': ' + cloudCtrlErrorText(cc.error), false);
  } else {
    const msg = $('cloud-ctrl-msg');
    // Leave a transient «Сохранено» in place; clear only our own hint/error.
    if (msg && !msg.classList.contains('is-ok')) cloudCtrlSetMsg('', true);
  }
}

// Bounded follow-up refreshes after a unit toggle - the unit start + first
// connect take a few seconds and the 5 s poll would look frozen. Two chained
// timeouts, never an interval (the endpoint probes the gateway in-CGI).
function cloudCtrlRefreshSoon() {
  const again = function () {
    if (typeof window.sa02mAliceRefresh === 'function') window.sa02mAliceRefresh();
  };
  setTimeout(again, 2000);
  setTimeout(again, 6000);
}

// The shared Alice pipeline is app/alice.js's; the handles are read lazily so a
// load-order slip degrades to a message rather than a TypeError.
window.cloudToggleControl = async function cloudToggleControl() {
  const btn = $('cloud-btn-ctrl');
  const action = btn && btn.dataset.action === 'cloud_control_disable' ? 'cloud_control_disable' : 'cloud_control_enable';
  if (btn) btn.disabled = true;
  cloudCtrlSetMsg(uiT('Сохранение'), true);
  try {
    if (typeof window.sa02mAliceApi !== 'function') throw new Error('alice api missing');
    const d = await window.sa02mAliceApi({ action: action });
    if (!d.ok) {
      cloudCtrlSetMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      cloudCtrlSetMsg(uiT('Сохранено'), true);
      cloudCtrlRefreshSoon();
    }
    if (typeof window.sa02mAliceRefresh === 'function') await window.sa02mAliceRefresh();
  } catch (e) {
    cloudCtrlSetMsg(uiT('Ошибка запроса API Алисы'), false);
  }
};

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

  const live = d.state === CLOUD_LIVE_STATE && svcActive;
  if (live && d.tunnel) {
    cloudShowRow('cloud-row-tunnel', true);
    const tEntry = CLOUD_TUNNEL_MAP[d.tunnel] || [d.tunnel, 'unk'];
    cloudSetBadgeEl($('cloud-tunnel-state'), tEntry[0], tEntry[1]);
  } else {
    cloudShowRow('cloud-row-tunnel', false);
    cloudSetBadgeEl($('cloud-tunnel-state'), '…', 'unk');
  }

  if (d.serial) {
    cloudShowRow('cloud-row-serial', true);
    const serial = $('cloud-dev-serial');
    if (serial) serial.textContent = d.serial;
  } else {
    cloudShowRow('cloud-row-serial', false);
  }

  const tsEl = $('cloud-last-ts');
  if (live && d.last_heartbeat) {
    cloudShowRow('cloud-row-ts', true);
    if (tsEl) tsEl.textContent = cloudRelativeTime(d.last_heartbeat);
  } else {
    cloudShowRow('cloud-row-ts', false);
    if (tsEl) tsEl.textContent = '—';
  }

  // Stand-down: the reason + time line, and the pair button relabelled. The
  // «Соединение» badge carries the state itself; «Туннель»/«Последний отчёт»
  // stay hidden (the stand-down status has neither key).
  const standDown = cloudIsStandDown(d);
  _cloudStandDown = standDown;
  const info = $('cloud-unlink-info');
  if (info) {
    if (standDown) {
      const why = CLOUD_REASON_MAP[d.reason_class] || CLOUD_REASON_MAP[d.state] || '';
      info.textContent = uiT('Причина') + ': ' + (why ? uiT(why) : String(d.reason || '')) +
        (d.unlinked_at ? ' · ' + cloudRelativeStamp(d.unlinked_at) : '');
      info.hidden = false;
    } else if (d.state === 'already_claimed') {
      // The 409 state carries `since` (agent ≥ 1.0.6.26) — fall back to `ts`.
      const at = Number(d.since || d.ts || 0);
      info.textContent = uiT('Причина') + ': ' + uiT(CLOUD_ALREADY_CLAIMED_REASON) +
        (at ? ' · ' + cloudRelativeTime(at) : '');
      info.hidden = false;
    } else if (d.state === 'unlink_failed') {
      const why = CLOUD_REASON_MAP[d.reason];
      info.textContent = uiT('Причина') + ': ' + (why ? uiT(why) : String(d.reason || ''));
      info.hidden = false;
    } else {
      info.hidden = true;
      info.textContent = '';
    }
  }
  const pairBtnEl = $('cloud-btn-pair');
  if (pairBtnEl && !pairBtnEl.disabled) pairBtnEl.textContent = cloudPairLabel(d);
  // While the binding could not be erased the agent is retrying the wipe and
  // nothing polls the pairing trigger — a live button would report a code
  // that never comes. Locked with the reason; unlocked in every other state.
  if (pairBtnEl) {
    const locked = d.state === 'unlink_failed';
    pairBtnEl.disabled = locked;
    pairBtnEl.title = locked ? uiT('Сначала нужно стереть привязку — агент повторяет попытку') : '';
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
      btn.textContent = uiT(_cloudStandDown ? 'Привязать заново' : 'Подключить к облаку');
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

// «Управление из облака» rides app/alice.js's poll - registered once, at
// DOMContentLoaded: that file is loaded AFTER this one, so window.sa02mAliceOnData
// does not exist yet at parse time. A listener registered late still gets the
// last payload at once (aliceOnData replays it), so the control never waits a tick.
function cloudCtrlInit() {
  if (!$('cloud-card')) return;
  if (typeof window.sa02mAliceOnData === 'function') window.sa02mAliceOnData(cloudRenderControl);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', cloudCtrlInit);
} else {
  cloudCtrlInit();
}

window.cloudScrollIntoView = function cloudScrollIntoView() {
  const card = $('cloud-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
};

})();
