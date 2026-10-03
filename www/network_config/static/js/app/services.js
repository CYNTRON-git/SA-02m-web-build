/* SA-02m Web Interface -- SERVICES (application services on the Management
   tab + system actions: reboot/shutdown/update). Extracted from app.js (F10
   decomposition). Plain classic script sharing the global scope; original load
   order preserved. See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   APPLICATION SERVICES (Management tab)
   ══════════════════════════════════════════════════════════════════════════ */
function svcCtlDisplayLabel(svc) {
  const id = String((svc && svc.id) || '').trim();
  const lab = String((svc && svc.label) || '').trim();
  if (id === 'codesys') return 'CODESYS';
  if (id === 'mqtt-bridge') return 'MQTT мост';
  if (id === 'mqtt-telemetry') return 'MQTT телеметрия';
  if (id === 'docker' || lab.toLowerCase() === 'docker') return 'Docker';
  if (id === 'mplc4' || lab.toLowerCase() === 'mplc4') return 'MPLC4';
  if (lab) return lab;
  return unitUiLabel(id);
}

function svcCtlRowState(svc) {
  if (svc.masked || svc.user_disabled) return 'disabled';
  return svc.active || 'inactive';
}

/** Кнопка «Пуск», если службу нужно включить (остановлена или отключена админом). */
function svcCtlWantsStart(svc) {
  if (!svc) return true;
  if (svc.masked || svc.user_disabled) return true;
  return !svcStateIsActive(svc.active);
}

let _lastSvcCtlData = null;
let _svcCtlLoadGen = 0;

/* Same ids as SERVICE_DEFS in etc/sa02m-web-service-ctl.sh. Painted at once
   on the Services sub-tab; the CGI then fills each row in place. A service
   the board does not manage is dropped only after a successful list. */
const SVC_CTL_CATALOG = [
  { id: 'alice', label: 'Яндекс Алиса' },
  { id: 'homekit', label: 'Apple HomeKit' },
  { id: 'homeconnect', label: 'Home Connect' },
  { id: 'docker', label: 'Docker' },
  { id: 'codesys', label: 'CODESYS' },
  { id: 'mplc4', label: 'MPLC4' },
  { id: 'mosquitto', label: 'Mosquitto' },
  { id: 'mqtt-bridge', label: 'MQTT мост' },
  { id: 'mqtt-telemetry', label: 'MQTT телеметрия' },
  { id: 'node-red', label: 'Node-RED' },
  { id: 'klogic', label: 'KLogic' },
];

function svcCtlT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(s) : s;
}

function servicesSubOpen() {
  const pane = document.getElementById('sys-pane-services');
  const tab = document.getElementById('tab-system');
  return !!(pane && !pane.hidden && tab && tab.classList.contains('active'));
}

function svcCtlFindRow(host, id) {
  const safe = String(id || '');
  if (!/^[A-Za-z0-9_-]+$/.test(safe)) return null;
  return host.querySelector('.svc-ctl-row[data-svc-id="' + safe + '"]');
}

function svcCtlEnsureRow(host, svc) {
  let row = svcCtlFindRow(host, svc.id);
  if (row) return row;
  row = document.createElement('div');
  row.className = 'svc-row svc-ctl-row';
  row.setAttribute('role', 'listitem');
  row.dataset.svcId = svc.id;
  const name = document.createElement('span');
  name.className = 'name mono';
  const toggle = document.createElement('span');
  toggle.className = 'svc-ctl-cell svc-ctl-toggle';
  const manage = document.createElement('span');
  manage.className = 'svc-ctl-cell svc-ctl-manage';
  const badge = document.createElement('span');
  badge.className = 'badge badge-unk';
  badge.id = 'svc-ctl-badge-' + svc.id;
  badge.textContent = '…';
  row.appendChild(name);
  row.appendChild(toggle);
  row.appendChild(manage);
  row.appendChild(badge);
  host.appendChild(row);
  return row;
}

function svcCtlSyncButton(cell, svc, action, flasherBusy) {
  if (!cell) return;
  const cur = cell.querySelector('button');
  if (!action) {
    if (cur) cell.textContent = '';
    return;
  }
  const blocked = action === 'start' && flasherBusy && (svc.id === 'mplc4' || svc.id === 'mqtt-bridge');
  if (cur && cur.dataset.svcId === svc.id && cur.dataset.svcAction === action) {
    cur.disabled = !!blocked;
    return;
  }
  cell.textContent = '';
  cell.appendChild(makeSvcCtlButton(svc, action, flasherBusy));
}

