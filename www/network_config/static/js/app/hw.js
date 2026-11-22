/* SA-02m Web Interface -- HW GPIO CONTROL (hardware GPIO block on the
   dashboard). Extracted from app.js (F10 decomposition). Plain classic script
   sharing the global scope; original load order preserved. See index.html for
   the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   HW GPIO CONTROL
   ══════════════════════════════════════════════════════════════════════════ */
// Errors belong to their control, remain readable on touch screens, and clear
// only on another action. Successful writes are shown by the actual button state.
const _hwControlErrors = {};
function setHwChannelStatus(channel, msg) {
  _hwControlErrors[channel] = msg ? String(msg) : '';
  const el = document.getElementById('hw-error-' + channel);
  if (!el) return;
  el.textContent = msg ? uiT(String(msg)) : '';
  el.hidden = !msg;
  document.querySelectorAll('.hw-btns[data-hw-ch="' + channel + '"] button').forEach(function (button) {
    if (msg) button.setAttribute('aria-invalid', 'true');
    else button.removeAttribute('aria-invalid');
  });
}
window.hwRerenderBlockStatus = function () {
  Object.keys(_hwControlErrors).forEach(function (channel) {
    setHwChannelStatus(channel, _hwControlErrors[channel]);
  });
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
// Keep the legacy wall deadline for a cached older status.js consumer.
let pendingUsbPowerMonotonicUntil = 0;
let pendingUsbPowerVal = /** @type {number|null} */ (null);

function clearPendingUsbPowerIfServerMatches(v) {
  if (pendingUsbPowerVal === null) return;
  if (hwLogicalFromPayload(v) === pendingUsbPowerVal) {
    pendingUsbPowerVal = null;
    pendingUsbPowerUntil = 0;
    pendingUsbPowerMonotonicUntil = 0;
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
  setHwChannelStatus('usb_power', '');

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
        // Кнопка «Сброс» горит голубым (как активный пункт меню) пока идёт сброс
        // — гаснет, когда питание USB снова появилось (по истечении sec).
        if (btn) btn.classList.add('hw-active-blue');
        applyHwChannel('hw-usb-st', 'usb_power', 0);
        pendingUsbPowerVal = 0;
        pendingUsbPowerUntil = Date.now() + (sec + 2) * 1000;
        pendingUsbPowerMonotonicUntil = performance.now() + (sec + 2) * 1000;
        bumpMainStatusEpoch();
        fetchStatusMain(true);
        window.setTimeout(function () {
          pendingUsbPowerVal = null;
          pendingUsbPowerUntil = 0;
          pendingUsbPowerMonotonicUntil = 0;
          if (btn) { btn.disabled = false; btn.classList.remove('hw-active-blue'); }
          bumpMainStatusEpoch();
          fetchStatusMain(true);
        }, sec * 1000 + 500);
        return;
      }
      if (btn) btn.disabled = false;
      if (j.error === 'reset_busy') setHwChannelStatus('usb_power', 'Сброс USB уже выполняется', 'warn');
      else if (j.error === 'channel_unavailable') setHwChannelStatus('usb_power', 'Канал недоступен для этого варианта устройства. Проверьте выбранный вариант.', 'error');
      else if (j.error === 'gpio_not_configured') setHwChannelStatus('usb_power', 'Канал не настроен в /etc/sa02m_hw.conf', 'error');
      else if (j.error === 'i2c_busy') setHwChannelStatus('usb_power', 'Шина I2C занята другой службой', 'error');
      else if (j.error === 'i2c_tools_missing') setHwChannelStatus('usb_power', 'На устройстве нет i2c-tools', 'error');
      else setHwChannelStatus('usb_power', 'Ошибка: ' + (j.error || j.error_code || 'unknown'), 'error');
    })
    .catch(function () {
      if (btn) btn.disabled = false;
      setHwChannelStatus('usb_power', 'Нет связи с сервером', 'error');
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
  setHwChannelStatus(channel, '');
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
          pendingUsbPowerMonotonicUntil = performance.now() + 15000;
        }
        bumpMainStatusEpoch();
        fetchStatusMain(true);
      }
      else if (j.error === 'channel_unavailable') setHwChannelStatus(channel, 'Канал недоступен для этого варианта устройства. Проверьте выбранный вариант.', 'error');
      else if (j.error === 'gpio_not_configured') setHwChannelStatus(channel, 'Канал не настроен в /etc/sa02m_hw.conf', 'error');
      else if (j.error === 'i2c_busy') setHwChannelStatus(channel, 'Шина I2C занята другой службой', 'error');
      else if (j.error === 'i2c_tools_missing') setHwChannelStatus(channel, 'На устройстве нет i2c-tools', 'error');
      else setHwChannelStatus(channel, 'Ошибка: ' + (j.error || j.error_code || 'unknown'), 'error');
    })
    .catch(() => setHwChannelStatus(channel, 'Нет связи с сервером', 'error'));
}

