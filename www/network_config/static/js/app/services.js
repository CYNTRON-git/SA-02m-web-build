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

function renderServicesControl(data) {
  _lastSvcCtlData = data;
  const host = document.getElementById('svc-ctl-list');
  if (!host) return;
  const flasherBusy = !!(data && data.flasher_busy);
  const list = ((data && data.services) || []).filter(function (svc) {
    return svcIsInstalledFlag(svc.installed);
  });
  if (!list.length) {
    host.innerHTML = '<p class="field-hint">' + (window.sa02mI18n ? window.sa02mI18n.t('Нет управляемых служб') : 'Нет управляемых служб') + '</p>';
    return;
  }
  const sorted = list.slice().sort(function (a, b) {
    return compareSvcDisplayName(svcCtlDisplayLabel(a), svcCtlDisplayLabel(b));
  });
  host.innerHTML = '';
  sorted.forEach(function (svc, i) {
    const wantStart = svcCtlWantsStart(svc);
    const action = wantStart ? 'start' : 'stop';
    const btnLabelRu = wantStart ? 'Пуск' : 'Стоп';
    const btnLabel = window.sa02mI18n ? window.sa02mI18n.t(btnLabelRu) : btnLabelRu;
    const btnClass = wantStart
      ? 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-on'
      : 'btn btn-sm hw-io-btn svc-ctl-btn hw-io-to-off';

    const r = document.createElement('div');
    r.className = 'svc-row svc-ctl-row';
    r.setAttribute('role', 'listitem');

    const name = document.createElement('span');
    name.className = 'name mono';
    name.textContent = svcCtlDisplayLabel(svc);

    const mid = document.createElement('span');
    mid.className = 'svc-ctl-mid';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = btnClass;
    btn.textContent = btnLabel;
    btn.dataset.svcId = svc.id;
    btn.dataset.svcAction = action;
    const pollBlocked = flasherBusy && wantStart && (svc.id === 'mplc4' || svc.id === 'mqtt-bridge');
    if (pollBlocked) {
      btn.disabled = true;
      btn.title = window.sa02mI18n
        ? window.sa02mI18n.t('Идёт прошивка или сканирование RS-485')
        : 'Идёт прошивка или сканирование RS-485';
    }
    btn.addEventListener('click', function () { serviceCtlAction(btn); });
    mid.appendChild(btn);

    const badge = document.createElement('span');
    badge.className = 'badge badge-unk';
    const bid = 'svc-ctl-badge-' + i;
    badge.id = bid;

    r.appendChild(name);
    r.appendChild(mid);
    r.appendChild(badge);
    host.appendChild(r);
    svcBadge(bid, svcCtlRowState(svc));
  });
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
  fetch('/cgi-bin/kernel_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
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
  fetch('/cgi-bin/kernel_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
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
  fetch('/cgi-bin/cpu_profile.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
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
  if (btn) btn.disabled = true;
  if (!host.querySelector('.svc-row')) {
    host.innerHTML = '<p class="field-hint">Загрузка…</p>';
  }
  fetch('/cgi-bin/services_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error === 'unauthorized') throw new Error('нет доступа');
      if (j.error === 'ctl_missing') throw new Error('скрипт управления не установлен на устройстве');
      if (!j.ok && j.error) throw new Error(j.error);
      renderServicesControl(j);
      if (forceToast) toast('Список служб обновлён', 'success');
      setTimeout(function () {
        fetchBackgroundPart('services', applyServicesStatus, true);
        fetchPriorityPart('priority');
      }, 1500);
    })
    .catch((e) => {
      host.innerHTML = '<p class="field-hint log-err">' + escHtml(e && e.message ? e.message : String(e)) + '</p>';
      if (forceToast) toast('Службы: ' + (e && e.message ? e.message : String(e)), 'error');
    })
    .finally(() => { if (btn) btn.disabled = false; });
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
  };
  return map[c] || c;
}

function pollServiceCtlState(id, action, maxMs, intervalMs) {
  const wantStart = action === 'start';
  const deadline = Date.now() + (maxMs || 90000);
  return new Promise(function (resolve, reject) {
    function tick() {
      fetch('/cgi-bin/services_ctrl.cgi', { credentials: 'same-origin', cache: 'no-store' })
        .then(async (r) => {
          const j = await r.json().catch(() => ({}));
          if (!r.ok || j.ok === false) throw new Error(svcCtlErrorMessage(j.error) || ('HTTP ' + r.status));
          const svc = (j.services || []).find(function (s) { return s && s.id === id; });
          if (!svc) throw new Error('unknown_service');
          renderServicesControl(j);
          if (svcCtlWantsStart(svc) !== wantStart) {
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
      fetch('/cgi-bin/services_ctrl.cgi?result=1&id=' + encodeURIComponent(id), {
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
  const verb = action === 'stop' ? 'Остановить' : 'Включить';
  const verbT = window.sa02mI18n ? window.sa02mI18n.t(verb) : verb;
  if (!confirm(verbT + ' «' + label + '»?')) return;
  btn.disabled = true;
  fetch('/cgi-bin/services_ctrl.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify({ id, action }),
  })
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(svcCtlErrorMessage(j.error) || ('HTTP ' + r.status));
      if (j.pending) {
        toast(action === 'stop' ? 'Остановка службы…' : 'Запуск службы…', 'info', 5000);
        return pollServiceCtlDone(id, action, 90000, 2000);
      }
      return j;
    })
    .then(function () {
      toast(action === 'stop' ? 'Служба остановлена и отключена' : 'Служба включена', 'success');
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
  fetch('/cgi-bin/restart.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
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
  fetch('/cgi-bin/reboot.cgi', {
    method: 'POST',
    redirect: 'manual',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
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
      toast('Перезагрузка… страница обновится через 60 с', 'info', 65000);
      setTimeout(() => location.reload(), 60000);
    })
    .catch((e) => {
      toast('Перезагрузка не запущена: ' + (e && e.message ? e.message : String(e)), 'error');
    });
}

function doLogout() {
  window.location.href = '/cgi-bin/logout.cgi';
}

