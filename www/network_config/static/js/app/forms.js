/* SA-02m Web Interface -- FORMS (load current network/time settings into the
   forms + network/time form submission). Extracted from app.js (F10
   decomposition). Plain classic script sharing the global scope; original load
   order preserved. See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   CONFIG — load current network/time settings into forms
   ══════════════════════════════════════════════════════════════════════════ */
let configLoaded = false;

function browserIanaTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (_) {
    return '';
  }
}

/**
 * Выставить f-tz: сначала таймзона с устройства (если есть), иначе — текущий пояс браузера (ПК).
 * Неизвестные IANA добавляются в список option, чтобы значение можно было применить.
 */
function timeZoneSelectApplyFromDeviceOrBrowser(tzSel, deviceTzRaw) {
  if (!tzSel) return;
  const inList = function (tz) {
    return !!tz && Array.from(tzSel.options).some(function (o) { return o.value === tz; });
  };
  const ensureOpt = function (value, label) {
    if (!value || inList(value)) return;
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label || value;
    tzSel.appendChild(o);
  };

  const deviceTz = deviceTzRaw != null ? String(deviceTzRaw).trim() : '';
  const browserTz = browserIanaTz();

  if (deviceTz) {
    if (inList(deviceTz)) {
      tzSel.value = deviceTz;
      return;
    }
    ensureOpt(deviceTz, deviceTz);
    tzSel.value = deviceTz;
    return;
  }
  if (browserTz) {
    if (inList(browserTz)) {
      tzSel.value = browserTz;
      return;
    }
    ensureOpt(browserTz, browserTz);
    tzSel.value = browserTz;
  }
}

function loadConfig() {
  if (configLoaded) return;
  fetch('/cgi-bin/config.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      configLoaded = true;
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
      timeZoneSelectApplyFromDeviceOrBrowser(document.getElementById('f-tz'), d.timezone);
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
        toast('Укажите IP и маску для Ethernet № 1', 'error');
        return;
      }
    }
    submitForm(f0, () => {
      configLoaded = false;
      toast('Ethernet № 1: конфиг сохранён, адрес применяется… При смене IP откройте новый адрес через 2–3 с.', 'success', 8000);
    });
  });

  /* eth1 */
  const f1 = document.getElementById('net-form-eth1');
  if (f1) f1.addEventListener('submit', e => {
    e.preventDefault();
    const en = document.getElementById('eth1-en')?.checked;
    if (en && !document.getElementById('f-ip1')?.value.trim()) {
      toast('Укажите IP для Ethernet № 2', 'error'); return;
    }
    submitForm(f1, () => {
      configLoaded = false;
      toast('Ethernet № 2: конфиг сохранён, адрес применяется… При смене IP откройте новый адрес через 2–3 с.', 'success', 8000);
    });
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

/** Разбор ответа apply.cgi (302 + Location: /?status=...) */
function parseApplyRedirect(response) {
  if (response.type === 'opaqueredirect') return { kind: 'unknown' };
  const loc = response.headers.get('Location') || '';
  if (loc.indexOf('error_time') !== -1) return { kind: 'error_time' };
  if (loc.indexOf('error_tz') !== -1) return { kind: 'error_tz' };
  if (loc.indexOf('applied_tz_failed') !== -1) return { kind: 'applied_tz_failed' };
  return { kind: 'ok' };
}

/** Network apply may drop TCP (status 0 / fetch reject) when IP changes — not an error. */
function isNetApplyForm(form) {
  return !!(form && form.querySelector('input[name="net_iface"]'));
}

function submitForm(form, onSuccess) {
  const data = new URLSearchParams(new FormData(form));
  const btn = form.querySelector('button[type=submit]');
  const netApply = isNetApplyForm(form);
  const finishOk = () => {
    configLoaded = false;
    onSuccess && onSuccess();
  };
  if (btn) btn.disabled = true;
  fetch('/cgi-bin/apply.cgi', {
    method: 'POST',
    body: data,
    redirect: 'manual',
    credentials: 'same-origin'
  })
    .then((r) => {
      // status 0 / opaqueredirect: connection gone after IP bounce — treat as applied
      if (netApply && (r.status === 0 || r.type === 'opaqueredirect')) {
        finishOk();
        return;
      }
      if (!r.ok && r.status !== 302 && r.status !== 301) {
        toast('Ошибка сервера: ' + r.status, 'error');
        return;
      }
      const pr = parseApplyRedirect(r);
      if (pr.kind === 'error_time') {
        toast(
          'Не удалось установить время. Проверьте формат и /var/log/sa02m_install.log на устройстве.',
          'error'
        );
        return;
      }
      if (pr.kind === 'error_tz') {
        toast('Таймзона не применена.', 'error');
        return;
      }
      if (pr.kind === 'applied_tz_failed') {
        toast('Настройки применены; таймзона не изменилась.', 'warn', 6000);
        finishOk();
        return;
      }
      finishOk();
    })
    .catch(() => {
      if (netApply) {
        finishOk();
        return;
      }
      toast('Ошибка отправки', 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
}

