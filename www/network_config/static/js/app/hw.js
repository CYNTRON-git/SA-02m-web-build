/* SA-02m Web Interface -- HW GPIO CONTROL (hardware GPIO block on the
   dashboard). Extracted from app.js (F10 decomposition). Plain classic script
   sharing the global scope; original load order preserved. See index.html for
   the ordered <script> tags. */
'use strict';

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

// Channels rendered as a single toggle button (green when ON, grey when OFF).
// usb_power keeps its power-state status line + Сброс button.
const HW_TOGGLE_CHANNELS = new Set(['do', 'beeper', 'alarm_led']);

function hwToggleBtn(channel) {
  return document.querySelector('.hw-btns[data-hw-ch="' + channel + '"] .hw-toggle-btn');
}

/** Reflect a toggle channel's logical state on its single button.
 *  ON → green (hw-on), OFF → grey (hw-off), unknown/-1 → «н/д» (na). */
function updateHwToggleBtn(channel, nv) {
  const btn = hwToggleBtn(channel);
  if (!btn) return;
  const words = HW_STATE_WORDS[channel] || ['ВЫКЛ', 'ВКЛ'];
  btn.classList.remove('hw-active-blue', 'hw-on', 'hw-off', 'na');
  btn.dataset.cur = String(nv);
  if (nv === -1) {
    btn.textContent = uiT('н/д');
    btn.classList.add('na');
    return;
  }
  btn.textContent = uiT(nv ? words[1] : words[0]);
  btn.classList.add(nv ? 'hw-on' : 'hw-off');
}

function applyHwChannel(stId, channel, v) {
  const nv = hwLogicalFromPayload(v);
  if (HW_TOGGLE_CHANNELS.has(channel)) {
    updateHwToggleBtn(channel, nv);
    return;
  }
  // usb_power (and any non-toggle channel): status-line rendering as before.
  const el = document.getElementById(stId);
  const words = HW_STATE_WORDS[channel] || ['ВЫКЛ', 'ВКЛ'];
  if (!el) return;
  if (nv === -1) {
    el.textContent = uiT('н/д');
    el.className = 'hw-status-val na';
    return;
  }
  el.textContent = uiT(nv ? words[1] : words[0]);
  el.className = 'hw-status-val ' + (nv ? 'on' : 'off');
}

/** Toggle a channel: flip its current logical state and push it to the backend.
 *  «н/д» (unknown) is treated as OFF → a click turns it ON. Disabled buttons
 *  (pin not configured) are inert. */
function toggleHw(channel) {
  const btn = hwToggleBtn(channel);
  if (!btn || btn.disabled) return;
  const cur = parseInt(btn.dataset.cur, 10);
  setHw(channel, cur === 1 ? 0 : 1);
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

  fetch('cgi-bin/hw_set.cgi', {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/x-www-form-urlencoded' }),
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
        // Кнопка «Сброс» горит голубым (как активный пункт меню) пока идёт сброс
        // — гаснет, когда питание USB снова появилось (по истечении sec).
        if (btn) btn.classList.add('hw-active-blue');
        applyHwChannel('hw-usb-st', 'usb_power', 0);
        pendingUsbPowerVal = 0;
        pendingUsbPowerUntil = Date.now() + (sec + 2) * 1000;
        bumpMainStatusEpoch();
        fetchStatusMain(true);
        window.setTimeout(function () {
          pendingUsbPowerVal = null;
          pendingUsbPowerUntil = 0;
          if (btn) { btn.disabled = false; btn.classList.remove('hw-active-blue'); }
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
  fetch('cgi-bin/hw_set.cgi', {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/x-www-form-urlencoded' }),
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

