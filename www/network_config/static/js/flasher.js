/* ─────────────────────────────────────────────────────────────────────────────
 * flasher.js  •  UI вкладки «Устройства RS-485»
 * Работает с демоном sa02m-flasher через /api/flasher/*. SSE-стрим событий
 * по /api/flasher/jobs/<id>/events. Кука session_token прокидывается nginx'ом.
 * ──────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const API = 'api/flasher';
  const FLASH_JOB_KEY = 'sa02m-flash-job-id';
  const SCAN_JOB_KEY = 'sa02m-scan-job-id';
  const state = {
    initialised: false,
    ports: [],
    devices: [],        // последний результат сканирования
    selectedDeviceIndices: new Set(), // индексы выбранных строк таблицы (multi-select)
    firmware: [],       // список прошивок (entries)
    latestStableVersion: '', // max stable manifest, kind=app (global fallback)
    latestBootloaderVersion: '', // max stable manifest, kind=bootloader (global fallback)
    latestByDevice: {}, // { "MR-02m"|"RTU-Sensor"|"CE-02m-3": {app, bootloader} } — per-family latest
    selectedFirmwareKey: '', // channel::file
    firmwareDisplayOrder: [], // channel::file, newest first (UI order)
    scanJobId: null,
    flashJobId: null,
    flashIrreversible: false,
    flasherGloballyBusy: false,
    scanStream: null,
    flashStream: null,
    scanPending: false,
    flashPending: false,
    portActionBusy: false,
    lastScanConfigKey: '',
    lastScanRequest: null,
    configOpen: false,
    configBusy: false,
    configLinkBusy: false,
    configBackgroundBusy: false,
    configDeviceIdx: -1,
    configTab: '',
    configSnapshot: null,
    configPollTimer: null,
    configFirstPollTimer: null,
    configNetworkDirty: false,
    configBodyKey: '',
    portRecoveryError: '',
    portRestorePending: false,
    scanArbitrationActive: false,
  };

  // Счётчик поколений опроса: позволяет отбросить устаревший ответ, если
  // был запущен более новый запрос (переключение вкладки, явное обновление).
  let _configPollSeq = 0;
  /** Очередь /device_config/* — один Modbus-сеанс на порт. */
  let _configApiTail = Promise.resolve();
  const CONFIG_API_TIMEOUT_MS = 120000;
  const CONFIG_BG_POLL_WAIT_MS = 120000;
  const CONFIG_POLL_INTERVAL_MS = 1000;
  /** CE-02m-3 factory CT ratio, K×1000 (device_config.CE_CT_RATIO_DEFAULT; the meter stores a written 0 as this). */
  const CE_CT_RATIO_DEFAULT = 4000;
  /** Daemon refusal of a CT-ratio write to firmware that resets on it (device_config.E_CE_FW_TOO_OLD_FOR_CT_WRITE). */
  const E_CE_FW_TOO_OLD_FOR_CT_WRITE = 'ce_fw_too_old_for_ct_write';
  /** AI sensor select под сохранением/редактированием — не перерисовывать вкладку. */
  const _aiSensorEditGuard = new Set();
  const _aiSensorEditGuardTimers = Object.create(null);
  const _aiSensorWriteInflight = new Set();
  const _AI_SENSOR_EDIT_GUARD_MS = 1500;

  const STATUS_AUTO_CLEAR_MS = 3000;
  const _inlineStatusTimers = Object.create(null);
  const _inlineStatusGen = Object.create(null);

  function bumpInlineStatusGen(key) {
    _inlineStatusGen[key] = (_inlineStatusGen[key] || 0) + 1;
    return _inlineStatusGen[key];
  }

  function clearInlineStatusTimer(key) {
    if (_inlineStatusTimers[key]) {
      clearTimeout(_inlineStatusTimers[key]);
      delete _inlineStatusTimers[key];
    }
  }

  function cancelInlineStatusAutoClear(key) {
    clearInlineStatusTimer(key);
    bumpInlineStatusGen(key);
  }

  function scheduleInlineStatusAutoClear(key, clearFn) {
    clearInlineStatusTimer(key);
    const gen = bumpInlineStatusGen(key);
    _inlineStatusTimers[key] = setTimeout(() => {
      delete _inlineStatusTimers[key];
      if (_inlineStatusGen[key] !== gen) return;
      clearFn();
    }, STATUS_AUTO_CLEAR_MS);
  }

  function $(id) { return document.getElementById(id); }

  function unitUiLabel(name) {
    const bare = String(name || '').replace(/\.(service|socket)$/i, '');
    const aliases = {
      mplc: 'MPLC4',
      mplc4: 'MPLC4',
      'sa02m-modbus-mqtt': 'MQTT',
      'modbus-mqtt': 'MQTT',
      MQTT: 'MQTT',
    };
    return aliases[bare] || bare;
  }

  function currentPort() {
    const sel = $('flasher-port');
    return state.ports.find(p => p.key === sel.value) || null;
  }

  function selectedBaudrates() {
    return Array.from(document.querySelectorAll('#flasher-baudrates input:checked')).map(el => parseInt(el.value, 10));
  }

  function setBadge(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'badge ' + (kind === 'ok' ? 'badge-ok' : kind === 'err' ? 'badge-err' : 'badge-unk');
  }

  function t(msg) {
    return window.sa02mI18n ? window.sa02mI18n.t(String(msg)) : String(msg);
  }
  // The name the i18n-dict-contract gate sweeps for literal strings.
  const uiT = t;

  function toast(msg, type) {
    const text = t(msg);
    if (window.toast) window.toast(text, type || 'info', STATUS_AUTO_CLEAR_MS);
    else console.log('[flasher]', text);
  }

  const FW_MSG_NO_INTERNET = 'Нет доступа к интернету';

  function isFirmwareOfflineError(raw) {
    const s = String(raw || '').trim().toLowerCase();
    if (!s) return false;
    if (s === FW_MSG_NO_INTERNET.toLowerCase()) return true;
    if (/^http\s*(error\s*)?(502|503|504)\b/.test(s)) return true;
    if (/^http\s+(502|503|504)\b/.test(s)) return true;
    return /temporary failure|name resolution|name or service not known|\bdns\b|econnrefused|enetunreach|network is unreachable|no route to host|connection refused|connection timed out|timed out|\btimeout\b|failed to fetch|fetch failed|networkerror|err_internet_disconnected|err_network_changed/.test(s);
  }

  function formatFirmwareError(raw, context) {
    const msg = String(raw || '').trim();
    if (!msg) {
      if (context === 'upload') return 'Не удалось загрузить прошивку';
      if (context === 'load') return 'Не удалось загрузить список прошивок';
      return 'Не удалось обновить список прошивок';
    }
    if (isFirmwareOfflineError(msg)) return FW_MSG_NO_INTERNET;

    const http = msg.match(/^HTTP(?:\s+Error)?\s+(\d{3})\b/i);
    if (http) {
      const code = parseInt(http[1], 10);
      if (code === 401 || code === 403) return 'Нет доступа';
      if (code >= 500 && code <= 504) return FW_MSG_NO_INTERNET;
      if (code >= 400) return 'Ошибка сервера (' + code + ')';
    }

    if (/^json:/i.test(msg)) return 'Некорректный ответ сервера прошивок';
    if (/^ошибка скачивания https?:\/\//i.test(msg) && isFirmwareOfflineError(msg)) {
      return FW_MSG_NO_INTERNET;
    }
    if (/^ошибка скачивания https?:\/\//i.test(msg)) {
      return 'Не удалось скачать прошивку с сервера';
    }
    if (/^manifest:/i.test(msg)) return msg.replace(/^manifest:\s*/i, '').trim() || FW_MSG_NO_INTERNET;
    if (/^манифест:\s*/i.test(msg)) return msg.replace(/^манифест:\s*/i, '').trim() || FW_MSG_NO_INTERNET;

    return msg;
  }

  function toastFirmwareError(raw, type, context) {
    toast(formatFirmwareError(raw, context), type || 'error');
  }

  async function apiGet(path) {
    const res = await fetch(API + path, { credentials: 'same-origin' });
    if (!res.ok) {
      let detail = '';
      try {
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        if (ct.includes('application/json')) {
          const j = await res.json();
          if (j && j.error) detail = ': ' + j.error;
        } else {
          const t = (await res.text()).trim().slice(0, 200);
          if (t) detail = ': ' + t;
        }
      } catch (_) {}
      throw new Error(`HTTP ${res.status}${detail}`);
    }
    return res.json();
  }

  // Every daemon POST carries the panel's CSRF token like every CGI POST
  // (1.0.6.65, selective-csrf-policy.md «Демоны»); a refusal is E_CSRF and
  // app.js's fetch guard refreshes + retries once. Gate: js-post-csrf-headers.
  // The typeof guard (plan §5.2) keeps a page whose app.js failed to load from
  // throwing here: the request then goes out without the token and the daemon
  // answers E_CSRF — a refusal, never a silent mutation.
  async function apiPost(path, body) {
    const res = await fetch(API + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: typeof withCsrfHeaders === 'function' ? withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }) : { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const data = await res.json(); if (data && data.error) msg = data.error; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  function persistJobId(kind, jobId) {
    if (!jobId) return;
    sessionStorage.setItem(kind === 'flash' ? FLASH_JOB_KEY : SCAN_JOB_KEY, jobId);
  }

  function clearPersistedJobId(kind) {
    sessionStorage.removeItem(kind === 'flash' ? FLASH_JOB_KEY : SCAN_JOB_KEY);
  }

  function anyPortActiveJob() {
    return (state.ports || []).some(p => p && p.active_job);
  }

  function isFlasherOperationActive() {
    return !!(
      state.scanPending || state.flashPending ||
      state.scanJobId || state.flashJobId ||
      state.flasherGloballyBusy || anyPortActiveJob()
    );
  }

  function updateGlobalBusyFromPorts() {
    state.flasherGloballyBusy = anyPortActiveJob();
  }

  function replayJobEventsToLog(events) {
    (events || []).forEach(e => {
      if (!e || typeof e.message !== 'string') return;
      const lv = e.level || 'info';
      if (lv === 'debug') return;
      if (e.kind === 'log' || e.kind === 'status' || e.kind === 'error') {
        logAppend(e.message, lv);
      } else if (e.kind === 'progress' && e.message) {
        logAppend(e.message, lv);
      }
    });
  }

  function jobKindIsFlash(kind) {
    return kind === 'flash' || kind === 'flash_batch';
  }

  async function attachToActiveJobs() {
    if (state.scanPending || state.flashPending || state.scanJobId || state.flashJobId) return;

    let status = null;
    try {
      status = await apiGet('/status');
      state.flasherGloballyBusy = !!(status && status.busy);
    } catch (_) {
      updateGlobalBusyFromPorts();
    }

    const activeJobs = (status && status.active_jobs) || [];
    const savedFlash = sessionStorage.getItem(FLASH_JOB_KEY);
    const savedScan = sessionStorage.getItem(SCAN_JOB_KEY);

    const flashJob = activeJobs.find(j => jobKindIsFlash(j.kind));
    const scanJob = activeJobs.find(j => j.kind === 'scan');

    const candidates = [
      { kind: 'flash', id: (flashJob && flashJob.id) || savedFlash },
      { kind: 'scan', id: (scanJob && scanJob.id) || savedScan },
    ];

    for (const item of candidates) {
      if (!item.id) continue;
      try {
        const snap = await apiGet('/jobs/' + item.id);
        if (snap.state !== 'pending' && snap.state !== 'running') {
          clearPersistedJobId(item.kind);
          continue;
        }
        await reconnectToJob(item.kind, item.id, snap);
        return;
      } catch (_) {
        clearPersistedJobId(item.kind);
      }
    }
    // status.busy may stick after a finished job when nothing was reattached —
    // fall back to live port active_job so Scan is not left permanently disabled.
    if (!state.scanJobId && !state.flashJobId) {
      updateGlobalBusyFromPorts();
    }
    syncActionButtons();
  }

  async function reconnectToJob(kind, jobId, snap) {
    const portKey = snap.port || '';
    if (portKey && $('flasher-port')) {
      $('flasher-port').value = portKey;
    }
    await loadPorts();
    const label = kind === 'flash' ? 'прошивки' : 'сканирования';
    logReset(`Восстановление ${label} (задача ${jobId.slice(0, 8)}…)`);
    replayJobEventsToLog(snap.events || []);
    if (typeof snap.progress === 'number') {
      setProgress(snap.progress, snap.message || `${snap.progress}%`, progressMetaFromData(snap));
    }

    if (kind === 'flash') {
      state.flashJobId = jobId;
      state.flashIrreversible = !!snap.irreversible;
      persistJobId('flash', jobId);
      setFlashButtons();
      state.flashStream = openStream(jobId, {
        onEnd: async state2 => {
          state.flashJobId = null;
          state.flashIrreversible = false;
          clearPersistedJobId('flash');
          hideProgress();
          if (state2 === 'error' || state2 === 'cancelled') {
            await loadPorts();
            updateGlobalBusyFromPorts();
            setFlashButtons();
            if (state2 === 'error') toast('Прошивка прервана или завершилась с ошибкой. Выполните сканирование.', 'error');
            else toast('Прошивка отменена', 'warn');
          } else {
            updateGlobalBusyFromPorts();
            setFlashButtons();
            toast('Прошивка завершена', 'success');
            await refreshScanAfterFlash();
          }
        },
      });
      toast('Восстановлено отслеживание прошивки', 'info');
      return;
    }

    state.scanJobId = jobId;
    persistJobId('scan', jobId);
    setScanButtons();
    state.scanStream = openStream(jobId, {
      onDeviceFound: dev => {
        upsertScannedDevice(dev);
        renderDevices();
      },
      onEnd: async state2 => {
        await finalizeScanEnd(jobId, portKey, state2);
      },
    });
    toast('Восстановлено отслеживание сканирования', 'info');
  }

  async function apiUpload(path, file) {
    const fd = new FormData();
    fd.append('file', file);
    // FormData keeps its own multipart Content-Type (boundary) — only the token is added.
    const res = await fetch(API + path, { method: 'POST', credentials: 'same-origin', headers: typeof withCsrfHeaders === 'function' ? withCsrfHeaders({}) : {}, body: fd });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const data = await res.json(); if (data && data.error) msg = data.error; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  /* ── Порты ──────────────────────────────────────────────────────────────── */

  function renderPortSelect() {
    const sel = $('flasher-port');
    const prev = sel.value;
    sel.innerHTML = '';
    if (!state.ports.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'Нет портов в ответе демона (проверьте sa02m-flasher и /etc/sa02m_flasher.conf)';
      opt.disabled = true;
      sel.appendChild(opt);
      return;
    }
    state.ports.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.key;
      const status = [];
      if (!p.exists) status.push('нет устройства');
      if (p.busy_pids != null && p.busy_pids.length) status.push('занят (PID ' + p.busy_pids.join(',') + ')');
      if (p.active_job) status.push('активная задача');
      opt.textContent = `${p.label || p.key} — ${p.device_path}` + (status.length ? ' [' + status.join(', ') + ']' : '');
      opt.disabled = false;
      opt.title = !p.exists ? 'Устройство ' + p.device_path + ' не найдено — проверьте udev/симлинки COM/RS-485' : '';
      sel.appendChild(opt);
    });
    const fallback = state.ports.find(p => p.exists) || state.ports[0];
    const want = state.ports.some(p => p.key === prev) ? prev : fallback.key;
    sel.value = want;
    if (!sel.value && fallback) {
      const ix = state.ports.findIndex(p => p.key === fallback.key);
      if (ix >= 0) sel.selectedIndex = ix;
    }
  }

  function reconcilePortRestoration() {
    if (state.ports.some(p => p.restore_pending)) {
      state.portRestorePending = true;
    } else if (state.portRestorePending && state.ports.length && state.ports.every(p => p.coordinator_available === true)) {
      state.portRestorePending = false;
      state.portRecoveryError = '';
    }
  }

  async function loadPorts() {
    try {
      try {
        const quick = await apiGet('/ports?quick=1');
        if (quick && (quick.ports || []).length) {
          state.ports = quick.ports;
          reconcilePortRestoration();
          updateGlobalBusyFromPorts();
          renderPortSelect();
          updatePortHint();
        }
      } catch (_) {
        /* достаточно полного ответа ниже */
      }
      const data = await apiGet('/ports');
      state.ports = data.ports || [];
      reconcilePortRestoration();
      updateGlobalBusyFromPorts();
      renderPortSelect();
      updatePortHint();
    } catch (err) {
      toast('Порты: ' + err.message, 'error');
    }
  }

  /** После scan/flash port_lease на сервере уже освободил порт; полный GET /ports не нужен. */
  function markPortIdleAfterJob(portKey) {
    const p = state.ports.find(x => x.key === portKey);
    if (!p) return false;
    p.active_job = null;
    if (Array.isArray(p.busy_pids)) p.busy_pids = [];
    updateGlobalBusyFromPorts();
    updatePortHint();
    return true;
  }

  /** End of scan: clear job/busy first, then re-enable Scan (hardpi scanfix). */
  async function finalizeScanEnd(jobId, portKey, endState) {
    state.scanJobId = null;
    state.scanPending = false;
    state.scanArbitrationActive = false;
    clearPersistedJobId('scan');
    hideProgress();
    try {
      if (jobId) {
        const snap = await apiGet('/jobs/' + jobId);
        replaceScannedDevices((snap.devices || []).map(d => Object.assign({}, d)));
        renderDevices();
      }
    } catch (_) {}
    const portRec = portKey ? state.ports.find(p => p.key === portKey) : null;
    const emptyResult = state.devices.length === 0;
    // On an empty or failed scan, reload port state so a poller-busy warning
    // names the *current* holder; the fast idle-path clears busy_pids locally
    // and would hide a still-running occupant.
    if (endState === 'done' && !emptyResult && portHasCompleteLineState(portRec)) {
      markPortIdleAfterJob(portKey);
    } else {
      await loadPorts();
    }
    updateGlobalBusyFromPorts();
    setScanButtons();
    if (endState === 'error') setScanStatus('Сканирование завершилось с ошибкой', 'error');
    else if (endState === 'cancelled') setScanStatus('Сканирование отменено', 'warn');
    else setScanStatus('Сканирование завершено. Найдено ' + state.devices.length + ' устройств.', 'success');
    // A nothing-found (or port-busy error) scan where a poller still holds the
    // line: name the holder and offer a one-click stop-and-rescan. This is the
    // more likely cause than a BACnet latch, so it takes precedence over the
    // BACnet-sniff offer below.
    if (maybeOfferPollerBusy(endState, portKey)) {
      hideBacnetSniffOffer();
    } else {
      // §5.5 Roster honesty: a scan that finds nothing may be a BACnet-latched
      // module (silent on Modbus). Offer a one-click passive MS/TP sniff to
      // diagnose it instead of leaving it lost.
      maybeOfferBacnetSniff(endState, portKey);
    }
  }

  function bacnetSniffOfferEl() { return $('flasher-bacnet-sniff-offer'); }

  function hideBacnetSniffOffer() {
    const el = bacnetSniffOfferEl();
    if (el) { el.hidden = true; el.innerHTML = ''; }
  }

  function maybeOfferBacnetSniff(endState, portKey) {
    const el = bacnetSniffOfferEl();
    if (!el) return;
    if (endState !== 'done' || state.devices.length > 0 || !portKey) { hideBacnetSniffOffer(); return; }
    el.hidden = false;
    el.innerHTML = '<span>' + escapeHtml(t('Ничего не найдено. Модуль мог быть переключён в BACnet — проверить активность MS/TP?')) +
      '</span> <button type="button" class="btn btn-sm" id="flasher-bacnet-sniff-btn">' +
      escapeHtml(t('Проверить BACnet-активность')) + '</button>';
    const btn = $('flasher-bacnet-sniff-btn');
    if (btn) btn.addEventListener('click', () => verifyBacnetOnPort(portKey));
  }

  function pollerBusyOfferEl() { return $('flasher-poller-busy-offer'); }

  function hidePollerBusyOffer() {
    const el = pollerBusyOfferEl();
    if (el) { el.hidden = true; el.innerHTML = ''; }
  }

  /** UI labels of the poller units holding this port (MPLC4 / MQTT / …). */
  function portHolderLabels(port) {
    const svc = port && Array.isArray(port.active_services) ? port.active_services : [];
    return svc.map(unitUiLabel).filter(Boolean);
  }

  /* Identify the remaining owner after an empty or refused scan. */
  function maybeOfferPollerBusy(endState, portKey) {
    const el = pollerBusyOfferEl();
    if (!el) return false;
    const emptyDone = endState === 'done' && state.devices.length === 0;
    if ((!emptyDone && endState !== 'error') || !portKey) { hidePollerBusyOffer(); return false; }
    const port = state.ports.find(p => p.key === portKey) || null;
    const labels = portHolderLabels(port);
    const pids = port && Array.isArray(port.busy_pids) ? port.busy_pids : [];
    // Only warn when the line is actually held by a poller unit or an occupant.
    if (!labels.length && !pids.length) { hidePollerBusyOffer(); return false; }
    let msg;
    // An operation can pause MQTT and the services explicitly assigned to its UART.
    const managed = (port && port.managed_services || []).map(unitUiLabel);
    const withButton = labels.length > 0 && pids.length <= labels.length && labels.every(label => managed.includes(label));
    if (labels.length) {
      msg = t('Ничего не найдено, но линию опрашивает: ') + labels.join(', ') + '.';
    } else {
      msg = t('Ничего не найдено, но порт удерживает сторонний процесс (PID ') + pids.join(', ') + '). ' +
        t('systemd не сообщает об активном unit опроса — освободите процесс на устройстве вручную.');
    }
    el.hidden = false;
    el.innerHTML = '<span>' + escapeHtml(msg) + '</span>' +
      (withButton
        ? ' <button type="button" class="btn btn-sm" id="flasher-poller-busy-btn">' +
          escapeHtml(t('Сканировать')) + '</button>'
        : '');
    if (withButton) {
      const btn = $('flasher-poller-busy-btn');
      if (btn) btn.addEventListener('click', () => stopPollersAndRescan(portKey));
    }
    return true;
  }

  /* The scan worker acquires and restores the selected UART itself. */
  async function stopPollersAndRescan(portKey) {
    if (state.scanJobId || state.flashJobId || state.scanPending || state.flashPending) {
      setScanStatus('Дождитесь завершения текущей задачи', 'warn');
      return;
    }
    if (state.portActionBusy || $('flasher-port').value !== portKey) return;
    hidePollerBusyOffer();
    await startScan();
  }

  /* Passive MS/TP sniff on a port from the roster offer (no config modal open).
     Family is unknown after an empty scan → MR default (38400 8N1), which also
     catches a DTV master running at 38400; a diagnostic "is anything alive". */
  async function verifyBacnetOnPort(portKey) {
    if (!portKey) return;
    if (state.scanJobId || state.flashJobId) { setScanStatus('Дождитесь завершения текущей задачи', 'warn'); return; }
    const btn = $('flasher-bacnet-sniff-btn');
    if (btn) btn.disabled = true;
    let res;
    try {
      res = await apiPost('/bacnet/verify', { port: portKey, family: 'mr' });
    } catch (err) {
      setScanStatus('Проверка BACnet: ' + err.message, 'error');
      if (btn) btn.disabled = false;
      return;
    }
    const jobId = res && res.job_id;
    if (!jobId) { if (btn) btn.disabled = false; return; }
    logReset('Проверка BACnet-активности на порту ' + portKey);
    openStream(jobId, {
      onEnd: async () => {
        let result = null;
        try { const snap = await apiGet('/jobs/' + jobId); result = snap && snap.result; } catch (_) {}
        const el = bacnetSniffOfferEl();
        if (el && result) {
          if (result.open_error) {
            el.innerHTML = '<span>' + escapeHtml(t('Проверка BACnet: ошибка порта') + ' — ' + result.open_error) + '</span>';
          } else if (result.alive) {
            el.innerHTML = '<span>' + escapeHtml(t('MS/TP активен — модуль в режиме BACnet. Кадров: ') + (result.frames_seen || 0)) + '</span>';
          } else {
            el.innerHTML = '<span>' + escapeHtml(t('Кадры MS/TP не обнаружены — модуля нет на этой линии.')) + '</span>';
          }
        }
        if (btn) btn.disabled = false;
        await loadPorts();
        updateGlobalBusyFromPorts();
        setScanButtons();
      },
    });
  }

  function portHasCompleteLineState(port) {
    return !!(port && port.busy_pids != null && port.active_services != null);
  }

  /** Подставить в журнал последние события последней задачи (GET /jobs), пока нет активного SSE. */
  async function loadRecentJobJournal() {
    if (state.scanJobId || state.flashJobId || state.scanPending || state.flashPending) return;
    try {
      const data = await apiGet('/jobs');
      if (state.scanJobId || state.flashJobId || state.scanPending || state.flashPending) return;
      const jobs = data.jobs || [];
      if (!jobs.length) {
        const box = $('flasher-log');
        if (box) {
          box.innerHTML = '';
          logAppend('Нет задач в памяти демона. Запустите сканирование или прошивку — строки журнала появятся здесь.', 'info');
        }
        return;
      }
      const j = jobs[0];
      const evs = j.events || [];
      logReset('Последняя задача: ' + (j.kind || '—') + ', порт ' + (j.port || '—') + ', состояние ' + (j.state || '—'));
      evs.forEach(e => {
        if (!e || typeof e.message !== 'string') return;
        const lv = e.level || 'info';
        if (lv === 'debug') return;
        if (e.kind === 'log' || e.kind === 'status' || e.kind === 'error') {
          logAppend(e.message, lv);
        } else if (e.kind === 'progress' && e.message) {
          logAppend(e.message, lv);
        }
      });
      if (!evs.length) {
        logAppend('(В снимке задачи нет сохранённых строк журнала)', 'debug');
      }
    } catch (err) {
      const box = $('flasher-log');
      if (box) {
        box.innerHTML = '';
        logAppend('Не удалось загрузить журнал с сервера: ' + err.message, 'error');
      }
    }
  }

  function firmwareEntryKey(entry) {
    if (!entry) return '';
    return `${entry.channel}::${entry.file}`;
  }

  function promoteFirmwareKey(key, select) {
    if (!key) return;
    state.firmwareDisplayOrder = [key, ...state.firmwareDisplayOrder.filter(k => k !== key)];
    if (select !== false) state.selectedFirmwareKey = key;
  }

  function promoteFirmwareEntry(entry, select) {
    promoteFirmwareKey(firmwareEntryKey(entry), select);
  }

  function orderedFirmwareEntries() {
    const entries = state.firmware;
    if (!entries.length) return [];
    const byKey = new Map(entries.map(e => [firmwareEntryKey(e), e]));
    const ordered = [];
    const seen = new Set();
    for (const key of state.firmwareDisplayOrder) {
      const e = byKey.get(key);
      if (e) {
        ordered.push(e);
        seen.add(key);
      }
    }
    for (const e of entries) {
      const key = firmwareEntryKey(e);
      if (!seen.has(key)) ordered.push(e);
    }
    return ordered;
  }

  /** Записи для таблицы «Доступные прошивки»: только скачанные в кеш (manifest + upload). */
  function visibleFirmwareEntries() {
    return orderedFirmwareEntries().filter(isFirmwareEntryDownloaded);
  }

  function pickFirmwareToAutoSelect(newEntries) {
    if (!newEntries.length) return null;
    const downloaded = newEntries.filter(isFirmwareEntryDownloaded);
    if (!downloaded.length) return null;
    const latestApp = state.latestStableVersion;
    if (latestApp) {
      const match = downloaded.find(e =>
        e.channel === 'stable' && String(e.kind || 'app').toLowerCase() === 'app' &&
        String(e.version || '').trim() === latestApp
      );
      if (match) return match;
    }
    return downloaded[0];
  }

  function firmwareEntryMeta(entry) {
    if (!entry) return '';
    return `${entry.sha256 || ''}:${entry.size || 0}:${entry.version || ''}`;
  }

  function pruneFirmwareDisplayOrder() {
    const valid = new Set(state.firmware.map(firmwareEntryKey));
    state.firmwareDisplayOrder = state.firmwareDisplayOrder.filter(k => valid.has(k));
  }

  function applyFirmwareListChanges(prevKeys, prevDownloaded, prevMeta) {
    if (!prevKeys) return;
    const added = state.firmware.filter(e => !prevKeys.has(firmwareEntryKey(e)));
    const newlyDownloaded = state.firmware.filter(e => {
      const key = firmwareEntryKey(e);
      return prevKeys.has(key) && !prevDownloaded.get(key) && isFirmwareEntryDownloaded(e);
    });
    const newlyUndownloaded = state.firmware.filter(e => {
      const key = firmwareEntryKey(e);
      return prevKeys.has(key) && prevDownloaded.get(key) && !isFirmwareEntryDownloaded(e);
    });
    const removed = [...prevKeys].filter(key =>
      !state.firmware.some(e => firmwareEntryKey(e) === key)
    );
    const updated = prevMeta
      ? state.firmware.filter(e => {
        const key = firmwareEntryKey(e);
        if (!prevKeys.has(key)) return false;
        const was = prevMeta.get(key);
        return was != null && firmwareEntryMeta(e) !== was;
      })
      : [];
    if (newlyUndownloaded.length || removed.length) {
      const sel = state.selectedFirmwareKey;
      if (sel && (removed.includes(sel) || newlyUndownloaded.some(e => firmwareEntryKey(e) === sel))) {
        state.selectedFirmwareKey = '';
      }
    }
    const changed = added.concat(newlyDownloaded.filter(e =>
      !added.some(a => firmwareEntryKey(a) === firmwareEntryKey(e))
    )).concat(newlyUndownloaded).concat(updated.filter(e => {
      const key = firmwareEntryKey(e);
      return !added.some(a => firmwareEntryKey(a) === key)
        && !newlyDownloaded.some(a => firmwareEntryKey(a) === key);
    }));
    if (!changed.length) {
      if (newlyUndownloaded.length || removed.length) {
        renderFirmware();
        updateFlashControls();
      }
      return;
    }
    changed.forEach(e => promoteFirmwareEntry(e, false));
    promoteFirmwareEntry(pickFirmwareToAutoSelect(changed), true);
  }

  function applyFirmwareStatusPayload(data) {
    if (!data || !Array.isArray(data.entries)) return false;
    state.firmware = data.entries;
    state.latestStableVersion = (data.latest_stable_version || '').trim();
    state.latestBootloaderVersion = (data.latest_bootloader_version || '').trim();
    state.latestByDevice = (data.latest_by_device && typeof data.latest_by_device === 'object')
      ? data.latest_by_device : {};
    pruneFirmwareDisplayOrder();
    return true;
  }

  function formatFirmwareSizeLabel(sizeBytes) {
    const n = parseInt(sizeBytes, 10) || 0;
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    const kb = Math.max(1, Math.round(n / 1024));
    return kb + ' kB';
  }

  function firmwareEntryDescription(entry) {
    if (!entry) return '';
    const kind = String(entry.kind || 'app').toLowerCase();
    const product = firmwareProductKindFromEntry(entry);
    const sizeStr = formatFirmwareSizeLabel(entry.size);
    const dlTag = isFirmwareEntryDownloaded(entry) ? '' : ' · ' + t('не скачан');

    if (product === 'dtv' && kind === 'bootloader') {
      return t('bootloader датчиков температуры и влажности ДТВ-RS-485') + ` · ${sizeStr}${dlTag}`;
    }
    if (product === 'mr' && kind === 'bootloader') {
      return t('bootloader модулей расширения МР-02м') + ` · ${sizeStr}${dlTag}`;
    }
    if (product === 'dtv' && kind === 'app') {
      return t('Датчики температуры и влажности ДТВ-RS-485') + ` · ${sizeStr}${dlTag}`;
    }
    if (product === 'mr' && kind === 'app') {
      return t('Модули расширения МР-02м') + ` · ${sizeStr}${dlTag}`;
    }
    const sig = (entry.signatures && entry.signatures.length)
      ? entry.signatures.join(', ')
      : t('все варианты MR-02м (общий образ)');
    const kindTag = kind !== 'app' ? ` · ${kind}` : '';
    return `${sig}${kindTag} · ${sizeStr}${dlTag}`;
  }

  function selectedFirmwareEntry() {
    const fwVal = state.selectedFirmwareKey;
    if (!fwVal) return null;
    const [channel, file] = fwVal.split('::');
    return state.firmware.find(e => e.channel === channel && e.file === file)
      || state.firmware.find(e => firmwareEntryKey(e) === fwVal)
      || state.firmware.find(e => e.file === file)
      || null;
  }

  function selectedDevicesForFlash() {
    return [...state.selectedDeviceIndices]
      .sort((a, b) => a - b)
      .map(i => state.devices[i])
      .filter(Boolean);
  }

  function selectedDeviceKeysFromIndices() {
    return new Set(
      selectedDevicesForFlash().map(d => scanDeviceRowKey(d))
    );
  }

  function restoreSelectionByKeys(keys) {
    state.selectedDeviceIndices.clear();
    if (!keys || !keys.size) return;
    state.devices.forEach((d, idx) => {
      if (keys.has(scanDeviceRowKey(d))) state.selectedDeviceIndices.add(idx);
    });
  }

  function toggleDeviceSelection(idx) {
    if (!state.devices[idx]) return;
    if (state.selectedDeviceIndices.has(idx)) {
      state.selectedDeviceIndices.delete(idx);
    } else {
      state.selectedDeviceIndices.add(idx);
    }
  }

  function validateMultiFlashSelection(devices) {
    const list = devices || selectedDevicesForFlash();
    if (!list.length) return 'Выберите устройство';
    const routes = list.map(d => resolveDeviceFlashRoute(d.signature));
    const unknown = list.filter((d, i) => routes[i] === 'unknown');
    if (unknown.length) {
      const sigs = unknown
        .map(d => stripBootloaderSignatureSuffix(d.signature) || '?')
        .join(', ');
      return `Сигнатура не распознана: ${sigs}. Выполните сканирование.`;
    }
    const hasMpMr = routes.some(r => r === 'mp_mr');
    const hasWb = routes.some(r => r === 'wb');
    if (hasMpMr && hasWb) {
      return 'Нельзя прошивать вместе модули MR/MP и сторонние (.wbfw). Выберите устройства одного типа.';
    }
    return '';
  }

  function firmwareSelectionMismatch(selectedDevices, fwEntry) {
    const list = Array.isArray(selectedDevices)
      ? selectedDevices
      : (selectedDevices ? [selectedDevices] : []);
    if (!list.length || !fwEntry) return false;
    return list.some(d => validateFirmwareSelectionForDevice(d.signature, fwEntry));
  }

  function isFirmwareEntryDownloaded(entry) {
    if (!entry) return false;
    return !!entry.downloaded;
  }

  function stableEntryForVersion(kind, version) {
    const ver = String(version || '').trim();
    if (!ver) return null;
    return state.firmware.find(e =>
      e.channel === 'stable' && e.kind === kind && String(e.version || '').trim() === ver
    ) || null;
  }

  function syncActionButtons() {
    const port = currentPort();
    const scanRunning = state.scanPending || !!state.scanJobId;
    const flashRunning = state.flashPending || !!state.flashJobId;
    const globalBusy = isFlasherOperationActive();
    const jobBusy = !!(port && port.active_job) || globalBusy;
    const hasReservations = state.ports.some(p => p.reserved || p.restore_pending || (p.released_services && p.released_services.length));
    const managedN = port && Array.isArray(port.managed_services) ? port.managed_services.length : 0;
    const selectedDevices = selectedDevicesForFlash();
    const anyChecked = selectedDevices.length > 0;
    const fwEntry = selectedFirmwareEntry();
    const hasFw = !!fwEntry;
    const fwReady = hasFw && isFirmwareEntryDownloaded(fwEntry);
    const selectionErr = anyChecked ? validateMultiFlashSelection(selectedDevices) : '';
    const fwMismatch = firmwareSelectionMismatch(selectedDevices, fwEntry);
    const flashBlocked = !!(selectionErr || fwMismatch);

    const scanBtn = $('flasher-scan-btn');
    if (scanBtn) {
      const canCancel = !!(state.scanJobId && !state.scanPending);
      scanBtn.classList.add('btn-primary');
      if (scanRunning) {
        scanBtn.textContent = 'Отмена';
        scanBtn.classList.add('is-cancel');
        scanBtn.disabled = !canCancel || flashRunning || state.portActionBusy;
        scanBtn.title = canCancel ? 'Отменить сканирование' : 'Запуск сканирования';
      } else {
        scanBtn.textContent = 'Сканировать';
        scanBtn.classList.remove('is-cancel');
        scanBtn.disabled = !port || !port.exists || flashRunning || jobBusy || state.portActionBusy;
        scanBtn.title = 'Сканировать шину RS-485';
      }
    }
    const canStopPollers = !!(port && port.exists && managedN);
    $('flasher-release-port-btn').disabled = !canStopPollers || scanRunning || flashRunning || jobBusy || state.portActionBusy;
    $('flasher-restore-port-btn').disabled = !port || scanRunning || flashRunning || jobBusy || state.portActionBusy || !hasReservations;
    const flashBtn = $('flasher-flash-btn');
    if (flashBtn) {
      const canCancelFlash = !!(state.flashJobId && !state.flashPending && !state.flashIrreversible);
      flashBtn.classList.add('btn-primary');
      if (flashRunning) {
        updateFlashButtonLabel(0, 'Отмена');
        flashBtn.classList.add('is-cancel');
        flashBtn.disabled = !canCancelFlash || scanRunning || state.portActionBusy;
        flashBtn.title = state.flashIrreversible
          ? 'Прошивка необратима — отмена невозможна'
          : (canCancelFlash ? 'Отменить прошивку' : 'Запуск прошивки');
      } else {
        flashBtn.classList.remove('is-cancel');
        updateFlashButtonLabel(selectedDevices.length);
        flashBtn.disabled = !port || !port.exists || scanRunning || jobBusy || !(anyChecked && fwReady) || flashBlocked;
        flashBtn.title = 'Прошить выбранные устройства';
      }
    }

    const mismatchEl = $('flasher-fw-mismatch');
    if (mismatchEl) {
      const mismatchText = selectionErr || (fwMismatch ? 'Выберите корректную прошивку' : '');
      if (mismatchText) {
        const wasHidden = mismatchEl.hidden;
        mismatchEl.textContent = mismatchText;
        mismatchEl.hidden = false;
        if (wasHidden) {
          scheduleInlineStatusAutoClear('flasher-fw-mismatch', () => {
            mismatchEl.textContent = '';
            mismatchEl.hidden = true;
          });
        }
      } else {
        cancelInlineStatusAutoClear('flasher-fw-mismatch');
        mismatchEl.textContent = '';
        mismatchEl.hidden = true;
      }
    }

    const fwHint = $('flasher-fw-hint');
    if (fwHint) {
      if (hasFw && !fwReady) {
        fwHint.textContent = 'Выбранный образ не скачан в кеш шлюза. Нажмите «Скачать» или «Выбрать».';
      } else {
        fwHint.textContent = '';
      }
    }
  }

  function updatePortHint() {
    const port = currentPort();
    const hint = $('flasher-port-hint');
    const recovering = state.ports.filter(p => p.restore_pending);
    if (!port) {
      setBadge('flasher-port-badge', 'Нет данных', 'unk');
      setBadge('flasher-poller-badge', 'Опрос не оценён', 'unk');
      hint.textContent = 'Выберите порт, чтобы увидеть состояние линии и опроса.';
      syncActionButtons();
      return;
    }

    if (!port.exists) setBadge('flasher-port-badge', 'Нет линии', 'err');
    else if (port.busy_pids == null) setBadge('flasher-port-badge', 'Проверка порта', 'unk');
    else if (port.active_job) setBadge('flasher-port-badge', 'Задача активна', 'unk');
    else if (port.busy_pids.length) setBadge('flasher-port-badge', 'Порт занят', 'err');
    else setBadge('flasher-port-badge', 'Порт свободен', 'ok');

    if (!port.exists) setBadge('flasher-poller-badge', 'Нет линии', 'unk');
    else if (port.restore_pending) setBadge('flasher-poller-badge', uiT('Восстановление ожидается'), 'err');
    else if (port.active_services == null) setBadge('flasher-poller-badge', 'Проверка опроса', 'unk');
    else if (port.active_services.length) setBadge('flasher-poller-badge', 'Опрос активен', 'unk');
    else if (port.reserved || (port.released_services && port.released_services.length)) setBadge('flasher-poller-badge', 'Опрос освобождён', 'ok');
    else if (port.busy_pids != null && port.busy_pids.length) setBadge('flasher-poller-badge', 'Опрос не определён', 'unk');
    else setBadge('flasher-poller-badge', 'Опрос не активен', 'ok');

    const pendingDetails =
      port.exists && (port.busy_pids == null || port.active_services == null);
    if (pendingDetails && !recovering.length) {
      hint.textContent = 'Проверка порта и опроса';
      syncActionButtons();
      return;
    }

    const bits = [];
    if (port.active_services && port.active_services.length) {
      bits.push(uiT('Линию сейчас занимают: ') + port.active_services.map(unitUiLabel).join(', ') + '.');
      if (port.active_services.includes('RS-485 Gateway')) {
        bits.push(uiT('Освободите линию во вкладке «Шлюз RS-485», затем повторите.'));
      } else {
        bits.push(uiT('Автоматически приостанавливается только MQTT и явно назначенные порту службы.'));
      }
    }
    if (port.released_services && port.released_services.length) {
      bits.push('Опрос вручную освобождён: ' + port.released_services.map(unitUiLabel).join(', ') + '.');
    }
    // The manual reserve pauses MQTT on the line even when no whole service is
    // configured for it — the state must be visible, or it is forgotten.
    if (port.reserved) bits.push('Порт зарезервирован вручную: опрос MQTT на нём приостановлен.');
    if (port.busy_pids && port.busy_pids.length) {
      bits.push('Порт удерживают PID ' + port.busy_pids.join(', ') + '.' +
        (port.active_services && port.active_services.length
          ? ' Если это не служба опроса, освободите процесс вручную.'
          : ' ' + uiT('Владелец не распознан по командной строке процесса. Проверьте его на устройстве.')));
    }
    if (port.active_job) bits.push('На линии выполняется активная задача, дождитесь её завершения.');
    if (!port.exists) bits.push('Устройство порта не найдено в системе.');
    const reserved = state.ports.filter(p => p.reserved).map(p => p.key);
    if (reserved.length) bits.push(uiT('Ручной резерв: ') + reserved.join(', ') + '. ' + uiT('Кнопка «Вернуть опрос» снимает все ручные резервы.'));
    recovering.forEach(p => {
      bits.push(uiT('Ожидается восстановление опроса: ') + p.key +
        ((p.restore_services || []).length ? ' (' + p.restore_services.map(unitUiLabel).join(', ') + ')' : '') +
        (p.restore_error ? ' — ' + p.restore_error : '') + '.');
    });
    if (recovering.length && recovering.every(p => p.coordinator_available === true)) bits.push(uiT('Координатор повторяет восстановление автоматически. «Вернуть опрос» проверяет результат.'));
    if (recovering.some(p => p.coordinator_available === false)) bits.push(uiT('Координатор недоступен; восстановление не подтверждено.'));
    if (state.portRecoveryError) bits.push(state.portRecoveryError);
    if (!bits.length) bits.push('Линия готова к сканированию и прошивке.');
    hint.textContent = bits.join(' ');
    syncActionButtons();
  }

  /* ── Репозиторий прошивок ─────────────────────────────────────────────── */

  async function loadFirmware(options) {
    const opts = options || {};
    const prevKeys = opts.trackChanges
      ? new Set(state.firmware.map(firmwareEntryKey))
      : null;
    const prevDownloaded = opts.trackChanges
      ? new Map(state.firmware.map(e => [firmwareEntryKey(e), isFirmwareEntryDownloaded(e)]))
      : null;
    const prevMeta = opts.trackChanges
      ? new Map(state.firmware.map(e => [firmwareEntryKey(e), firmwareEntryMeta(e)]))
      : null;
    try {
      const data = await apiGet('/firmware');
      applyFirmwareStatusPayload(data);
      if (opts.selectKey) {
        promoteFirmwareKey(opts.selectKey, true);
      } else if (opts.trackChanges) {
        applyFirmwareListChanges(prevKeys, prevDownloaded, prevMeta);
      }
      renderFirmware(data);
      updateFlashControls();
    } catch (err) {
      toastFirmwareError(err.message, 'error', 'load');
    }
  }

  // Manifest device family (manifest_device_for_signature) → firmware product
  // kind (firmwareProductKindFromEntry) — for the family filter below.
  const FAMILY_TO_PRODUCT = { 'MR-02m': 'mr', 'RTU-Sensor': 'dtv', 'CE-02m-3': 'ce' };

  /* Единственное семейство среди выбранных устройств ('' — нет выбора или смешение). */
  function selectedDeviceFamily() {
    const devs = selectedDevicesForFlash();
    if (!devs.length) return '';
    const fams = new Set(devs.map(d => manifestDeviceForSignature(d.signature)).filter(Boolean));
    return fams.size === 1 ? [...fams][0] : '';
  }

  /* Отфильтровать список прошивок под семейство выбранного устройства
     (нераспознанные/загруженные вручную образы не скрываем). */
  function firmwareEntriesForFamily(entries, family) {
    const product = FAMILY_TO_PRODUCT[family];
    if (!product) return entries;
    return entries.filter(e => {
      const k = firmwareProductKindFromEntry(e);
      return k === product || k === '';
    });
  }

  function renderFirmware(data) {
    const list = $('flasher-fw-list');
    const prevKey = state.selectedFirmwareKey;
    const visible = firmwareEntriesForFamily(visibleFirmwareEntries(), selectedDeviceFamily());
    if (!visible.length) {
      list.textContent = t('Нет скачанных прошивок. Нажмите «Скачать» или выберите .fw вручную.');
      state.selectedFirmwareKey = '';
    } else {
      list.innerHTML = '';
      visible.forEach(e => {
        const row = document.createElement('div');
        const key = firmwareEntryKey(e);
        row.className = 'flasher-fw-row is-selectable';
        if (key === prevKey) row.classList.add('is-selected');
        row.innerHTML = `<span class="flasher-fw-name">${escapeHtml(e.file)}</span>` +
          `<span class="flasher-fw-meta">${escapeHtml(firmwareEntryDescription(e))}</span>`;
        row.addEventListener('click', () => {
          state.selectedFirmwareKey = state.selectedFirmwareKey === key ? '' : key;
          if (state.selectedFirmwareKey) promoteFirmwareKey(key, true);
          renderFirmware(data);
          updateFlashControls();
        });
        list.appendChild(row);
      });
      if (prevKey && !visible.some(e => firmwareEntryKey(e) === prevKey)) {
        state.selectedFirmwareKey = '';
      }
    }
  }

  function maxVersionFromDevices(field) {
    let best = null;
    let bestRaw = '';
    for (const d of state.devices) {
      const raw = String(d[field] || '').trim();
      const t = parseVersionTuple(raw);
      if (!t) continue;
      if (!best || compareVersionTuple(t, best) > 0) {
        best = t;
        bestRaw = raw;
      }
    }
    return bestRaw;
  }

  async function refreshManifest(download) {
    try {
      const body = { download: !!download };
      if (download) {
        const app = maxVersionFromDevices('app_version');
        const bl = maxVersionFromDevices('bootloader_version');
        if (app || bl) {
          body.keep_current = {};
          if (app) body.keep_current.app = app;
          if (bl) body.keep_current.bootloader = bl;
        }
      }
      const res = await apiPost('/firmware/refresh', body);
      await loadFirmware({ trackChanges: !!download });
      if (res.error) {
        toastFirmwareError(res.error, 'warn', 'refresh');
      } else if (download) {
        const n = visibleFirmwareEntries().length;
        let msg = n
          ? ('Скачано в кеш: ' + n + ' прошивок')
          : 'Манифест обновлён, но файлы в кеш не загружены';
        if (res.purged && res.purged.length) {
          msg += ', очищено из кеша: ' + res.purged.length;
        }
        toast(msg, n ? 'success' : 'warn');
      } else {
        let msg = 'Список прошивок обновлён (записей: ' + res.entries + ')';
        if (res.purged && res.purged.length) {
          msg += ', очищено из кеша: ' + res.purged.length;
        }
        toast(msg, 'success');
      }
    } catch (err) {
      toastFirmwareError(err.message, 'error', 'refresh');
    }
  }

  async function uploadFirmware(file) {
    if (!file) return;
    try {
      const res = await apiUpload('/firmware/upload', file);
      toast('Загружено: ' + (res.entry && res.entry.file || file.name), 'success');
      const selectKey = res.entry ? firmwareEntryKey(res.entry) : '';
      await loadFirmware(selectKey ? { selectKey } : { trackChanges: true });
    } catch (err) {
      toastFirmwareError(err.message, 'error', 'upload');
    }
  }

  async function clearFirmwareCache() {
    if (!confirm(t('Удалить все скачанные прошивки из кеша шлюза?\nСписок в манифесте сохранится; файлы нужно будет скачать или загрузить заново.'))) return;
    try {
      const res = await apiPost('/firmware/clear', {});
      const n = (res.cleared && res.cleared.length) || 0;
      toast(n ? ('Очищено файлов: ' + n) : 'Кеш прошивок уже пуст', 'success');
      state.selectedFirmwareKey = '';
      if (!applyFirmwareStatusPayload(res)) {
        await loadFirmware();
        return;
      }
      renderFirmware();
      updateFlashControls();
    } catch (err) {
      toastFirmwareError(err.message, 'error', 'clear');
    }
  }

  /* ── Версии: сравнение с манифестом (общий образ — только по version) ─── */

  function parseVersionTuple(s) {
    if (s == null || s === '') return null;
    const parts = String(s).trim().split('.').slice(0, 4);
    const nums = [];
    for (const p of parts) {
      if (!/^\d+$/.test(p)) return null;
      nums.push(parseInt(p, 10));
    }
    if (!nums.length) return null;
    while (nums.length < 4) nums.push(0);
    return nums;
  }

  function compareVersionTuple(a, b) {
    for (let i = 0; i < 4; i++) {
      if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
    }
    return 0;
  }

  /* Синхронизировать логику с sa02m_flasher.module_profiles.is_mp_module_signature_for_batch_flash */
  function stripBootloaderSignatureSuffix(sig) {
    let s = String(sig || '').trim();
    if (s.toUpperCase().endsWith('_BL')) return s.slice(0, -3).trim();
    return s;
  }

  function isMpModuleSignatureForFirmwareHint(sig) {
    let s = stripBootloaderSignatureSuffix(sig);
    const n = s.toUpperCase().replace(/\s/g, '');
    if (!n || n === 'NONE' || n === '—' || n === '?') return false;
    const hintKeys = [
      '6DO8DI', '16DO', '12AO', '6DO', '14DI', '10DICON', '6DO5DI2AO', '6AO6AI', '6AI6AO', 'AO6AI6', '6AI2AO', 'AI6AO2', '12AI',
      '4DO6DI', '4TO6DI', 'TO4DI6', 'CE02M3', 'CE-02M-3', 'ENMETER', 'EN_METER',
      'SENSOR', 'SENS.',
    ];
    for (const key of hintKeys) {
      if (n.includes(key.replace(/-/g, '')) || n.includes(key) || n.startsWith(key.slice(0, 4))) return true;
    }
    const extra = ['DO6DI8', '6DO5DI2AO', 'DO4DI6', 'TO4DI6', '4TO6DI'];
    for (const tok of extra) {
      if (n.includes(tok)) return true;
    }
    const compact = n.replace(/-/g, '').replace(/_/g, '');
    for (const token of ['MP02M', 'MR02M', 'ENMETER']) {
      if (compact.includes(token)) return true;
    }
    return false;
  }

  const WB_RELAY_SIG_PREFIXES = ['MR2M', 'MR3', 'MR6', 'MRPS', 'MRWL', 'MRWM', 'MRM2'];
  const WB_MAO4_SIG_PREFIXES = ['MAO4'];
  const LINE_PROFILE_MP_MR = { baudrate: 115200, parity: 'N', stopbits: 1 };
  const LINE_PROFILE_WB_APP = { baudrate: 19200, parity: 'N', stopbits: 2 };

  function isWirenboardModuleSignature(sig) {
    let s = stripBootloaderSignatureSuffix(sig);
    if (!s) return false;
    const u = s.toUpperCase();
    if (u === 'NONE' || u === '—' || u === '?' || u === 'UNKNOWN') return false;
    if (isMpModuleSignatureForFirmwareHint(s)) return false;
    const n = s.toUpperCase().replace(/\s/g, '');
    for (const prefix of WB_MAO4_SIG_PREFIXES) {
      if (n.startsWith(prefix)) return true;
    }
    for (const prefix of WB_RELAY_SIG_PREFIXES) {
      if (n.startsWith(prefix)) return true;
    }
    if (s.length < 2 || s.length > 32) return false;
    if (!/^[A-Za-z0-9._-]+$/.test(s)) return false;
    if (!/[A-Za-z]/.test(s)) return false;
    return true;
  }

  function lineProfileFromScan(device, fallback) {
    const d = device || {};
    const fb = fallback || LINE_PROFILE_WB_APP;
    const baud = Number(d.baudrate) || fb.baudrate;
    let parity = String(d.parity || fb.parity).toUpperCase();
    if (parity !== 'N' && parity !== 'E' && parity !== 'O') parity = fb.parity;
    let stopbits = Number(d.stopbits) || fb.stopbits;
    if (stopbits !== 1 && stopbits !== 2) stopbits = fb.stopbits;
    return { baudrate: baud, parity, stopbits };
  }

  function resolveDeviceFlashRoute(signature) {
    if (isMpModuleSignatureForFirmwareHint(signature)) return 'mp_mr';
    if (isWirenboardModuleSignature(signature)) return 'wb';
    return 'unknown';
  }

  function validateFirmwareForDevice(signature, fileName) {
    const route = resolveDeviceFlashRoute(signature);
    const fwIsWbfw = isWbfwFirmwareFile(fileName);
    const sig = stripBootloaderSignatureSuffix(signature) || '?';
    if (route === 'unknown') {
      return `Сигнатура «${sig}» не распознана. Выполните сканирование.`;
    }
    if (route === 'mp_mr' && fwIsWbfw) {
      return `Для модуля MR/MP-02m («${sig}») выберите прошивку .fw, не .wbfw.`;
    }
    if (route === 'wb' && !fwIsWbfw) {
      return `Для стороннего устройства «${sig}» выберите прошивку .wbfw.`;
    }
    return '';
  }

  function normalizeProductToken(value) {
    return String(value || '').trim().toUpperCase().replace(/\s/g, '').replace(/-/g, '').replace(/_/g, '');
  }

  function tokenLooksLikeDtv(token) {
    const n = normalizeProductToken(token);
    if (!n) return false;
    return n.includes('DTV') || n.includes('RTUSENSOR') || n.includes('SENSOR') || n.startsWith('SENS');
  }

  function tokenLooksLikeCe(token) {
    const n = normalizeProductToken(token);
    if (!n) return false;
    // ENMETER mezzanine belongs to the CE-02m-3 family — parity with Python
    // module_profiles.SPECIAL_SIG_CODES (EN_METER/ENMETER → MP02_CE02M3).
    // normalizeProductToken strips '_'/'-', so EN_METER and ENMETER both match.
    return n.includes('CE02M3') || n.includes('CE02M') || n.includes('ENMETER');
  }

  function deviceProductKindForFlash(signature) {
    const cfgKind = deviceConfigKindFromSignature(signature);
    if (cfgKind) return cfgKind;
    if (resolveDeviceFlashRoute(signature) === 'wb') return 'wb';
    return '';
  }

  function firmwareProductKindFromEntry(entry) {
    if (!entry) return '';
    const file = String(entry.file || '');
    if (isWbfwFirmwareFile(file)) return 'wb';
    const dev = normalizeProductToken(entry.device);
    if (tokenLooksLikeDtv(dev)) return 'dtv';
    if (tokenLooksLikeCe(dev)) return 'ce';
    for (const sig of (entry.signatures || [])) {
      if (tokenLooksLikeDtv(sig)) return 'dtv';
      if (tokenLooksLikeCe(sig)) return 'ce';
    }
    const fn = normalizeProductToken(file);
    if (tokenLooksLikeDtv(fn)) return 'dtv';
    if (tokenLooksLikeCe(fn)) return 'ce';
    if ((entry.kind || 'app') === 'bootloader') return 'mr';
    if (fn.includes('MR02M') || fn.includes('MP02M') || dev.includes('MR02M') || dev.includes('MP02M')) return 'mr';
    if (file.toLowerCase().endsWith('.fw') || file.toLowerCase().endsWith('.bin')) return 'mr';
    return '';
  }

  /* Семейство прошивки (ключ `device` манифеста) по сигнатуре устройства.
     Зеркало sa02m_flasher.module_profiles.manifest_device_for_signature:
     CE-02m-3 / DTV (RTU-Sensor) — по специфике, иначе MR-02m; '' — неизвестно. */
  function manifestDeviceForSignature(signature) {
    const sig = stripBootloaderSignatureSuffix(signature);
    if (tokenLooksLikeCe(sig)) return 'CE-02m-3';
    if (tokenLooksLikeDtv(sig)) return 'RTU-Sensor';
    if (isMpModuleSignatureForFirmwareHint(sig)) return 'MR-02m';
    return '';
  }

  /* Последняя версия прошивки семейства устройства (app|bootloader). '' — нет
     образа для семейства. Неизвестное семейство → глобальный максимум. */
  function latestVersionForDevice(signature, kind) {
    const key = kind === 'bootloader' ? 'bootloader' : 'app';
    const fam = manifestDeviceForSignature(signature);
    const map = state.latestByDevice || {};
    if (fam && Object.prototype.hasOwnProperty.call(map, fam)) {
      return String((map[fam] && map[fam][key]) || '').trim();
    }
    return key === 'bootloader' ? state.latestBootloaderVersion : state.latestStableVersion;
  }

  function validateFirmwareSelectionForDevice(signature, entry) {
    const routeErr = validateFirmwareForDevice(signature, entry && entry.file);
    if (routeErr) return routeErr;
    const devKind = deviceProductKindForFlash(signature);
    const fwKind = firmwareProductKindFromEntry(entry);
    if (!devKind || !fwKind) return '';
    if (devKind === fwKind) return '';
    return 'Выберите корректную прошивку';
  }

  function resolveApplicationLineProfile(signature, device, isWbfwFirmware) {
    const d = device || {};
    const appBaud = Number(d.app_line_baud);
    if (Number.isFinite(appBaud) && appBaud > 0) {
      return {
        baudrate: appBaud,
        parity: String(d.app_line_parity || 'N').toUpperCase(),
        stopbits: Number(d.app_line_stopbits) || 1,
      };
    }
    if (isWbfwFirmware || isWirenboardModuleSignature(signature)) {
      return lineProfileFromScan(d, LINE_PROFILE_WB_APP);
    }
    if (isMpModuleSignatureForFirmwareHint(signature) || !isWbfwFirmware) {
      return Object.assign({}, LINE_PROFILE_MP_MR);
    }
    return lineProfileFromScan(d, LINE_PROFILE_WB_APP);
  }

  function buildFlashTargetFromDevice(dev) {
    const sig = dev && dev.signature;
    const route = resolveDeviceFlashRoute(sig);
    const isWbRoute = route === 'wb';
    const line = resolveApplicationLineProfile(sig, dev, isWbRoute);
    return {
      address: dev.address,
      serial: dev.serial,
      signature: dev.signature,
      in_bootloader: dev.in_bootloader,
      baudrate: line.baudrate,
      parity: line.parity,
      stopbits: line.stopbits,
      app_line_baud: line.baudrate,
      app_line_parity: line.parity,
      app_line_stopbits: line.stopbits,
    };
  }

  function countDevicesWithSameModbusAddress(address, devices) {
    const addr = Number(address);
    if (!Number.isFinite(addr) || addr < 1 || addr > 247) return 0;
    return (devices || []).filter(d => Number(d && d.address) === addr).length;
  }

  function duplicateModbusAddressOnLine(device, devices) {
    if (device && device.duplicate_modbus_address_on_line === true) return true;
    return countDevicesWithSameModbusAddress(device && device.address, devices) > 1;
  }

  function resolveUseFastModbusForFlash(device, devices) {
    const duplicate = duplicateModbusAddressOnLine(device, devices);
    return { useFast: duplicate, duplicate };
  }

  function serialValidForFastModbus(serial) {
    const sn = Number(serial) >>> 0;
    return sn !== 0 && sn !== 0xFFFFFFFF && (sn & 0xFFFF0000) === 0x0E0A0000;
  }

  function isWbfwFirmwareFile(fileName) {
    return String(fileName || '').toLowerCase().endsWith('.wbfw');
  }

  function deviceConfigKindFromSignature(sig) {
    const raw = stripBootloaderSignatureSuffix(sig);
    const n = String(raw || '').trim().toUpperCase().replace(/\s/g, '');
    if (!n || n === 'NONE' || n === '—' || n === '?') return '';
    // Carel — ПЕРЕД подсказками MP/MR: сигнатура установки приходит из ответа на
    // функцию 17, а не из holding 290, и ни одна из них не должна попасть в
    // ветку модуля расширения (иначе строке предложат прошивку МР-02м).
    if (signatureLooksLikeCarel(n)) return 'carel';
    // Лента — тоже до подсказок MP/MR: её сигнатура не проходит батч-прошивочный
    // отбор, и без этой ветки строка не открывалась бы двойным кликом.
    if (signatureLooksLikeLed(n)) return 'led';
    // MTDx62-MB presence sensor: the daemon's scan writes its model into the
    // signature (mtd_config.MTD_MODEL) — also before the MP/MR hints.
    if (n === 'MTDX62-MB') return 'mtd';
    if (n.includes('SENSOR') || n.startsWith('SENS.') || n === 'SENS' || n.startsWith('SENS')) return 'dtv';
    if (n.includes('CE02M3') || n.includes('CE-02M-3') || n.includes('CE-02M3')) return 'ce';
    if (isMpModuleSignatureForFirmwareHint(raw)) return 'mr';
    return '';
  }

  function isDeviceConfigSupported(d) {
    return !!deviceConfigKindFromSignature(d && d.signature);
  }

  function deviceConfigTitle(kind, sig) {
    if (kind === 'dtv') return 'Датчик Sens / DTV-RS-485';
    if (kind === 'ce') return 'Анализатор сети CE-02м-3';
    if (kind === 'carel') return 'Приточная установка Carel';
    if (kind === 'led') return ledWindow().title();
    if (kind === 'mtd') return mtdWindow().title();
    return String(sig || '').trim() || 'Модуль MR/MP-02м';
  }

  /* Синхронизировать с sa02m_flasher.module_profiles._SIGNATURE_HINTS.
     «6AO6AI» / «6AI6AO» / «AO6AI6» — одна плата (MP02_AO6AI6); BL bl_sig_match
     принимает все три. */
  const SIGNATURE_IO_HINTS = {
    '6DO8DI': [6, 8, 0, 0],
    'DO6DI8': [6, 8, 0, 0],
    '16DO': [16, 0, 0, 0],
    '12AO': [0, 0, 12, 0],
    '6DO': [6, 0, 0, 0],
    '14DI': [0, 14, 0, 0],
    '10DICON': [0, 10, 0, 0],
    '10DI': [0, 10, 0, 0], // short-form alias for '10DICON' (mqtt.js inferModuleTypeFromName alias list)
    '6DO5DI2AO': [6, 5, 2, 0],
    '6AO6AI': [0, 0, 6, 6],
    '6AI6AO': [0, 0, 6, 6],
    'AO6AI6': [0, 0, 6, 6],
    '6AI2AO': [0, 0, 2, 6],
    'AI6AO2': [0, 0, 2, 6],
    '12AI': [0, 0, 0, 12],
    '4DO6DI': [4, 6, 0, 0],
    'DO4DI6': [4, 6, 0, 0],
    '4TO6DI': [4, 6, 4, 0],
    'TO4DI6': [4, 6, 4, 0],
    'CE02M3': [0, 0, 0, 0],
  };

  function normalizeModuleSignature(sig) {
    return String(stripBootloaderSignatureSuffix(sig) || '').trim().toUpperCase().replace(/\s/g, '');
  }

  function capsFromSignature(sig) {
    const n = normalizeModuleSignature(sig);
    if (!n) return null;
    // Длинные токены сначала: иначе короткий ключ («6DO») совпадает внутри
    // более длинного («6DO5DI2AO») раньше самого длинного ключа.
    const keys = Object.keys(SIGNATURE_IO_HINTS).sort((a, b) => b.length - a.length);
    for (const key of keys) {
      if (n.includes(key)) return SIGNATURE_IO_HINTS[key].slice();
    }
    return null;
  }

  function relayModePanelFromSignature(sig) {
    const n = normalizeModuleSignature(sig);
    return n.includes('6DO8DI') || n.includes('DO6DI8') || n.includes('4DO6DI') || n.includes('DO4DI6');
  }

  function buildConfigSnapshotStubFromDevice(dev) {
    if (!dev) return null;
    const sig = dev.signature || '';
    const kind = deviceConfigKindFromSignature(sig);
    if (!kind) return null;
    const line = resolveApplicationLineProfile(sig, dev, false);
    const stub = {
      kind,
      snapshot_detail: 'stub',
      info: {
        address: dev.address,
        serial: dev.serial,
        signature: sig,
        app_version: dev.app_version || '',
        bootloader_version: dev.bootloader_version || '',
        line: {
          baudrate: line.baudrate,
          parity: line.parity,
          stopbits: line.stopbits,
        },
      },
      network: {
        address: dev.address,
        baudrate: dev.baudrate || line.baudrate,
        parity: dev.parity || line.parity,
        stopbits: dev.stopbits || line.stopbits,
        fast_modbus: false,
      },
    };
    if (kind === 'carel') {
      // Личность установки берётся из строки скана: живого чтения регистров
      // личности у Carel нет (docs/contracts/carel-ahu.md §1).
      stub.family = carelFamilyFromSignature(sig);
      stub.info.model = carelModelLabel(sig);
      stub.info.carel_variant = String(dev.carel_variant || '');
      stub.info.variant_label = carelVariantLabel(dev.carel_variant);
      stub.network.writable = false;
      stub.carel = {};
    }
    if (kind === 'led') stub.led = {};
    if (kind === 'mtd') {
      // The sensor answers on the line the scan found it on; the house line
      // profile (MR 115200 for an unknown signature) would move the window —
      // configDeviceFromSnapshot follows info.line for this kind.
      stub.info.model = String(sig || '').trim();
      stub.info.line = {
        baudrate: dev.baudrate || line.baudrate,
        parity: dev.parity || 'N',
        stopbits: dev.stopbits || 1,
      };
      stub.network.writable = true;
      stub.mtd = {};
    }
    if (kind === 'mr') {
      const caps = capsFromSignature(sig) || [0, 0, 0, 0];
      stub.mr = {
        module: {
          max_do: caps[0],
          max_di: caps[1],
          max_ao: caps[2],
          max_ai: caps[3],
          relay_mode_panel: relayModePanelFromSignature(sig),
        },
        mcu: {},
      };
    }
    return stub;
  }

  function firmwareAppUpdateHintForDevice(d) {
    if (!isMpModuleSignatureForFirmwareHint(d.signature)) return '';
    const fam = manifestDeviceForSignature(d.signature);
    const latest = latestVersionForDevice(d.signature, 'app');
    if (fam && !latest) {
      // Family recognised but the manifest carries no image for it — never
      // offer another family's version (regression: DTV/СЭ shown MR-02m's).
      return '<div class="flasher-sub flasher-fw-nofw-hint">нет прошивки для этого устройства</div>';
    }
    if (!latest) return '';
    const lv = parseVersionTuple(latest);
    const dv = parseVersionTuple(d.app_version);
    if (!lv || !dv) return '';
    if (compareVersionTuple(lv, dv) <= 0) return '';
    const entry = stableEntryForVersion('app', latest);
    const cached = entry && isFirmwareEntryDownloaded(entry);
    const suffix = cached ? '' : ' (не скачан — «Скачать»)';
    return `<div class="flasher-sub flasher-fw-update-hint">есть ${escapeHtml(latest)}${escapeHtml(suffix)}</div>`;
  }

  function firmwareBlUpdateHintForDevice(d) {
    if (!isMpModuleSignatureForFirmwareHint(d.signature)) return '';
    const latest = latestVersionForDevice(d.signature, 'bootloader');
    if (!latest) return '';
    const lv = parseVersionTuple(latest);
    const dv = parseVersionTuple(d.bootloader_version);
    if (!lv || !dv) return '';
    if (compareVersionTuple(lv, dv) <= 0) return '';
    const entry = stableEntryForVersion('bootloader', latest);
    const cached = entry && isFirmwareEntryDownloaded(entry);
    const suffix = cached ? '' : ' (не скачан — «Скачать»)';
    return `<div class="flasher-sub flasher-fw-update-hint">есть ${escapeHtml(latest)}${escapeHtml(suffix)}</div>`;
  }

  /* ── Таблица устройств ────────────────────────────────────────────────── */

  function deviceAddressNumeric(dev) {
    const raw = dev && dev.address;
    if (raw == null || raw === '—') return Number.MAX_SAFE_INTEGER;
    const n = Number(raw);
    return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
  }

  function shouldSortDevicesTable() {
    return !state.scanJobId && !state.scanPending;
  }

  function sortDevicesByAddress() {
    const selectedKeys = selectedDeviceKeysFromIndices();
    state.devices.sort((a, b) => {
      const aa = deviceAddressNumeric(a);
      const ab = deviceAddressNumeric(b);
      if (aa !== ab) return aa - ab;
      const ba = Number(a.baudrate) || 0;
      const bb = Number(b.baudrate) || 0;
      if (ba !== bb) return ba - bb;
      return deviceSerialNumeric(a) - deviceSerialNumeric(b);
    });
    restoreSelectionByKeys(selectedKeys);
  }

  function renderDevices() {
    if (shouldSortDevicesTable()) sortDevicesByAddress();
    const tbody = $('flasher-devices-table').querySelector('tbody');
    tbody.innerHTML = '';
    if (!state.devices.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="flasher-empty">Устройств не найдено.</td></tr>';
      updateFlashControls();
      return;
    }
    state.devices.forEach((d, idx) => {
      const tr = document.createElement('tr');
      let rowClickTimer = null;
      tr.classList.add('is-selectable');
      if (state.selectedDeviceIndices.has(idx)) tr.classList.add('is-selected');
      if (isDeviceConfigSupported(d)) {
        tr.classList.add('flasher-device-config-row');
      }
      tr.innerHTML = `
        <td>${escapeHtml(String(d.address ?? '—'))}</td>
        <td>${escapeHtml(d.serial_hex || '—')}<div class="flasher-sub">${escapeHtml(d.serial_dec || '')}</div></td>
        <td>${escapeHtml(d.signature || '—')}</td>
        <td>${escapeHtml(d.app_version || '—')}${firmwareAppUpdateHintForDevice(d)}</td>
        <td>${escapeHtml(d.bootloader_version || '—')}${firmwareBlUpdateHintForDevice(d)}</td>
        <td>${escapeHtml(String(d.baudrate || '—'))} ${escapeHtml(String(d.parity || ''))}${escapeHtml(String(d.stopbits || ''))}</td>
      `;
      tbody.appendChild(tr);
      tr.addEventListener('click', () => {
        if (rowClickTimer) clearTimeout(rowClickTimer);
        rowClickTimer = setTimeout(() => {
          rowClickTimer = null;
          if (!state.devices[idx]) return;
          toggleDeviceSelection(idx);
          renderDevices();
          renderFirmware();  // re-filter «Доступные прошивки» to the selected device's family
        }, 220);
      });
      if (isDeviceConfigSupported(d)) {
        tr.addEventListener('dblclick', (ev) => {
          if (rowClickTimer) {
            clearTimeout(rowClickTimer);
            rowClickTimer = null;
          }
          if (ev.target && ev.target.closest && ev.target.closest('input, button, label, select')) return;
          openConfigModal(idx);
        });
      }
    });
    updateFlashControls();
  }

  function updateFlashControls() {
    syncActionButtons();
  }

  /* ── Окно настройки устройства ────────────────────────────────────────── */

  function configModalEl(id) { return $(id); }

  function currentConfigDevice() {
    return state.configDeviceIdx >= 0 ? state.devices[state.configDeviceIdx] || null : null;
  }

  function stopConfigPolling() {
    clearTimeout(state.configFirstPollTimer);
    state.configFirstPollTimer = null;
    if (state.configPollTimer) {
      clearInterval(state.configPollTimer);
      state.configPollTimer = null;
    }
  }

  function configPollTick() {
    if (!state.configOpen) return;
    if (state.configBusy || state.configBackgroundBusy) return;
    const port = currentPort();
    // User reads may pause a configured service. A one-second timer must
    // never stop and restart that service between panel updates.
    if (!port || port.coordinator_available === false || port.restore_pending) return;
    const pauseWholeService = Array.isArray(port.whole_service_scope)
      ? port.whole_service_scope.length > 0
      : !(Array.isArray(port.managed_services) && port.managed_services.length === 1 && port.managed_services[0] === 'MQTT');
    if (!port.reserved && pauseWholeService) return;
    refreshConfigSnapshot(true, 'panel');
  }

  function startConfigPolling() {
    stopConfigPolling();
    if (!state.configOpen) return;
    state.configPollTimer = setInterval(configPollTick, CONFIG_POLL_INTERVAL_MS);
    state.configFirstPollTimer = setTimeout(() => {
      state.configFirstPollTimer = null;
      configPollTick();
    }, 250);
  }

  async function waitConfigBackgroundIdle(timeoutMs) {
    const deadline = Date.now() + (timeoutMs != null ? timeoutMs : CONFIG_BG_POLL_WAIT_MS);
    while (state.configBackgroundBusy && Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 40); });
    }
  }


  function setConfigBanner(text, type) {
    state.configBannerText = text || '';
    state.configBannerType = type || '';
    renderConfigBanner();
    const key = 'flasher-config-banner';
    if (!text || state.portRecoveryError) cancelInlineStatusAutoClear(key);
    else scheduleInlineStatusAutoClear(key, () => setConfigBanner(''));
  }

  function setConfigPollError(text) {
    state.configPollError = text || '';
    renderConfigBanner();
  }

  function renderConfigBanner() {
    const el = configModalEl('flasher-config-banner');
    if (!el) return;
    let text = state.configBannerText || '';
    let type = state.configBannerType || '';
    if (state.configPollError) {
      text = [text, state.configPollError].filter(Boolean).join(' ');
      type = 'error';
    }
    if (state.portRecoveryError) {
      text = text && text !== state.portRecoveryError ? text + ' ' + state.portRecoveryError : state.portRecoveryError;
      type = 'error';
    }
    if (!text) {
      el.hidden = true;
      el.textContent = '';
      el.className = 'flasher-config-banner';
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = 'flasher-config-banner' + (type === 'error' ? ' is-error' : '');
  }

  function setConfigBusy(busy) {
    state.configBusy = !!busy;
    if (state.configSnapshot?.kind === 'mr') patchRelayControls(state.configSnapshot);
  }

  function ensureConfigCloseEnabled() {
    const closeBtn = configModalEl('flasher-config-close-btn');
    if (closeBtn) closeBtn.disabled = !!state.configLinkBusy;
  }

  /** A link change (address/baud) and the bridge relink after it must finish:
      closing the window restores the port lease and restarts the bridge, and
      a page reload would drop the relink. While it runs, Close is disabled and
      F5 / tab close asks first (plan mtd-settings-window F4). */
  function setConfigLinkBusy(busy) {
    state.configLinkBusy = !!busy;
    ensureConfigCloseEnabled();
  }

  function aiSensorEditGuardAdd(channel) {
    const ch = Number(channel);
    if (!Number.isFinite(ch) || ch <= 0) return;
    _aiSensorEditGuard.add(ch);
    const key = String(ch);
    if (_aiSensorEditGuardTimers[key]) {
      clearTimeout(_aiSensorEditGuardTimers[key]);
      delete _aiSensorEditGuardTimers[key];
    }
  }

  function aiSensorEditGuardReleaseLater(channel, ms) {
    const ch = Number(channel);
    if (!Number.isFinite(ch) || ch <= 0) return;
    if (_aiSensorWriteInflight.has(ch)) return;
    const key = String(ch);
    const delay = ms != null ? ms : _AI_SENSOR_EDIT_GUARD_MS;
    if (_aiSensorEditGuardTimers[key]) clearTimeout(_aiSensorEditGuardTimers[key]);
    _aiSensorEditGuardTimers[key] = setTimeout(() => {
      if (_aiSensorWriteInflight.has(ch)) return;
      _aiSensorEditGuard.delete(ch);
      delete _aiSensorEditGuardTimers[key];
    }, delay);
  }

  function mergeAiChannelFromPoll(prevCh, pollCh) {
    const merged = Object.assign({}, prevCh || {}, pollCh || {});
    if (pollCh && (Object.prototype.hasOwnProperty.call(pollCh, 'fault_low') || Object.prototype.hasOwnProperty.call(pollCh, 'fault_high')) && !Object.prototype.hasOwnProperty.call(pollCh, 'fault_known')) merged.fault_known = false;
    if (pollCh?.filters && !pollCh.filters.known) merged.filters = {...pollCh.filters, known: {kalman: false, sps: false, avg: false, tau: false}};
    return merged;
  }

  function clearAiConfigEditState() {
    _aiSensorEditGuard.clear();
    Object.keys(_aiSensorEditGuardTimers).forEach(function (key) {
      clearTimeout(_aiSensorEditGuardTimers[key]);
      delete _aiSensorEditGuardTimers[key];
    });
    _aiSensorWriteInflight.clear();
  }

  async function configApi(path, body, validateQueued) {
    const isDeviceConfig = String(path || '').startsWith('/device_config/') || path === '/bus_mode';
    const run = async () => {
      if (validateQueued && !validateQueued()) throw new Error(uiT('Настройки не прочитаны. Обновите данные перед записью.'));
      const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const timer = ctrl
        ? setTimeout(function () { try { ctrl.abort(); } catch (_) {} }, CONFIG_API_TIMEOUT_MS)
        : null;
      let res;
      try {
        // Known: the abort timer keeps running through the guard's one CSRF
        // retry — a device-config retry outliving CONFIG_API_TIMEOUT_MS aborts.
        res = await fetch(API + path, {
          method: 'POST',
          credentials: 'same-origin',
          headers: typeof withCsrfHeaders === 'function' ? withCsrfHeaders({ 'Content-Type': 'application/json; charset=utf-8' }) : { 'Content-Type': 'application/json; charset=utf-8' },
          body: JSON.stringify(body || {}),
          signal: ctrl ? ctrl.signal : undefined,
        });
        if (!res.ok) {
          let msg = `HTTP ${res.status}`;
          let data = null;
          try { data = await res.json(); if (data && data.error) msg = data.error; } catch (_) {}
          if (path === '/device_config/holding' && res.status === 409 &&
              data?.error_code === 'AI_TYPE_ACCEPTED_UNCONFIRMED' && data.ai_write?.status === 'accepted_unconfirmed') return data;
          const err = new Error(msg);
          if (data && data.error_code) err.code = String(data.error_code);
          if (data && data.fw_version) err.fwVersion = String(data.fw_version);
          throw err;
        }
        // Keep the same request budget through body consumption, including errors.
        return await res.json();
      } finally {
        if (timer) clearTimeout(timer);
      }
    };
    if (!isDeviceConfig) return run();
    const job = _configApiTail.then(run);
    _configApiTail = job.catch(() => {});
    return job;
  }

  function serialHex(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '—';
    return '0x' + (n >>> 0).toString(16).toUpperCase().padStart(8, '0');
  }

  function configDeviceFromSnapshot(snap) {
    const dev = currentConfigDevice();
    if (!dev || !snap || !snap.info || !snap.network) return;
    if (snap.kind === 'mtd') {
      // The MTD's network block is its registers 7..9, which may hold values
      // the sensor applies only after a power cycle: the window keeps talking
      // on the line the daemon actually used (info.address / info.line).
      const ln = snap.info.line || {};
      if (snap.info.address != null) dev.address = snap.info.address;
      if (ln.baudrate) dev.baudrate = ln.baudrate;
      if (ln.parity) dev.parity = ln.parity;
      if (ln.stopbits) dev.stopbits = ln.stopbits;
      dev.app_version = snap.info.app_version || dev.app_version;
      return;
    }
    dev.address = snap.network.address;
    dev.baudrate = snap.network.baudrate;
    dev.parity = snap.network.parity;
    dev.stopbits = snap.network.stopbits;
    dev.signature = snap.info.signature || dev.signature;
    dev.serial = snap.info.serial || dev.serial;
    dev.serial_hex = serialHex(dev.serial);
    dev.serial_dec = String((Number(dev.serial) >>> 0) || '');
    dev.app_version = snap.info.app_version || dev.app_version;
    dev.bootloader_version = snap.info.bootloader_version || dev.bootloader_version;
  }

  function formatFloat(val, digits) {
    const n = Number(val);
    return Number.isFinite(n) ? n.toFixed(digits) : '—';
  }

  function formatCePower(val, unitLow, unitHigh) {
    const n = Number(val);
    if (!Number.isFinite(n)) return '—';
    if (Math.abs(n) >= 1000) return (n / 1000).toFixed(2) + ' ' + unitHigh;
    return n.toFixed(0) + ' ' + unitLow;
  }

  const MODULE_RELAY_MODES = [
    { value: 0, label: '0 - Modbus' },
    { value: 1, label: '1 - DI->DO фиксация' },
    { value: 2, label: '2 - DI->DO тоггл' },
    { value: 3, label: '3 - Вентиляторы x2' },
    { value: 4, label: '4 - Вентиляторы x4' },
    { value: 5, label: '5 - Приводы штор' },
  ];
  const MODULE_RELAY_OPTION_BITS = [
    { bit: 0, label: 'Бит 0 - зеркало DI->DO' },
    { bit: 1, label: 'Бит 1 - планировщик окон' },
    { bit: 2, label: 'Бит 2 - восстановление состояния' },
    { bit: 3, label: 'Бит 3 - шторы по DI' },
    { bit: 4, label: 'Бит 4 - поочередное включение при питании' },
  ];
  const MODULE_TIMER_MODES = [
    { value: 0, label: 'Выкл' },
    { value: 1, label: 'Задержка вкл.' },
    { value: 2, label: 'Задержка выкл.' },
    { value: 3, label: 'Пульс при вкл.' },
    { value: 4, label: 'Пульс при выкл.' },
    { value: 5, label: 'Мигание' },
  ];
  const MODULE_DI_MODES = [
    { value: 0, label: '0 - Счетчик импульсов' },
    { value: 1, label: '1 - Кнопка' },
  ];
  const MODULE_AI_SAMPLE_RATES = [20, 45, 90, 175, 330, 600, 1000];
  const MODULE_AI_UI_BUCKETS = [
    { id: 'off',  label: 'Выключен' },
    { id: 'ntc',  label: 'NTC' },
    { id: 'rtd',  label: 'RTD' },
    { id: 'volt', label: '0–10 В' },
    { id: 'curr', label: '4–20 мА' },
    { id: 'tc_k', label: 'ТХА' },
    { id: 'dry',  label: 'DIN' },
  ];
  const _AI_NTC = new Set([1, 2, 3, 4, 5, 6, 7]);
  const _AI_RTD = new Set([
    8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
  ]);
  const _AI_VOLT = new Set([34, 35, 36, 37]);
  const _AI_CURR = new Set([38, 39, 40]);
  const _AI_TC_K = new Set([41]);
  const _AI_DRY = new Set([42]);

  const _AI_RTD_2WIRE = new Set([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);
  const _AI_RTD_3WIRE = new Set([
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
  ]);

  function aiRtdTwoWireFromCode(code) {
    return !_AI_RTD_3WIRE.has(Number(code) & 0xFFFF);
  }

  function aiUiRtdSubchoicesForWire(twoWire) {
    const allow = twoWire ? _AI_RTD_2WIRE : _AI_RTD_3WIRE;
    return MODULE_AI_SENSOR_CHOICES.filter(([code]) => allow.has(Number(code) & 0xFFFF));
  }

  function aiUiSensorBucket(code) {
    const c = Number(code) & 0xFFFF;
    if (!c) return 'off';
    if (_AI_NTC.has(c)) return 'ntc';
    if (_AI_RTD.has(c)) return 'rtd';
    if (_AI_VOLT.has(c)) return 'volt';
    if (_AI_CURR.has(c)) return 'curr';
    if (_AI_TC_K.has(c)) return 'tc_k';
    if (_AI_DRY.has(c)) return 'dry';
    return 'off';
  }

  function aiUiSubchoicesForBucket(bucket) {
    const b = String(bucket || 'off');
    if (b === 'off') return [[0, 'Выключен']];
    const out = [];
    MODULE_AI_SENSOR_CHOICES.forEach(item => {
      const code = item[0];
      if (!code) return;
      if (aiUiSensorBucket(code) === b) out.push(item);
    });
    return out.length ? out : [[0, 'Выключен']];
  }

  function aiUiCalibrationApplicable(sensorCode) {
    // Смещение калибровки (Holding base+4) применимо к температуре ∪ напряжению
    // ∪ току (вкл. дифф.); скрыто только для «Выключен» и «сухого контакта».
    // Эталон: MR-02m-flasher module_profiles.ai_ui_uses_value_calibration.
    const b = aiUiSensorBucket(sensorCode);
    return b === 'ntc' || b === 'rtd' || b === 'tc_k' || b === 'volt' || b === 'curr';
  }

  // Калибровка активных входов (ток/напряжение) — целое, весь int16;
  // температурные — десятые доли °C, клип ±100. Эталон:
  // ai_calibration_is_integer / ai_calibration_clamp.
  function aiCalibrationIsInteger(sensorCode) {
    const b = aiUiSensorBucket(sensorCode);
    return b === 'volt' || b === 'curr';
  }

  function aiCalibrationClampByCode(sensorCode, value) {
    return aiCalibrationIsInteger(sensorCode)
      ? clampInt(value, -32768, 32767, 0)
      : clampInt(value, -100, 100, 0);
  }

  // Типы датчиков, для которых MR-02m заполняет Input 107/108 (авария диапазона /
  // обрыв / КЗ): ток/напряжение/дифф ∪ NTC/RTD/ТХА. Эталон:
  // ai_sensor_uses_input_range_limit_registers.
  function aiSensorUsesLimitRegisters(sensorCode) {
    const b = aiUiSensorBucket(sensorCode);
    return b === 'volt' || b === 'curr' || b === 'ntc' || b === 'rtd' || b === 'tc_k';
  }

  // Расшифровка аварии AI по битам Input 107 (ниже предела) / 108 (выше).
  // NTC/RTD/ТХА: below→«Обрыв датчика», above(NTC/RTD)→«Короткое замыкание на
  // линии»; активные: «Ниже/Выше диапазона измерения». Эталон:
  // ai_input_limit_range_message. Возвращает массив RU-строк — каждая ключ DICT,
  // перевод берёт наблюдатель i18n (потому части рендерятся отдельными <span>).
  function aiFaultParts(below, above, sensorCode) {
    if (!aiSensorUsesLimitRegisters(sensorCode)) return [];
    const b = aiUiSensorBucket(sensorCode);
    const isWiring = (b === 'ntc' || b === 'rtd' || b === 'tc_k');
    const isResistance = (b === 'ntc' || b === 'rtd');
    const parts = [];
    if (isWiring) {
      if (below) parts.push('Обрыв датчика');
      if (above && isResistance) parts.push('Короткое замыкание на линии');
      return parts;
    }
    if (below) parts.push('Ниже диапазона измерения');
    if (above) parts.push('Выше диапазона измерения');
    return parts;
  }

  function aiFaultChipHtml(parts) {
    if (!parts || !parts.length) return '';
    return parts.map(p => `<span>${escapeHtml(p)}</span>`).join(' / ');
  }

  function aiFaultView(ai) {
    const applicable = aiSensorUsesLimitRegisters(ai.sensor_code);
    const unknown = applicable && ai.fault_known !== true;
    return {parts: unknown ? ['Статус аварий недоступен'] : aiFaultParts(ai.fault_low, ai.fault_high, ai.sensor_code),
      className: 'badge ' + (unknown ? 'badge-warn' : 'badge-err') + ' ai-fault-chip'};
  }

  // Короткий тег режима для пункта сайдбара «Аналоговый вход AI…»: точный тег по
  // Modbus-коду типа, как в эталоне ai_sidebar_nav_mode_tag (module_profiles.py):
  // ВЫКЛ / NTC / RTD / 0-10 / 0-30 / 4-20 / 0-5мА / 0-20мА / ±50мВ / ±2В / DIN / ТХА.
  function aiSidebarTagFromCode(code) {
    const c = Number(code) & 0xFFFF;
    if (c === 0) return 'ВЫКЛ';
    const b = aiUiSensorBucket(c);
    if (b === 'ntc') return 'NTC';
    if (b === 'rtd') return 'RTD';
    if (c === 34) return '0-10';
    if (c === 35) return '0-30';
    if (c === 40) return '4-20';
    if (c === 38) return '0-5мА';
    if (c === 39) return '0-20мА';
    if (c === 36) return '±50мВ';
    if (c === 37) return '±2В';
    if (b === 'dry')  return 'DIN';
    if (b === 'tc_k') return 'ТХА';
    return 'AI';
  }

  // Справочные пределы температуры (десятые °C) по Modbus-коду типа — из таблиц прошивки MR-02m
  const _AI_SENSOR_LIMITS_TENTHS = {
    1: [-500, 1500],
    2: [-550, 1250], 3: [-400, 1200], 7: [-400, 1200],
    4: [-550, 1550], 5: [-550, 1250], 6: [-550, 1250],
    8: [-2000, 3000], 9: [-2000, 3000], 10: [-2000, 3000], 11: [-2000, 3000],
    12: [-2000, 8500], 13: [-2000, 8500], 14: [-2000, 8500],
    15: [-1800, 2000], 16: [-1800, 2000], 17: [-1800, 2000],
    18: [-600, 1800], 19: [-600, 1800], 20: [-600, 1800],
    21: [-2000, 3000], 22: [-2000, 3000], 23: [-2000, 3000], 24: [-2000, 3000],
    25: [-2000, 8500], 26: [-2000, 8500], 27: [-2000, 8500],
    28: [-1800, 2000], 29: [-1800, 2000], 30: [-1800, 2000],
    31: [-600, 1800], 32: [-600, 1800], 33: [-600, 1800],
    41: [-2700, 13690],
  };

  function aiSensorRefLimits(code) {
    const c = Number(code) & 0xFFFF;
    const lim = _AI_SENSOR_LIMITS_TENTHS[c];
    if (!lim) return null;
    return { lo: (lim[0] / 10).toFixed(1) + ' °C', hi: (lim[1] / 10).toFixed(1) + ' °C' };
  }

  // Дифф-режимы 36 (±50 мВ) и 37 (±2 В) задают шкалу пределами измерения —
  // числовая подсказка соответствия raw = физ. значение. Числовая, без i18n.
  function aiActiveLimitHint(code) {
    const c = Number(code) & 0xFFFF;
    if (c === 36) return '-100 = ' + (-100 / 10).toFixed(1);   // ±50 мВ, шкала 10, .1f
    if (c === 37) return '100 = ' + (100 / 100).toFixed(2);    // ±2 В, шкала 100, .2f
    return '';
  }

  // Содержимое блока пределов #cfg-mr-ai-limits-*. Для активных аналоговых
  // режимов (volt/curr) — два редактируемых поля (Holding base+5/base+6, int16),
  // засеянных из снапшота; для температурных — справочный (read-only) блок.
  function aiLimitsBoxHtml(channel, code, ai) {
    const bucket = aiUiSensorBucket(code);
    if (bucket === 'volt' || bucket === 'curr') {
      const lo = ai && ai.limit_low != null ? ai.limit_low : 0;
      const hi = ai && ai.limit_high != null ? ai.limit_high : 0;
      const hint = aiActiveLimitHint(code);
      const hintHtml = hint
        ? `<div class="flasher-config-note" id="cfg-mr-ai-limit-hint-${channel}">${escapeHtml(hint)}</div>`
        : '';
      return `
      <div class="ai-limits-row">
        <span>Нижний предел:</span>
        <input id="cfg-mr-ai-limit-lo-${channel}" type="number" min="-32768" max="32767"
          value="${escapeHtml(String(lo))}" data-mr-ai-limit="${channel}" data-mr-ai-limit-which="lo" class="ai-cal-input-sm" />
      </div>
      <div class="ai-limits-row">
        <span>Верхний предел:</span>
        <input id="cfg-mr-ai-limit-hi-${channel}" type="number" min="-32768" max="32767"
          value="${escapeHtml(String(hi))}" data-mr-ai-limit="${channel}" data-mr-ai-limit-which="hi" class="ai-cal-input-sm" />
      </div>${hintHtml}`;
    }
    const lim = aiSensorRefLimits(code);
    return lim ? `
      <div class="ai-limits-row"><span>Нижний предел:</span><strong>${escapeHtml(lim.lo)}</strong></div>
      <div class="ai-limits-row"><span>Верхний предел:</span><strong>${escapeHtml(lim.hi)}</strong></div>` : '';
  }

  function _bindAiLimitInputs(container) {
    if (!container) return;
    container.querySelectorAll('[data-mr-ai-limit]').forEach(el => {
      el.addEventListener('blur', () => {
        applyAiLimit(parseInt(el.dataset.mrAiLimit, 10), el.dataset.mrAiLimitWhich);
      });
    });
  }

  function aiUiQuantityLabels(bucket) {
    const b = String(bucket || 'off');
    if (b === 'off')  return ['—', '—'];
    if (b === 'ntc' || b === 'rtd') return ['Сопротивление', 'Ом'];
    if (b === 'volt') return ['Напряжение', 'В'];
    if (b === 'curr') return ['Ток', 'мА'];
    if (b === 'dry')  return ['Логический вход', '—'];
    if (b === 'tc_k') return ['Термопара', 'мВ'];
    return ['—', '—'];
  }

  const MODULE_AI_SENSOR_CHOICES = [
    [0, 'Выключен'],
    [1, 'NTC 1.8k (B3380)'],
    [2, 'NTC 5k (B3470)'],
    [3, 'NTC 10k (B3950)'],
    [4, 'NTC 10k (B3988)'],
    [5, 'NTC 10k (B3435)'],
    [6, 'NTC 10k (B3470)'],
    [7, 'NTC 100k (B3950)'],
    [8, 'Pt50 (α385), 2-пров.'],
    [9, 'Pt100 (α385), 2-пров.'],
    [10, 'Pt500 (α385), 2-пров.'],
    [11, 'Pt1000 (α385), 2-пров.'],
    [12, 'Pt50 (α391), 50П'],
    [13, 'Pt100 (α391), 100П'],
    [14, 'Pt1000 (α391), 1000П'],
    [15, 'Pt50 (α428), 50М'],
    [16, 'Pt100 (α428), 100М'],
    [17, 'Pt1000 (α428), 1000М'],
    [18, 'Ni100 (α617)'],
    [19, 'Ni500 (α617)'],
    [20, 'Ni1000 (α617)'],
    [21, 'Pt50 (α385), 3-пров.'],
    [22, 'Pt100 (α385), 3-пров.'],
    [23, 'Pt500 (α385), 3-пров.'],
    [24, 'Pt1000 (α385), 3-пров.'],
    [25, 'Pt50 (α391), 50П, 3-пров.'],
    [26, 'Pt100 (α391), 100П, 3-пров.'],
    [27, 'Pt1000 (α391), 1000П, 3-пров.'],
    [28, 'Pt50 (α428), 50М, 3-пров.'],
    [29, 'Pt100 (α428), 100М, 3-пров.'],
    [30, 'Pt1000 (α428), 1000М, 3-пров.'],
    [31, 'Ni100 (α617), 3-пров.'],
    [32, 'Ni500 (α617), 3-пров.'],
    [33, 'Ni1000 (α617), 3-пров.'],
    [34, 'Напряжение 0–10 В'],
    [35, 'Напряжение 0–30 В'],
    [36, 'Дифф. напряжение ±50 мВ'],
    [37, 'Дифф. напряжение ±2 В'],
    [38, 'Ток 0–5 мА'],
    [39, 'Ток 0–20 мА'],
    [40, 'Ток 4–20 мА'],
    [41, 'Термопара K (ТХА)'],
    [42, 'Сухой контакт'],
  ];

  function clampInt(value, min, max, fallback) {
    const parsed = parseInt(value, 10);
    const num = Number.isFinite(parsed) ? parsed : fallback;
    return Math.max(min, Math.min(max, num));
  }

  function signedToUint16(value) {
    const n = clampInt(value, -32768, 32767, 0);
    return ((n % 0x10000) + 0x10000) % 0x10000;
  }

  // ── AI: engineering kind ──────────────────────────────────────────────────

  const _AI_ENG_S32_MAX = 2147483647;

  function aiSensorEngineeringKind(code) {
    const c = Number(code) & 0xFFFF;
    if (!c) return 'off';
    if (c === 34 || c === 35) return 'voltage_010';
    if (c === 38 || c === 39 || c === 40) return 'current';
    if (c === 42) return 'logic';
    if (c === 36) return 'diff_mv';
    if (c === 37) return 'diff_v';
    if (_AI_NTC.has(c) || _AI_RTD.has(c) || c === 41) return 'temp';
    return 'unknown';
  }

  function aiSensorRawPhysicalKind(code) {
    const c = Number(code) & 0xFFFF;
    if (!c) return 'off';
    if (c === 34 || c === 35 || c === 36 || c === 37 || c === 41) return 'microvolt';
    if (c === 38 || c === 39 || c === 40) return 'nanoamp';
    if (c === 42) return 'centiohm';
    if (_AI_NTC.has(c) || _AI_RTD.has(c)) return 'centiohm';
    return 'unknown';
  }

  function aiFormatScaledDisplay(sensorCode, scaledInt) {
    const k = aiSensorEngineeringKind(sensorCode);
    if (k === 'off') return '—';
    if (scaledInt == null) return '—';
    const v = Number(scaledInt);
    if (!Number.isFinite(v) || v === _AI_ENG_S32_MAX) return '—';
    if (k === 'temp')        return (v / 10).toFixed(1) + ' °C';
    // S16_MIN (-32768) — прошивочный сентинел «значение недействительно» для
    // 16-битного пересчитанного (reg+3). Эталон format_ai_scaled_display.
    if (v === -32768) return '—';
    // Ток (0-5/0-20/4-20 мА) и напряжение (0-10/0-30 В): пересчитанное — сырое
    // целое из регистра, без единицы и без масштаба. Эталон: str(v).
    if (k === 'current')     return String(v);
    if (k === 'voltage_010') return String(v);
    if (k === 'diff_mv')     return (v / 10).toFixed(1) + ' мВ';
    if (k === 'diff_v')      return (v / 100).toFixed(2) + ' В';
    return String(v);
  }

  function aiFormatMeasuredDisplay(sensorCode, rawS32) {
    const rk = aiSensorRawPhysicalKind(sensorCode);
    if (rk === 'off') return '—';
    if (rawS32 == null) return '—';
    const v = Number(rawS32);
    if (!Number.isFinite(v)) return '—';
    if (rk === 'centiohm') {
      if (v === _AI_ENG_S32_MAX || v < 0) return '—';
      const ohms = v / 100;
      const parts = [String(v), ohms.toFixed(2) + '\u00a0Ом'];
      if (ohms >= 1e6) parts.push((ohms / 1e6).toFixed(3) + '\u00a0МОм');
      else if (ohms >= 1000) parts.push((ohms / 1000).toFixed(2) + '\u00a0кОм');
      return parts.join(' → ');
    }
    if (rk === 'nanoamp') {
      if (v === _AI_ENG_S32_MAX || v < 0) return '—';
      const ua = v / 1000;
      const parts = [String(v), ua.toFixed(2) + '\u00a0мкА'];
      if (ua >= 1e6) parts.push((ua / 1e6).toFixed(3) + '\u00a0А');
      else if (ua >= 1000) parts.push((ua / 1000).toFixed(2) + '\u00a0мА');
      return parts.join(' → ');
    }
    if (rk === 'microvolt') {
      if (v === _AI_ENG_S32_MAX) return '—';
      const mv = v / 1000;
      const parts = [String(v), mv.toFixed(1) + '\u00a0мВ'];
      if (Math.abs(v) >= 1e6) parts.push((v / 1e6).toFixed(2) + '\u00a0В');
      return parts.join(' → ');
    }
    return String(v);
  }

  function moduleMeta(snap) {
    return snap && snap.mr && snap.mr.module ? snap.mr.module : null;
  }

  function moduleAiChannel(snap, channel) {
    const items = (((snap || {}).mr || {}).ai || {}).channels || [];
    return items.find(item => Number(item.channel) === Number(channel)) || null;
  }

  // ---- Схемы подключения (порт эталонных SVG из каталога «Подключения») -------
  // (тип модуля × вкладка × канал × код датчика) → файл(ы) схемы. MONO — одна
  // схема; PAIR — две колонки (DI/DO). Точное соответствие эталону
  // module_config_window.py (_embed_podklyucheniya_mono_svg /
  // _embed_podklyucheniya_pair_svgs). Типы без схемы в эталоне (10DI, 6DO5DI2AO,
  // 4TO6DI, CE) диаграммы не имеют — как и в эталоне. SVG-исходники — Inkscape с
  // чёрными штрихами, поэтому рендерятся на светлой «подложке» (--wiring-plate),
  // читаемой в обеих темах; эталон перекрашивает штрих в цвет темы (моно) —
  // светлая подложка сохраняет и информативную синюю подсветку активной клеммы.
  const WIRING_ASSET_BASE = 'static/wiring/';

  function wiringAssetVersion() {
    return (typeof APP_VERSION !== 'undefined' && APP_VERSION) ? String(APP_VERSION) : '';
  }

  function moduleWiringKey(meta) {
    if (!meta) return null;
    const d = Number(meta.max_do || 0), i = Number(meta.max_di || 0);
    const o = Number(meta.max_ao || 0), a = Number(meta.max_ai || 0);
    if (o === 6 && a === 6) return '6ai6ao';
    if (a === 12 && o === 0) return '12ai';
    if (o === 12 && a === 0 && d === 0 && i === 0) return '12ao';
    if (d === 6 && i === 8) return '6do8di';
    if (d === 4 && i === 6 && o === 0) return '4do6di';
    if (i === 14 && d === 0) return '14di';
    if (d === 16 && i === 0) return '16do';
    if (d === 6 && i === 0 && o === 0) return '6do';
    return null;
  }

  // Вариант AI-схемы по коду датчика (эталон _refresh_ai66_wiring /
  // _refresh_ai12_wiring): ТХА → «2», 3-проводное RTD → «3», иначе базовый.
  function aiWiringVariantSuffix(code) {
    const b = aiUiSensorBucket(code);
    if (b === 'tc_k') return '2';
    if (b === 'rtd' && !aiRtdTwoWireFromCode(code)) return '3';
    return '';
  }

  // Файлы схемы для (тип, вкладка, канал, код датчика): [] — нет схемы,
  // 1 элемент — mono, 2 — пара колонок.
  function moduleWiringFiles(mkey, tab, channel, sensorCode) {
    if (!mkey) return [];
    const ch = Number(channel) || 0;
    if (tab === 'ai') {
      if (aiUiSensorBucket(sensorCode) === 'off') return [];  // «Выключен» — схема скрыта (эталон)
      if (mkey === '6ai6ao') return ['mp-02m_6ai6ao_up' + aiWiringVariantSuffix(sensorCode) + '.svg'];
      if (mkey === '12ai')   return ['mp-02m_12ai_' + (ch <= 6 ? 'down' : 'up') + aiWiringVariantSuffix(sensorCode) + '.svg'];
      return [];
    }
    if (tab === 'ao') {
      if (mkey === '6ai6ao') return ['mp-02m_6ai6ao_down.svg'];
      if (mkey === '12ao')   return ['mp-02m_12ao_' + (ch <= 6 ? 'down' : 'up') + '.svg'];
      return [];
    }
    if (tab === 'di') {
      if (mkey === '6do8di') return ['mp-02m_6do8di_down.svg', 'mp-02m_6do8di_down2.svg'];
      if (mkey === '4do6di') return ['mp-02m_4do6di_down.svg', 'mp-02m_4do6di_down2.svg'];
      if (mkey === '14di')   return ch <= 7
        ? ['mp-02m_14di_down.svg', 'mp-02m_14di_down2.svg']
        : ['mp-02m_14di_up.svg', 'mp-02m_14di_up2.svg'];
      return [];
    }
    if (tab === 'do') {
      if (mkey === '6do8di') return ['mp-02m_6do8di_up.svg', 'mp-02m_6do8di_up2.svg'];
      if (mkey === '4do6di') return ['mp-02m_4do6di_up.svg', 'mp-02m_4do6di_up2.svg'];
      if (mkey === '6do')    return ['mp-02m_6do_' + (ch <= 3 ? 'down' : 'up') + '.svg'];
      if (mkey === '16do')   return ['mp-02m_16do_' + (ch <= 8 ? 'down' : 'up') + '.svg'];
      return [];
    }
    return [];
  }

  function moduleWiringDiagramHtml(snap, tab, channel, sensorCode) {
    const files = moduleWiringFiles(moduleWiringKey(moduleMeta(snap)), tab, channel, sensorCode);
    if (!files.length) return '';
    const ver = wiringAssetVersion();
    const q = ver ? ('?v=' + encodeURIComponent(ver)) : '';
    const cols = files.map(f =>
      `<div class="wiring-diagram-col"><img class="wiring-diagram-img" alt="Схема подключения" loading="lazy" src="${escapeHtml(WIRING_ASSET_BASE + f + q)}" /></div>`
    ).join('');
    return `<section class="flasher-config-card wiring-diagram-card">
        <h4>СХЕМА ПОДКЛЮЧЕНИЯ</h4>
        <div class="wiring-diagram-plate${files.length === 2 ? ' wiring-diagram-pair' : ''}">${cols}</div>
      </section>`;
  }

  function mergeMrMinimalIntoFull(prevMr, minMr) {
    if (!prevMr || !minMr) return minMr || prevMr;
    const out = JSON.parse(JSON.stringify(prevMr));
    out.ai_apply = minMr.ai_apply;
    out.module = Object.assign({}, out.module || {}, minMr.module || {});
    if (Object.prototype.hasOwnProperty.call(minMr, 'inactivity_s')) {
      out.inactivity_s = minMr.inactivity_s;
      out.inactivity_known = minMr.inactivity_known === true;
    } else if (Object.prototype.hasOwnProperty.call(minMr, 'inactivity_known')) out.inactivity_known = minMr.inactivity_known === true;
    out.relay = Object.assign({}, out.relay || {}, minMr.relay || {});
    for (const field of ['mode', 'options', 'power_stagger']) {
      if (Object.prototype.hasOwnProperty.call(minMr.relay || {}, field) && !Object.prototype.hasOwnProperty.call(minMr.relay, field + '_known')) out.relay[field + '_known'] = false;
    }
    for (const family of ['do', 'di', 'ao']) {
      if (!minMr[family]) continue;
      out[family] = out[family] || {};
      for (const [key, values] of Object.entries(minMr[family])) out[family][key] = Array.isArray(values) ? values.slice() : [];
    }
    if (minMr.ai && Array.isArray(minMr.ai.channels)) {
      const deepAi =
        minMr.ai.channels.some(
          c => c && (Object.prototype.hasOwnProperty.call(c, 'measured_raw') || Object.prototype.hasOwnProperty.call(c, 'filters')),
        );
      if (deepAi) {
        const maxAi = Number((out.module || {}).max_ai || 0);
        const byCh = {};
        (out.ai && out.ai.channels ? out.ai.channels : []).forEach(c => {
          byCh[Number(c.channel)] = Object.assign({}, c);
        });
        minMr.ai.channels.forEach(mc => {
          const ch = Number(mc.channel);
          byCh[ch] = mergeAiChannelFromPoll(byCh[ch], mc);
        });
        const list = [];
        for (let ch = 1; ch <= maxAi; ch++) {
          if (byCh[ch]) list.push(byCh[ch]);
        }
        out.ai = { channels: list };
      } else {
        const maxAi = Number((out.module || {}).max_ai || 0);
        const byCh = {};
        (out.ai && out.ai.channels ? out.ai.channels : []).forEach(c => {
          byCh[Number(c.channel)] = Object.assign({}, c);
        });
        minMr.ai.channels.forEach(mc => {
          const ch = Number(mc.channel);
          const base = byCh[ch] || {};
          byCh[ch] = mergeAiChannelFromPoll(base, mc);
        });
        const list = [];
        for (let ch = 1; ch <= maxAi; ch++) {
          if (byCh[ch]) list.push(byCh[ch]);
        }
        out.ai = out.ai || {};
        out.ai.channels = list;
      }
    }
    if (minMr.mcu && typeof minMr.mcu === 'object' && Object.keys(minMr.mcu).length) {
      out.mcu = Object.assign({}, out.mcu || {}, minMr.mcu);
    }
    return out;
  }

  function mergeDeviceConfigSnapshot(prev, snap) {
    if (
      !snap ||
      (snap.snapshot_detail !== 'minimal' && snap.snapshot_detail !== 'panel') ||
      !prev ||
      prev.kind !== snap.kind
    )
      return snap;
    if (snap.kind !== 'mr') return snap;
    const merged = Object.assign({}, snap);
    merged.mr = mergeMrMinimalIntoFull(prev.mr, snap.mr);
    merged.snapshot_detail = 'full';
    if (state.configTab === 'network' && state.configNetworkDirty) {
      const pn = prev.network || {};
      merged.network = Object.assign({}, pn);
      merged.info = Object.assign({}, merged.info || {}, snap.info || {});
      merged.info.address = pn.address;
      merged.info.line = {
        baudrate: pn.baudrate,
        parity: pn.parity,
        stopbits: pn.stopbits,
      };
    }
    return merged;
  }

  /**
   * Не перерисовывать тело модалки, если пользователь активно редактирует
   * любое поле ввода в теле конфиг-окна (input/select/textarea).
   * Предотвращает сброс введённых значений при фоновом опросе каждые 4 с.
   */
  function shouldSkipConfigBodyRerender() {
    if (_aiSensorEditGuard.size > 0 || _aiSensorWriteInflight.size > 0) return true;
    if (!state.configOpen || !state.configSnapshot) return false;
    if (state.configTab === 'relay' && configModalEl('flasher-config-body')?.querySelector('[data-relay-dirty]')) return true;
    if (state.configSnapshot.kind === 'mr' && configModalEl('flasher-config-body')?.querySelector('[data-optional-dirty]')) return true;
    const el = document.activeElement;
    if (!el) return false;
    const tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') return false;
    const body = configModalEl('flasher-config-body');
    return body ? body.contains(el) : false;
  }

  function patchConfigLiveReadouts(snap) {
    if (snap && snap.kind === 'carel') return patchCarelLiveReadouts(snap);
    if (snap && snap.kind === 'led') return ledWindow().patch(snap);
    if (snap && snap.kind === 'mtd') return mtdWindow().patch(snap);
    if (!snap || snap.kind !== 'mr') return;
    const mr = snap.mr || {};
    patchRelayControls(snap);
    const mcu = mr.mcu || {};
    const setText = function (id, text) {
      const el = configModalEl(id);
      if (el) el.textContent = text;
    };
    if (mcu.power_v != null) setText('cfg-mr-mcu-power', Number(mcu.power_v).toFixed(2) + ' В');
    if (mcu.temp_c != null) setText('cfg-mr-mcu-temp', Number(mcu.temp_c).toFixed(1) + ' °C');
    if (mcu.uptime_str != null) setText('cfg-mr-mcu-uptime', String(mcu.uptime_str));

    const ao = mr.ao || {};
    (ao.current_raw || []).forEach(function (v, idx) {
      const ch = idx + 1;
      setText('cfg-mr-ao-live-' + ch, moduleAoDisplay(snap, v));
      if (ao.current_raw) setText('cfg-mr-ao-raw-' + ch, String(ao.current_raw[idx] ?? 0));
    });

    const bits = ((mr.do || {}).bits || []);
    const doCounts = (mr.do || {}).counts || [];
    for (let ch = 1; ch <= Number((mr.module || {}).max_do || 0); ch++) {
      setText('cfg-mr-do-count-' + ch, String(doCounts[ch - 1] ?? '—'));
    }
    bits.forEach(function (on, idx) {
      const ch = idx + 1;
      const el = configModalEl('cfg-mr-do-state-' + ch);
      if (!el) return;
      el.textContent = on ? 'Вкл' : 'Выкл';
      el.classList.toggle('do-state-on', !!on);
    });

    const diVals = ((mr.di || {}).values || []);
    const di = mr.di || {};
    for (let ch = 1; ch <= Number((mr.module || {}).max_di || 0); ch++) {
      const idx = ch - 1;
      setText('cfg-mr-di-count-' + ch, String((di.counts || [])[idx] ?? '—'));
      setText('cfg-mr-di-press-' + ch, ['short_counts', 'long_counts', 'double_counts'].map(key => String((di[key] || [])[idx] ?? '—')).join(' / '));
      setText('cfg-mr-di-freq-' + ch, String((di.freq || [])[idx] ?? '—') + ' ' + uiT('Гц'));
    }
    diVals.forEach(function (v, idx) {
      const ch = idx + 1;
      const el = configModalEl('cfg-mr-di-state-' + ch);
      if (!el) return;
      const on = Number(v) !== 0;
      el.textContent = on ? 'Активен' : 'Неактивен';
      el.classList.toggle('di-state-on', on);
    });

    if (mr.ai && Array.isArray(mr.ai.channels)) {
      mr.ai.channels.forEach(function (ch) {
        const n = Number(ch.channel);
        if (!n) return;
        const measuredEl = configModalEl('cfg-mr-ai-measured-' + n);
        if (measuredEl) measuredEl.textContent = aiFormatMeasuredDisplay(ch.sensor_code, ch.measured_raw);
        const scaledEl = configModalEl('cfg-mr-ai-scaled-' + n);
        if (scaledEl) scaledEl.textContent = aiFormatScaledDisplay(ch.sensor_code, ch.scaled_raw);
        // Авария диапазона/обрыв/КЗ (Input 107/108) — обновляем в реальном времени,
        // как measured/scaled, даже когда полный ре-рендер пропущен (фокус/edit-guard).
        const faultEl = configModalEl('cfg-mr-ai-fault-' + n);
        if (faultEl) {
          const view = aiFaultView(ch);
          faultEl.innerHTML = aiFaultChipHtml(view.parts);
          faultEl.className = view.className;
          faultEl.hidden = !view.parts.length;
        }
      });
    }
  }

  function aoSafeHoldingRegForChannel(snap, channel) {
    const regs = ((((snap || {}).mr || {}).ao || {}).safe_holding_regs || []);
    const idx = channel - 1;
    if (idx >= 0 && idx < regs.length) return clampInt(regs[idx], 0, 65535, 503 + idx);
    return 503 + channel - 1;
  }

  function moduleDoTabInfo(snap, channel) {
    const bits = ((((snap || {}).mr || {}).do || {}).bits || []);
    if (bits[channel - 1] == null) return { suffix: ' - —', live: false };
    const on = !!bits[channel - 1];
    return { suffix: ` - ${on ? 'ВКЛ' : 'ВЫКЛ'}`, live: on };
  }

  function moduleDiTabInfo(snap, channel) {
    const values = ((((snap || {}).mr || {}).di || {}).values || []);
    if (values[channel - 1] == null) return { suffix: ' - —', live: false };
    const on = Number(values[channel - 1] || 0) !== 0;
    return { suffix: ` - ${on ? 'ВКЛ' : 'ВЫКЛ'}`, live: on };
  }

  function moduleAoTabInfo(snap, channel) {
    const raw = ((((snap || {}).mr || {}).ao || {}).current_raw || [])[channel - 1];
    return {
      suffix: ' - ' + moduleAoDisplay(snap, raw),
      live: raw != null && Number(raw) > 0,
    };
  }

  function moduleAoIsDimmer(snap) {
    const mod = ((snap || {}).mr || {}).module || {};
    if (mod.code != null && Number(mod.code) !== 0) return Number(mod.code) === 15;
    return /4TO6DI|TO4DI6/.test(normalizeModuleSignature(((snap || {}).info || {}).signature));
  }

  function moduleAoDisplay(snap, raw) {
    if (raw == null || raw === '' || !Number.isFinite(Number(raw))) return '—';
    return moduleAoIsDimmer(snap) ? (Number(raw) / 10).toFixed(1) + ' %' : (Number(raw) / 100).toFixed(2) + ' В';
  }

  function moduleAiTabInfo(snap, channel) {
    const ai = moduleAiChannel(snap, channel);
    const sensorCode = Number(ai && ai.sensor_code || 0);
    const tag = (ai && ai.sidebar_tag) ? String(ai.sidebar_tag) : aiSidebarTagFromCode(sensorCode);
    // Зелёный фон вкладки — только для сухого контакта (DIN) в состоянии ВКЛ
    const isDin = aiUiSensorBucket(sensorCode) === 'dry';
    const live = isDin && Number(ai && ai.scaled_raw != null ? ai.scaled_raw : 0) !== 0;
    return { suffix: tag ? ` - ${tag}` : '', live };
  }

  function configTabsForSnapshot(snap) {
    if (!snap) return [];
    if (snap.kind === 'led') return ledWindow().tabs(snap);
    if (snap.kind === 'mtd') return mtdWindow().tabs(snap);
    if (snap.kind === 'carel') {
      const alarms = ((snap.carel || {}).alarms || []).length;
      return [
        { id: 'info', label: 'Сведения' },
        { id: 'network', label: 'Сеть' },
        { id: 'plant', label: 'Установка' },
        // id вкладки входов/выходов = CAREL_IO_TAB: демон читает дорогой блок
        // io_* только когда окно прислало именно это имя в active_tab.
        { id: CAREL_IO_TAB, label: 'Входы/выходы' },
        { id: 'alarms', label: 'Тревоги', suffix: alarms ? ' - ' + alarms : '' },
      ];
    }
    if (snap.kind === 'dtv') return [
      { id: 'info', label: 'Сведения' },
      { id: 'measures', label: 'Измерения' },
      { id: 'settings', label: 'Настройки' },
      { id: 'network', label: 'Сеть' },
    ];
    if (snap.kind === 'ce') return [
      { id: 'info', label: 'Сведения' },
      { id: 'measures', label: 'Измерения' },
      { id: 'settings', label: 'ТТ и фазы' },
      { id: 'network', label: 'Сеть' },
    ];
    if (snap.kind === 'mr') {
      const meta = moduleMeta(snap) || { max_do: 0, max_di: 0, max_ao: 0, max_ai: 0, relay_mode_panel: false };
      const tabs = [
        { id: 'info', label: 'Сведения' },
        { id: 'network', label: 'Сеть' },
      ];
      if (meta.relay_mode_panel) tabs.push({ id: 'relay', label: 'Реле и задержки' });
      for (let i = 1; i <= Number(meta.max_di || 0); i++) {
        tabs.push({ id: 'di_' + i, label: 'Дискретный вход DI' + i, ...moduleDiTabInfo(snap, i) });
      }
      for (let i = 1; i <= Number(meta.max_do || 0); i++) {
        tabs.push({ id: 'do_' + i, label: 'Дискретный выход DO' + i, ...moduleDoTabInfo(snap, i) });
      }
      for (let i = 1; i <= Number(meta.max_ao || 0); i++) {
        tabs.push({ id: 'ao_' + i, label: 'Аналоговый выход АО' + i, ...moduleAoTabInfo(snap, i) });
      }
      for (let i = 1; i <= Number(meta.max_ai || 0); i++) {
        tabs.push({ id: 'ai_' + i, label: 'Аналоговый вход AI' + i, ...moduleAiTabInfo(snap, i) });
      }
      return tabs;
    }
    return [
      { id: 'info', label: 'Сведения' },
      { id: 'network', label: 'Сеть' },
    ];
  }

  function renderModuleInfoTab(snap) {
    const info = snap.info || {};
    const mr = snap.mr || {};
    const mcu = mr.mcu || {};
    const serialNum = info.serial != null ? (info.serial >>> 0) : null;
    const serialHexStr = serialNum != null ? ('0x' + serialNum.toString(16).toUpperCase().padStart(8, '0')) : '—';
    const serialDecStr = serialNum != null ? String(serialNum >>> 0) : '—';

    function fmtRam(bytes) {
      if (bytes == null) return '—';
      const b = Number(bytes);
      if (!Number.isFinite(b)) return '—';
      return b < 1024 ? b + ' байт' : (b / 1024).toFixed(1) + ' КБ';
    }
    function fmtVal(v, fallback) { return v != null ? String(v) : (fallback || '—'); }

    const powerStr  = mcu.power_v  != null ? mcu.power_v.toFixed(2) + ' В' : '—';
    const tempStr   = mcu.temp_c   != null ? mcu.temp_c.toFixed(1) + ' °C' : '—';
    const uptimeStr = mcu.uptime_str != null ? mcu.uptime_str : '—';

    return `
      <div class="cfg-info-section">

        <!-- ── 3 плитки МК ── -->
        <div class="cfg-info-tiles">
          <div class="cfg-info-tile">
            <div class="cfg-info-tile-title">Питание МК</div>
            <div class="cfg-info-tile-val" id="cfg-mr-mcu-power">${escapeHtml(powerStr)}</div>
          </div>
          <div class="cfg-info-tile">
            <div class="cfg-info-tile-title">Температура МК</div>
            <div class="cfg-info-tile-val" id="cfg-mr-mcu-temp">${escapeHtml(tempStr)}</div>
          </div>
          <div class="cfg-info-tile">
            <div class="cfg-info-tile-title">Время с загрузки МК</div>
            <div class="cfg-info-tile-val" id="cfg-mr-mcu-uptime" style="font-size:1.05rem">${escapeHtml(uptimeStr)}</div>
          </div>
        </div>

        <!-- ── Устройство ── -->
        <section class="flasher-config-card">
          <h4>Устройство</h4>
          <dl class="flasher-config-kv">
            <div><dt>Тип</dt><dd>Модуль расширения МР-02м</dd></div>
            <div><dt>Модель</dt><dd>${escapeHtml(info.signature || '—')}</dd></div>
            <div><dt>Серийный № (hex)</dt><dd class="mono">${escapeHtml(serialHexStr)}</dd></div>
            <div><dt>Серийный № (дек)</dt><dd class="mono">${escapeHtml(serialDecStr)}</dd></div>
            <div><dt>Версия ПО</dt><dd>${escapeHtml(info.app_version || '—')}</dd></div>
            <div><dt>Загрузчик</dt><dd>${escapeHtml(info.bootloader_version || '—')}</dd></div>
            <div><dt>Сигнатура</dt><dd>${escapeHtml(info.signature || '—')}</dd></div>
            <div><dt>Наработка в днях</dt><dd>${escapeHtml(fmtVal(mcu.op_days_str))}</dd></div>
            <div><dt>Свободная ОЗУ</dt><dd>${escapeHtml(fmtRam(mcu.ram_free))}</dd></div>
            <div><dt>Используемая ОЗУ</dt><dd>${escapeHtml(fmtRam(mcu.ram_used))}</dd></div>
            <div><dt>Счётчик обновлений</dt><dd>${escapeHtml(fmtVal(mcu.fw_updates))}</dd></div>
            <div><dt>Причина перезагрузки</dt><dd>${escapeHtml(fmtVal(mcu.reset_reason))}</dd></div>
          </dl>
        </section>

        <!-- ── Конфигурация файлом ── -->
        <section class="flasher-config-card">
          <h4>Конфигурация файлом</h4>
          <p class="flasher-config-note" style="margin-bottom:12px">Экспорт / импорт настроек модуля в JSON; перезагрузка устройства.</p>
          <div class="cfg-info-config-btns">
            <button class="btn btn-sm btn-warn" type="button" id="cfg-mr-reboot-btn">Перезагрузить</button>
            <label class="btn btn-sm" style="cursor:pointer" title="Загрузить конфигурацию из JSON-файла">
              Загрузить
              <input type="file" id="cfg-mr-import-file" accept=".json" hidden />
            </label>
            <button class="btn btn-sm" type="button" id="cfg-mr-export-btn">Сохранить</button>
          </div>
          <div id="cfg-mr-file-preview" aria-live="polite">${moduleFilePreviewHtml()}</div>
          <div class="flasher-config-note" style="margin-top:10px">
            Modbus watchdog (рег. 134):
            <input id="cfg-mr-inactivity-global" type="number" min="0" max="255"
              value="${escapeHtml(String(mr.inactivity_known === true ? mr.inactivity_s : ''))}"
              style="width:70px;margin:0 6px;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border-md);border-radius:var(--radius-xs);color:var(--text);" /> с
            <button class="btn btn-sm" type="button" id="cfg-mr-inactivity-save-btn" style="margin-left:4px">Сохранить</button>
          </div>
        </section>

      </div>
    `;
  }

  function renderModuleRelayTab(snap) {
    const relay = ((snap.mr || {}).relay || {});
    const caps = (moduleMeta(snap) || {}).relay_capabilities || {};
    const options = Number(relay.options || 0), mode = relay.mode_known ? relay.mode : null;
    const ready = relay.mode_known && relay.options_known && relay.power_stagger_known;
    const type = Number((moduleMeta(snap) || {}).code);
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Реле и задержки</h4>
          <p id="cfg-mr-relay-observed">${escapeHtml(uiT('Текущий режим'))}: ${mode == null ? '—' : escapeHtml(moduleRelayModeLabel(type, mode))}</p>
          <p class="flasher-config-note">${escapeHtml(uiT('Настройки могут изменить автономную работу выходов. Чтение подтверждает текущее состояние, но не сохранение после отключения питания.'))}</p>
          ${type === 11 ? `<p class="flasher-config-note">${escapeHtml(uiT('6DO5DI2AO: в режиме 3 отсутствуют необходимые DI-разрешения для полноценного управления двумя вентиляторами. В режиме 4 команда запуска относится только к вентилятору 1; AO задают скорость.'))}</p>` : ''}
          <div class="flasher-config-form">
            <label for="cfg-mr-relay-mode">Режим работы</label>
            <select id="cfg-mr-relay-mode" ${!relay.mode_known ? 'disabled' : ''}>
              ${mode == null ? '<option value="" selected>—</option>' : ''}
              ${(caps.modes || []).map(value => `<option value="${value}" ${value === mode ? 'selected' : ''}>${escapeHtml(moduleRelayModeLabel(type, value))}</option>`).join('')}
            </select>
            ${MODULE_RELAY_OPTION_BITS.filter(item => item.bit > 0 && Object.prototype.hasOwnProperty.call(caps.option_modes || {}, item.bit)).map(item => `
              <label class="checkbox-line"><input type="checkbox" id="cfg-mr-relay-opt-${item.bit}" ${(options & (1 << item.bit)) ? 'checked' : ''} ${!relay.options_known || !(caps.option_modes[item.bit] || []).includes(mode) ? 'disabled' : ''} /> ${escapeHtml(item.label)}</label>
            `).join('')}
            <label for="cfg-mr-relay-stagger">Задержка вкл., с, 0 = выкл.:</label>
            <input id="cfg-mr-relay-stagger" type="number" min="0" max="60" value="${relay.power_stagger_known ? escapeHtml(String(relay.power_stagger)) : ''}" ${!relay.power_stagger_known ? 'disabled' : ''} />
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-mr-relay-save-btn" ${!ready ? 'disabled' : ''}>Сохранить</button>
          </div>
          <p class="flasher-config-note">${escapeHtml(uiT('Восстановление состояния действует только в режиме тоггл релейных модулей. Неактивные и зарезервированные опции сохраняются.'))}</p>
          ${renderRelayAdvanced(snap)}
        </section>
      </div>
    `;
  }

  function moduleRelayModeLabel(type, mode) {
    if (type === 15 && mode === 1) return uiT('1 - Панель с фиксацией DI');
    if (type === 15 && mode === 2) return uiT('2 - Панель тоггл по фронту');
    const item = MODULE_RELAY_MODES.find(item => item.value === mode);
    return item ? uiT(item.label) : '—';
  }

  function moduleDoCommandAllowed(snap, channel, intent) {
    if (!snap || !snap.mr) return false;
    const meta = moduleMeta(snap) || {}, caps = meta.relay_capabilities, relay = snap.mr.relay || {};
    if (!Number.isInteger(channel) || channel < 1 || channel > Number(meta.max_do)) return false;
    if (typeof ((snap.mr.do || {}).bits || [])[channel - 1] !== 'boolean') return false;
    if (intent === 'drive') return !!(caps && relay.mode_known && relay.mode === 5 && relay.advanced_supported && channel <= 2 * caps.drive_count);
    if (!caps) return Number(meta.code) === 2 || Number(meta.code) === 4;
    if (caps.modes.length > 1 && !relay.mode_known) return false;
    if (Number(meta.code) === 11) {
      if ([1, 2].includes(relay.mode)) return false;
      if ([3, 4].includes(relay.mode)) return channel === 1 && relay.advanced_supported === true;
    }
    return true;
  }

  function moduleDiWritable(snap) {
    const relay = (snap?.mr || {}).relay || {};
    return Number((moduleMeta(snap) || {}).code) !== 11 || (relay.mode_known === true && ![3, 4].includes(relay.mode));
  }

  function moduleSettingFields(snap, family, channel) {
    const mr = snap?.mr || {}, block = mr[family] || {}, index = channel - 1;
    const field = (id, value, minimum, maximum) => ({id, value, known: Number.isInteger(value) && value >= minimum && value <= maximum});
    if (family === 'do') {
      const timer = (block.timer_words || [])[index];
      return [field(`cfg-mr-do-safe-${channel}`, (block.safe || [])[index], 0, 1), ...(channel <= 6 ? [
        field(`cfg-mr-do-mode-${channel}`, Number.isInteger(timer) ? timer & 255 : undefined, 0, 5),
        field(`cfg-mr-do-time-${channel}`, Number.isInteger(timer) && timer >= 0 && timer <= 65535 ? timer >> 8 : undefined, 0, 255),
        field(`cfg-mr-do-redelay-${channel}`, (block.redelay || [])[index], 0, 999)] : [])];
    }
    if (family === 'di') return [field(`cfg-mr-di-mode-${channel}`, (block.mode || [])[index], 0, 1),
      field(`cfg-mr-di-debounce-${channel}`, (block.debounce || [])[index], 0, 100),
      field(`cfg-mr-di-long-${channel}`, (block.long_press || [])[index], 500, 5000),
      field(`cfg-mr-di-double-${channel}`, (block.double_click || [])[index], 0, 2000)];
    if (family === 'ao') return [field(`cfg-mr-ao-set-${channel}`, (block.setpoint || [])[index], 0, 1000),
      field(`cfg-mr-ao-safe-${channel}`, (block.safe || [])[index], 0, 1000),
      {...field(`cfg-mr-ao-inactivity-${channel}`, mr.inactivity_s, 0, 255), known: mr.inactivity_known === true && Number.isInteger(mr.inactivity_s) && mr.inactivity_s >= 0 && mr.inactivity_s <= 255}];
    return [];
  }

  function moduleSettingGroupKnown(snap, family, channel) {
    const meta = moduleMeta(snap) || {}, fields = moduleSettingFields(snap, family, channel);
    return Number.isInteger(channel) && channel >= 1 && channel <= Number(meta['max_' + family]) && fields.length > 0 &&
      fields.every(field => field.known) && (family !== 'di' || moduleDiWritable(snap));
  }

  function moduleSettingInputsReady(snap, family, channel) {
    return moduleSettingFields(snap, family, channel).every(field => {
      const input = configModalEl(field.id);
      return input && !input.disabled && input.value !== '';
    });
  }

  function moduleWatchdogKnown(snap) {
    const mr = snap?.mr || {}, meta = moduleMeta(snap) || {};
    return mr.inactivity_known === true && Number.isInteger(mr.inactivity_s) && mr.inactivity_s >= 0 && mr.inactivity_s <= 255 && (Number(meta.max_do) > 0 || Number(meta.max_ao) > 0);
  }

  function aiFiltersKnown(ai) {
    const filters = ai?.filters;
    return !!filters && ['kalman', 'sps', 'avg', 'tau'].every(key => filters.known?.[key] === true) &&
      [0, 1].includes(filters.kalman) && MODULE_AI_SAMPLE_RATES.includes(filters.sps) &&
      Number.isInteger(filters.avg) && filters.avg >= 0 && filters.avg <= 50 &&
      Number.isInteger(filters.tau) && filters.tau >= 0 && filters.tau <= 65000 && Number.isInteger(filters.stor) && filters.stor >= 0 && filters.stor <= 11;
  }

  const AI_BOUNDARY_NOTE = 'Применение типа AI не подтверждено. Пределы и параметры АЦП доступны только для чтения.';
  const AI_TYPE_ACCEPTED = 'Команда смены типа AI принята. Применение не подтверждено.';
  const AI_RESULT_UNKNOWN = 'Применение настройки AI не подтверждено. Обновите состояние.';

  function aiApplyIdle(snap) {
    return snap?.mr?.ai_apply?.state === 'idle';
  }

  function patchOptionalSettingControls(snap) {
    const mr = snap.mr || {}, meta = moduleMeta(snap) || {}, body = configModalEl('flasher-config-body');
    if (!body) return;
    const busy = state.configBusy || state.configFileBusy;
    const applyField = (el, known, value, enabled) => {
      if (!el) return;
      el.disabled = !!busy || !enabled;
      if (!el.dataset.optionalDirty && el !== document.activeElement) {
        if (el.type === 'checkbox') el.checked = known && !!value;
        else el.value = known ? String(value) : '';
      }
      el.title = known ? '' : uiT('Настройки не прочитаны. Обновите данные перед записью.');
    };
    for (const family of ['do', 'di', 'ao']) {
      for (let channel = 1; channel <= Number(meta['max_' + family] || 0); channel++) {
        const save = body.querySelector(`[data-mr-${family}-save="${channel}"]`);
        if (!save) continue;
        const known = moduleSettingGroupKnown(snap, family, channel);
        save.disabled = !!busy || !known;
        moduleSettingFields(snap, family, channel).forEach(field => applyField(configModalEl(field.id), field.known, field.value, known));
        const id = `cfg-mr-${family}-settings-known-${channel}`;
        let note = configModalEl(id);
        if (!note) {note = document.createElement('p'); note.id = id; note.className = 'flasher-config-note'; save.parentElement.appendChild(note);}
        note.hidden = known; note.textContent = uiT('Настройки не прочитаны. Обновите данные перед записью.');
      }
    }
    const inactivity = configModalEl('cfg-mr-inactivity-global');
    const inactivityKnown = moduleWatchdogKnown(snap);
    applyField(inactivity, inactivityKnown, mr.inactivity_s, inactivityKnown);
    const inactivitySave = configModalEl('cfg-mr-inactivity-save-btn');
    if (inactivitySave) inactivitySave.disabled = !!busy || !inactivityKnown;
    for (const ai of mr.ai?.channels || []) {
      const channel = Number(ai.channel), idle = aiApplyIdle(snap);
      const boundaryNote = configModalEl(`cfg-mr-ai-apply-${channel}`);
      if (boundaryNote) boundaryNote.hidden = idle;
      for (const name of ['lo', 'hi']) {
        const limit = configModalEl(`cfg-mr-ai-limit-${name}-${channel}`);
        if (limit) limit.disabled = !!busy || !idle;
      }
      if (!ai.filters) continue;
      const known = aiFiltersKnown(ai);
      for (const key of ['kalman', 'sps', 'avg', 'tau']) {
        const readable = ai.filters.known?.[key] === true;
        applyField(configModalEl(`cfg-mr-ai-${key}-${channel}`), readable, ai.filters[key],
          key === 'kalman' ? readable : known && idle);
      }
      const note = configModalEl(`cfg-mr-ai-filters-known-${channel}`);
      if (note) {note.hidden = known; note.textContent = uiT('Настройки не прочитаны. Обновите данные перед записью.');}
    }
  }

  function renderRelayAdvanced(snap) {
    const relay = snap.mr.relay || {}, caps = moduleMeta(snap).relay_capabilities || {};
    if (!caps.fan_status && !caps.dimming) return '';
    if (!relay.advanced_supported) return `<p class="flasher-config-note">${escapeHtml(uiT('Расширенные функции доступны после подтверждения прошивки не ниже 1.0.12.22.'))}</p>`;
    const mode = relay.mode_known ? relay.mode : null;
    const fields = (relay.advanced_fields || []).filter(field =>
      caps.dimming ? field.path.startsWith('relay.dimming.') : mode === 5 ? field.path.startsWith('relay.drives.') :
      [3, 4].includes(mode) && (field.path.startsWith('relay.schedule.') || (mode === 3 && field.path === 'relay.primary_fans')));
    const known = fields.length && fields.every(field => field.known);
    const fans = relay.fans || {};
    const status = [3, 4].includes(mode) ? `<div id="cfg-mr-fan-status">${relayFanStatusHtml(relay)}</div>
      <button class="btn btn-sm" type="button" id="cfg-mr-fan-reset" ${!fans.known ? 'disabled' : ''}>${escapeHtml(uiT('Сбросить аварии вентиляторов'))}</button>` : '';
    const drives = mode === 5 ? Array.from({length: caps.drive_count || 0}, (_, idx) => `<div class="flasher-config-actions"><strong>${escapeHtml(uiT('Привод'))} ${idx + 1}</strong>
      ${[['Открыть', 1, true], ['Отменить открытие', 1, false], ['Закрыть', 2, true], ['Отменить закрытие', 2, false]].map(([label, offset, value]) => `<button class="btn btn-sm" type="button" data-relay-drive="${2 * idx + offset}" data-relay-on="${value}" ${!relay.mode_known ? 'disabled' : ''}>${escapeHtml(uiT(label))}</button>`).join('')}</div>`).join('') : '';
    return `${status}${drives}<details><summary>${escapeHtml(uiT('Расширенные настройки'))}</summary>
      ${caps.fan_status ? `<p class="flasher-config-note">${escapeHtml(uiT('Расписание использует дни цикла 1–7 и время с момента запуска модуля, а не календарь СА. Пустое окно: начало равно концу; переход через полночь допустим.'))}</p>
        <p class="flasher-config-note">${escapeHtml(uiT('Расписание, приводы и таймеры релейных модулей действуют в RAM и могут сброситься после перезапуска. Нулевой deadtime означает 500 мс; нулевое общее время — 10 с; нулевое время направления использует общее.'))}</p>` : ''}
      <div class="flasher-config-form">${fields.map(field => `<label>${escapeHtml(configurationPathLabel(field.path))}
        <input data-relay-field="${escAttr(field.path)}" type="${field.boolean ? 'checkbox' : 'number'}" min="${field.minimum}" max="${field.maximum}" ${field.boolean ? field.value ? 'checked' : '' : `value="${field.known ? field.value : ''}"`} ${!field.known ? 'disabled' : ''} /></label>`).join('')}</div>
      <button class="btn btn-primary" type="button" id="cfg-mr-relay-advanced-save" ${!known ? 'disabled' : ''}>${escapeHtml(uiT('Применить выбранные настройки'))}</button></details>`;
  }

  function relayFanStatusHtml(relay) {
    const fans = relay.fans || {};
    if (!fans.known) return `<p>${escapeHtml(uiT('Статус аварий недоступен'))}</p>`;
    return (fans.running || []).map((on, idx) => `<p>${escapeHtml(uiT('Вентилятор'))} ${idx + 1}: ${escapeHtml(uiT(on ? 'Работает' : 'Остановлен'))}; ${escapeHtml(uiT(fans.alarms[idx] ? 'Авария' : 'Нет аварии'))}</p>`).join('') + `<p>${escapeHtml(uiT('Пожар'))}: ${escapeHtml(uiT(fans.fire ? 'Активен' : 'Неактивен'))}</p>`;
  }

  function patchRelayControls(snap) {
    const relay = (snap.mr || {}).relay || {}, meta = moduleMeta(snap) || {}, caps = meta.relay_capabilities || {};
    const busy = state.configBusy || state.configFileBusy;
    const observed = configModalEl('cfg-mr-relay-observed');
    if (observed) observed.textContent = uiT('Текущий режим') + ': ' + (relay.mode_known ? moduleRelayModeLabel(Number(meta.code), relay.mode) : '—');
    const fans = configModalEl('cfg-mr-fan-status');
    if (fans) fans.innerHTML = relayFanStatusHtml(relay);
    const body = configModalEl('flasher-config-body');
    if (!body) return;
    body.querySelectorAll('input[id^="cfg-mr-di-"],select[id^="cfg-mr-di-"],[data-mr-di-save],[data-mr-di-reset]').forEach(el => {el.disabled = !!busy || !moduleDiWritable(snap);});
    body.querySelectorAll('[data-mr-do-on],[data-mr-do-off]').forEach(button => {
      const channel = Number(button.dataset.mrDoOn || button.dataset.mrDoOff);
      button.disabled = !!busy || !moduleDoCommandAllowed(snap, channel);
      const fan = Number(meta.code) === 11 && relay.mode_known && [3, 4].includes(relay.mode) && channel === 1;
      button.textContent = uiT(button.dataset.mrDoOn ? fan ? 'Запуск вентилятора 1' : 'Включить' : fan ? 'Остановить вентилятор' : 'Выключить');
    });
    body.querySelectorAll('[data-relay-drive]').forEach(button => {button.disabled = !!busy || !moduleDoCommandAllowed(snap, Number(button.dataset.relayDrive), 'drive');});
    const mode = configModalEl('cfg-mr-relay-mode');
    if (mode) mode.disabled = !!busy || !relay.mode_known;
    const selectedMode = mode && mode.value !== '' ? Number(mode.value) : relay.mode;
    MODULE_RELAY_OPTION_BITS.forEach(item => {
      const el = configModalEl('cfg-mr-relay-opt-' + item.bit);
      if (el) el.disabled = !!busy || !relay.options_known || !(caps.option_modes?.[item.bit] || []).includes(selectedMode);
    });
    const save = configModalEl('cfg-mr-relay-save-btn');
    if (save) save.disabled = !!busy || !relay.mode_known || !relay.options_known || !relay.power_stagger_known;
    const fields = new Map((relay.advanced_fields || []).map(field => [field.path, field]));
    const controls = Array.from(body.querySelectorAll('[data-relay-field]'));
    controls.forEach(el => {el.disabled = !!busy || !relay.mode_known || !relay.advanced_supported || !fields.get(el.dataset.relayField)?.known;});
    const advanced = configModalEl('cfg-mr-relay-advanced-save');
    if (advanced) advanced.disabled = !!busy || !controls.length || controls.some(el => el.disabled);
    const reset = configModalEl('cfg-mr-fan-reset');
    if (reset) reset.disabled = !!busy || !relay.mode_known || ![3, 4].includes(relay.mode) || !relay.advanced_supported || !relay.fans?.known;
    patchOptionalSettingControls(snap);
  }

  function renderModuleDoTab(snap, channel) {
    const mr = snap.mr || {};
    const bits = ((mr.do || {}).bits || []);
    const counts = ((mr.do || {}).counts || []);
    const safe = ((mr.do || {}).safe || []);
    const timerWords = ((mr.do || {}).timer_words || []);
    const redelay = ((mr.do || {}).redelay || []);
    const idx = channel - 1;
    const timerWord = timerWords[idx];
    const timerMode = Number.isInteger(timerWord) ? timerWord & 0xFF : null;
    const timerTime = Number.isInteger(timerWord) ? (timerWord >> 8) & 0xFF : '';
    const relay = mr.relay || {}, meta = moduleMeta(snap) || {}, caps = meta.relay_capabilities || {};
    const commandAllowed = moduleDoCommandAllowed(snap, channel);
    const fanCommand = Number(meta.code) === 11 && [3, 4].includes(relay.mode) && channel === 1;
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>DO${channel}</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Состояние</span><strong id="cfg-mr-do-state-${channel}" class="do-state-value${bits[idx] ? ' do-state-on' : ''}">${bits[idx] == null ? '—' : bits[idx] ? 'Вкл' : 'Выкл'}</strong></div>
            <div class="flasher-config-row"><span>Счетчик включений</span><strong id="cfg-mr-do-count-${channel}">${escapeHtml(String(counts[idx] ?? '—'))}</strong></div>
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-sm ${bits[idx] ? '' : 'btn-success'}" type="button" data-mr-do-on="${channel}" data-do-channel="${channel}" ${!commandAllowed ? 'disabled' : ''}>${fanCommand ? uiT('Запуск вентилятора 1') : uiT('Включить')}</button>
            <button class="btn btn-sm ${bits[idx] ? 'btn-danger' : ''}" type="button" data-mr-do-off="${channel}" data-do-channel="${channel}" ${!commandAllowed ? 'disabled' : ''}>${fanCommand ? uiT('Остановить вентилятор') : uiT('Выключить')}</button>
            <button class="btn btn-sm" type="button" data-mr-do-reset="1">Сброс счетчиков DO</button>
          </div>
          <p id="cfg-mr-do-command-note" class="flasher-config-note">${escapeHtml(uiT(!commandAllowed ? 'Команда недоступна: проверьте текущий режим и данные модуля.' : caps.legacy_manual_transition && relay.mode !== 0 ? 'Команда DO переведёт модуль в режим Modbus. Автономный алгоритм будет остановлен.' : 'Команда принята отдельно от фактического состояния: таймеры и блокировки могут задержать выход.'))}</p>
        </section>
        <section class="flasher-config-card">
          <h4>Настройки канала</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-do-safe-${channel}">Безопасное состояние, 0/1</label>
            <input id="cfg-mr-do-safe-${channel}" type="number" min="0" max="1" value="${escapeHtml(String(safe[idx] ?? ''))}" />
            ${channel <= 6 ? `
              <label for="cfg-mr-do-mode-${channel}">Таймер, режим</label>
              <select id="cfg-mr-do-mode-${channel}">
                ${timerMode == null ? '<option value="" selected>—</option>' : ''}
                ${MODULE_TIMER_MODES.map(item => `<option value="${item.value}" ${Number(item.value) === timerMode ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('')}
              </select>
              <label for="cfg-mr-do-time-${channel}">Таймер, время x0.1 c</label>
              <input id="cfg-mr-do-time-${channel}" type="number" min="0" max="255" value="${escapeHtml(String(timerTime))}" />
              <label for="cfg-mr-do-redelay-${channel}">Пауза повторного включения, с</label>
              <input id="cfg-mr-do-redelay-${channel}" type="number" min="0" max="999" value="${escapeHtml(String(redelay[idx] ?? ''))}" />
            ` : ''}
          </div>
          <div class="flasher-config-note">Таймеры / redelay — только DO1…DO6 (как в desktop).</div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-do-save="${channel}">Сохранить DO${channel}</button>
          </div>
        </section>
      </div>
      ${moduleWiringDiagramHtml(snap, 'do', channel, 0)}
    `;
  }

  function renderModuleDiTab(snap, channel) {
    const mr = snap.mr || {};
    const di = mr.di || {};
    const idx = channel - 1;
    return `
      <div class="flasher-config-grid flasher-config-di-split">
        <section class="flasher-config-card">
          <h4>DI${channel}</h4>
          ${!moduleDiWritable(snap) ? `<p class="flasher-config-note">${escapeHtml(uiT('Настройки DI недоступны в режиме вентилятора или при неизвестном режиме.'))}</p>` : ''}
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Состояние</span><strong id="cfg-mr-di-state-${channel}" class="${Number((di.values || [])[idx] || 0) ? 'di-state-on' : ''}">${(di.values || [])[idx] == null ? '—' : Number(di.values[idx]) ? 'Активен' : 'Неактивен'}</strong></div>
            <div class="flasher-config-row"><span>Счетчик импульсов</span><strong id="cfg-mr-di-count-${channel}">${escapeHtml(String((di.counts || [])[idx] ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>Коротких / длинных / двойных</span><strong id="cfg-mr-di-press-${channel}">${escapeHtml(String((di.short_counts || [])[idx] ?? '—'))} / ${escapeHtml(String((di.long_counts || [])[idx] ?? '—'))} / ${escapeHtml(String((di.double_counts || [])[idx] ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>Частота</span><strong id="cfg-mr-di-freq-${channel}">${escapeHtml(String((di.freq || [])[idx] ?? '—'))} Гц</strong></div>
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-sm" type="button" data-mr-di-reset="1">Сброс счетчиков DI</button>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Настройки входа</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-di-mode-${channel}">Режим входа</label>
            <select id="cfg-mr-di-mode-${channel}">
              ${(di.mode || [])[idx] == null ? '<option value="" selected>—</option>' : ''}
              ${MODULE_DI_MODES.map(item => `<option value="${item.value}" ${Number(item.value) === Number((di.mode || [])[idx]) ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('')}
            </select>
            <label for="cfg-mr-di-debounce-${channel}">Антидребезг, мс</label>
            <input id="cfg-mr-di-debounce-${channel}" type="number" min="0" max="100" value="${escapeHtml(String((di.debounce || [])[idx] ?? ''))}" />
            <label for="cfg-mr-di-long-${channel}">Длинное нажатие, мс</label>
            <input id="cfg-mr-di-long-${channel}" type="number" min="500" max="5000" value="${escapeHtml(String((di.long_press || [])[idx] ?? ''))}" />
            <label for="cfg-mr-di-double-${channel}">Окно двойного нажатия, мс</label>
            <input id="cfg-mr-di-double-${channel}" type="number" min="0" max="2000" value="${escapeHtml(String((di.double_click || [])[idx] ?? ''))}" />
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-di-save="${channel}">Сохранить DI${channel}</button>
          </div>
        </section>
      </div>
      ${moduleWiringDiagramHtml(snap, 'di', channel, 0)}
    `;
  }

  function renderModuleAoTab(snap, channel) {
    const mr = snap.mr || {};
    const ao = mr.ao || {};
    const idx = channel - 1;
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>AO${channel}</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Текущее значение</span><strong id="cfg-mr-ao-live-${channel}">${moduleAoDisplay(snap, (ao.current_raw || [])[idx])}</strong></div>
            <div class="flasher-config-row"><span>Raw</span><strong id="cfg-mr-ao-raw-${channel}">${escapeHtml(String((ao.current_raw || [])[idx] ?? '—'))}</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Уставки AO</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-ao-set-${channel}">${moduleAoIsDimmer(snap) ? 'Задание, 0–1000 (1000 = 100%)' : 'Задание, 0–1000 (1000 = 10.00 В)'}</label>
            <input id="cfg-mr-ao-set-${channel}" type="number" min="0" max="1000" value="${escapeHtml(String((ao.setpoint || [])[idx] ?? ''))}" />
            <label for="cfg-mr-ao-safe-${channel}">${moduleAoIsDimmer(snap) ? 'Безопасное состояние, 0–1000 (1000 = 100%)' : 'Безопасное состояние, 0–1000 (1000 = 10.00 В)'}</label>
            <input id="cfg-mr-ao-safe-${channel}" type="number" min="0" max="1000" value="${escapeHtml(String((ao.safe || [])[idx] ?? ''))}" />
            <label for="cfg-mr-ao-inactivity-${channel}">Время без опроса, с</label>
            <input id="cfg-mr-ao-inactivity-${channel}" type="number" min="0" max="255" value="${escapeHtml(String(mr.inactivity_known === true ? mr.inactivity_s : ''))}" />
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-ao-save="${channel}">Сохранить AO${channel}</button>
          </div>
        </section>
      </div>
      ${moduleAoIsDimmer(snap) ? '<div class="flasher-config-note">Заданная яркость, не измерение напряжения нагрузки. Включение выхода задаётся отдельно через DO.</div>' : ''}
      ${moduleWiringDiagramHtml(snap, 'ao', channel, 0)}
    `;
  }

  function renderModuleAiTab(snap, channel) {
    const ai = moduleAiChannel(snap, channel);
    if (!ai) return '<div class="flasher-empty">Канал AI не найден.</div>';
    const filters = ai.filters || null;
    const sensorCode = Number(ai.sensor_code || 0);
    const bucket = ai.ui_bucket || aiUiSensorBucket(sensorCode);
    // Видимость/диапазон калибровки выводим на клиенте из sensor_code (эталонный
    // предикат ai_ui_uses_value_calibration): активные volt/curr тоже калибруются.
    // Единственный источник — sensor_code; снимок поля applicability не несёт
    // (см. docs/contracts/module-config-ai.md).
    const calOk = aiUiCalibrationApplicable(sensorCode);
    const calInt = aiCalibrationIsInteger(sensorCode);
    const calLabel = calInt ? 'Калибровка (смещение)' : 'Калибровка';
    const calMin = calInt ? -32768 : -100;
    const calMax = calInt ? 32767 : 100;
    const faultView = aiFaultView(ai);

    const isRtd = bucket === 'rtd';
    const rtdTwoWire = isRtd ? aiRtdTwoWireFromCode(sensorCode) : true;
    const subchoices = isRtd ? aiUiRtdSubchoicesForWire(rtdTwoWire) : aiUiSubchoicesForBucket(bucket);
    const subchoiceOptions = subchoices.map(item => {
      const sel = Number(item[0]) === sensorCode ? 'selected' : '';
      return `<option value="${item[0]}" ${sel}>${escapeHtml(item[1])}</option>`;
    }).join('');

    const measuredStr = aiFormatMeasuredDisplay(sensorCode, ai.measured_raw);
    const scaledStr   = aiFormatScaledDisplay(sensorCode, ai.scaled_raw);
    const [magnitude, unit] = aiUiQuantityLabels(bucket);

    const modeRadios = MODULE_AI_UI_BUCKETS.map(b => `
      <label class="ai-mode-radio-label">
        <input type="radio" name="cfg-mr-ai-mode-${channel}" value="${escapeHtml(b.id)}"
          data-mr-ai-bucket="${channel}" ${b.id === bucket ? 'checked' : ''} />
        <span>${escapeHtml(b.label)}</span>
      </label>`).join('');

    const limitsHtml = aiLimitsBoxHtml(channel, sensorCode, ai);

    const rtdWireHtml = isRtd ? `
      <div class="ai-wire-row">
        <span>Подключение</span>
        <div class="ai-wire-toggle" id="cfg-mr-ai-wire-wrap-${channel}">
          <button class="ai-wire-btn${rtdTwoWire ? ' active' : ''}"
            data-mr-ai-wire="${channel}" data-wire="2">Двухпроводное</button>
          <button class="ai-wire-btn${!rtdTwoWire ? ' active' : ''}"
            data-mr-ai-wire="${channel}" data-wire="3">Трёхпроводное</button>
        </div>
      </div>` : '';

    const filterHtml = filters ? `
      <label class="ai-filter-check">
        <input id="cfg-mr-ai-kalman-${channel}" type="checkbox"
          ${filters.known?.kalman === true && filters.kalman ? 'checked' : ''} data-mr-ai-filter="${channel}" />
        Фильтр Калмана
      </label>
      <div class="ai-filter-row">
        <span>Частота опроса АЦП, выб./сек:</span>
        <select id="cfg-mr-ai-sps-${channel}" data-mr-ai-filter="${channel}">
          ${filters.known?.sps !== true ? '<option value="" selected>—</option>' : ''}
          ${MODULE_AI_SAMPLE_RATES.map(r => `<option value="${r}" ${filters.known?.sps === true && Number(r) === filters.sps ? 'selected' : ''}>${r}</option>`).join('')}
        </select>
      </div>
      <div class="ai-filter-row">
        <span>Число выборок, 0...50:</span>
        <input id="cfg-mr-ai-avg-${channel}" type="number" min="0" max="50"
          value="${escapeHtml(String(filters.known?.avg === true ? filters.avg : ''))}" data-mr-ai-filter="${channel}" />
      </div>
      <div class="ai-filter-row">
        <span>Пост. времени НЧ-фильтра, мс.:</span>
        <input id="cfg-mr-ai-tau-${channel}" type="number" min="0" max="65000"
          value="${escapeHtml(String(filters.known?.tau === true ? filters.tau : ''))}" data-mr-ai-filter="${channel}" />
      </div><p class="flasher-config-note" id="cfg-mr-ai-filters-known-${channel}"></p>` : '';

    return `
      <div class="ai-channel-panel">
        <h3 class="ai-channel-title">Аналоговые входы (AI) AI${channel}</h3>

        <section class="flasher-config-card ai-section-measures">
          <span class="${faultView.className}" id="cfg-mr-ai-fault-${channel}" ${faultView.parts.length ? '' : 'hidden'}>${aiFaultChipHtml(faultView.parts)}</span>
          <h4>ИЗМЕРЕНИЯ</h4>
          <div class="ai-measures-grid">
            <div class="ai-measure-row">
              <span>Измеренное значение с АЦП</span>
              <strong class="ai-raw-fmt" id="cfg-mr-ai-measured-${channel}">${escapeHtml(measuredStr)}</strong>
            </div>
            <div class="ai-measure-row">
              <span>Пересчитанное значение</span>
              <div class="ai-measure-value-group">
                <strong id="cfg-mr-ai-scaled-${channel}">${escapeHtml(scaledStr)}</strong>
                <div class="ai-cal-strip" id="cfg-mr-ai-cal-strip-${channel}" ${calOk ? '' : 'hidden'}>
                  <span id="cfg-mr-ai-cal-label-${channel}">${escapeHtml(calLabel)}</span>
                  <button class="ai-cal-btn" data-mr-ai-cal-step="${channel}" data-step="-1">−</button>
                  <input id="cfg-mr-ai-cal-${channel}" type="number" min="${calMin}" max="${calMax}"
                    value="${escapeHtml(String(ai.calibration ?? 0))}"
                    data-mr-ai-cal="${channel}" class="ai-cal-input-sm" />
                  <button class="ai-cal-btn" data-mr-ai-cal-step="${channel}" data-step="1">+</button>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section class="flasher-config-card ai-section-settings">
          <h4>НАСТРОЙКА ВХОДА</h4>
          <p id="cfg-mr-ai-apply-${channel}" class="flasher-config-note" ${aiApplyIdle(snap) ? 'hidden' : ''}>${escapeHtml(uiT(AI_BOUNDARY_NOTE))}</p>
          <div class="ai-settings-3col">
            <div class="ai-mode-col">
              <div class="ai-col-subtitle">РЕЖИМ РАБОТЫ</div>
              <div class="ai-mode-radios">${modeRadios}</div>
            </div>
            <div class="ai-sensor-col">
              <div class="ai-quantity-row"><span>Величина:</span><strong id="cfg-mr-ai-mag-${channel}">${escapeHtml(magnitude)}</strong></div>
              <div class="ai-quantity-row"><span>Единица:</span><strong id="cfg-mr-ai-unit-${channel}">${escapeHtml(unit)}</strong></div>
              <select id="cfg-mr-ai-sensor-${channel}" class="ai-sensor-select" data-mr-ai-sensor="${channel}">${subchoiceOptions}</select>
              <div id="cfg-mr-ai-wire-row-${channel}">${rtdWireHtml}</div>
              <div id="cfg-mr-ai-limits-${channel}" class="ai-limits-block">${limitsHtml}</div>
            </div>
            <div class="ai-filter-col" id="cfg-mr-ai-filter-col-${channel}">
              <div class="ai-col-subtitle">AI${channel}: ФИЛЬТР И ОПРОС АЦП</div>
              ${filterHtml}
            </div>
          </div>
        </section>

        <div class="wiring-diagram-slot" id="cfg-mr-ai-wiring-${channel}">${moduleWiringDiagramHtml(snap, 'ai', channel, sensorCode)}</div>
      </div>
    `;
  }

  function renderInfoTab(snap) {
    const info = snap.info || {};
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Устройство</h4>
          <dl class="flasher-config-kv">
            <div><dt>Сигнатура</dt><dd>${escapeHtml(info.signature || '—')}</dd></div>
            <div><dt>Серийный №</dt><dd class="mono">${escapeHtml(serialHex(info.serial))}</dd></div>
            <div><dt>Адрес</dt><dd>${escapeHtml(String(info.address ?? '—'))}</dd></div>
            <div><dt>Прошивка</dt><dd>${escapeHtml(info.app_version || '—')}</dd></div>
            <div><dt>Бутлоадер</dt><dd>${escapeHtml(info.bootloader_version || '—')}</dd></div>
            <div><dt>Тип</dt><dd>${escapeHtml(info.type_code == null ? '—' : String(info.type_code))}</dd></div>
          </dl>
        </section>
        <section class="flasher-config-card">
          <h4>Линия</h4>
          <dl class="flasher-config-kv">
            <div><dt>Скорость</dt><dd>${escapeHtml(String(info.line && info.line.baudrate || '—'))}</dd></div>
            <div><dt>Чётность</dt><dd>${escapeHtml(String(info.line && info.line.parity || '—'))}</dd></div>
            <div><dt>Стоп-биты</dt><dd>${escapeHtml(String(info.line && info.line.stopbits || '—'))}</dd></div>
            <div><dt>Fast Modbus</dt><dd>${snap.network && snap.network.fast_modbus ? 'Вкл.' : 'Выкл.'}</dd></div>
          </dl>
          <div class="flasher-config-note">Окно использует те же Modbus-регистры сети, что и desktop-flasher: 110–112, 122, 128.</div>
        </section>
      </div>
    `;
  }

  function renderNetworkTab(snap) {
    const net = snap.network || {};
    const parity = String(net.parity || 'N');
    const baud = Number(net.baudrate) || 9600;
    const stop = Number(net.stopbits) || 1;
    const addr = Number(net.address) || 1;
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Сеть RS-485</h4>
          <div class="flasher-config-form">
            <label for="cfg-net-addr">Modbus-адрес</label>
            <input id="cfg-net-addr" type="number" min="1" max="247" value="${addr}" />

            <label for="cfg-net-baud">Скорость</label>
            <select id="cfg-net-baud">
              ${[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200].map(v => `<option value="${v}" ${v === baud ? 'selected' : ''}>${v}</option>`).join('')}
            </select>

            <label for="cfg-net-parity">Чётность</label>
            <select id="cfg-net-parity">
              ${['N', 'O', 'E'].map(v => `<option value="${v}" ${v === parity ? 'selected' : ''}>${v}</option>`).join('')}
            </select>

            <label for="cfg-net-stop">Стоп-биты</label>
            <select id="cfg-net-stop">
              ${[1, 2].map(v => `<option value="${v}" ${v === stop ? 'selected' : ''}>${v}</option>`).join('')}
            </select>

            <label class="checkbox-line"><input id="cfg-net-fast" type="checkbox" ${net.fast_modbus ? 'checked' : ''} /> Fast Modbus (рег. 122)</label>
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-net-save-btn">Сохранить</button>
            <button class="btn btn-sm" type="button" id="cfg-net-refresh-btn">Обновить</button>
          </div>
          <div class="flasher-config-note">Сохранение выполняется в безопасном порядке, как в desktop-flasher: сначала адрес, затем Fast Modbus, затем линия 110–112.</div>
        </section>
        ${renderBusModeCard(snap)}
      </div>
    `;
  }

  /* Полевая шина (рег. 122, 3-state) — селектор + проверка/возврат BACnet (§5.1/5.2/5.4).
     Показывается ТОЛЬКО для семейств с реальным регистром (MR/DTV); для CE/WB/
     bootloader daemon отдаёт bus_mode_supported=false и карта скрыта. */
  function renderBusModeCard(snap) {
    if (!snap || !snap.bus_mode_supported) return '';
    const mode = Number((snap.network || {}).bus_mode);
    const cur = [0, 1, 2].indexOf(mode) >= 0 ? mode : 1;
    const opt = (v, label) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${label}</option>`;
    return `
      <section class="flasher-config-card" id="cfg-busmode-card">
        <h4>Полевая шина</h4>
        <div class="flasher-config-form">
          <label for="cfg-busmode-select">Протокол шины (рег. 122)</label>
          <select id="cfg-busmode-select">
            ${opt(0, 'Классический Modbus')}
            ${opt(1, 'Fast Modbus')}
            ${opt(2, 'BACnet MS/TP')}
          </select>
        </div>
        <div class="flasher-config-actions">
          <button class="btn btn-primary" type="button" id="cfg-busmode-apply-btn">Применить протокол</button>
          <button class="btn btn-sm" type="button" id="cfg-busmode-verify-btn">Проверка BACnet</button>
          <button class="btn btn-sm" type="button" id="cfg-busmode-recover-btn">Вернуть в Modbus</button>
        </div>
        <div class="flasher-config-note">BACnet MS/TP переключает модуль в режим MS/TP-сервера для сторонней BMS. Переключение перезагружает модуль и убирает его из Modbus-опроса. Возврат — «Вернуть в Modbus» или физическая кнопка сброса на устройстве. Переключайте по одному модулю за раз.</div>
      </section>
    `;
  }

  /* Семейство bus_mode из снимка (для текста диалога восстановления). */
  function busModeFamily(snap) {
    return (snap && snap.family) || (snap && snap.kind === 'dtv' ? 'dtv' : 'mr');
  }

  /* Текст пути восстановления per-family для диалога-подтверждения (flasher-контракт:
     назвать путь возврата ДО записи). */
  function busModeRecoveryText(family) {
    if (family === 'dtv') {
      return t('Возврат в Modbus: «Вернуть в Modbus» (по проводу, WriteProperty AV:122=0), физическая кнопка сброса на устройстве или BACnet-мастер.');
    }
    return t('Возврат в Modbus: «Вернуть в Modbus» (по проводу, WriteProperty MSV:1), физическая кнопка сброса на устройстве или перепрошивка через SWD.');
  }

  function renderCeMeasuresTab(snap) {
    const live = (snap.ce && snap.ce.live) || {};
    return `
      <div class="flasher-config-measure-grid">
        <section class="flasher-config-card">
          <h4>Напряжения</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Фаза A</span><strong>${formatFloat(live.ua, 1)} В</strong></div>
            <div class="flasher-config-row"><span>Фаза B</span><strong>${formatFloat(live.ub, 1)} В</strong></div>
            <div class="flasher-config-row"><span>Фаза C</span><strong>${formatFloat(live.uc, 1)} В</strong></div>
            <div class="flasher-config-row"><span>Uab</span><strong>${formatFloat(live.uab, 1)} В</strong></div>
            <div class="flasher-config-row"><span>Ubc</span><strong>${formatFloat(live.ubc, 1)} В</strong></div>
            <div class="flasher-config-row"><span>Uca</span><strong>${formatFloat(live.uca, 1)} В</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Токи</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Фаза A</span><strong>${formatFloat(live.ia, 3)} А</strong></div>
            <div class="flasher-config-row"><span>Фаза B</span><strong>${formatFloat(live.ib, 3)} А</strong></div>
            <div class="flasher-config-row"><span>Фаза C</span><strong>${formatFloat(live.ic, 3)} А</strong></div>
            <div class="flasher-config-row"><span>Нейтраль</span><strong>${formatFloat(live.in, 3)} А</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Мощности</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>P суммарная</span><strong>${formatCePower(live.pt, 'Вт', 'кВт')}</strong></div>
            <div class="flasher-config-row"><span>Q суммарная</span><strong>${formatCePower(live.qt, 'Вар', 'кВар')}</strong></div>
            <div class="flasher-config-row"><span>S суммарная</span><strong>${formatCePower(live.st, 'ВА', 'кВА')}</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Качество сети</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Частота</span><strong>${formatFloat(live.freq, 2)} Гц</strong></div>
            <div class="flasher-config-row"><span>cosφ A / B / C</span><strong>${formatFloat(live.cfa, 3)} / ${formatFloat(live.cfb, 3)} / ${formatFloat(live.cfc, 3)}</strong></div>
            <div class="flasher-config-row"><span>cosφ суммарный</span><strong>${formatFloat(live.cft, 3)}</strong></div>
            <div class="flasher-config-row"><span>Темп. ASIC</span><strong>${escapeHtml(String(live.tasic ?? '—'))} °C</strong></div>
          </div>
        </section>
      </div>
    `;
  }

  function ceCtFwTooOldText(fwVersion) {
    return uiT('Прошивка CE-02м-3 ниже 1.0.7.5: запись коэффициентов ТТ отключена — на старых версиях она перезагружает устройство. Обновите прошивку.')
      + ' (' + (fwVersion || '—') + ')';
  }

  function renderCeSettingsTab(snap) {
    const cfg = (snap.ce && snap.ce.config) || {};
    // A hint from the snapshot; the daemon re-reads the version before a CT write.
    const ctLocked = !!(snap.ce && snap.ce.ct_write && snap.ce.ct_write.supported === false);
    const ctAttr = ctLocked ? ' disabled' : '';
    const ctNote = ctLocked
      ? `<div class="flasher-config-note" id="cfg-ce-ct-fw-note">${escapeHtml(ceCtFwTooOldText(snap.info && snap.info.app_version))}</div>`
      : '';
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Настройка ТТ</h4>
          <div class="flasher-config-form" id="cfg-ce-form">
            <label for="cfg-ce-ph-loss">Обнаружение пропажи фаз</label>
            <select id="cfg-ce-ph-loss"><option value="0" ${Number(cfg.ph_loss) === 0 ? 'selected' : ''}>0</option><option value="1" ${Number(cfg.ph_loss) === 1 ? 'selected' : ''}>1</option></select>

            <label for="cfg-ce-inv-a">Инверсия ТТ A</label>
            <select id="cfg-ce-inv-a"><option value="0" ${Number(cfg.inv_a) === 0 ? 'selected' : ''}>0</option><option value="1" ${Number(cfg.inv_a) === 1 ? 'selected' : ''}>1</option></select>

            <label for="cfg-ce-inv-b">Инверсия ТТ B</label>
            <select id="cfg-ce-inv-b"><option value="0" ${Number(cfg.inv_b) === 0 ? 'selected' : ''}>0</option><option value="1" ${Number(cfg.inv_b) === 1 ? 'selected' : ''}>1</option></select>

            <label for="cfg-ce-inv-c">Инверсия ТТ C</label>
            <select id="cfg-ce-inv-c"><option value="0" ${Number(cfg.inv_c) === 0 ? 'selected' : ''}>0</option><option value="1" ${Number(cfg.inv_c) === 1 ? 'selected' : ''}>1</option></select>

            <label for="cfg-ce-kt-a">Коэффициент ТТ A (K×1000)</label>
            <input id="cfg-ce-kt-a" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_a ?? CE_CT_RATIO_DEFAULT))}"${ctAttr} />

            <label for="cfg-ce-kt-b">Коэффициент ТТ B (K×1000)</label>
            <input id="cfg-ce-kt-b" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_b ?? CE_CT_RATIO_DEFAULT))}"${ctAttr} />

            <label for="cfg-ce-kt-c">Коэффициент ТТ C (K×1000)</label>
            <input id="cfg-ce-kt-c" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_c ?? CE_CT_RATIO_DEFAULT))}"${ctAttr} />
          </div>
          ${ctNote}
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-ce-save-btn">Сохранить</button>
          </div>
        </section>
      </div>
    `;
  }

  function renderDtvMeasuresTab(snap) {
    const live = (snap.dtv && snap.dtv.live) || {};
    return `
      <div class="flasher-config-measure-grid">
        <section class="flasher-config-card">
          <h4>Температура</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>DS18B20</span><strong>${formatFloat(live.t_ds, 1)} °C</strong></div>
            <div class="flasher-config-row"><span>MCP9808</span><strong>${formatFloat(live.t_mcp, 1)} °C</strong></div>
            <div class="flasher-config-row"><span>HDC1080</span><strong>${formatFloat(live.t_hdc, 1)} °C</strong></div>
            <div class="flasher-config-row"><span>BME280</span><strong>${formatFloat(live.t_bme280, 1)} °C</strong></div>
            <div class="flasher-config-row"><span>BME680</span><strong>${formatFloat(live.t_bme680, 1)} °C</strong></div>
            <div class="flasher-config-row"><span>NTC/PT1000</span><strong>${formatFloat(live.t_ext, 1)} °C</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Влажность</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>HDC1080</span><strong>${formatFloat(live.h_hdc, 1)} %</strong></div>
            <div class="flasher-config-row"><span>BME280</span><strong>${formatFloat(live.h_bme280, 1)} %</strong></div>
            <div class="flasher-config-row"><span>BME680</span><strong>${formatFloat(live.h_bme680, 1)} %</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Давление и высота</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>BME280</span><strong>${escapeHtml(String(live.p_bme280_mmhg ?? '—'))} мм рт.ст. / ${formatFloat(live.p_bme280_kpa, 2)} кПа</strong></div>
            <div class="flasher-config-row"><span>BME680</span><strong>${escapeHtml(String(live.p_bme680_mmhg ?? '—'))} мм рт.ст. / ${formatFloat(live.p_bme680_kpa, 2)} кПа</strong></div>
            <div class="flasher-config-row"><span>Высота BME280</span><strong>${escapeHtml(String(live.alt_bme280 ?? '—'))} м</strong></div>
            <div class="flasher-config-row"><span>Высота BME680</span><strong>${escapeHtml(String(live.alt_bme680 ?? '—'))} м</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Качество воздуха</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>BME680 VOC</span><strong>${escapeHtml(String(live.voc ?? '—'))} кОм</strong></div>
            <div class="flasher-config-row"><span>BME680 IAQ</span><strong>${escapeHtml(String(live.iaq_bme ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>BME680 CO₂ экв.</span><strong>${escapeHtml(String(live.co2_bme ?? '—'))} ppm</strong></div>
            <div class="flasher-config-row"><span>ZMOD4410 TVOC / IAQ</span><strong>${escapeHtml(String(live.tvoc_z ?? '—'))} / ${escapeHtml(String(live.iaq_z ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>ZMOD4410 eCO₂ / EtOH</span><strong>${formatFloat(live.eco2_z, 0)} ppm / ${escapeHtml(String(live.etoh_z ?? '—'))}</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Присутствие</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Освещённость</span><strong>${escapeHtml(String(live.light ?? '—'))} %</strong></div>
            <div class="flasher-config-row"><span>Присутствие по DI</span><strong>${escapeHtml(String(live.presence ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>Присутствие LD2412</span><strong>${escapeHtml(String(live.presence_ld2412 ?? '—'))}</strong></div>
            <div class="flasher-config-row"><span>Дальность (движ./стат./общ.)</span><strong>${escapeHtml(String(live.ld_dist_mov ?? '—'))} / ${escapeHtml(String(live.ld_dist_still ?? '—'))} / ${escapeHtml(String(live.ld_dist ?? '—'))} см</strong></div>
          </div>
        </section>
      </div>
    `;
  }

  function renderDtvSettingsTab(snap) {
    const settings = (snap.dtv && snap.dtv.settings) || {};
    const calib = Array.isArray(settings.calibration_offsets) ? settings.calibration_offsets : [];
    const ma = Array.isArray(settings.moving_average_depths) ? settings.moving_average_depths : [];
    const calibLabels = [
      'DS18B20', 'MCP9808', 'HDC1080 T', 'BME280 T', 'BME680 T', 'NTC/PT1000', 'HDC1080 RH', 'BME280 RH', 'BME680 RH',
    ];
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Основные настройки</h4>
          <div class="flasher-config-form">
            <label for="cfg-dtv-ext">Источник внешней температуры</label>
            <select id="cfg-dtv-ext">
              <option value="0" ${Number(settings.ext_temp_select) === 0 ? 'selected' : ''}>NTC10K</option>
              <option value="1" ${Number(settings.ext_temp_select) === 1 ? 'selected' : ''}>PT1000</option>
            </select>

            <label for="cfg-dtv-delay">Задержка выключения присутствия, с</label>
            <input id="cfg-dtv-delay" type="number" min="0" max="65535" value="${escapeHtml(String(settings.presence_off_delay ?? 0))}" />

            <label class="checkbox-line"><input id="cfg-dtv-buzzer" type="checkbox" ${settings.buzzer_on ? 'checked' : ''} /> Пищалка (coil 1)</label>
            <label class="checkbox-line"><input id="cfg-dtv-leds" type="checkbox" ${settings.leds_on ? 'checked' : ''} /> Все светодиоды (coil 2)</label>
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-dtv-main-save-btn">Применить</button>
          </div>
        </section>

        <section class="flasher-config-card">
          <h4>Калибровочные смещения</h4>
          <div class="flasher-config-list">
            ${calibLabels.map((label, idx) => `
              <div class="flasher-config-inline-fields">
                <label for="cfg-dtv-cal-${idx}">${escapeHtml(label)}</label>
                <input id="cfg-dtv-cal-${idx}" type="number" value="${escapeHtml(String(calib[idx] ?? 0))}" />
              </div>
            `).join('')}
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-dtv-cal-save-btn">Сохранить смещения</button>
          </div>
        </section>

        <section class="flasher-config-card">
          <h4>Скользящее среднее</h4>
          <div class="flasher-config-list">
            ${Array.from({ length: 9 }).map((_, idx) => `
              <div class="flasher-config-inline-fields">
                <label for="cfg-dtv-ma-${idx}">Слот ${idx + 1}</label>
                <input id="cfg-dtv-ma-${idx}" type="number" min="1" max="50" value="${escapeHtml(String(ma[idx] ?? 1))}" />
              </div>
            `).join('')}
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-dtv-ma-save-btn">Сохранить фильтрацию</button>
          </div>
        </section>
      </div>
    `;
  }

  /* ── Приточная установка Carel (c.pCOmini / uAria) ───────────────────────
     Окно повторяет страницы десктопного прошивальщика (ветка `carel`,
     specialized_pages_carel.py): Сведения · Сеть · Установка · Входы/выходы ·
     Тревоги. Карта регистров и смысл значений живут в общем пакете и в демоне
     (docs/contracts/carel-ahu.md) — здесь только показ и имена команд.
     Живые значения обновляются НА МЕСТЕ (patchCarelLiveReadouts): полный
     ре-рендер тела раз в секунду сбрасывал бы прокрутку и ввод оператора. */

  /** Токены сигнатур Carel — зеркало sa02m_carel.carel_ahu._SIG_CARELS. */
  const CAREL_SIGNATURE_TOKENS = [
    'CRSTDRAHAQ', 'CRKRFAHAQ', 'CRSTDM_AHU', 'CRSTDM', 'UARIA',
    'C.PCOMINI', 'CPCOMINI', 'C.PCO',
  ];
  /** Действия окна — зеркало device_config.CAREL_ACTIONS: всё вне списка демон отвергает. */
  const CAREL_ACTIONS = [
    'start', 'stop', 'alarm_reset', 'net_enable', 'sys_mode',
    'sp_winter', 'sp_summer', 'fan_supply', 'fan_exhaust', 'fan_step',
  ];
  const CAREL_FAMILY_CRST = 'crst';
  const CAREL_FAMILY_UARIA = 'uaria';
  /** Имя вкладки входов/выходов = device_config.CAREL_IO_TAB (дорогой блок по запросу). */
  const CAREL_IO_TAB = 'carel_io';
  const CAREL_IO_COLUMNS = [
    { key: 'io_u', id: 'cfg-carel-io-u', title: 'Аналоговые входы (датчики)', bits: false },
    { key: 'io_no', id: 'cfg-carel-io-no', title: 'Дискретные выходы (реле)', bits: true },
    { key: 'io_di', id: 'cfg-carel-io-di', title: 'Цифровые входы', bits: true },
    { key: 'io_ao', id: 'cfg-carel-io-ao', title: 'Аналоговые выходы', bits: false },
  ];
  const CAREL_SYS_MODES = [
    { value: 0, label: '0 - Выключено' },
    { value: 1, label: '1 - Включено' },
    { value: 2, label: '2 - Расписание' },
    { value: 3, label: '3 - Цифровой вход' },
    { value: 4, label: '4 - Расписание и цифровой вход' },
    { value: 5, label: '5 - th-Tune' },
  ];
  /* Состояние установки: цвет берётся из штатного словаря badge-* (обе темы уже
     проверены на контраст) — новых цветов окно не заводит. */
  const CAREL_PLANT_STATES = {
    alarm: { badge: 'badge-err', label: 'Авария' },
    run: { badge: 'badge-ok', label: 'Работает' },
    stop: { badge: 'badge-unk', label: 'Остановлена' },
  };
  const CAREL_VARIANT_LABELS = { B: 'Basic', E: 'Enhanced', H: 'HighEnd' };

  function signatureLooksLikeCarel(sig) {
    const n = String(stripBootloaderSignatureSuffix(sig) || '').trim().toUpperCase().replace(/\s/g, '');
    if (!n || n === 'NONE' || n === '—' || n === '-' || n === '?') return false;
    return CAREL_SIGNATURE_TOKENS.some(function (key) { return n.includes(key); });
  }

  /** Зеркало carel_ahu.family_from_signature — нужно до первого снимка (заглушка окна). */
  function carelFamilyFromSignature(sig) {
    const n = String(stripBootloaderSignatureSuffix(sig) || '').trim().toUpperCase().replace(/\s/g, '');
    return (n.includes('UARIA') || n.includes('CRSTDM')) ? CAREL_FAMILY_UARIA : CAREL_FAMILY_CRST;
  }

  function carelModelLabel(sig) {
    return carelFamilyFromSignature(sig) === CAREL_FAMILY_UARIA ? 'uAria' : 'c.pCOmini';
  }

  function carelVariantLabel(variant) {
    return CAREL_VARIANT_LABELS[String(variant || '').trim().toUpperCase()] || '';
  }

  function carelFamily(snap) {
    const fam = String((snap && snap.family) || '').trim().toLowerCase();
    if (fam === CAREL_FAMILY_UARIA || fam === CAREL_FAMILY_CRST) return fam;
    return carelFamilyFromSignature((snap && snap.info && snap.info.signature) || '');
  }

  function carelIsUaria(snap) { return carelFamily(snap) === CAREL_FAMILY_UARIA; }

  function carelBlock(snap) { return (snap && snap.carel) || {}; }

  /** Число с единицей; прочерк — когда датчика нет (float32 NaN) или блок не
      прочитан. null через formatFloat дал бы «0.0 °C» — правдоподобное
      показание вместо отсутствия данных (контракт §5, случай IR3 без датчика). */
  function carelValueText(value, digits, unit) {
    if (value == null || value === '') return '—';
    const txt = formatFloat(value, digits == null ? 1 : digits);
    if (txt === '—' || !unit) return txt;
    return txt + ' ' + unit;
  }

  /** Значение поля ввода: непрочитанная уставка оставляет поле пустым, а не «0.0». */
  function carelInputValue(value, digits) {
    if (value == null || value === '') return '';
    const txt = formatFloat(value, digits == null ? 1 : digits);
    return txt === '—' ? '' : txt;
  }

  function carelOnOffText(value) {
    if (value == null) return '—';
    return value ? 'Вкл' : 'Выкл';
  }

  /* uAria season = coil 17 (heat_cool: 0 heating = ЗИМА, 1 cooling = ЛЕТО), the
     value MQTT and the devices card show. HR34 (season_code) is only the
     season-change mode; its label comes from sa02m_carel.UARIA_SEASON via the
     daemon (season_mode_text), the code is the fallback for an older daemon. */
  function carelUariaSeasonText(c) {
    if (!c || c.heat_cool == null) return '—';
    return c.heat_cool ? 'ЛЕТО' : 'ЗИМА';
  }

  function carelUariaSeasonModeText(c) {
    if (!c) return '—';
    if (c.season_mode_text) return String(c.season_mode_text);
    return c.season_code == null ? '—' : String(c.season_code);
  }

  function carelPlantStateView(carel) {
    const c = carel || {};
    // Молчащий ПЛК — это не «остановлена»: отсутствие ответа нельзя показывать состоянием.
    if (c.answered === false) return { badge: 'badge-unk', label: 'Нет данных' };
    const st = String(c.plant_state || '').trim().toLowerCase();
    return CAREL_PLANT_STATES[st] || CAREL_PLANT_STATES.stop;
  }

  /** Строка тревоги «код — текст»; EN-текст приходит из карты — наблюдатель i18n его не знает. */
  function carelAlarmLine(row) {
    const r = row || {};
    const en = window.sa02mI18n && window.sa02mI18n.lang === 'en' && r.text_en;
    return String(r.code || '') + ' — ' + String(en || r.text || '');
  }

  /* Ответ на команду и такт опроса вне вкладки «Входы/выходы» дорогой блок io_*
     не читают: без переноса прошлых колонок таблицы моргали бы пустыми. */
  const CAREL_IO_KEYS = ['io_u', 'io_no', 'io_di', 'io_ao'];

  function mergeCarelSnapshot(prev, snap) {
    if (!snap || snap.kind !== 'carel') return snap;
    const pc = (prev && prev.kind === 'carel' && prev.carel) || null;
    if (!pc) return snap;
    const carel = Object.assign({}, snap.carel || {});
    CAREL_IO_KEYS.forEach(function (key) {
      if (!Array.isArray(carel[key]) && Array.isArray(pc[key])) carel[key] = pc[key];
    });
    return Object.assign({}, snap, { carel: carel });
  }

  /* ── Светодиодная лента LED (RGBW_WS2812, тип 120) ───────────────────────
     Окно живёт в static/js/flasher/led.js (план led-window-1.0.6.40, F1): здесь
     только шов — опознание строки, заголовки и маршрутизация вкладок/тела/
     живых значений/событий в модуль через инжектированный host. */

  /** Алиасы сигнатуры — зеркало sa02m_led.led_mb2ws_map.LED_SIGNATURE_ALIASES /
      LED_SIGNATURE_PREFIXES (тест окна читает их из карты). «LED» и «RGBW» —
      только точное совпадение: трёхбуквенный префикс LED крадёт сигнатуру
      Wiren Board «ledGe» (урок зафиксирован в карте). */
  const LED_SIGNATURE_ALIASES = ['RGBW_WS2812', 'RGBWWS2812', 'RGBW', 'LED'];
  const LED_SIGNATURE_PREFIXES = ['RGBW_WS2812', 'RGBWWS2812'];

  /** `n` — уже нормализованная сигнатура (без суффикса загрузчика, верхний регистр). */
  function signatureLooksLikeLed(n) {
    if (!n) return false;
    if (LED_SIGNATURE_ALIASES.indexOf(n) >= 0) return true;
    return LED_SIGNATURE_PREFIXES.some(function (p) { return n.startsWith(p); });
  }

  let _ledWindow = null;
  /** Ленивая сборка модуля окна ленты: host — всё, что модулю нужно от модалки,
      без обращений к window.* из него. */
  function ledWindow() {
    if (!_ledWindow) {
      if (!window.sa02mLedWindow) throw new Error('flasher/led.js не загружен');
      _ledWindow = window.sa02mLedWindow.create({
        escapeHtml: escapeHtml,
        t: t,
        toast: toast,
        configModalEl: configModalEl,
        configApi: configApi,
        getState: function () { return state; },
        applyConfigSnapshot: applyConfigSnapshot,
        setConfigBusy: setConfigBusy,
        setConfigBanner: setConfigBanner,
        clampInt: clampInt,
        currentConfigDevice: currentConfigDevice,
        currentPort: function () { return $('flasher-port').value; },
        // Команда ленты поднимает поколение опроса: ответ уже летящего опроса
        // не перерисует старое состояние поверх снимка, пришедшего с записью.
        invalidatePolls: function () { ++_configPollSeq; },
        activeElement: function () { return document.activeElement; },
        refreshSnapshot: function () { return refreshConfigSnapshot(false, 'full'); },
      });
    }
    return _ledWindow;
  }

  let _mtdWindow = null;
  /** The MTD presence-sensor window (flasher/mtd.js): the same host as the
      strip, plus the bridge relink helper and the link-change close guard. */
  function mtdWindow() {
    if (!_mtdWindow) {
      if (!window.sa02mMtdWindow) throw new Error('flasher/mtd.js не загружен');
      if (!window.sa02mBridgeRelink) throw new Error('flasher/bridge-relink.js не загружен');
      _mtdWindow = window.sa02mMtdWindow.create({
        escapeHtml: escapeHtml,
        t: t,
        toast: toast,
        configModalEl: configModalEl,
        configApi: configApi,
        getState: function () { return state; },
        applyConfigSnapshot: applyConfigSnapshot,
        setConfigBusy: setConfigBusy,
        setConfigBanner: setConfigBanner,
        setLinkBusy: setConfigLinkBusy,
        currentConfigDevice: currentConfigDevice,
        currentPort: function () { return $('flasher-port').value; },
        invalidatePolls: function () { ++_configPollSeq; },
        activeElement: function () { return document.activeElement; },
        refreshSnapshot: function () { return refreshConfigSnapshot(false, 'full'); },
        bridge: window.sa02mBridgeRelink,
      });
    }
    return _mtdWindow;
  }

  /** Тег вкладки для опроса. У ленты фоновый `panel`-такт вкладки «Сцена» идёт с
      пустым active_tab — базовый блок 400×20 без дорогих 453/494/516/640
      (план §8, F6); остальные виды шлют вкладку как есть. */
  function configPollActiveTab(detail) {
    const tab = state.configTab || '';
    if (state.configSnapshot && state.configSnapshot.kind === 'led') return ledWindow().pollTab(tab, detail);
    return tab;
  }

  function renderCarelInfoTab(snap) {
    const info = snap.info || {};
    const line = info.line || {};
    const model = info.model || carelModelLabel(info.signature);
    const variant = info.variant_label || carelVariantLabel(info.carel_variant);
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Устройство</h4>
          <dl class="flasher-config-kv">
            <div><dt>Тип</dt><dd>Приточная установка</dd></div>
            <div><dt>Модель</dt><dd>${escapeHtml(model)}</dd></div>
            <div><dt>Сигнатура</dt><dd class="mono">${escapeHtml(info.signature || '—')}</dd></div>
            <div><dt>Версия ПО</dt><dd>${escapeHtml(info.app_version || '—')}</dd></div>
            <div><dt>Исполнение платы</dt><dd>${escapeHtml(variant || '—')}</dd></div>
            <div><dt>Адрес</dt><dd>${escapeHtml(String(info.address ?? '—'))}</dd></div>
          </dl>
        </section>
        <section class="flasher-config-card">
          <h4>Линия</h4>
          <dl class="flasher-config-kv">
            <div><dt>Скорость</dt><dd>${escapeHtml(String(line.baudrate || '—'))}</dd></div>
            <div><dt>Чётность</dt><dd>${escapeHtml(String(line.parity || '—'))}</dd></div>
            <div><dt>Стоп-биты</dt><dd>${escapeHtml(String(line.stopbits || '—'))}</dd></div>
          </dl>
          <div class="flasher-config-note">Установка опознана ответом на функцию 17: регистр сигнатуры и серийный номер контроллер Carel не отдаёт.</div>
        </section>
      </div>
    `;
  }

  function renderCarelNetworkTab(snap) {
    const net = snap.network || {};
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Параметры линии (только просмотр)</h4>
          <dl class="flasher-config-kv">
            <div><dt>Modbus-адрес</dt><dd>${escapeHtml(String(net.address ?? '—'))}</dd></div>
            <div><dt>Скорость</dt><dd>${escapeHtml(String(net.baudrate || '—'))}</dd></div>
            <div><dt>Чётность</dt><dd>${escapeHtml(String(net.parity || '—'))}</dd></div>
            <div><dt>Стоп-биты</dt><dd>${escapeHtml(String(net.stopbits || '—'))}</dd></div>
          </dl>
          <div class="flasher-config-note">Смена адреса, скорости и чётности — с клавиатуры контроллера (uAria: Hd01–Hd03; c.pCO: Sv01–Sv05), по сети эти параметры не пишутся.</div>
        </section>
      </div>
    `;
  }

  function renderCarelPlantTab(snap) {
    const c = carelBlock(snap);
    const view = carelPlantStateView(c);
    const settings = carelIsUaria(snap) ? renderCarelUariaSettings(c) : renderCarelCrstSettings(c);
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Состояние установки</h4>
          <div class="cfg-carel-head">
            <span class="badge ${view.badge}" id="cfg-carel-state-badge">${escapeHtml(view.label)}</span>
            <span class="cfg-carel-status" id="cfg-carel-status">${escapeHtml(c.unit_status_text || '—')}</span>
          </div>
          <dl class="flasher-config-kv">
            <div><dt>Температура притока</dt><dd id="cfg-carel-sat">${escapeHtml(carelValueText(c.sat, 1, '°C'))}</dd></div>
            <div><dt>Температура обратной воды</dt><dd id="cfg-carel-rwt">${escapeHtml(carelValueText(c.rwt, 1, '°C'))}</dd></div>
            <div><dt>Температура снаружи</dt><dd id="cfg-carel-oat">${escapeHtml(carelValueText(c.oat, 1, '°C'))}</dd></div>
            <div><dt>Клапан нагрева</dt><dd id="cfg-carel-valve">${escapeHtml(carelValueText(c.valve, 1, '%'))}</dd></div>
            <div><dt>Насос</dt><dd id="cfg-carel-pump">${escapeHtml(carelOnOffText(c.pump))}</dd></div>
          </dl>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-carel-start-btn">Пуск</button>
            <button class="btn btn-warn" type="button" id="cfg-carel-stop-btn">Стоп</button>
          </div>
        </section>
        ${settings}
      </div>
    `;
  }

  function renderCarelCrstSettings(c) {
    return `
      <section class="flasher-config-card">
        <h4>Команды и уставки</h4>
        <div class="flasher-config-form">
          <label for="cfg-carel-sp-w">Уставка зима, °C</label>
          <input id="cfg-carel-sp-w" data-carel-field="1" type="number" step="0.1" min="0" max="99" value="${escapeHtml(carelInputValue(c.sp_w, 1))}" />

          <label for="cfg-carel-sp-s">Уставка лето, °C</label>
          <input id="cfg-carel-sp-s" data-carel-field="1" type="number" step="0.1" min="0" max="99" value="${escapeHtml(carelInputValue(c.sp_s, 1))}" />

          <label for="cfg-carel-fan-sa">Приточный вентилятор, %</label>
          <input id="cfg-carel-fan-sa" data-carel-field="1" type="number" step="0.1" min="20" max="100" value="${escapeHtml(carelInputValue(c.fan_sa, 1))}" />

          <label for="cfg-carel-fan-ea">Вытяжной вентилятор, %</label>
          <input id="cfg-carel-fan-ea" data-carel-field="1" type="number" step="0.1" min="20" max="100" value="${escapeHtml(carelInputValue(c.fan_ea, 1))}" />

          <label for="cfg-carel-sys-mode">Режим работы</label>
          <select id="cfg-carel-sys-mode" data-carel-field="1">
            ${CAREL_SYS_MODES.map(m => `<option value="${m.value}" ${Number(c.mode) === m.value ? 'selected' : ''}>${escapeHtml(m.label)}</option>`).join('')}
          </select>

          <label class="checkbox-line"><input id="cfg-carel-ma18" type="checkbox" ${c.ma18 ? 'checked' : ''} /> Разрешение запуска по сети</label>
        </div>
        <dl class="flasher-config-kv" style="margin-top:12px">
          <div><dt>Запуск по сети</dt><dd id="cfg-carel-run">${escapeHtml(carelOnOffText(c.bms_run))}</dd></div>
        </dl>
        <div class="flasher-config-actions">
          <button class="btn btn-primary" type="button" id="cfg-carel-apply-btn">Применить уставки</button>
        </div>
      </section>
    `;
  }

  function renderCarelUariaSettings(c) {
    return `
      <section class="flasher-config-card">
        <h4>Команды и уставки</h4>
        <div class="flasher-config-form">
          <label for="cfg-carel-sp-w">Уставка зима, °C</label>
          <input id="cfg-carel-sp-w" data-carel-field="1" type="number" step="0.1" min="0" max="50" value="${escapeHtml(carelInputValue(c.sp_w, 1))}" />

          <label for="cfg-carel-sp-s">Уставка лето, °C</label>
          <input id="cfg-carel-sp-s" data-carel-field="1" type="number" step="0.1" min="0" max="50" value="${escapeHtml(carelInputValue(c.sp_s, 1))}" />

          <label for="cfg-carel-fan-step">Скорость вентилятора, ступень 1–10</label>
          <input id="cfg-carel-fan-step" data-carel-field="1" type="number" step="1" min="1" max="10" value="${escapeHtml(c.fan_sp == null ? '' : String(c.fan_sp))}" />

          <label class="checkbox-line"><input id="cfg-carel-gs04" type="checkbox" ${c.gs04 ? 'checked' : ''} /> Разрешение запуска по сети</label>
        </div>
        <dl class="flasher-config-kv" style="margin-top:12px">
          <div><dt>Запуск по сети</dt><dd id="cfg-carel-run">${escapeHtml(carelOnOffText(c.uaria_run))}</dd></div>
          <div><dt>Расчётный выход</dt><dd id="cfg-carel-fan-calc">${escapeHtml(carelValueText(c.fan_calc, 1, '%'))}</dd></div>
          <div><dt>Фактический AO</dt><dd id="cfg-carel-fan-act">${escapeHtml(carelValueText(c.fan, 1, '%'))}</dd></div>
          <div><dt>Локальный терминал (только чтение)</dt><dd id="cfg-carel-local">${escapeHtml(carelOnOffText(c.uaria_local))}</dd></div>
          <div><dt>Сезон</dt><dd id="cfg-carel-season">${escapeHtml(carelUariaSeasonText(c))}</dd></div>
          <div><dt>Режим сезона</dt><dd id="cfg-carel-season-mode">${escapeHtml(carelUariaSeasonModeText(c))}</dd></div>
        </dl>
        <div class="flasher-config-actions">
          <button class="btn btn-primary" type="button" id="cfg-carel-apply-btn">Применить уставки</button>
        </div>
        <div class="flasher-config-note">Карта uAria: пуск по сети — катушка 0, разрешение Gs04 — катушка 13. Катушка 30 принадлежит местному терминалу и из веба не пишется.</div>
      </section>
    `;
  }

  function carelIoRowHead(row) {
    const r = row || {};
    return String(r.tag || '') + (r.text ? ' · ' + String(r.text) : '');
  }

  function carelIoRowValue(row, bits) {
    const r = row || {};
    return bits ? carelOnOffText(r.on == null ? null : !!r.on) : carelValueText(r.value, 1, r.unit || '');
  }

  function carelIoRowHtml(row, bits) {
    const cls = bits && (row || {}).on === true ? ' do-state-on' : '';
    return `<div class="flasher-config-row"><span>${escapeHtml(carelIoRowHead(row))}</span>` +
      `<strong class="do-state-value${cls}">${escapeHtml(carelIoRowValue(row, bits))}</strong></div>`;
  }

  function renderCarelIoTab(snap) {
    const c = carelBlock(snap);
    const cards = CAREL_IO_COLUMNS.map(function (col) {
      const rows = c[col.key] || [];
      const body = rows.length
        ? rows.map(r => carelIoRowHtml(r, col.bits)).join('')
        : '<div class="flasher-empty">Нет данных</div>';
      return `
        <section class="flasher-config-card">
          <h4>${escapeHtml(col.title)}</h4>
          <div class="flasher-config-list" id="${col.id}">${body}</div>
        </section>
      `;
    }).join('');
    return `
      <div class="flasher-config-grid">${cards}</div>
      <div class="flasher-config-note" style="margin-top:14px">Показаны переменные карты BMS (функции программы) и их состояния. Номера клемм по сети не читаются: что на какой клемме — задаёт мастер входов/выходов в самом ПЛК.</div>
    `;
  }

  function renderCarelAlarmsTab(snap) {
    const alarms = carelBlock(snap).alarms || [];
    const lines = alarms.length ? alarms.map(carelAlarmLine) : ['Нет активных тревог'];
    const body = lines.map(line => `<div class="flasher-config-row"><span>${escapeHtml(line)}</span></div>`).join('');
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Тревоги</h4>
          <div class="flasher-config-list" id="cfg-carel-alarms">${body}</div>
          <div class="flasher-config-actions">
            <button class="btn btn-warn" type="button" id="cfg-carel-alarm-reset-btn">Сброс тревог</button>
          </div>
          <div class="flasher-config-note">Сброс — импульс 5 с на катушке сброса. Активный контакт на клемме останется, и тревога поднимется снова.</div>
        </section>
      </div>
    `;
  }

  function renderCarelTab(snap) {
    const tab = state.configTab;
    if (tab === 'network') return renderCarelNetworkTab(snap);
    if (tab === 'plant') return renderCarelPlantTab(snap);
    if (tab === CAREL_IO_TAB) return renderCarelIoTab(snap);
    if (tab === 'alarms') return renderCarelAlarmsTab(snap);
    return renderCarelInfoTab(snap);
  }

  /* ── Живые значения на месте ───────────────────────────────────────────── */

  /** Поле оператора не затирается: под фокусом или с несохранённой правкой. */
  function carelFieldHolds(el, activeEl) {
    if (!el) return true;
    if (activeEl && el === activeEl) return true;
    return !!(el.dataset && el.dataset.carelDirty === '1');
  }

  function carelSetText(id, text) {
    const el = configModalEl(id);
    if (el) el.textContent = text;
  }

  function carelSetField(id, value, activeEl) {
    const el = configModalEl(id);
    if (!el || carelFieldHolds(el, activeEl)) return;
    if (el.type === 'checkbox') el.checked = !!value;
    else el.value = value;
  }

  /** Строки списка обновляются на месте: innerHTML сбрасывал бы прокрутку. */
  function carelPatchList(id, rows, bits) {
    const host = configModalEl(id);
    if (!host) return;
    const list = rows || [];
    if (host.children.length !== list.length) {
      host.innerHTML = list.length
        ? list.map(r => carelIoRowHtml(r, bits)).join('')
        : '<div class="flasher-empty">Нет данных</div>';
      return;
    }
    list.forEach(function (row, idx) {
      const node = host.children[idx];
      if (!node) return;
      const head = node.querySelector('span');
      const val = node.querySelector('strong');
      const headText = carelIoRowHead(row);
      if (head && head.textContent !== headText) head.textContent = headText;
      if (!val) return;
      const text = carelIoRowValue(row, bits);
      if (val.textContent !== text) val.textContent = text;
      val.classList.toggle('do-state-on', !!(bits && (row || {}).on === true));
    });
  }

  function carelPatchAlarms(snap) {
    const host = configModalEl('cfg-carel-alarms');
    if (!host) return;
    const alarms = carelBlock(snap).alarms || [];
    const lines = alarms.length ? alarms.map(carelAlarmLine) : ['Нет активных тревог'];
    if (host.children.length !== lines.length) {
      host.innerHTML = lines.map(line => `<div class="flasher-config-row"><span>${escapeHtml(line)}</span></div>`).join('');
      return;
    }
    lines.forEach(function (line, idx) {
      const node = host.children[idx];
      const span = node && node.querySelector('span');
      if (span && span.textContent !== line) span.textContent = line;
    });
  }

  function patchCarelLiveReadouts(snap) {
    const c = carelBlock(snap);
    const activeEl = typeof document !== 'undefined' ? document.activeElement : null;
    const view = carelPlantStateView(c);
    const badge = configModalEl('cfg-carel-state-badge');
    if (badge) {
      badge.textContent = view.label;
      badge.className = 'badge ' + view.badge;
    }
    carelSetText('cfg-carel-status', c.unit_status_text || '—');
    carelSetText('cfg-carel-sat', carelValueText(c.sat, 1, '°C'));
    carelSetText('cfg-carel-rwt', carelValueText(c.rwt, 1, '°C'));
    carelSetText('cfg-carel-oat', carelValueText(c.oat, 1, '°C'));
    carelSetText('cfg-carel-valve', carelValueText(c.valve, 1, '%'));
    carelSetText('cfg-carel-pump', carelOnOffText(c.pump));
    carelSetField('cfg-carel-sp-w', carelInputValue(c.sp_w, 1), activeEl);
    carelSetField('cfg-carel-sp-s', carelInputValue(c.sp_s, 1), activeEl);
    if (carelIsUaria(snap)) {
      carelSetText('cfg-carel-run', carelOnOffText(c.uaria_run));
      carelSetText('cfg-carel-fan-calc', carelValueText(c.fan_calc, 1, '%'));
      carelSetText('cfg-carel-fan-act', carelValueText(c.fan, 1, '%'));
      carelSetText('cfg-carel-local', carelOnOffText(c.uaria_local));
      carelSetText('cfg-carel-season', carelUariaSeasonText(c));
      carelSetText('cfg-carel-season-mode', carelUariaSeasonModeText(c));
      carelSetField('cfg-carel-fan-step', c.fan_sp == null ? '' : String(c.fan_sp), activeEl);
      carelSetField('cfg-carel-gs04', !!c.gs04, activeEl);
    } else {
      carelSetText('cfg-carel-run', carelOnOffText(c.bms_run));
      carelSetField('cfg-carel-fan-sa', carelInputValue(c.fan_sa, 1), activeEl);
      carelSetField('cfg-carel-fan-ea', carelInputValue(c.fan_ea, 1), activeEl);
      carelSetField('cfg-carel-sys-mode', c.mode == null ? '' : String(c.mode), activeEl);
      carelSetField('cfg-carel-ma18', !!c.ma18, activeEl);
    }
    CAREL_IO_COLUMNS.forEach(function (col) {
      if (Array.isArray(c[col.key])) carelPatchList(col.id, c[col.key], col.bits);
    });
    carelPatchAlarms(snap);
  }

  /* ── Команды установки ─────────────────────────────────────────────────── */

  function clearCarelDirtyFields() {
    const body = configModalEl('flasher-config-body');
    if (!body) return;
    body.querySelectorAll('[data-carel-field]').forEach(function (el) {
      delete el.dataset.carelDirty;
    });
  }

  async function carelCommand(action, params, okMsg) {
    if (CAREL_ACTIONS.indexOf(action) < 0) return;
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    setConfigBusy(true);
    try {
      const snap = await configApi('/device_config/carel_write', {
        port, device: dev, action, params: params || {},
      });
      clearCarelDirtyFields();
      applyConfigSnapshot(snap, false);
      if (okMsg) toast(okMsg, 'success');
    } catch (err) {
      setConfigBanner('Команда установки: ' + err.message, 'error');
      toast('Установка Carel: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  function carelFieldNumber(id) {
    const el = configModalEl(id);
    if (!el) return null;
    const raw = String(el.value == null ? '' : el.value).replace(',', '.').trim();
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function carelFieldChanged(id) {
    const el = configModalEl(id);
    return !!(el && el.dataset && el.dataset.carelDirty === '1');
  }

  /** Какие уставки уходят по «Применить»: только правленые поля с числом — лишняя
      запись это лишний сеанс на общей линии, а у uAria ещё и два слова float32. */
  function carelSetpointPlan(uaria, changed, number) {
    const plan = [
      { id: 'cfg-carel-sp-w', action: 'sp_winter' },
      { id: 'cfg-carel-sp-s', action: 'sp_summer' },
    ];
    if (uaria) plan.push({ id: 'cfg-carel-fan-step', action: 'fan_step' });
    else plan.push({ id: 'cfg-carel-fan-sa', action: 'fan_supply' }, { id: 'cfg-carel-fan-ea', action: 'fan_exhaust' });
    return plan
      .filter(item => changed(item.id) && number(item.id) != null)
      .map(item => ({ action: item.action, value: number(item.id) }));
  }

  async function applyCarelSetpoints() {
    const todo = carelSetpointPlan(carelIsUaria(state.configSnapshot), carelFieldChanged, carelFieldNumber);
    if (!todo.length) {
      toast('Изменённых уставок нет', 'info');
      return;
    }
    for (const item of todo) {
      await carelCommand(item.action, { value: item.value }, '');
    }
    toast('Уставки сохранены', 'success');
  }

  function carelStartPlant() {
    const msg = carelIsUaria(state.configSnapshot)
      ? 'Включить установку по сети (катушка 0)? При необходимости сначала уйдёт Gs04 (катушка 13).'
      : 'Включить установку по сети? Уйдут разрешение Ma18 (катушка 130), выдержка и команда пуска (катушка 65).';
    if (!confirm(t(msg))) return;
    carelCommand('start', {}, 'Команда пуска отправлена');
  }

  function carelStopPlant() {
    if (!confirm(t('Остановить установку по сети? Разрешение сети и пуск с клавиатуры остаются как были.'))) return;
    carelCommand('stop', {}, 'Команда останова отправлена');
  }

  function carelResetAlarms() {
    if (!confirm(t('Сбросить тревоги (импульс 5 с)? Активный контакт на клемме останется.'))) return;
    carelCommand('alarm_reset', {}, 'Сброс тревог выполнен');
  }

  function wireCarelBodyEvents(body) {
    body.querySelectorAll('[data-carel-field]').forEach(function (el) {
      const mark = function () { el.dataset.carelDirty = '1'; };
      el.addEventListener('input', mark);
      el.addEventListener('change', mark);
    });
    const start = body.querySelector('#cfg-carel-start-btn');
    if (start) start.addEventListener('click', carelStartPlant);
    const stop = body.querySelector('#cfg-carel-stop-btn');
    if (stop) stop.addEventListener('click', carelStopPlant);
    const apply = body.querySelector('#cfg-carel-apply-btn');
    if (apply) apply.addEventListener('click', applyCarelSetpoints);
    const reset = body.querySelector('#cfg-carel-alarm-reset-btn');
    if (reset) reset.addEventListener('click', carelResetAlarms);
    // Разрешение сети и режим работы уходят сразу, а не по «Применить»: пуск
    // оценивается ПЛК по уже защёлкнутому разрешению (контракт §4).
    const ma18 = body.querySelector('#cfg-carel-ma18');
    if (ma18) ma18.addEventListener('change', () => carelCommand('net_enable', { enable: ma18.checked }, 'Разрешение сети записано'));
    const gs04 = body.querySelector('#cfg-carel-gs04');
    if (gs04) gs04.addEventListener('change', () => carelCommand('net_enable', { enable: gs04.checked }, 'Разрешение сети записано'));
    const mode = body.querySelector('#cfg-carel-sys-mode');
    if (mode) mode.addEventListener('change', () => carelCommand('sys_mode', { value: clampInt(mode.value, 0, 5, 0) }, 'Режим работы записан'));
  }

  function renderConfigBody() {
    const host = configModalEl('flasher-config-body');
    if (!host) return;
    const snap = state.configSnapshot;
    if (!snap) {
      host.innerHTML = '<div class="flasher-empty">Загрузка настроек</div>';
      return;
    }
    let html = '';
    if (snap.kind === 'carel') html = renderCarelTab(snap);
    else if (snap.kind === 'mtd') html = mtdWindow().render(snap, state.configTab);
    else if (state.configTab === 'network') html = renderNetworkTab(snap);
    else if (snap.kind === 'led' && /^led_/.test(state.configTab)) html = ledWindow().render(snap, state.configTab);
    else if (state.configTab === 'relay' && snap.kind === 'mr') html = renderModuleRelayTab(snap);
    else if (snap.kind === 'mr' && /^do_\d+$/.test(state.configTab)) html = renderModuleDoTab(snap, parseInt(state.configTab.split('_')[1], 10));
    else if (snap.kind === 'mr' && /^di_\d+$/.test(state.configTab)) html = renderModuleDiTab(snap, parseInt(state.configTab.split('_')[1], 10));
    else if (snap.kind === 'mr' && /^ao_\d+$/.test(state.configTab)) html = renderModuleAoTab(snap, parseInt(state.configTab.split('_')[1], 10));
    else if (snap.kind === 'mr' && /^ai_\d+$/.test(state.configTab)) html = renderModuleAiTab(snap, parseInt(state.configTab.split('_')[1], 10));
    else if (state.configTab === 'measures' && snap.kind === 'ce') html = renderCeMeasuresTab(snap);
    else if (state.configTab === 'settings' && snap.kind === 'ce') html = renderCeSettingsTab(snap);
    else if (state.configTab === 'measures' && snap.kind === 'dtv') html = renderDtvMeasuresTab(snap);
    else if (state.configTab === 'settings' && snap.kind === 'dtv') html = renderDtvSettingsTab(snap);
    else if (snap.kind === 'mr') html = renderModuleInfoTab(snap);
    else html = renderInfoTab(snap);
    host.innerHTML = html;
    state.configBodyKey = configBodyRenderKey(snap);
    wireConfigBodyEvents();
  }

  /* Ключ отрисованного тела: тот же ключ ⇒ каркас не изменился и живые значения
     обновляются на месте. Иначе innerHTML раз в секунду сбрасывал бы прокрутку
     таблиц входов/выходов и ввод оператора. */
  function configBodyRenderKey(snap) {
    if (snap && snap.kind === 'led') return ledWindow().renderKey(snap, state.configTab);
    if (snap && snap.kind === 'mtd') return mtdWindow().renderKey(snap, state.configTab);
    if (!snap || snap.kind !== 'carel') return '';
    const c = carelBlock(snap);
    const counts = CAREL_IO_KEYS.concat(['alarms'])
      .map(key => (Array.isArray(c[key]) ? c[key].length : -1))
      .join(',');
    return ['carel', state.configTab || '', carelFamily(snap), counts].join('|');
  }

  function configBodyIsPatchable(snap) {
    if (!snap || (snap.kind !== 'carel' && snap.kind !== 'led' && snap.kind !== 'mtd') || !state.configBodyKey) return false;
    const host = configModalEl('flasher-config-body');
    if (!host || !host.firstChild) return false;
    return state.configBodyKey === configBodyRenderKey(snap);
  }

  function renderConfigTabs() {
    const host = configModalEl('flasher-config-tabs');
    if (!host) return;
    const tabs = configTabsForSnapshot(state.configSnapshot);
    host.innerHTML = tabs.map(tab => {
      const classes = ['flasher-config-tab'];
      if (tab.id === state.configTab) classes.push('active');
      if (tab.live) classes.push('is-live');
      return `<button type="button" class="${classes.join(' ')}" data-config-tab="${tab.id}">` +
        `<span class="flasher-config-tab-row">` +
        `<span class="flasher-config-tab-main">${escapeHtml(tab.label)}</span>` +
        (tab.suffix ? `<span class="flasher-config-tab-suffix">${escapeHtml(tab.suffix)}</span>` : '') +
        `</span>` +
      `</button>`;
    }).join('');
    host.querySelectorAll('[data-config-tab]').forEach(btn => {
      btn.addEventListener('click', async () => {
        state.configTab = btn.dataset.configTab || 'info';
        state.configBodyKey = '';
        renderConfigTabs();
        renderConfigBody();
        await waitConfigBackgroundIdle();
        await refreshConfigSnapshot(true, 'full');
      });
    });
  }

  function applyConfigSnapshot(snap, silent) {
    let merged = snap;
    if (
      snap &&
      (snap.snapshot_detail === 'minimal' || snap.snapshot_detail === 'panel') &&
      state.configSnapshot &&
      state.configSnapshot.kind === snap.kind
    ) {
      merged = mergeDeviceConfigSnapshot(state.configSnapshot, snap);
    }
    if (merged && merged.kind === 'carel') merged = mergeCarelSnapshot(state.configSnapshot, merged);
    if (merged && merged.kind === 'led') merged = ledWindow().merge(state.configSnapshot, merged);
    state.configSnapshot = merged;
    configDeviceFromSnapshot(merged);
    renderDevices();
    const title = configModalEl('flasher-config-title');
    const sub = configModalEl('flasher-config-sub');
    const kicker = configModalEl('flasher-config-kicker');
    if (title) title.textContent = deviceConfigTitle(merged.kind, merged.info && merged.info.signature);
    if (kicker) kicker.textContent = merged.kind === 'dtv' ? 'Настройка датчика' : merged.kind === 'ce' ? 'Настройка анализатора сети' : merged.kind === 'carel' ? 'Настройка приточной установки' : merged.kind === 'led' ? ledWindow().kicker() : merged.kind === 'mtd' ? mtdWindow().kicker() : 'Настройка модуля расширения';
    if (sub) {
      const info = merged.info || {};
      const line = `${info.address ?? '—'} адр. · ${(merged.network && merged.network.baudrate) || '—'} ${(merged.network && merged.network.parity) || 'N'}${(merged.network && merged.network.stopbits) || 1}`;
      // У Carel серийного номера нет (holding 270/271 ПЛК не отдаёт) — вместо
      // прочерка в подзаголовке стоит модель из ответа на функцию 17.
      // The MTD has no serial either; its header names the line the daemon
      // actually talked on (info.line), not the registers 7..9.
      const ln = info.line || {};
      sub.textContent = merged.kind === 'carel'
        ? `${line} · ${info.model || carelModelLabel(info.signature)}`
        : merged.kind === 'mtd'
        ? `${info.address ?? '—'} адр. · ${ln.baudrate || '—'} ${ln.parity || 'N'}${ln.stopbits || 1} · ${info.model || info.signature || ''}`
        : `${line} · ${serialHex(info.serial)}`;
    }
    if (!state.configTab) state.configTab = 'info';
    const available = configTabsForSnapshot(merged).map(t => t.id);
    if (!available.includes(state.configTab)) state.configTab = available[0] || 'info';
    renderConfigTabs();
    if (shouldSkipConfigBodyRerender() || configBodyIsPatchable(merged)) patchConfigLiveReadouts(merged);
    else renderConfigBody();
    if (!silent) setConfigBanner('', '');
  }

  async function refreshConfigSnapshot(silent, detail) {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    // Каждый вызов получает порядковый номер. Устаревший ответ (когда уже
    // запущен более новый опрос) отбрасывается, не обновляя UI.
    const mySeq = ++_configPollSeq;
    if (silent) {
      state.configBackgroundBusy = true;
    } else {
      setConfigBusy(true);
    }
    try {
      const snap = await configApi('/device_config/snapshot', {
        port,
        device: dev,
        snapshot_detail: detail || 'full',
        active_tab: configPollActiveTab(detail || 'full'),
      });
      if (mySeq !== _configPollSeq) return;
      applyConfigSnapshot(snap, !!silent);
      setConfigPollError('');
    } catch (err) {
      if (mySeq !== _configPollSeq) return;
      const hasSnapshot = state.configSnapshot && state.configSnapshot.snapshot_detail !== 'stub';
      setConfigPollError(uiT(hasSnapshot ? 'Данные не обновлены. Последний успешный снимок сохранён. ' : 'Данные не обновлены. Состояние устройства неизвестно. ') + err.message);
      if (!silent) {
        toast('Настройка устройства: ' + err.message, 'error');
      }
    } finally {
      if (silent) {
        state.configBackgroundBusy = false;
      } else {
        setConfigBusy(false);
      }
    }
  }

  async function openConfigModal(idx) {
    if (isFlasherOperationActive()) {
      toast('Дождитесь окончания сканирования или прошивки', 'warn');
      return;
    }
    if (!state.devices[idx] || !isDeviceConfigSupported(state.devices[idx])) return;
    stopConfigPolling();
    ++_configPollSeq;
    state.configOpen = true;
    state.configDeviceIdx = idx;
    state.configTab = 'info';
    state.configSnapshot = null;
    state.configFilePreview = null;
    state.configFileBusy = false;
    setConfigPollError('');
    state.configNetworkDirty = false;
    state.configBodyKey = '';
    state.configBusy = false;
    state.configLinkBusy = false;
    state.configBackgroundBusy = false;
    clearAiConfigEditState();
    configModalEl('flasher-config-modal').hidden = false;
    document.body.style.overflow = 'hidden';
    ensureConfigCloseEnabled();
    const stub = buildConfigSnapshotStubFromDevice(state.devices[idx]);
    if (stub) {
      applyConfigSnapshot(stub, true);
    } else {
      renderConfigBody();
    }
    await refreshConfigSnapshot(false);
    startConfigPolling();
    ensureConfigCloseEnabled();
  }

  function closeConfigModal() {
    if (state.configLinkBusy) {
      toast('Дождитесь окончания смены параметров связи', 'warn');
      return;
    }
    stopConfigPolling();
    ++_configPollSeq; // инвалидируем все текущие in-flight запросы этой сессии
    ensureConfigCloseEnabled();
    state.configOpen = false;
    state.configBusy = false;
    state.configBackgroundBusy = false;
    state.configDeviceIdx = -1;
    state.configSnapshot = null;
    state.configTab = '';
    state.configBodyKey = '';
    const modal = configModalEl('flasher-config-modal');
    if (modal) modal.hidden = true;
    document.body.style.overflow = '';
    clearAiConfigEditState();
  }

  async function saveConfigNetwork() {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    const network = {
      address: parseInt(configModalEl('cfg-net-addr').value, 10) || 1,
      baudrate: parseInt(configModalEl('cfg-net-baud').value, 10) || 9600,
      parity: configModalEl('cfg-net-parity').value || 'N',
      stopbits: parseInt(configModalEl('cfg-net-stop').value, 10) || 1,
      fast_modbus: !!configModalEl('cfg-net-fast').checked,
    };
    setConfigBusy(true);
    try {
      const snap = await configApi('/device_config/network', { port, device: dev, network });
      applyConfigSnapshot(snap, false);
      state.configNetworkDirty = false;
      toast('Параметры сети сохранены', 'success');
    } catch (err) {
      setConfigBanner('Сохранение сети: ' + err.message, 'error');
      toast('Сеть устройства: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  /* ── Полевая шина: применить протокол (§5.1 / §5.3) ─────────────────────── */
  async function applyBusMode() {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    const sel = configModalEl('cfg-busmode-select');
    if (!dev || !port || !sel) return;
    const snap = state.configSnapshot || {};
    const family = busModeFamily(snap);
    const mode = parseInt(sel.value, 10);
    if ([0, 1, 2].indexOf(mode) < 0) return;
    if (mode === 2) {
      // flasher-контракт: диалог подтверждения, называющий путь восстановления ДО записи.
      const ok = confirm(
        t('Переключить модуль в BACnet MS/TP?') + '\n\n' +
        t('Модуль перезагрузится, перестанет отвечать по Modbus и пропадёт из опроса.') + '\n' +
        busModeRecoveryText(family) + '\n\n' +
        t('Переключайте модули по одному.'),
      );
      if (!ok) return;
    }
    setConfigBusy(true);
    try {
      const res = await configApi('/bus_mode', { port, device: dev, mode });
      if (mode !== 2) {
        // Live switch (classic ↔ Fast): no reboot — apply the fresh snapshot.
        if (res && res.snapshot) applyConfigSnapshot(res.snapshot, false);
        toast('Протокол шины применён', 'success');
        return;
      }
      // BACnet: reboot is deferred (§5.3). Treat the post-write window as
      // "applying" ≥6 s, then re-probe: silence ⇒ went to BACnet; still Modbus
      // with reg122==2 ⇒ inert firmware / not applied (never a false success).
      toast('BACnet выбран — применяется, ждите перезагрузку модуля', 'info');
      setConfigBanner('Применяется BACnet MS/TP — проверяю состояние модуля…', 'info');
      await new Promise(r => setTimeout(r, 6500));
      let probe = null;
      try {
        probe = await configApi('/device_config/snapshot', { port, device: dev });
      } catch (_) {
        probe = null; // timeout ⇒ модуль ушёл на MS/TP
      }
      if (!probe) {
        setConfigBanner('Модуль переключён в BACnet MS/TP (Modbus не отвечает — это ожидаемо).', 'success');
        toast('Модуль переключён в BACnet MS/TP', 'success');
      } else if (Number((probe.network || {}).bus_mode) === 2) {
        setConfigBanner('Прошивка без поддержки BACnet или переключение не применилось (модуль по-прежнему на Modbus).', 'warn');
        toast('BACnet не активирован: прошивка без поддержки BACnet', 'warn');
        applyConfigSnapshot(probe, false);
      } else {
        setConfigBanner('Переключение не применилось — модуль остался на Modbus.', 'warn');
        applyConfigSnapshot(probe, false);
      }
    } catch (err) {
      setConfigBanner('Протокол шины: ' + err.message, 'error');
      toast('Полевая шина: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  /* ── Полевая шина: проверка BACnet (§5.2) / возврат в Modbus (§5.4) ──────── */
  async function runBacnetJob(path, body, opts) {
    stopConfigPolling();
    setConfigBusy(true);
    let res;
    try {
      res = await apiPost(path, body);
    } catch (err) {
      setConfigBusy(false);
      setConfigBanner((opts.label || 'BACnet') + ': ' + err.message, 'error');
      toast((opts.label || 'BACnet') + ': ' + err.message, 'error');
      return;
    }
    const jobId = res && res.job_id;
    if (!jobId) { setConfigBusy(false); return; }
    openStream(jobId, {
      onEnd: async endState => {
        let result = null;
        try {
          const snap = await apiGet('/jobs/' + jobId);
          result = snap && snap.result;
        } catch (_) {}
        try { if (opts.onResult) opts.onResult(result, endState); } catch (_) {}
        setConfigBusy(false);
        if (opts.refreshAfter) { try { await refreshConfigSnapshot(false, 'full'); } catch (_) {} }
      },
    });
  }

  async function verifyBacnet() {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    const snap = state.configSnapshot || {};
    const family = busModeFamily(snap);
    setConfigBanner('Пассивный сниф MS/TP (только чтение)…', 'info');
    await runBacnetJob('/bacnet/verify', { port, device: dev, family }, {
      label: 'Проверка BACnet',
      onResult: result => {
        if (!result) { setConfigBanner('Проверка BACnet завершена.', 'info'); return; }
        if (result.open_error) {
          setConfigBanner(t('Проверка BACnet: ошибка порта') + ' — ' + result.open_error, 'error');
        } else if (result.alive) {
          setConfigBanner(t('MS/TP активен — модуль в режиме BACnet. Кадров: ') + (result.frames_seen || 0), 'success');
          toast('MS/TP активен', 'success');
        } else {
          setConfigBanner('Нет кадров MS/TP — модуль не активен на шине BACnet.', 'warn');
          toast('Кадры MS/TP не обнаружены', 'warn');
        }
      },
    });
  }

  async function recoverBacnet() {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    const snap = state.configSnapshot || {};
    const family = busModeFamily(snap);
    // Advanced op — second explicit confirmation (§5.4): TX on the wire, reboots the module.
    const ok = confirm(
      t('Вернуть модуль в Modbus по проводу?') + '\n\n' +
      t('Это передаёт кадры MS/TP на линию и перезагружает модуль. Выполняйте только когда СА-02м владеет сегментом (порт занят флешером), по одному модулю за раз.'),
    );
    if (!ok) return;
    setConfigBanner('Возврат в Modbus по проводу…', 'info');
    await runBacnetJob('/bacnet/recover', {
      port,
      device: dev,
      family,
      address: dev.address,
      prior_mode: Number((snap.network || {}).bus_mode),
    }, {
      label: 'Вернуть в Modbus',
      refreshAfter: true,
      onResult: result => {
        if (result && result.recovered) {
          setConfigBanner('Модуль возвращён в Modbus.', 'success');
          toast('Модуль возвращён в Modbus', 'success');
        } else {
          setConfigBanner('Модуль не вернулся — используйте физическую кнопку сброса на устройстве.', 'warn');
          toast('Возврат не удался — нужна кнопка сброса', 'warn');
        }
      },
    });
  }

  async function writeConfigHolding(reg, value, successText, validateQueued) {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return null;
    const snap = await configApi('/device_config/holding', { port, device: dev, reg, value }, validateQueued);
    applyConfigSnapshot(snap, false);
    if (successText) toast(successText, 'success');
    return snap;
  }

  async function writeConfigCoil(coil, value, successText, relayAction) {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return null;
    const snap = await configApi('/device_config/coil', { port, device: dev, coil, value, ...(relayAction ? {relay_action: relayAction} : {}) });
    applyConfigSnapshot(snap, false);
    if (successText) toast(successText, 'success');
    return snap;
  }

  async function saveCeSettings() {
    const snap = state.configSnapshot || {};
    const ctLocked = !!(snap.ce && snap.ce.ct_write && snap.ce.ct_write.supported === false);
    const items = [
      { reg: 553, value: parseInt(configModalEl('cfg-ce-ph-loss').value, 10) || 0 },
      { reg: 554, value: parseInt(configModalEl('cfg-ce-inv-a').value, 10) || 0 },
      { reg: 555, value: parseInt(configModalEl('cfg-ce-inv-b').value, 10) || 0 },
      { reg: 556, value: parseInt(configModalEl('cfg-ce-inv-c').value, 10) || 0 },
    ];
    // 557-559 go last: a refusal or a failed persist then leaves phase loss and
    // inversion already saved, and the message says only the CT part is open.
    const ctItems = [
      { reg: 557, value: parseInt(configModalEl('cfg-ce-kt-a').value, 10) || CE_CT_RATIO_DEFAULT },
      { reg: 558, value: parseInt(configModalEl('cfg-ce-kt-b').value, 10) || CE_CT_RATIO_DEFAULT },
      { reg: 559, value: parseInt(configModalEl('cfg-ce-kt-c').value, 10) || CE_CT_RATIO_DEFAULT },
    ];
    setConfigBusy(true);
    try {
      for (const item of items) await writeConfigHolding(item.reg, item.value, '');
      if (ctLocked) {
        setConfigBanner(ceCtFwTooOldText(snap.info && snap.info.app_version), 'warn');
        toast(uiT('Настройки CE-02м-3 сохранены, кроме коэффициентов ТТ'), 'warn');
        return;
      }
      // Each CT write answers with the meter's Input 65519 verdict, read ~2 s after it.
      const verdicts = [];
      for (const item of ctItems) {
        const res = await writeConfigHolding(item.reg, item.value, '');
        verdicts.push((res && res.ce_persist && res.ce_persist.status) || 'unknown');
      }
      if (verdicts.includes('not_persisted')) {
        setConfigBanner(uiT('Коэффициенты ТТ записаны, но устройство сообщило об ошибке сохранения в EEPROM (рег. 65519) — после отключения питания вернутся прежние значения.'), 'error');
        toast(uiT('CE-02м-3: коэффициенты ТТ не сохранены в EEPROM'), 'error');
      } else if (verdicts.includes('unknown')) {
        setConfigBanner(uiT('Коэффициенты ТТ записаны, сохранение в EEPROM не подтверждено (рег. 65519) — проверьте значения после перезапуска устройства.'), 'warn');
        toast(uiT('Настройки CE-02м-3 сохранены, сохранение ТТ не подтверждено'), 'warn');
      } else {
        toast('Настройки CE-02м-3 сохранены', 'success');
      }
    } catch (err) {
      if (err && err.code === E_CE_FW_TOO_OLD_FOR_CT_WRITE) {
        setConfigBanner(ceCtFwTooOldText(err.fwVersion), 'error');
        toast(uiT('Настройки CE-02м-3 сохранены, кроме коэффициентов ТТ'), 'warn');
      } else {
        setConfigBanner('Запись настроек CE-02м-3: ' + err.message, 'error');
        toast('CE-02м-3: ' + err.message, 'error');
      }
    } finally {
      setConfigBusy(false);
    }
  }

  async function saveDtvMainSettings() {
    setConfigBusy(true);
    try {
      await writeConfigHolding(6, parseInt(configModalEl('cfg-dtv-ext').value, 10) || 0, '');
      await writeConfigHolding(27, parseInt(configModalEl('cfg-dtv-delay').value, 10) || 0, '');
      await writeConfigCoil(1, !!configModalEl('cfg-dtv-buzzer').checked, '');
      await writeConfigCoil(2, !!configModalEl('cfg-dtv-leds').checked, '');
      toast('Настройки датчика сохранены', 'success');
    } catch (err) {
      setConfigBanner('Запись настроек датчика: ' + err.message, 'error');
      toast('Sens / DTV: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  async function saveDtvCalibration() {
    setConfigBusy(true);
    try {
      for (let i = 0; i < 9; i++) {
        const val = parseInt(configModalEl('cfg-dtv-cal-' + i).value, 10) || 0;
        await writeConfigHolding(31 + i, val, '');
      }
      toast('Калибровочные смещения сохранены', 'success');
    } catch (err) {
      setConfigBanner('Запись смещений датчика: ' + err.message, 'error');
      toast('Смещения DTV: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  async function saveDtvMovingAverage() {
    const regs = [534, 537, 540, 543, 546, 549, 552, 555, 558];
    setConfigBusy(true);
    try {
      for (let i = 0; i < regs.length; i++) {
        const raw = parseInt(configModalEl('cfg-dtv-ma-' + i).value, 10) || 1;
        const val = Math.max(1, Math.min(50, raw));
        await writeConfigHolding(regs[i], val, '');
      }
      toast('Параметры фильтрации сохранены', 'success');
    } catch (err) {
      setConfigBanner('Запись фильтрации датчика: ' + err.message, 'error');
      toast('Фильтрация DTV: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  async function writeHoldingBatch(items, successText, errorPrefix, validateQueued, verifyAi = false) {
    if (validateQueued && !validateQueued()) return false;
    setConfigBusy(true);
    try {
      for (const item of items) {
        const response = await writeConfigHolding(item.reg, item.value, '', validateQueued);
        if (verifyAi && response?.ai_write?.status !== 'confirmed') throw new Error(uiT(AI_RESULT_UNKNOWN));
      }
      if (successText) toast(successText, 'success');
      return true;
    } catch (err) {
      if (verifyAi && state.configSnapshot?.mr) state.configSnapshot.mr.ai_apply = {state: 'unknown'};
      setConfigBanner(errorPrefix + err.message, 'error');
      toast(errorPrefix + err.message, 'error');
      return false;
    } finally {
      setConfigBusy(false);
    }
  }

  async function saveModuleRelay() {
    const relay = (state.configSnapshot.mr || {}).relay || {};
    if (state.configBusy || !relay.mode_known || !relay.options_known || !relay.power_stagger_known) return;
    let options = relay.options;
    MODULE_RELAY_OPTION_BITS.forEach((item) => {
      const checkbox = configModalEl('cfg-mr-relay-opt-' + item.bit);
      if (checkbox && !checkbox.disabled) options = checkbox.checked ? options | (1 << item.bit) : options & ~(1 << item.bit);
    });
    await applyModuleSettings({relay: {mode: Number(configModalEl('cfg-mr-relay-mode').value), options,
      power_stagger_s: Number(configModalEl('cfg-mr-relay-stagger').value)}});
  }

  async function toggleModuleDo(channel, on, relayAction) {
    if (state.configBusy) return;
    const prev = state.configSnapshot;
    if (!moduleDoCommandAllowed(prev, channel, relayAction)) return;
    const relay = prev.mr.relay || {}, caps = (moduleMeta(prev) || {}).relay_capabilities || {};
    if (!relayAction && caps.legacy_manual_transition && relay.mode !== 0 && !confirm(uiT('Команда DO переведёт модуль в режим Modbus. Автономный алгоритм будет остановлен.'))) return;
    if (!relayAction && Number(moduleMeta(prev).code) === 11 && [3, 4].includes(relay.mode)) relayAction = 'fan';
    setConfigBusy(true);
    try {
      await writeConfigCoil(channel, !!on, uiT('Команда принята. Показано фактическое состояние выхода.'), relayAction);
    } catch (err) {
      setConfigBanner(`DO${channel}: ` + err.message, 'error');
      toast(`DO${channel}: ` + err.message, 'error');
      await refreshConfigSnapshot(false, 'full');
    } finally {
      setConfigBusy(false);
    }
  }

  async function resetModuleDoCounters() {
    await writeHoldingBatch([{ reg: 135, value: 1 }], 'Счетчики DO сброшены', 'DO: ');
  }

  async function saveMrGlobalInactivity() {
    const valid = () => moduleWatchdogKnown(state.configSnapshot);
    if (state.configBusy || !valid() || !configModalEl('cfg-mr-inactivity-global')?.value) return;
    await writeHoldingBatch([
      { reg: 134, value: clampInt(configModalEl('cfg-mr-inactivity-global').value, 0, 255, 0) },
    ], 'Таймаут линии сохранён', 'Modbus: ', valid);
  }

  async function saveModuleDo(channel) {
    const valid = () => moduleSettingGroupKnown(state.configSnapshot, 'do', channel);
    if (state.configBusy || !valid() || !moduleSettingInputsReady(state.configSnapshot, 'do', channel)) return;
    const items = [
      { reg: 600 + channel - 1, value: clampInt(configModalEl(`cfg-mr-do-safe-${channel}`).value, 0, 1, 0) },
    ];
    const modeEl = configModalEl(`cfg-mr-do-mode-${channel}`);
    const timeEl = configModalEl(`cfg-mr-do-time-${channel}`);
    const redelayEl = configModalEl(`cfg-mr-do-redelay-${channel}`);
    if (modeEl && timeEl && redelayEl) {
      const timerMode = clampInt(modeEl.value, 0, 5, 0);
      const timerTime = clampInt(timeEl.value, 0, 255, 0);
      items.push({ reg: 616 + channel - 1, value: ((timerTime & 0xFF) << 8) | (timerMode & 0xFF) });
      items.push({ reg: 623 + channel - 1, value: clampInt(redelayEl.value, 0, 999, 0) });
    }
    await writeHoldingBatch(items, `Настройки DO${channel} сохранены`, `DO${channel}: `, valid);
  }

  async function resetModuleDiCounters() {
    await writeHoldingBatch([{ reg: 694, value: 1 }], 'Счетчики DI сброшены', 'DI: ');
  }

  async function saveModuleDi(channel) {
    const valid = () => moduleSettingGroupKnown(state.configSnapshot, 'di', channel);
    if (state.configBusy || !valid() || !moduleSettingInputsReady(state.configSnapshot, 'di', channel)) return;
    const items = [
      { reg: 630 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-mode-${channel}`).value, 0, 1, 0) },
      { reg: 646 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-debounce-${channel}`).value, 0, 100, 0) },
      { reg: 662 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-long-${channel}`).value, 500, 5000, 500) },
      { reg: 678 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-double-${channel}`).value, 0, 2000, 0) },
    ];
    await writeHoldingBatch(items, `Настройки DI${channel} сохранены`, `DI${channel}: `, valid);
  }

  async function saveModuleAo(channel) {
    const valid = () => moduleSettingGroupKnown(state.configSnapshot, 'ao', channel);
    if (state.configBusy || !valid() || !moduleSettingInputsReady(state.configSnapshot, 'ao', channel)) return;
    const safeReg = aoSafeHoldingRegForChannel(state.configSnapshot, channel);
    await writeHoldingBatch([
      { reg: 33 + channel - 1, value: clampInt(configModalEl(`cfg-mr-ao-set-${channel}`).value, 0, 1000, 0) },
      { reg: safeReg, value: clampInt(configModalEl(`cfg-mr-ao-safe-${channel}`).value, 0, 1000, 0) },
      { reg: 134, value: clampInt(configModalEl(`cfg-mr-ao-inactivity-${channel}`).value, 0, 255, 0) },
    ], `Настройки AO${channel} сохранены`, `AO${channel}: `, valid);
  }

  async function applyAiSensorCode(channel, desiredCode) {
    const ch = Number(channel);
    if (state.configBusy || !Number.isFinite(ch) || ch <= 0) return;
    const ai = moduleAiChannel(state.configSnapshot, ch);
    if (!ai) return;
    const base = Number(ai.register_base);
    if (!Number.isFinite(base)) return;

    let sensorVal;
    if (desiredCode != null) {
      sensorVal = clampInt(desiredCode, 0, 42, 0);
    } else {
      const sensorEl = configModalEl(`cfg-mr-ai-sensor-${ch}`);
      if (!sensorEl) return;
      sensorVal = clampInt(sensorEl.value, 0, 42, 0);
    }

    const currentCode = Number(ai.sensor_code) & 0xFFFF;
    if (currentCode === sensorVal) return;

    aiSensorEditGuardAdd(ch);
    _aiSensorWriteInflight.add(ch);
    setConfigBusy(true);
    try {
      const response = await writeConfigHolding(base, sensorVal, '');
      if (response?.ai_write?.status === 'confirmed') {
        toast(`Тип AI${ch} применён`, 'success');
      } else {
        const message = uiT(response?.ai_write?.status === 'accepted_unconfirmed' ? AI_TYPE_ACCEPTED : AI_RESULT_UNKNOWN);
        setConfigBanner(message, 'info');
        toast(message, 'warn');
      }
    } catch (err) {
      if (state.configSnapshot?.mr) state.configSnapshot.mr.ai_apply = {state: 'unknown'};
      const message = uiT(AI_RESULT_UNKNOWN) + ' ' + err.message;
      setConfigBanner(message, 'error');
      toast(message, 'error');
    } finally {
      _aiSensorWriteInflight.delete(ch);
      setConfigBusy(false);
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  async function applyAiCalibration(channel) {
    const ch = Number(channel);
    const ai = moduleAiChannel(state.configSnapshot, ch);
    if (!ai) return;
    const base = Number(ai.register_base);
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${ch}`);
    const calEl = configModalEl(`cfg-mr-ai-cal-${ch}`);
    if (!sensorEl || !calEl) return;
    if (!aiUiCalibrationApplicable(clampInt(sensorEl.value, 0, 42, 0))) return;
    const wantCal = parseInt(calEl.value, 10) || 0;
    if (Number(ai.calibration) === wantCal) return;
    aiSensorEditGuardAdd(ch);
    try {
      await writeHoldingBatch(
        [{ reg: base + 4, value: signedToUint16(calEl.value) }],
        `Калибровка AI${ch} применена`, `AI${ch}: `, undefined, true
      );
    } finally {
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  async function applyAiLimit(channel, which) {
    const ch = Number(channel);
    const ai = moduleAiChannel(state.configSnapshot, ch);
    if (!ai) return;
    const base = Number(ai.register_base);
    if (!Number.isFinite(base)) return;
    const isHi = String(which) === 'hi';
    const el = configModalEl(`cfg-mr-ai-limit-${isHi ? 'hi' : 'lo'}-${ch}`);
    const valid = () => aiApplyIdle(state.configSnapshot);
    if (!el || el.disabled || state.configBusy || !valid()) return;
    // Пределы редактируются только для активных аналоговых режимов.
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${ch}`);
    const code = sensorEl ? clampInt(sensorEl.value, 0, 42, 0) : (Number(ai.sensor_code) & 0xFFFF);
    const bucket = aiUiSensorBucket(code);
    if (bucket !== 'volt' && bucket !== 'curr') return;
    const snapKey = isHi ? 'limit_high' : 'limit_low';
    const current = clampInt(ai[snapKey] != null ? ai[snapKey] : 0, -32768, 32767, 0);
    const want = clampInt(el.value, -32768, 32767, 0);
    if (current === want) { el.value = String(current); return; }
    const reg = base + (isHi ? 6 : 5);
    aiSensorEditGuardAdd(ch);
    try {
      const ok = await writeHoldingBatch(
        [{ reg: reg, value: signedToUint16(want) }],
        `Предел AI${ch} применён`, `AI${ch}: `, valid, true
      );
      el.value = String(ok ? want : current);
    } finally {
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  async function applyAiFilters(channel, key = 'tau') {
    const ch = Number(channel), ai = moduleAiChannel(state.configSnapshot, ch);
    const independent = key === 'kalman';
    const valid = () => {
      const current = moduleAiChannel(state.configSnapshot, ch);
      return independent ? current?.filters?.known?.kalman === true :
        aiApplyIdle(state.configSnapshot) && aiFiltersKnown(current);
    };
    if (state.configBusy || !valid()) return;
    const el = configModalEl(`cfg-mr-ai-${key}-${ch}`);
    if (!el || el.disabled || (!independent && el.value === '')) return;
    const value = independent ? Number(el.checked) : Number(el.value);
    const stor = ai.filters.stor;
    const registers = {kalman: 491 + stor, sps: 533 + 3 * stor, avg: 534 + 3 * stor, tau: 535 + 3 * stor};
    if (!Number.isInteger(stor) || stor < 0 || stor > 11 || !Number.isInteger(value) ||
        value < 0 || (key === 'sps' && !MODULE_AI_SAMPLE_RATES.includes(value)) ||
        (key === 'avg' && value > 50) || (key === 'tau' && value > 65000) || ai.filters[key] === value) return;
    aiSensorEditGuardAdd(ch);
    try {
      await writeHoldingBatch([{reg: registers[key], value}], `Фильтры AI${ch} применены`, `AI${ch}: `, valid, true);
    } finally {
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  function refreshAiCalibrationVisibility(channel) {
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
    const strip = configModalEl(`cfg-mr-ai-cal-strip-${channel}`);
    if (!sensorEl) return;
    const code = parseInt(sensorEl.value, 10) || 0;
    const ok = aiUiCalibrationApplicable(code);
    if (strip) strip.hidden = !ok;
    // Диапазон/подпись калибровки зависят от режима: активные — целый int16
    // «Калибровка (смещение)»; температурные — ±100 «Калибровка».
    const intMode = aiCalibrationIsInteger(code);
    const inp = configModalEl(`cfg-mr-ai-cal-${channel}`);
    if (inp) {
      inp.min = String(intMode ? -32768 : -100);
      inp.max = String(intMode ? 32767 : 100);
    }
    const lbl = configModalEl(`cfg-mr-ai-cal-label-${channel}`);
    if (lbl) lbl.textContent = intMode ? 'Калибровка (смещение)' : 'Калибровка';
  }

  function _aiRebuildSensorOptions(channel, choices) {
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
    if (!sensorEl) return;
    const prev = sensorEl.value;
    sensorEl.innerHTML = choices.map(([code, lbl]) => {
      const isSel = String(code) === String(prev) ? 'selected' : '';
      return `<option value="${code}" ${isSel}>${escapeHtml(lbl)}</option>`;
    }).join('');
    if (!choices.some(([code]) => String(code) === prev)) {
      sensorEl.value = String(choices[0][0]);
    }
  }

  function _aiUpdateModeUi(channel, bucket) {
    const isRtd = bucket === 'rtd';
    const [mag, unit] = aiUiQuantityLabels(bucket);

    const magEl  = configModalEl(`cfg-mr-ai-mag-${channel}`);
    const unitEl = configModalEl(`cfg-mr-ai-unit-${channel}`);
    if (magEl)  magEl.textContent  = mag;
    if (unitEl) unitEl.textContent = unit;

    // Wire toggle: показываем для RTD; перестраиваем с 2-проводной по умолчанию
    const wireRow = configModalEl(`cfg-mr-ai-wire-row-${channel}`);
    if (wireRow) {
      if (isRtd) {
        wireRow.innerHTML = `
          <div class="ai-wire-row">
            <span>Подключение</span>
            <div class="ai-wire-toggle" id="cfg-mr-ai-wire-wrap-${channel}">
              <button class="ai-wire-btn active" data-mr-ai-wire="${channel}" data-wire="2">Двухпроводное</button>
              <button class="ai-wire-btn" data-mr-ai-wire="${channel}" data-wire="3">Трёхпроводное</button>
            </div>
          </div>`;
        wireRow.querySelectorAll('[data-mr-ai-wire]').forEach(btn => {
          btn.addEventListener('click', () => _aiOnWireToggle(channel, btn.dataset.wire === '2'));
        });
      } else {
        wireRow.innerHTML = '';
      }
    }

    // Подтипы
    const choices = isRtd ? aiUiRtdSubchoicesForWire(true) : aiUiSubchoicesForBucket(bucket);
    _aiRebuildSensorOptions(channel, choices);
    refreshAiCalibrationVisibility(channel);
    _aiUpdateLimits(channel);
  }

  function _aiOnWireToggle(channel, twoWire) {
    const wrap = configModalEl(`cfg-mr-ai-wire-wrap-${channel}`);
    if (wrap) {
      wrap.querySelectorAll('.ai-wire-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.wire === (twoWire ? '2' : '3'));
      });
    }
    const choices = aiUiRtdSubchoicesForWire(twoWire);
    _aiRebuildSensorOptions(channel, choices);
    refreshAiCalibrationVisibility(channel);
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
    const code = sensorEl ? clampInt(sensorEl.value, 0, 42, 0) : 0;
    applyAiSensorCode(channel, code);
    _aiUpdateLimits(channel);
  }

  function _aiUpdateLimits(channel) {
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
    const limBox   = configModalEl(`cfg-mr-ai-limits-${channel}`);
    if (!limBox) return;
    const code = sensorEl ? (parseInt(sensorEl.value, 10) || 0) : 0;
    // Пересеиваем из снапшота, а не из устаревшего DOM (bucket/subtype switch).
    const ai = moduleAiChannel(state.configSnapshot, channel);
    limBox.innerHTML = aiLimitsBoxHtml(channel, code, ai);
    _bindAiLimitInputs(limBox);
    patchOptionalSettingControls(state.configSnapshot);
    _aiUpdateWiringDiagram(channel, code);
  }

  // Схема подключения AI зависит от режима (ТХА / 3-пров. RTD / «Выключен») —
  // обновляем при смене режима/подтипа, как эталон (_refresh_ai66_wiring).
  function _aiUpdateWiringDiagram(channel, code) {
    const slot = configModalEl(`cfg-mr-ai-wiring-${channel}`);
    if (!slot) return;
    slot.innerHTML = moduleWiringDiagramHtml(state.configSnapshot, 'ai', channel, code);
  }

  function setupAiBucketHandlers(body) {
    // Режим работы: radio buttons
    body.querySelectorAll('[data-mr-ai-bucket]').forEach(radio => {
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        const channel = parseInt(radio.dataset.mrAiBucket, 10);
        _aiUpdateModeUi(channel, radio.value);
        applyAiSensorCode(channel);
      });
    });

    // RTD wire toggle buttons (статически вставленные при первом рендере для RTD-каналов)
    body.querySelectorAll('[data-mr-ai-wire]').forEach(btn => {
      btn.addEventListener('click', () => {
        const channel = parseInt(btn.dataset.mrAiWire, 10);
        _aiOnWireToggle(channel, btn.dataset.wire === '2');
      });
    });

    // Подтип: пишет сразу при изменении + обновляет пределы
    body.querySelectorAll('select[id^="cfg-mr-ai-sensor-"]').forEach(sel => {
      const channel = parseInt(sel.id.replace('cfg-mr-ai-sensor-', ''), 10);
      sel.addEventListener('focus', () => aiSensorEditGuardAdd(channel));
      sel.addEventListener('blur', () => aiSensorEditGuardReleaseLater(channel));
      sel.addEventListener('change', () => {
        const code = clampInt(sel.value, 0, 42, 0);
        refreshAiCalibrationVisibility(channel);
        _aiUpdateLimits(channel);
        applyAiSensorCode(channel, code);
      });
      refreshAiCalibrationVisibility(channel);
      _aiUpdateLimits(channel);
    });

    // Калибровка: поле — blur; кнопки ± — step
    body.querySelectorAll('[data-mr-ai-cal]').forEach(el => {
      el.addEventListener('blur', () => applyAiCalibration(parseInt(el.dataset.mrAiCal, 10)));
    });
    body.querySelectorAll('[data-mr-ai-cal-step]').forEach(btn => {
      btn.addEventListener('click', () => {
        const channel = parseInt(btn.dataset.mrAiCalStep, 10);
        const step = parseInt(btn.dataset.step, 10) || 0;
        const inp = configModalEl(`cfg-mr-ai-cal-${channel}`);
        if (!inp) return;
        const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
        const code = sensorEl ? clampInt(sensorEl.value, 0, 42, 0) : 0;
        const cur = parseInt(inp.value, 10) || 0;
        inp.value = String(aiCalibrationClampByCode(code, cur + step));
        applyAiCalibration(channel);
      });
    });

    // Пределы измерения (активные режимы volt/curr): blur — запись base+5/base+6
    _bindAiLimitInputs(body);

    // Фильтры АЦП: change для select/checkbox, blur для числовых полей
    body.querySelectorAll('[data-mr-ai-filter]').forEach(el => {
      const ch = parseInt(el.dataset.mrAiFilter, 10);
      if (el.tagName === 'SELECT' || el.type === 'checkbox') {
        el.addEventListener('change', () => applyAiFilters(ch, el.id.split('-')[3]));
      } else {
        el.addEventListener('blur', () => applyAiFilters(ch, el.id.split('-')[3]));
      }
    });
  }

  function moduleFilePreviewHtml() {
    const preview = state.configFilePreview;
    if (!preview) return '';
    const result = preview.result || {};
    const list = (items, label) => !items || !items.length ? '' : `<p>${escapeHtml(uiT(label))}</p><ul>${items.map(item => {
      const path = typeof item === 'string' ? item : item.path;
      const pendingType = item.acknowledged && item.status === 'unknown' && path.endsWith('.sensor_code');
      return `<li>${escapeHtml(configurationPathLabel(path))}${typeof item === 'object' ? ': ' + escapeHtml(JSON.stringify(item.previous)) + ' → ' + escapeHtml(JSON.stringify(item.expected ?? item.observed ?? item.status)) : ''} <small class="mono">(${escapeHtml(path)})</small>${pendingType ? ' — ' + escapeHtml(uiT(AI_TYPE_ACCEPTED)) : ''}</li>`;
    }).join('')}</ul>`;
    return `<h4>${escapeHtml(uiT('Проверка файла конфигурации'))}</h4>
      <p>${escapeHtml(preview.name)} · ${escapeHtml(preview.port)} · ${escapeHtml(String(preview.device.address))} · ${escapeHtml(preview.device.signature || '')}</p>
      ${list(result.changes, 'Изменения')}${list(result.unavailable, 'Отсутствующие настройки')}${list(result.unsupported, 'Неподдерживаемые настройки')}${list(result.normalizations, 'Нормализация значений')}
      ${list(result.applied, 'Подтверждено чтением')}${list(result.failed, 'Неизвестный результат')}${list(result.remaining, 'Не выполнено')}
      <p class="flasher-config-note">${escapeHtml(uiT('Настройки могут изменить автономную работу выходов. Чтение подтверждает текущее состояние, но не сохранение после отключения питания.'))}</p>
      ${preview.error ? `<p class="flasher-config-note">${escapeHtml(preview.error)}</p>` : ''}
      ${result.apply_blocked ? `<p class="flasher-config-note">${escapeHtml(uiT(AI_BOUNDARY_NOTE))}</p>` : ''}
      ${!result.apply_blocked && !result.complete && !preview.used ? `<label class="checkbox-line"><input id="cfg-mr-file-partial" type="checkbox" ${preview.allowPartial ? 'checked' : ''} />${escapeHtml(uiT('Применить только доступные настройки'))}</label>` : ''}
      <button class="btn btn-sm" type="button" id="cfg-mr-file-apply" ${result.apply_blocked || preview.used || state.configFileBusy || (!result.complete && !preview.allowPartial) ? 'disabled' : ''}>${escapeHtml(uiT('Применить файл'))}</button>
      <button class="btn btn-sm" type="button" id="cfg-mr-file-refresh" ${state.configFileBusy ? 'disabled' : ''}>${escapeHtml(uiT('Обновить состояние'))}</button>`;
  }

  function renderModuleFilePreview() {
    const host = configModalEl('cfg-mr-file-preview');
    if (host) { host.innerHTML = moduleFilePreviewHtml(); wireModuleFilePreview(); }
  }

  function wireModuleFilePreview() {
    const apply = configModalEl('cfg-mr-file-apply');
    if (apply) apply.onclick = applyModuleFile;
    const partial = configModalEl('cfg-mr-file-partial');
    if (partial) partial.onchange = () => { state.configFilePreview.allowPartial = partial.checked; renderModuleFilePreview(); };
    const refresh = configModalEl('cfg-mr-file-refresh');
    if (refresh) refresh.onclick = () => refreshConfigSnapshot(false, 'full');
  }

  function setModuleFileBusy(busy) {
    state.configFileBusy = !!busy;
    setConfigBusy(busy);
    setConfigLinkBusy(busy);
    const body = configModalEl('flasher-config-body');
    if (body) body.querySelectorAll('input,select,textarea,button').forEach(el => {
      if (busy) {
        if (el.dataset.fileWasDisabled == null) el.dataset.fileWasDisabled = el.disabled ? '1' : '0';
        el.disabled = true;
      } else if (el.dataset.fileWasDisabled != null) {
        el.disabled = el.dataset.fileWasDisabled === '1'; delete el.dataset.fileWasDisabled;
      }
    });
    if (!busy) {
      renderModuleFilePreview();
      if (state.configSnapshot?.kind === 'mr') patchRelayControls(state.configSnapshot);
    }
  }

  function configurationFileError(error) {
    return error && error.name === 'AbortError'
      ? uiT('Время ожидания истекло. Результат неизвестен: обновите состояние, не повторяйте запись автоматически.')
      : String(error && error.message || error);
  }

  async function exportModuleConfig() {
    if (state.configBusy || !state.configSnapshot || state.configSnapshot.kind !== 'mr') return;
    const device = currentConfigDevice(), port = $('flasher-port').value;
    if (!device || !port) return;
    setModuleFileBusy(true);
    try {
      const result = await configApi('/device_config/export', {port, device});
      if (!result.ok || !result.configuration) throw new Error(result.error || result.restoration_error || uiT('Экспорт не выполнен'));
      if (!result.complete) {
        const warning = uiT('Экспорт неполный: недоступные настройки не сохранены.') + '\n' + (result.configuration.coverage.unavailable || []).join(', ');
        setConfigBanner(warning, 'error');
        if (!confirm(warning)) return;
      }
      if (result.ai_apply?.state && result.ai_apply.state !== 'idle') {
        setConfigBanner(uiT('Файл содержит прочитанные значения AI. Тип датчика может ещё применяться.'), 'info');
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(result.configuration, null, 2)], {type: 'application/json'}));
      const a = document.createElement('a');
      a.href = url; a.download = `config_${String(device.signature || 'module').replace(/[^a-zA-Z0-9_-]/g, '_')}.json`; a.click();
      URL.revokeObjectURL(url);
      toast(result.complete ? 'Конфигурация сохранена в файл' : 'Экспорт неполный: недоступные настройки не сохранены.', result.complete ? 'success' : 'warn');
    } catch (error) {setConfigBanner(configurationFileError(error), 'error');}
    finally {setModuleFileBusy(false);}
  }

  async function importModuleConfig(fileInput) {
    if (state.configBusy || !fileInput || !fileInput.files || !fileInput.files[0]) return;
    const file = fileInput.files[0]; fileInput.value = '';
    const device = currentConfigDevice(), port = $('flasher-port').value;
    if (!device || !port || !state.configSnapshot || state.configSnapshot.kind !== 'mr') return;
    state.configFilePreview = null; renderModuleFilePreview();
    setModuleFileBusy(true);
    try {
      if (file.size > 48 * 1024) throw new Error(uiT('Файл конфигурации превышает 48 КиБ'));
      const configuration = JSON.parse(await file.text());
      const result = await configApi('/device_config/import', {port, device, configuration, preview_only: true, allow_partial: false});
      if (!result.ok) throw new Error(result.error || result.restoration_error || uiT('Проверка файла не выполнена'));
      state.configFilePreview = {name: file.name, port, device: {...device}, configuration, result, allowPartial: false, used: false};
      setConfigBanner('');
    } catch (error) {setConfigBanner(configurationFileError(error), 'error');}
    finally {setModuleFileBusy(false);}
  }

  async function applyModuleFile() {
    const preview = state.configFilePreview;
    if (state.configBusy || !preview || preview.used || preview.result.apply_blocked || (!preview.result.complete && !preview.allowPartial)) return;
    const current = currentConfigDevice();
    if (!current || $('flasher-port').value !== preview.port || ['address', 'baudrate', 'parity', 'stopbits', 'signature'].some(key => current[key] !== preview.device[key])) return;
    preview.used = true;
    setModuleFileBusy(true);
    try {
      const result = await configApi('/device_config/import', {port: preview.port, device: preview.device, configuration: preview.configuration,
        preview_only: false, allow_partial: preview.allowPartial});
      preview.result = result;
      if (!result.ok) preview.error = result.restoration_error || result.error || uiT('Неизвестный результат: обновите состояние перед дальнейшими действиями.');
      setConfigBanner(result.ok ? uiT(result.complete ? 'Настройки подтверждены чтением' : 'Применена только часть настроек') : preview.error, result.ok ? 'info' : 'error');
      await refreshConfigSnapshot(false, 'full');
    } catch (error) {preview.error = configurationFileError(error); setConfigBanner(preview.error, 'error');}
    finally {setModuleFileBusy(false);}
  }

  function configurationPathLabel(path) {
    const labels = {safe: 'Безопасное состояние', safe_raw: 'Безопасная уставка', timer_function: 'Режим таймера', timer_deciseconds: 'Время таймера, 0.1 с',
      minimum_off_s: 'Минимальное отключение, с', mode: 'Режим работы', options: 'Опции', power_stagger_s: 'Поочерёдное включение, с',
      debounce_ms: 'Антидребезг, мс', long_press_ms: 'Долгое нажатие, мс', double_click_ms: 'Двойное нажатие, мс',
      sensor_code: 'Тип датчика', calibration: 'Калибровка', limit_low: 'Нижний предел', limit_high: 'Верхний предел', kalman: 'Фильтр Калмана',
      sps: 'Частота АЦП, SPS', average: 'Количество усреднений', tau_ms: 'Постоянная фильтра, мс', inactivity_s: 'Таймаут линии, с',
      primary_fans: 'Основные вентиляторы, маска 0–3', start_hour: 'Начало, час', start_minute: 'Начало, минута', end_hour: 'Конец, час', end_minute: 'Конец, минута', enabled: 'Включено',
      deadtime_ms: 'Пауза смены направления, мс', default_run_s: 'Общее время движения, с', opening_deciseconds: 'Время открытия, 0.1 с', closing_deciseconds: 'Время закрытия, 0.1 с',
      powerup_action: 'При включении: 0 нет, 1 открыть, 2 закрыть', safe_action: 'При потере связи: 0 нет, 1 открыть, 2 закрыть', edge: 'Фронт: 0 передний, 1 задний'};
    const parts = String(path || '').split('.');
    let prefix = '';
    if (['do', 'di', 'ao', 'ai'].includes(parts[0])) prefix = parts[0].toUpperCase() + parts[1];
    else if (parts[1] === 'schedule' && parts[2] === 'days') prefix = uiT('День цикла') + ' ' + (Number(parts[3]) + 1);
    else if (parts[1] === 'drives' && parts[2] === 'channels') prefix = uiT('Привод') + ' ' + parts[3];
    else if (parts[1] === 'dimming' && parts[2] === 'channels') prefix = uiT('Канал диммирования') + ' ' + parts[3];
    return (prefix ? prefix + ': ' : '') + uiT(labels[parts[parts.length - 1]] || 'Неизвестная настройка');
  }

  function moduleSettingsFile(settings) {
    const meta = moduleMeta(state.configSnapshot) || {};
    return {format: 'sa02m-mr-config', schema_version: 1,
      module: {type_code: Number(meta.code), io_counts: {do: Number(meta.max_do), di: Number(meta.max_di), ao: Number(meta.max_ao), ai: Number(meta.max_ai)}, app_version: null}, settings};
  }

  async function applyModuleSettings(settings) {
    if (state.configBusy) return;
    const device = currentConfigDevice(), port = $('flasher-port').value;
    if (!device || !port) return;
    setModuleFileBusy(true);
    try {
      const result = await configApi('/device_config/import', {port, device, configuration: moduleSettingsFile(settings), preview_only: false, allow_partial: true});
      if (!result.ok) throw new Error(result.restoration_error || result.error || (result.failed || []).map(item => configurationPathLabel(item.path) + ': ' + uiT('Неизвестный результат')).join('; ') || uiT('Неизвестный результат'));
      configModalEl('flasher-config-body').querySelectorAll('[data-relay-dirty]').forEach(el => delete el.dataset.relayDirty);
      await refreshConfigSnapshot(false, 'full');
      setConfigBanner(uiT('Выбранные настройки подтверждены чтением'), 'info');
    } catch (error) {setConfigBanner(configurationFileError(error), 'error');}
    finally {setModuleFileBusy(false);}
  }

  async function saveRelayAdvanced() {
    const relay = (state.configSnapshot.mr || {}).relay || {};
    if (state.configBusy || !relay.mode_known || !relay.advanced_supported) return;
    const controls = Array.from(configModalEl('flasher-config-body').querySelectorAll('[data-relay-field]'));
    const fields = new Map((relay.advanced_fields || []).map(field => [field.path, field]));
    if (!controls.length || controls.some(el => !fields.get(el.dataset.relayField)?.known || el.disabled)) return;
    const settings = {relay: {}};
    for (const el of controls) {
      const parts = el.dataset.relayField.split('.').slice(1);
      let item = settings.relay;
      while (parts.length > 1) {
        const key = parts.shift();
        if (key === 'channels' || key === 'days') {
          const number = Number(parts.shift()), id = key === 'days' ? 'day' : 'channel';
          const list = item[key] || (item[key] = []);
          let child = list.find(entry => entry[id] === number);
          if (!child) {child = {[id]: number}; list.push(child);}
          item = child;
        } else item = item[key] || (item[key] = {});
      }
      item[parts[0]] = fields.get(el.dataset.relayField).boolean ? el.checked : el.value === '' ? null : Number(el.value);
    }
    await applyModuleSettings(settings);
  }

  async function rebootModuleDevice() {
    if (!confirm(t('Перезагрузить модуль?\nЛиния может быть временно недоступна.'))) return;
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    setConfigBusy(true);
    try {
      // Запись в рег. 120 — команда перезагрузки модуля (FC06 reg=120, как в desktop flasher)
      await configApi('/device_config/holding', { port, device: dev, reg: 120, value: 1 });
      toast('Команда перезагрузки отправлена', 'success');
      await new Promise(r => setTimeout(r, 2000));
      await refreshConfigSnapshot(false, 'full');
    } catch (err) {
      // Таймаут при перезагрузке — нормально; обновляем снимок
      toast('Перезагрузка: ' + err.message, 'warn');
      await new Promise(r => setTimeout(r, 2000));
      await refreshConfigSnapshot(false, 'full');
    } finally {
      setConfigBusy(false);
    }
  }

  function wireConfigBodyEvents() {
    const body = configModalEl('flasher-config-body');
    if (!body) return;
    wireCarelBodyEvents(body);
    if (state.configSnapshot && state.configSnapshot.kind === 'led') ledWindow().wire(body);
    if (state.configSnapshot && state.configSnapshot.kind === 'mtd') mtdWindow().wire(body);
    const saveNet = body.querySelector('#cfg-net-save-btn');
    if (saveNet) saveNet.addEventListener('click', saveConfigNetwork);
    const refreshNet = body.querySelector('#cfg-net-refresh-btn');
    if (refreshNet) refreshNet.addEventListener('click', () => refreshConfigSnapshot(false, 'full'));
    const busApply = body.querySelector('#cfg-busmode-apply-btn');
    if (busApply) busApply.addEventListener('click', applyBusMode);
    const busVerify = body.querySelector('#cfg-busmode-verify-btn');
    if (busVerify) busVerify.addEventListener('click', verifyBacnet);
    const busRecover = body.querySelector('#cfg-busmode-recover-btn');
    if (busRecover) busRecover.addEventListener('click', recoverBacnet);
    const ceSave = body.querySelector('#cfg-ce-save-btn');
    if (ceSave) ceSave.addEventListener('click', saveCeSettings);
    const dtvMain = body.querySelector('#cfg-dtv-main-save-btn');
    if (dtvMain) dtvMain.addEventListener('click', saveDtvMainSettings);
    const dtvCal = body.querySelector('#cfg-dtv-cal-save-btn');
    if (dtvCal) dtvCal.addEventListener('click', saveDtvCalibration);
    const dtvMa = body.querySelector('#cfg-dtv-ma-save-btn');
    if (dtvMa) dtvMa.addEventListener('click', saveDtvMovingAverage);
    const relaySave = body.querySelector('#cfg-mr-relay-save-btn');
    if (relaySave) relaySave.addEventListener('click', saveModuleRelay);
    const advancedSave = body.querySelector('#cfg-mr-relay-advanced-save');
    if (advancedSave) advancedSave.addEventListener('click', saveRelayAdvanced);
    body.querySelectorAll('[data-relay-drive]').forEach(button => button.addEventListener('click', () => toggleModuleDo(Number(button.dataset.relayDrive), button.dataset.relayOn === 'true', 'drive')));
    const fanReset = body.querySelector('#cfg-mr-fan-reset');
    if (fanReset) fanReset.addEventListener('click', async () => {
      const relay = (state.configSnapshot.mr || {}).relay || {};
      if (state.configBusy || !relay.mode_known || ![3, 4].includes(relay.mode) || !relay.advanced_supported || !relay.fans?.known) return;
      if (!confirm(uiT('Сброс аварий может разрешить повторный запуск вентиляторов. Продолжить?'))) return;
      await writeHoldingBatch([{reg: 137, value: 1}], uiT('Сброс принят. Проверьте фактические аварии.'), uiT('Вентиляторы') + ': ');
    });
    if (state.configTab === 'relay') body.querySelectorAll('input,select').forEach(el => {
      el.addEventListener('input', () => {el.dataset.relayDirty = '1';});
      el.addEventListener('change', () => {el.dataset.relayDirty = '1'; patchRelayControls(state.configSnapshot);});
    });
    if (state.configSnapshot?.kind === 'mr') patchRelayControls(state.configSnapshot);
    body.querySelectorAll('[data-mr-ai-filter], [id^="cfg-mr-do-safe-"], [id^="cfg-mr-do-mode-"], [id^="cfg-mr-do-time-"], [id^="cfg-mr-do-redelay-"], [id^="cfg-mr-di-mode-"], [id^="cfg-mr-di-debounce-"], [id^="cfg-mr-di-long-"], [id^="cfg-mr-di-double-"], [id^="cfg-mr-ao-set-"], [id^="cfg-mr-ao-safe-"], [id^="cfg-mr-ao-inactivity-"], #cfg-mr-inactivity-global').forEach(el => {
      el.addEventListener('input', () => {el.dataset.optionalDirty = '1';});
      el.addEventListener('change', () => {el.dataset.optionalDirty = '1';});
    });
    function parseDoChannel(btn) {
      const raw = btn.getAttribute('data-do-channel') || btn.getAttribute('data-mr-do-on') || btn.getAttribute('data-mr-do-off');
      const ch = parseInt(raw, 10);
      return Number.isFinite(ch) && ch >= 1 ? ch : NaN;
    }
    body.querySelectorAll('[data-mr-do-on]').forEach(btn => btn.addEventListener('click', () => {
      const ch = parseDoChannel(btn);
      if (!Number.isFinite(ch)) return;
      toggleModuleDo(ch, true);
    }));
    body.querySelectorAll('[data-mr-do-off]').forEach(btn => btn.addEventListener('click', () => {
      const ch = parseDoChannel(btn);
      if (!Number.isFinite(ch)) return;
      toggleModuleDo(ch, false);
    }));
    body.querySelectorAll('[data-mr-do-save]').forEach(btn => btn.addEventListener('click', () => saveModuleDo(parseInt(btn.dataset.mrDoSave, 10))));
    body.querySelectorAll('[data-mr-di-save]').forEach(btn => btn.addEventListener('click', () => saveModuleDi(parseInt(btn.dataset.mrDiSave, 10))));
    body.querySelectorAll('[data-mr-ao-save]').forEach(btn => btn.addEventListener('click', () => saveModuleAo(parseInt(btn.dataset.mrAoSave, 10))));
    // AI: setupAiBucketHandlers обрабатывает все события AI (sensor/cal/filters)
    setupAiBucketHandlers(body);
    body.querySelectorAll('[data-mr-do-reset]').forEach(btn => btn.addEventListener('click', resetModuleDoCounters));
    body.querySelectorAll('[data-mr-di-reset]').forEach(btn => btn.addEventListener('click', resetModuleDiCounters));
    const mrInactSave = body.querySelector('#cfg-mr-inactivity-save-btn');
    if (mrInactSave) mrInactSave.addEventListener('click', saveMrGlobalInactivity);
    const mrExportBtn = body.querySelector('#cfg-mr-export-btn');
    if (mrExportBtn) mrExportBtn.addEventListener('click', exportModuleConfig);
    const mrImportFile = body.querySelector('#cfg-mr-import-file');
    if (mrImportFile) mrImportFile.addEventListener('change', () => importModuleConfig(mrImportFile));
    wireModuleFilePreview();
    const mrRebootBtn = body.querySelector('#cfg-mr-reboot-btn');
    if (mrRebootBtn) mrRebootBtn.addEventListener('click', rebootModuleDevice);
    ['cfg-net-addr', 'cfg-net-baud', 'cfg-net-parity', 'cfg-net-stop'].forEach(id => {
      const el = body.querySelector('#' + id);
      if (el) el.addEventListener('input', () => { state.configNetworkDirty = true; });
      if (el) el.addEventListener('change', () => { state.configNetworkDirty = true; });
    });
    const fastEl = body.querySelector('#cfg-net-fast');
    if (fastEl) fastEl.addEventListener('change', () => { state.configNetworkDirty = true; });
  }

  /* ── Прогресс/лог SSE ─────────────────────────────────────────────────── */

  const logBuffer = [];

  function logLineText(line) {
    return window.sa02mI18n ? window.sa02mI18n.t(String(line)) : String(line);
  }

  function renderLogRow(entry) {
    const cls = entry.level === 'error' ? 'log-err' : entry.level === 'warn' ? 'log-warn' : entry.level === 'debug' ? 'log-dim' : '';
    const row = document.createElement('div');
    row.className = 'log-line ' + (cls || '');
    row.dataset.logSrc = entry.line;
    row.textContent = `[${entry.ts}] ${logLineText(entry.line)}`;
    return row;
  }

  function logAppend(line, level) {
    const box = $('flasher-log');
    if (!box) return;
    const ts = new Date().toLocaleTimeString();
    const entry = { line: String(line), level: level || 'info', ts: ts };
    logBuffer.push(entry);
    box.appendChild(renderLogRow(entry));
    box.scrollTop = box.scrollHeight;
  }

  function logReset(title) {
    logBuffer.length = 0;
    const box = $('flasher-log');
    if (box) box.innerHTML = '';
    if (title) logAppend(title, 'info');
  }

  window.flasherRerenderLog = function () {
    const box = $('flasher-log');
    if (!box) return;
    box.innerHTML = '';
    logBuffer.forEach(function (entry) {
      box.appendChild(renderLogRow(entry));
    });
    box.scrollTop = box.scrollHeight;
  };

  let _lastProgress = null;

  function translateProgressMessage(message) {
    return message
      ? (window.sa02mI18n ? window.sa02mI18n.t(String(message)) : String(message))
      : '';
  }

  function progressMetaFromData(data) {
    const d = data && typeof data === 'object' ? data : {};
    const meta = {};
    if (d.address != null && d.address !== '') meta.address = Number(d.address);
    if (d.baudrate != null && d.baudrate !== '') meta.baudrate = Number(d.baudrate);
    if (d.step != null && d.step !== '') meta.step = Number(d.step);
    if (d.step_total != null && d.step_total !== '') meta.stepTotal = Number(d.step_total);
    return meta;
  }

  function formatStandardScanDetail(meta, message) {
    const addr = meta && Number.isFinite(meta.address) ? meta.address : null;
    const baud = meta && Number.isFinite(meta.baudrate) ? meta.baudrate : null;
    if (addr != null && baud != null) {
      return translateProgressMessage(`Адрес ${addr}, ${baud}`);
    }
    if (message) return translateProgressMessage(message);
    return '';
  }

  function isScanArbitrationUi() {
    return state.scanArbitrationActive && (state.scanPending || state.scanJobId);
  }

  function isStandardScanUi() {
    return (state.scanPending || state.scanJobId) && !isScanArbitrationUi();
  }

  function setProgress(pct, message, meta) {
    const wrap = $('flasher-progress');
    const active = state.scanPending || state.scanJobId || state.flashPending || state.flashJobId;
    if (!active) {
      hideProgress();
      return;
    }
    const arbitration = isScanArbitrationUi();
    const standardScan = isStandardScanUi();
    const progressMeta = meta || {};
    _lastProgress = { pct, message, meta: progressMeta, arbitration, standardScan };
    wrap.hidden = false;
    wrap.classList.toggle('flasher-progress--arbitration', arbitration);
    const detailEl = $('flasher-progress-detail');
    const label = $('flasher-progress-label');
    const msg = translateProgressMessage(message);
    if (arbitration) {
      if (detailEl) detailEl.hidden = true;
      label.textContent = msg || translateProgressMessage('Поиск');
      return;
    }
    const clamped = Math.max(0, Math.min(100, pct));
    $('flasher-progress-fill').style.width = clamped + '%';
    if (standardScan) {
      const detailText = formatStandardScanDetail(progressMeta, message);
      if (detailEl) {
        if (detailText) {
          detailEl.hidden = false;
          detailEl.textContent = detailText;
        } else {
          detailEl.hidden = true;
          detailEl.textContent = '';
        }
      }
      if (Number.isFinite(progressMeta.step) && Number.isFinite(progressMeta.stepTotal) && progressMeta.stepTotal > 0) {
        label.textContent = `${progressMeta.step}/${progressMeta.stepTotal}`;
      } else {
        label.textContent = `${clamped}%`;
      }
      return;
    }
    if (detailEl) {
      detailEl.hidden = true;
      detailEl.textContent = '';
    }
    label.textContent = msg || `${clamped}%`;
  }

  function hideProgress() {
    const wrap = $('flasher-progress');
    wrap.hidden = true;
    wrap.classList.remove('flasher-progress--arbitration');
    const detailEl = $('flasher-progress-detail');
    if (detailEl) {
      detailEl.hidden = true;
      detailEl.textContent = '';
    }
    _lastProgress = null;
    state.scanArbitrationActive = false;
  }

  window.flasherRerenderProgress = function () {
    if (_lastProgress) {
      setProgress(_lastProgress.pct, _lastProgress.message, _lastProgress.meta || {});
    }
  };

  let _lastScanStatus = null;

  function setScanStatus(msg, type) {
    const el = $('flasher-scan-status');
    if (!el) return;
    const key = 'flasher-scan-status';
    if (!msg) {
      _lastScanStatus = null;
      cancelInlineStatusAutoClear(key);
      el.hidden = true;
      el.textContent = '';
      el.className = 'flasher-scan-status';
      return;
    }
    _lastScanStatus = { msg: String(msg), type: type || '' };
    el.hidden = false;
    el.textContent = window.sa02mI18n ? window.sa02mI18n.t(_lastScanStatus.msg) : _lastScanStatus.msg;
    el.className = 'flasher-scan-status' + (type ? ' ' + type : '');
    scheduleInlineStatusAutoClear(key, () => setScanStatus(''));
  }

  window.flasherRerenderFirmware = function () {
    const list = $('flasher-fw-list');
    if (!list) return;
    renderFirmware();
  };

  window.flasherRerenderScanStatus = function () {
    if (_lastScanStatus) setScanStatus(_lastScanStatus.msg, _lastScanStatus.type);
  };

  function clearScanStatus() {
    setScanStatus('');
  }

  function openStream(jobId, handlers) {
    const url = `${API}/jobs/${jobId}/events`;
    /* Только URL: для same-origin куки и так уходят; EventSourceInit/withCredentials ломает часть WebView. */
    const es = new EventSource(url);
    let finished = false;
    let pollTimer = null;
    let errLogged = false;

    function teardown() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      try { es.close(); } catch (_) {}
    }

    function finish(st) {
      if (finished) return;
      finished = true;
      teardown();
      if (handlers && handlers.onEnd) handlers.onEnd(st);
    }

    async function pollJobStatus() {
      if (finished) return;
      try {
        const snap = await apiGet('/jobs/' + jobId);
        if (typeof snap.progress === 'number') {
          setProgress(snap.progress, snap.message || `${snap.progress}%`, progressMetaFromData(snap));
        }
        if (state.flashJobId && snap.irreversible) {
          state.flashIrreversible = true;
          setFlashButtons();
        }
        const st = snap.state;
        if (st === 'running' || st === 'pending') return;
        if (snap.error) logAppend('Ошибка: ' + snap.error, 'error');
        const lastEv = (snap.events || []).slice(-1)[0];
        if (lastEv && lastEv.message) logAppend(lastEv.message, lastEv.level || 'info');
        if (st !== 'done') logAppend(`Готово: ${st}`, st === 'cancelled' ? 'warn' : 'warn');
        finish(st);
      } catch (err) {
        if (String(err.message || '').includes('HTTP 404')) {
          logAppend(
            'Задача не найдена (демон перезапущен?). Прошивка могла прерваться — выполните сканирование.',
            'warn',
          );
          finish('error');
        }
      }
    }

    function startPollFallback() {
      if (pollTimer || finished) return;
      pollJobStatus();
      pollTimer = setInterval(pollJobStatus, 2000);
    }

    es.addEventListener('log', ev => {
      const p = safeParse(ev.data);
      if (!p || p.level === 'debug') return;
      logAppend(p.message || '', p.level);
    });
    es.addEventListener('progress', ev => {
      const p = safeParse(ev.data);
      if (!p) return;
      const d = p.data || {};
      const pct = (typeof d.progress === 'number') ? d.progress : 0;
      setProgress(pct, p.message || '', progressMetaFromData(d));
    });
    es.addEventListener('status', ev => {
      const p = safeParse(ev.data);
      if (!p || p.level === 'debug') return;
      logAppend(p.message || '', p.level || 'info');
      if (state.flashJobId && (p.message || '').indexOf('необратим') >= 0) {
        state.flashIrreversible = true;
        setFlashButtons();
      }
    });
    es.addEventListener('device_found', ev => {
      const p = safeParse(ev.data);
      if (p && handlers && handlers.onDeviceFound) handlers.onDeviceFound(p.data || {});
    });
    es.addEventListener('error', ev => {
      const p = safeParse(ev.data);
      if (p) logAppend('Ошибка: ' + (p.message || ''), 'error');
    });
    es.addEventListener('end', ev => {
      const p = safeParse(ev.data);
      const st = p && p.state ? p.state : 'done';
      if (st !== 'done') {
        logAppend(`Готово: ${st}`, st === 'cancelled' ? 'warn' : 'warn');
      }
      finish(st);
    });
    es.onerror = () => {
      if (!errLogged) {
        errLogged = true;
        logAppend(
          'Потеряно SSE-соединение с демоном (сеть/прокси или перезапуск сервиса). Опрос статуса задачи…',
          'warn',
        );
      }
      startPollFallback();
    };
    return es;
  }

  function safeParse(s) { try { return JSON.parse(s); } catch (_) { return null; } }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  function scanDeviceRowKey(dev) {
    const wbSerial = Number(dev && dev.wb_scan_serial);
    if (Number.isFinite(wbSerial) && wbSerial > 0 && wbSerial !== 0xFFFFFFFF) {
      return 'wb:' + String(wbSerial >>> 0);
    }
    return [
      'line',
      Number(dev && dev.address) || 0,
      Number(dev && dev.baudrate) || 0,
      String(dev && dev.parity || ''),
      Number(dev && dev.stopbits) || 0,
    ].join(':');
  }

  function mergeScanField(prevValue, nextValue) {
    if (nextValue == null) return prevValue;
    if (typeof nextValue === 'string') {
      const trimmed = nextValue.trim();
      if (!trimmed || trimmed === '—') return prevValue;
      return nextValue;
    }
    if (typeof nextValue === 'number') {
      if (!Number.isFinite(nextValue)) return prevValue;
      if (nextValue === 0 && typeof prevValue === 'number' && prevValue > 0) return prevValue;
      return nextValue;
    }
    return nextValue;
  }

  function upsertScannedDevice(dev) {
    const key = scanDeviceRowKey(dev);
    const idx = state.devices.findIndex(item => scanDeviceRowKey(item) === key);
    if (idx < 0) {
      state.devices.push(Object.assign({}, dev));
      return;
    }
    const prev = state.devices[idx] || {};
    const merged = Object.assign({}, prev);
    Object.keys(dev || {}).forEach((field) => {
      merged[field] = mergeScanField(prev[field], dev[field]);
    });
    state.devices[idx] = merged;
  }

  function replaceScannedDevices(list) {
    const selectedKeys = selectedDeviceKeysFromIndices();
    state.devices = [];
    state.selectedDeviceIndices.clear();
    (list || []).forEach((dev) => {
      upsertScannedDevice(dev);
    });
    restoreSelectionByKeys(selectedKeys);
  }

  function deviceSerialNumeric(dev) {
    if (!dev) return 0xFFFFFFFF;
    if (dev.serial != null) {
      const sn = Number(dev.serial);
      if (Number.isFinite(sn)) return sn >>> 0;
    }
    if (dev.serial_dec) {
      const dec = Number(dev.serial_dec);
      if (Number.isFinite(dec)) return dec >>> 0;
    }
    return 0xFFFFFFFF;
  }

  /* ── Запуск сканирования ─────────────────────────────────────────────── */

  function scanConfigKeyFromBody(body) {
    return JSON.stringify({
      port: body.port,
      mode: body.mode,
      addrMin: body.addr_min,
      addrMax: body.addr_max,
      bauds: (body.baudrates || []).slice().sort((a, b) => a - b),
      parity: body.parity || 'N',
      stopbits: body.stopbits || 1,
    });
  }

  function buildScanRequestFromUi() {
    const port = $('flasher-port').value;
    const mode = $('flasher-mode').value;
    const addrMin = parseInt($('flasher-addr-min').value, 10) || 1;
    const addrMax = parseInt($('flasher-addr-max').value, 10) || 10;
    const bauds = selectedBaudrates();
    const body = {
      port: port,
      mode: mode,
      addr_min: addrMin,
      addr_max: addrMax,
      baudrates: bauds,
      parity: 'N',
      stopbits: 1,
    };
    const scanConfigKey = scanConfigKeyFromBody(body);
    body.timing_profile = state.lastScanConfigKey === scanConfigKey ? 'standard' : 'aggressive';
    return { body, scanConfigKey, baudCount: bauds.length };
  }

  function portReadyForScan(portKey) {
    const port = (state.ports || []).find(p => p.key === portKey);
    if (!port || !port.exists) return false;
    if (port.active_job) return false;
    const busy = port.busy_pids && port.busy_pids.length;
    const polling = port.active_services && port.active_services.length;
    return !busy && !polling;
  }

  async function startScanJob(body, scanConfigKey, logIntro) {
    if (state.scanJobId || state.scanPending) return false;

    state.devices = [];
    state.selectedDeviceIndices.clear();
    renderDevices();
    clearScanStatus();
    hideBacnetSniffOffer();
    hidePollerBusyOffer();
    state.scanPending = true;
    state.scanArbitrationActive = body.mode === 'fast';
    logReset(logIntro + ' на ' + body.port);
    if (body.mode === 'fast') {
      const bauds = (body.baudrates || []).slice().sort((a, b) => b - a);
      setProgress(0, portReadyForScan(body.port)
        ? (bauds.length ? `Поиск на ${bauds[0]}` : 'Поиск')
        : 'Подготовка порта');
    } else {
      const addrMin = body.addr_min || 1;
      const bauds = (body.baudrates || []).slice().sort((a, b) => b - a);
      const firstBaud = bauds.length ? bauds[0] : 0;
      if (portReadyForScan(body.port)) {
        setProgress(0, firstBaud ? `Адрес ${addrMin}, ${firstBaud}` : `Адрес ${addrMin}`, {
          address: addrMin,
          baudrate: firstBaud || undefined,
        });
      } else {
        setProgress(0, 'Подготовка порта');
      }
    }
    setScanButtons();

    try {
      const res = await apiPost('/scan', body);
      state.lastScanConfigKey = scanConfigKey;
      state.scanPending = false;
      state.scanJobId = res.job_id;
      persistJobId('scan', res.job_id);
      setScanButtons();
      state.scanStream = openStream(res.job_id, {
        onDeviceFound: dev => {
          upsertScannedDevice(dev);
          renderDevices();
        },
        onEnd: async state2 => {
          await finalizeScanEnd(res.job_id, body.port, state2);
        },
      });
      return true;
    } catch (err) {
      state.scanPending = false;
      state.scanArbitrationActive = false;
      updateGlobalBusyFromPorts();
      setScanButtons(); hideProgress();
      setScanStatus('Сканирование: ' + err.message, 'error');
      return false;
    }
  }

  async function startScan() {
    if (state.scanJobId) { setScanStatus('Сканирование уже выполняется', 'warn'); return; }
    const req = buildScanRequestFromUi();
    if (!req.baudCount) { setScanStatus('Выберите хотя бы одну скорость', 'warn'); return; }
    state.lastScanRequest = Object.assign({}, req.body);
    await startScanJob(req.body, req.scanConfigKey, 'Старт сканирования');
  }

  async function refreshScanAfterFlash() {
    if (state.scanJobId || state.scanPending) return;

    let body;
    let scanConfigKey;
    if (state.lastScanRequest) {
      body = Object.assign({}, state.lastScanRequest, { timing_profile: 'standard' });
      scanConfigKey = scanConfigKeyFromBody(body);
    } else {
      const req = buildScanRequestFromUi();
      if (!req.baudCount) return;
      body = req.body;
      scanConfigKey = req.scanConfigKey;
    }

    await startScanJob(body, scanConfigKey, 'Обновление списка устройств');
  }

  async function cancelScan() {
    if (!state.scanJobId) return;
    try { await apiPost('/cancel', { job_id: state.scanJobId }); } catch (_) {}
  }

  function setScanButtons() {
    syncActionButtons();
  }

  /* ── Прошивка ─────────────────────────────────────────────────────────── */

  function updateFlashButtonLabel(count, forcedLabel) {
    const flashBtn = $('flasher-flash-btn');
    if (!flashBtn) return;
    const n = Number(count) || 0;
    const label = forcedLabel || (n > 1 ? `Прошить (${n})` : 'Прошить');
    let textEl = flashBtn.querySelector('.flasher-flash-btn-label');
    if (!textEl) {
      textEl = document.createElement('span');
      textEl.className = 'flasher-flash-btn-label';
      flashBtn.textContent = '';
      flashBtn.appendChild(textEl);
    }
    textEl.textContent = label;
  }

  async function startFlash() {
    if (state.flashJobId) { toast('Прошивка уже выполняется', 'warn'); return; }
    const targets = selectedDevicesForFlash();
    if (!targets.length) { toast('Выберите устройство', 'warn'); return; }
    const selectionErr = validateMultiFlashSelection(targets);
    if (selectionErr) {
      toast(selectionErr, 'warn');
      return;
    }
    const fwEntry = selectedFirmwareEntry();
    if (!fwEntry) { toast('Выберите файл прошивки', 'warn'); return; }
    if (!isFirmwareEntryDownloaded(fwEntry)) {
      toast('Образ не скачан в кеш шлюза. Нажмите «Скачать» или загрузите .fw вручную.', 'warn');
      return;
    }
    for (const target of targets) {
      const routeErr = validateFirmwareSelectionForDevice(target.signature, fwEntry);
      if (routeErr) {
        toast(routeErr, 'warn');
        return;
      }
    }

    const channel = fwEntry.channel;
    const file = fwEntry.file;
    const port = $('flasher-port').value;
    const allPeers = state.devices.map(d => buildFlashTargetFromDevice(d));
    const flashTargets = targets.map((target) => {
      const flashTarget = buildFlashTargetFromDevice(target);
      const { duplicate } = resolveUseFastModbusForFlash(target, state.devices);
      if (duplicate) flashTarget.duplicate_modbus_address_on_line = true;
      return flashTarget;
    });

    for (const target of targets) {
      const { useFast } = resolveUseFastModbusForFlash(target, state.devices);
      if (useFast && !serialValidForFastModbus(target.serial)) {
        toast('При одинаковых Modbus-адресах на линии нужен серийный номер. Выполните сканирование и выберите устройство.', 'warn');
        return;
      }
    }

    const useFastAny = targets.some(t => resolveUseFastModbusForFlash(t, state.devices).useFast);

    const intro = targets.length > 1
      ? `Пакетная прошивка ${targets.length} устройств файлом ${file}`
      : `Прошивка устройства файлом ${file}`;
    logReset(intro);
    if (useFastAny) {
      targets.forEach((target) => {
        const { useFast } = resolveUseFastModbusForFlash(target, state.devices);
        if (!useFast) return;
        const dupCount = countDevicesWithSameModbusAddress(target.address, state.devices);
        logAppend(
          `Быстрый Modbus (0xFD 0x46): адр.${target.address} — ${dupCount} устройств с этим адресом, прошивка по SN ${serialHex(target.serial)}.`,
          'info',
        );
      });
    }
    setProgress(0, 'Запуск');
    state.flashPending = true;
    setFlashButtons();

    try {
      const res = await apiPost('/flash_batch', {
        port: port,
        firmware_channel: channel,
        firmware_file: file,
        use_fast_modbus: useFastAny,
        devices_on_port: allPeers,
        targets: flashTargets,
      });
      state.flashPending = false;
      state.flashJobId = res.job_id;
      state.flashIrreversible = false;
      persistJobId('flash', res.job_id);
      setFlashButtons();
      state.flashStream = openStream(res.job_id, {
        onEnd: async state2 => {
          state.flashJobId = null;
          state.flashIrreversible = false;
          clearPersistedJobId('flash');
          hideProgress();
          if (state2 === 'error' || state2 === 'cancelled') {
            await loadPorts();
            updateGlobalBusyFromPorts();
            setFlashButtons();
            if (state2 === 'error') toast('Прошивка прервана или завершилась с ошибкой. Выполните сканирование.', 'error');
            else toast('Прошивка отменена', 'warn');
          } else {
            updateGlobalBusyFromPorts();
            setFlashButtons();
            toast('Прошивка завершена', 'success');
            await refreshScanAfterFlash();
          }
        },
      });
    } catch (err) {
      state.flashPending = false;
      updateGlobalBusyFromPorts();
      setFlashButtons(); hideProgress();
      toast('Прошивка: ' + err.message, 'error');
    }
  }

  async function cancelFlash() {
    if (!state.flashJobId) return;
    if (state.flashIrreversible) {
      toast('Прошивка необратима — отмена невозможна', 'warn');
      return;
    }
    try {
      await apiPost('/cancel', { job_id: state.flashJobId });
    } catch (err) {
      if (String(err.message || '').indexOf('flash_irreversible') >= 0) {
        state.flashIrreversible = true;
        setFlashButtons();
        toast('Прошивка необратима — отмена невозможна', 'warn');
        return;
      }
      toast('Отмена: ' + err.message, 'error');
    }
  }

  function setFlashButtons() {
    syncActionButtons();
  }

  function portReleaseFailure(res) {
    const retained = (res && (res.manual_reserved_ports || res.reserved_ports)) || [];
    const reason = (res && res.failed && res.failed.length)
      ? res.failed.join(', ') : (res && res.error) || uiT('Порт не зарезервирован.');
    return uiT('Освобождение RS-485: ') + reason + (retained.length
      ? '. ' + uiT('Сохранён ручной резерв: ') + retained.join(', ') + '. ' + uiT('Кнопка «Вернуть опрос» снимает все ручные резервы.') : '');
  }

  /** True only after a successful batch reserved the selected port. */
  async function releasePortPollers() {
    const port = currentPort();
    if (!port) return false;
    state.portActionBusy = true;
    syncActionButtons();
    let didStop = false;
    try {
      const res = await apiPost('/ports/release', { port: port.key });
      if (!res.ok || (res.failed && res.failed.length)) throw new Error(portReleaseFailure(res));
      didStop = !!(res.port && res.port.reserved);
      if (!didStop) throw new Error(portReleaseFailure(res));
      state.portRecoveryError = '';
      toast(uiT('Порты зарезервированы: ') + (res.manual_reserved_ports || res.reserved_ports || [port.key]).join(', '), 'success');
    } catch (err) {
      state.portRecoveryError = err.message;
      toast(err.message, 'error');
    } finally {
      state.portActionBusy = false;
      await loadPorts();
    }
    return didStop;
  }

  async function restorePortPollers() {
    const port = currentPort();
    if (!port) return;
    state.portActionBusy = true;
    syncActionButtons();
    try {
      const res = await apiPost('/ports/restore', { port: port.key });
      const lab = (a) => (a || []).map(unitUiLabel).join(', ');
      if (!res.ok || (res.failed && res.failed.length) || (res.pending_restores && res.pending_restores.length)) {
        throw new Error(lab(res.failed) || res.error || uiT('Восстановление опроса не подтверждено.'));
      }
      state.portRecoveryError = '';
      if (res.restarted && res.restarted.length) {
        toast('Опрос восстановлен: ' + lab(res.restarted), 'success');
      } else {
        toast('Штатный опрос уже работает или не был освобождён вручную', 'info');
      }
    } catch (err) {
      state.portRecoveryError = uiT('Восстановление опроса: ') + err.message;
      toast(state.portRecoveryError, 'error');
    } finally {
      state.portActionBusy = false;
      await loadPorts();
    }
  }

  /* ── Инициализация ────────────────────────────────────────────────────── */

  function wireEvents() {
    $('flasher-port').addEventListener('change', updatePortHint);
    $('flasher-refresh-ports-btn').addEventListener('click', loadPorts);
    $('flasher-release-port-btn').addEventListener('click', releasePortPollers);
    $('flasher-restore-port-btn').addEventListener('click', restorePortPollers);
    $('flasher-scan-btn').addEventListener('click', () => {
      if (state.scanJobId || state.scanPending) cancelScan();
      else startScan();
    });
    $('flasher-fw-refresh-btn').addEventListener('click', () => refreshManifest(true));
    $('flasher-fw-clear-btn').addEventListener('click', clearFirmwareCache);
    $('flasher-fw-upload').addEventListener('change', (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (f) uploadFirmware(f);
      ev.target.value = '';
    });
    $('flasher-flash-btn').addEventListener('click', () => {
      if (state.flashJobId || state.flashPending) cancelFlash();
      else startFlash();
    });
    configModalEl('flasher-config-close-btn').addEventListener('click', closeConfigModal);
    configModalEl('flasher-config-modal').addEventListener('click', (ev) => {
      if (ev.target && ev.target.dataset && ev.target.dataset.closeConfigModal === '1') closeConfigModal();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && state.configOpen) closeConfigModal();
    });
    window.addEventListener('beforeunload', (ev) => {
      if (state.scanJobId || state.flashJobId || state.configLinkBusy) {
        ev.preventDefault();
        ev.returnValue = '';
      }
    });
  }

  window.flasherInit = async function () {
    if (state.initialised) {
      await loadPorts();
      loadFirmware();
      await attachToActiveJobs();
      if (!state.scanJobId && !state.flashJobId) loadRecentJobJournal();
      return;
    }
    state.initialised = true;
    wireEvents();
    await loadPorts();
    loadFirmware();
    await attachToActiveJobs();
    if (!state.scanJobId && !state.flashJobId) loadRecentJobJournal();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      if (window.flasherInit) window.flasherInit();
    }, { once: true });
  } else if (window.flasherInit) {
    window.flasherInit();
  }
})();