function svcCtlSyncRow(row, svc, flasherBusy, pending) {
  const name = row.querySelector('.name');
  if (name) name.textContent = svcCtlDisplayLabel(svc);
  const toggle = row.querySelector('.svc-ctl-toggle');
  const manage = row.querySelector('.svc-ctl-manage');
  const badge = row.querySelector('.badge');
  if (pending) {
    if (toggle) toggle.textContent = '';
    if (manage) manage.textContent = '';
    if (badge) {
      badge.textContent = '…';
      badge.className = 'badge badge-unk';
    }
    return;
  }
  const installed = svcIsInstalledFlag(svc.installed);
  if (!installed) {
    svcCtlSyncButton(toggle, svc, null, flasherBusy);
    svcCtlSyncButton(manage, svc, 'install', flasherBusy);
  } else {
    svcCtlSyncButton(toggle, svc, svcCtlWantsStart(svc) ? 'start' : 'stop', flasherBusy);
    svcCtlSyncButton(manage, svc, 'uninstall', flasherBusy);
  }
  if (!badge) return;
  if (!badge.id) badge.id = 'svc-ctl-badge-' + svc.id;
  if (installed) svcBadge(badge.id, svcCtlRowState(svc));
  else {
    badge.textContent = svcCtlT('Не установлен');
    badge.className = 'badge badge-unk';
  }
}

/** Rows already on screen keep their last badge. Only a still-empty «…» becomes «не отвечает». */
function markServicesControlNoAnswer() {
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  const label = svcCtlT('не отвечает');
  host.querySelectorAll('.svc-ctl-row .badge').forEach(function (badge) {
    if (badge.textContent.trim() === '…') {
      badge.textContent = label;
      badge.className = 'badge badge-unk';
    }
  });
}

function showServicesControlNow() {
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  if (host.querySelector('.svc-ctl-row')) return;
  if (_lastSvcCtlData && _lastSvcCtlData.services && _lastSvcCtlData.services.length) {
    renderServicesControl(_lastSvcCtlData);
    return;
  }
  renderServicesControl({
    placeholder: true,
    services: SVC_CTL_CATALOG.map(function (s) {
      return { id: s.id, label: s.label, statePending: true };
    }),
  });
}

/** RU label + CSS class per control action (install/start/stop/uninstall). */
function svcCtlBtnMeta(action) {
  switch (action) {
    case 'install':   return { ru: 'Установить', cls: 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-on' };
    case 'start':     return { ru: 'Пуск',       cls: 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-on' };
    case 'stop':      return { ru: 'Стоп',       cls: 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-off' };
    case 'uninstall': return { ru: 'Удалить',    cls: 'btn btn-sm svc-ctl-btn btn-danger' };
    default:          return { ru: action,       cls: 'btn btn-sm svc-ctl-btn' };
  }
}

/** Build one control button (created inside the renderer → re-render survival). */
function makeSvcCtlButton(svc, action, flasherBusy) {
  const meta = svcCtlBtnMeta(action);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = meta.cls;
  btn.textContent = window.sa02mI18n ? window.sa02mI18n.t(meta.ru) : meta.ru;
  btn.dataset.svcId = svc.id;
  btn.dataset.svcAction = action;
  if (action === 'start' && flasherBusy && (svc.id === 'mplc4' || svc.id === 'mqtt-bridge')) {
    btn.disabled = true;
    btn.title = window.sa02mI18n
      ? window.sa02mI18n.t('Идёт прошивка или сканирование RS-485')
      : 'Идёт прошивка или сканирование RS-485';
  }
  btn.addEventListener('click', function () { serviceCtlAction(btn); });
  return btn;
}

function renderServicesControl(data) {
  const pending = !!(data && data.placeholder);
  if (!pending) _lastSvcCtlData = data;
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  let child = host.firstElementChild;
  while (child) {
    const next = child.nextElementSibling;
    if (child.classList.contains('field-hint')) child.remove();
    child = next;
  }
  const flasherBusy = !!(data && data.flasher_busy);
  const list = ((data && data.services) || []).slice();
  if (!list.length) {
    if (!pending) {
      host.querySelectorAll('.svc-ctl-row').forEach(function (row) { row.remove(); });
      const p = document.createElement('p');
      p.className = 'field-hint';
      p.textContent = svcCtlT('Нет управляемых служб');
      host.appendChild(p);
      applyManagedServiceCardVisibility(list);
    }
    return;
  }
  const sorted = list.slice().sort(function (a, b) {
    return compareSvcDisplayName(svcCtlDisplayLabel(a), svcCtlDisplayLabel(b));
  });
  const keep = {};
  sorted.forEach(function (svc) {
    if (!svc || !svc.id) return;
    keep[svc.id] = true;
    const row = svcCtlEnsureRow(host, svc);
    svcCtlSyncRow(row, svc, flasherBusy, pending || !!svc.statePending);
    host.appendChild(row);
  });
  if (!pending) {
    host.querySelectorAll('.svc-ctl-row').forEach(function (row) {
      if (!keep[row.dataset.svcId]) row.remove();
    });
    applyManagedServiceCardVisibility(list);
  }
}

/** Управление-tab tiles whose visibility follows a managed service's run state:
    shown while the service runs, hidden when stopped/disabled. Keyed on the same
    svc-ctl list the rows use, so a Пуск/Стоп there flips the card on the next
    poll. Operates on tiles OUTSIDE #svc-ctl-list, so it survives the row
    re-render; a service absent from the list (not present on this HW/build)
    leaves its card untouched — fail-safe to visible. data-hide-for variant
    hiding is independent (these tiles carry none). */
function applyManagedServiceCardVisibility(list) {
  const byId = {};
  (list || []).forEach(function (s) { if (s && s.id) byId[s.id] = s; });
  setManagedServiceCardVisible('alice-card', byId.alice);
  setManagedServiceCardVisible('mplc-proj-card', byId.mplc4);
}

function setManagedServiceCardVisible(cardId, svc) {
  const card = document.getElementById(cardId);
  if (!card || !svc) return;
  // svcCtlWantsStart is true when stopped/disabled/masked → hide the tile.
  card.hidden = svcCtlWantsStart(svc);
}

window.refreshServicesControlI18n = function () {
  if (_lastSvcCtlData) renderServicesControl(_lastSvcCtlData);
};

function kernelProfileLabel(p) {
  return p === 'rt' ? uiT('RT (Real Time)') : uiT('SMP (без Real Time)');
}

function kernelErrorMessage(code) {
  const map = {
    zimage_missing: uiT('Образ ядра не установлен на устройстве'),
    modules_missing: uiT('Модули ядра не установлены'),
    fat_mount_failed: uiT('Не удалось смонтировать FAT-раздел загрузки'),
    busy: uiT('Переключение ядра уже выполняется'),
    bad_profile: uiT('Неверный профиль ядра'),
    bad_action: uiT('Неверное действие'),
    fat_write_failed: uiT('Не удалось записать ядро на загрузочный раздел'),
    sudo_failed: uiT('Нет прав sudo'),
    ctl_missing: uiT('Скрипт переключения ядра не установлен'),
  };
  return map[code] || code;
}

function cpuProfileErrorMessage(code) {
  const map = {
    rt_kernel_active: uiT('Управление частотой недоступно на RT-ядре'),
    cpufreq_unavailable: uiT('Cpufreq не поддерживается'),
    bad_profile: uiT('Неверный профиль частоты'),
    apply_failed: uiT('Не удалось применить профиль'),
    sudo_failed: uiT('Нет прав sudo'),
    ctl_missing: uiT('Скрипт управления CPU не установлен'),
  };
  return map[code] || code;
}

function cpuProfileLabel(id) {
  const map = {
    adaptive: uiT('Авто (адаптивная)'),
    low: uiT('Минимальная'),
    medium: uiT('Средняя'),
    high: uiT('Высокая (адапт.)'),
    performance: uiT('Максимальная'),
  };
  return map[id] || id;
}

let _lastKernelCtrlData = null;
let _lastSystemStatus = null;

function isRtKernelMode(running, desired) {
  return running === 'rt' || desired === 'rt';
}

function syncCpuProfileSectionVisibility(running, desired, uiAvailable) {
  const section = document.getElementById('cpu-profile-section');
  if (!section) return false;
  if (isRtKernelMode(running, desired)) {
    section.style.display = 'none';
    return false;
  }
  if (uiAvailable === 0 || uiAvailable === false) {
    section.style.display = 'none';
    return false;
  }
  if (uiAvailable === 1 || uiAvailable === true) {
    section.style.display = '';
    return true;
  }
  return section.style.display !== 'none';
}

function renderKernelControl(j) {
  _lastKernelCtrlData = j;
  const runEl = document.getElementById('kernel-run-label');
  const hintEl = document.getElementById('kernel-pending-hint');
  const sel = document.getElementById('kernel-profile-select');
  const btn = document.getElementById('kernel-apply-btn');
  if (!runEl || !j) return;

  setText('kernel-run-label', kernelProfileLabel(j.running));
  const smpOk = j.smp_zimage === 1 && j.smp_modules === 1;
  const rtOk = j.rt_zimage === 1 && j.rt_modules === 1;

  if (sel && j.desired) sel.value = j.desired;
  if (hintEl) {
    if (j.reboot_pending === 1 || j.reboot_pending === true) {
      hintEl.style.display = '';
      hintEl.textContent = uiT('Требуется перезагрузка для применения выбранного ядра');
    } else {
      hintEl.style.display = 'none';
      hintEl.textContent = '';
    }
  }
  if (btn && sel) {
    const target = sel.value;
    const canSwitch = (target === 'smp' && smpOk) || (target === 'rt' && rtOk);
    const pending = j.reboot_pending === 1 || j.reboot_pending === true;
    btn.disabled = !canSwitch || (j.running === target && !pending);
  }
  // Refresh reinstalls the RUNNING profile's canonical image → enabled when that
  // profile's artifact is valid (running=smp → smpOk, running=rt → rtOk).
  const refreshBtn = document.getElementById('kernel-refresh-btn');
  if (refreshBtn) {
    const runOk = (j.running === 'smp' && smpOk) || (j.running === 'rt' && rtOk);
    refreshBtn.disabled = !runOk;
  }
  const uiAvail = _lastSystemStatus ? _lastSystemStatus.cpu_profile_ui_available : null;
  syncCpuProfileSectionVisibility(j.running, j.desired, uiAvail);
}

function loadKernelControl(forceToast) {
  const sel = document.getElementById('kernel-profile-select');
  if (sel && !sel.dataset.kernelBound) {
    sel.dataset.kernelBound = '1';
    sel.addEventListener('change', function () {
      if (_lastKernelCtrlData) {
        renderKernelControl(Object.assign({}, _lastKernelCtrlData, { desired: sel.value }));
      }
    });
  }
  fetch('cgi-bin/kernel_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error === 'unauthorized') throw new Error(uiT('нет доступа'));
      if (j.error === 'ctl_missing') throw new Error(kernelErrorMessage('ctl_missing'));
      if (j.ok === false && j.error) throw new Error(kernelErrorMessage(j.error));
      renderKernelControl(j);
      if (forceToast) toast(uiT('Статус ядра обновлён'), 'success');
    })
    .catch((e) => {
      if (forceToast) toast(uiT('Ядро: ') + (e && e.message ? e.message : String(e)), 'error');
    });
}

function applyKernelProfile() {
  const sel = document.getElementById('kernel-profile-select');
  const profile = sel ? sel.value : '';
  if (!profile) return;
  let msg = uiT('Переключить ядро на ') + kernelProfileLabel(profile) + uiT('? Устройство перезагрузится.');
  if (profile === 'smp') {
    msg += ' ' + uiT('SMP-ядро с Docker доступно. CODESYS может работать на SMP, но возможны дрожание цикла; для промышленного PLC рекомендуется RT.');
  }
  if (!confirm(msg)) return;

  const btn = document.getElementById('kernel-apply-btn');
  if (btn) btn.disabled = true;
  fetch('cgi-bin/kernel_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: JSON.stringify({ profile: profile }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(kernelErrorMessage(j.error) || ('HTTP ' + r.status));
      if (j.warnings && String(j.warnings).indexOf('codesys_requires_rt') >= 0) {
        toast(uiT('CODESYS запущен — на SMP возможны дрожание цикла; RT рекомендуется для PLC'), 'warn', 8000);
      }
      renderKernelControl(j);
      if (j.reboot_required) {
        if (confirm(uiT('Ядро подготовлено. Перезагрузить сейчас?'))) doReboot();
        else toast(uiT('Перезагрузите устройство для применения ядра'), 'info', 8000);
      } else if (j.noop) {
        toast(uiT('Это ядро уже активно'), 'info');
      } else {
        toast(uiT('Настройка ядра сохранена'), 'success');
      }
    })
    .catch((e) => {
      toast(uiT('Ядро: ') + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
}

/** Reinstall the running profile's canonical zImage onto the boot FAT slot
    (same-profile refresh). The device decides identical-vs-changed via cmp:
    refreshed===1 → bytes changed, offer a reboot; refreshed===0 → already
    up to date, info toast. No profile change, so no CODESYS/RT warning path. */
function refreshKernelBoot() {
  const btn = document.getElementById('kernel-refresh-btn');
  if (btn) btn.disabled = true;
  fetch('cgi-bin/kernel_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: JSON.stringify({ action: 'refresh' }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(kernelErrorMessage(j.error) || ('HTTP ' + r.status));
      if (j.refreshed === 1 || j.reboot_required === 1 || j.reboot_required === true) {
        toast(uiT('Загрузочное ядро обновлено'), 'success');
        if (confirm(uiT('Ядро подготовлено. Перезагрузить сейчас?'))) doReboot();
        else toast(uiT('Перезагрузите устройство для применения ядра'), 'info', 8000);
      } else {
        toast(uiT('Загрузочное ядро уже актуально'), 'info');
      }
    })
    .catch((e) => {
      toast(uiT('Ядро: ') + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
}

function applyCpuFrequencyLabels(d) {
  const freq = document.getElementById('cpu-profile-freq-label');
  if (!freq || !d) return;
  const raw = d.cpu_freq_mhz != null ? d.cpu_freq_mhz : d.cur_mhz;
  const mhz = raw != null && raw !== '' ? raw : '—';
  freq.textContent = mhz + ' ' + uiT('МГц');
}

function updateCpuProfileTile(d) {
  const section = document.getElementById('cpu-profile-section');
  if (!section || !d) return;

  let running = _lastKernelCtrlData ? _lastKernelCtrlData.running : null;
  let desired = _lastKernelCtrlData ? _lastKernelCtrlData.desired : null;
  const ksel = document.getElementById('kernel-profile-select');
  if (ksel) desired = ksel.value;
  if (!running && (d.kernel_is_rt === 1 || d.kernel_is_rt === true)) running = 'rt';
  else if (!running && (d.kernel_is_rt === 0 || d.kernel_is_rt === false)) running = 'smp';

  const show = syncCpuProfileSectionVisibility(running, desired, d.cpu_profile_ui_available);
  if (!show) return;

  const sel = document.getElementById('cpu-profile-select');
  if (sel && d.cpu_profile) sel.value = d.cpu_profile;
  applyCpuFrequencyLabels(d);
}

function applyCpuProfile() {
  const sel = document.getElementById('cpu-profile-select');
  const profile = sel ? sel.value : '';
  if (!profile) return;
  const btn = document.getElementById('cpu-profile-apply-btn');
  if (btn) btn.disabled = true;
  fetch('cgi-bin/cpu_profile.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: JSON.stringify({ profile: profile }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(cpuProfileErrorMessage(j.error) || ('HTTP ' + r.status));
      applyCpuFrequencyLabels(j);
      toast(uiT('Профиль частоты: ') + cpuProfileLabel(profile), 'success');
      fetchBackgroundPart('system', applySystemStatus);
      fetchBackgroundPart('load', applyLoadStatus);
      setTimeout(fetchPriorityPart, 1500, 'priority');
    })
    .catch((e) => {
      toast(uiT('CPU: ') + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
}

function loadServicesControl(forceToast) {
  const host = document.getElementById('svc-ctl-list');
  const btn = document.getElementById('svc-ctl-refresh-btn');
  if (!host) return;
  showServicesControlNow();
  if (btn) btn.disabled = true;
  const gen = ++_svcCtlLoadGen;
  fetch('cgi-bin/services_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error === 'unauthorized') throw new Error('нет доступа');
      if (j.error === 'ctl_missing') throw new Error('скрипт управления не установлен на устройстве');
      if (!j.ok && j.error) throw new Error(j.error);
      return j;
    })
    .then(function (j) {
      if (gen !== _svcCtlLoadGen) return;
      renderServicesControl(j);
      if (!servicesSubOpen()) return;
      if (forceToast) toast('Список служб обновлён', 'success');
      setTimeout(function () {
        if (gen !== _svcCtlLoadGen || !servicesSubOpen()) return;
        fetchBackgroundPart('services', applyServicesStatus, true);
        fetchPriorityPart('priority');
      }, 1500);
    })
    .catch((e) => {
      if (gen !== _svcCtlLoadGen) return;
      markServicesControlNoAnswer();
      if (forceToast && servicesSubOpen()) toast('Службы: ' + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(function () {
      if (gen === _svcCtlLoadGen && btn) btn.disabled = false;
    });
}

function svcCtlErrorMessage(code) {
  const c = String(code || '');
  const map = {
    missing_id: 'не указана служба',
    bad_action: 'неверное действие',
    unknown_service: 'служба не найдена',
    disable_failed: 'не удалось отключить автозапуск (служба может подняться после перезагрузки)',
    enable_failed: 'не удалось включить автозапуск',
    still_running: 'процесс службы всё ещё работает',
    start_failed: 'не удалось запустить службу',
    flasher_busy: 'идёт прошивка или сканирование RS-485',
    sudo_failed: 'нет прав sudo для управления службами',
    ctl_missing: 'скрипт управления не установлен',
    timeout: 'истекло время ожидания смены состояния',
    staging_missing: 'установочные файлы не найдены на устройстве',
    no_internet: 'нет доступа в интернет для установки Node-RED',
    not_installable: 'служба не поддерживает установку/удаление',
    install_failed: 'не удалось установить службу',
    uninstall_failed: 'не удалось удалить службу',
    purge_blocked: 'не удалось полностью удалить пакет',
  };
  return map[c] || c;
}

/** Toast text while an async control action is pending. */
function svcCtlActionPendingMsg(action) {
  const m = {
    install: 'Устанавливается',
    uninstall: 'Удаляется',
    stop: 'Остановка службы',
    start: 'Запуск службы',
  };
  const ru = m[action] || '…';
  return window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
}

/** Toast text on successful completion of an async control action. */
function svcCtlActionDoneMsg(action) {
  const m = {
    install: 'Служба установлена и запущена',
    uninstall: 'Служба удалена',
    stop: 'Служба остановлена и отключена',
    start: 'Служба включена',
  };
  const ru = m[action] || 'Готово';
  return window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
}

function pollServiceCtlState(id, action, maxMs, intervalMs) {
  const wantStart = action === 'start';
  const deadline = Date.now() + (maxMs || 90000);
  return new Promise(function (resolve, reject) {
    function tick() {
      fetch('cgi-bin/services_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
        .then(async (r) => {
          const j = await r.json().catch(() => ({}));
          if (!r.ok || j.ok === false) throw new Error(svcCtlErrorMessage(j.error) || ('HTTP ' + r.status));
          const svc = (j.services || []).find(function (s) { return s && s.id === id; });
          renderServicesControl(j);
          // Action-aware completion: install waits installed=true, uninstall
          // waits installed=false (or the row gone), start/stop keep the flip.
          let done = false;
          if (action === 'install') {
            done = !!(svc && svcIsInstalledFlag(svc.installed));
          } else if (action === 'uninstall') {
            done = !svc || !svcIsInstalledFlag(svc.installed);
          } else {
            if (!svc) throw new Error('unknown_service');
            done = svcCtlWantsStart(svc) !== wantStart;
          }
          if (done) {
            resolve(svc);
            return;
          }
          if (Date.now() >= deadline) {
            reject(new Error('timeout'));
            return;
          }
          setTimeout(tick, intervalMs || 2000);
        })
        .catch(reject);
    }
    setTimeout(tick, 1500);
  });
}

function pollServiceCtlResult(id, maxMs, intervalMs) {
  const deadline = Date.now() + (maxMs || 90000);
  return new Promise(function (resolve, reject) {
    function tick() {
      fetch('cgi-bin/services_ctrl.cgi?result=1&id=' + encodeURIComponent(id), {
        credentials: 'same-origin',
        cache: 'no-store',
      })
        .then(async (r) => {
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error('HTTP ' + r.status);
          if (j.pending) {
            if (Date.now() >= deadline) {
              reject(new Error('timeout'));
              return;
            }
            setTimeout(tick, intervalMs || 2000);
            return;
          }
          if (j.ok === false && j.error) {
            reject(new Error(j.error));
            return;
          }
          resolve(j);
        })
        .catch(reject);
    }
    setTimeout(tick, 1500);
  });
}

function pollServiceCtlDone(id, action, maxMs, intervalMs) {
  return pollServiceCtlResult(id, maxMs, intervalMs)
    .then(function () { return pollServiceCtlState(id, action, maxMs, intervalMs); });
}

function serviceCtlAction(btn) {
  const id = btn && btn.dataset ? btn.dataset.svcId : '';
  const action = btn && btn.dataset ? btn.dataset.svcAction : '';
  if (!id || !action) return;
  if (action === 'start' && _lastSvcCtlData && _lastSvcCtlData.flasher_busy &&
      (id === 'mplc4' || id === 'mqtt-bridge')) {
    toast(svcCtlErrorMessage('flasher_busy'), 'warn');
    return;
  }
  const label = btn.closest('.svc-row')?.querySelector('.name')?.textContent || id;
  let confirmMsg;
  if (action === 'uninstall') {
    const tpl = window.sa02mI18n
      ? window.sa02mI18n.t('Удалить «X» полностью (со всеми данными)?')
      : 'Удалить «X» полностью (со всеми данными)?';
    confirmMsg = tpl.replace('X', label);
  } else {
    const verbMap = { stop: 'Остановить', start: 'Включить', install: 'Установить' };
    const verb = verbMap[action] || action;
    const verbT = window.sa02mI18n ? window.sa02mI18n.t(verb) : verb;
    confirmMsg = verbT + ' «' + label + '»?';
  }
  if (!confirm(confirmMsg)) return;
  btn.disabled = true;
  // Install/uninstall can run several minutes (dpkg + start/verify) → 10-min
  // poll deadline; start/stop keep 90 s.
  const longOp = action === 'install' || action === 'uninstall';
  const pollMax = longOp ? 600000 : 90000;
  fetch('cgi-bin/services_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: JSON.stringify({ id, action }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(svcCtlErrorMessage(j.error) || ('HTTP ' + r.status));
      if (j.pending) {
        toast(svcCtlActionPendingMsg(action), 'info', longOp ? 8000 : 5000);
        return pollServiceCtlDone(id, action, pollMax, 2000);
      }
      return j;
    })
    .then(function () {
      toast(svcCtlActionDoneMsg(action), 'success');
      setTimeout(function () {
        fetchBackgroundPart('services', applyServicesStatus, true);
        fetchPriorityPart('priority');
      }, 1500);
    })
    .catch((e) => {
      toast('Служба: ' + (e && e.message ? svcCtlErrorMessage(e.message) || e.message : String(e)), 'error');
      loadServicesControl(false);
    })
    .finally(() => { btn.disabled = false; });
}

/* ══════════════════════════════════════════════════════════════════════════
   SYSTEM ACTIONS
   ══════════════════════════════════════════════════════════════════════════ */
function doRestart() {
  if (!confirm('Перезапустить службы nginx и fcgiwrap?')) return;
  fetch('cgi-bin/restart.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: '{}',
  })
    .then(async (r) => {
      if (!r.ok) {
        const t = await r.text().catch(() => '');
        throw new Error((t || '').trim().slice(0, 120) || ('HTTP ' + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (j && j.ok === false) throw new Error(j.error || 'отклонено');
      return j;
    })
    .then(() => {
      toast('Команда перезапуска отправлена. Если systemd недоступен, смотрите /var/log/sa02m_install.log', 'success', 8000);
      setTimeout(fetchStatus, 2000);
    })
    .catch((e) => {
      toast('Перезапуск служб: ' + (e && e.message ? e.message : String(e)), 'error');
    });
}

function doReboot() {
  if (!confirm('Перезагрузить контроллер?')) return;
  fetch('cgi-bin/reboot.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
    body: '{}',
  })
    .then(async (r) => {
      if (!r.ok) {
        const t = await r.text().catch(() => '');
        throw new Error((t || '').trim().slice(0, 120) || ('HTTP ' + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (j && j.ok === false) {
        // A live update runner: the board postponed the reboot (reboot.cgi
        // E_UPDATE_RUNNING, 1.0.6.52) — not an error, the update finishes first.
        if (j.error_code === 'E_UPDATE_RUNNING') return null;
        throw new Error(j.error || 'отклонено');
      }
      return j;
    })
    .then((j) => {
      if (j === null) {
        toast('Идёт обновление — перезагрузка отложена', 'info', 8000);
        return;
      }
      toast('Перезагрузка… страница обновится через 60 с', 'info', 65000);
      setTimeout(() => location.reload(), 60000);
    })
    .catch((e) => {
      toast('Перезагрузка не запущена: ' + (e && e.message ? e.message : String(e)), 'error');
    });
}

function doLogout() {
  window.location.href = 'cgi-bin/logout.cgi';
}

