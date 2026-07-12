/* ─────────────────────────────────────────────────────────────────────────────
 * flasher.js  •  UI вкладки «Устройства RS-485»
 * Работает с демоном sa02m-flasher через /api/flasher/*. SSE-стрим событий
 * по /api/flasher/jobs/<id>/events. Кука session_token прокидывается nginx'ом.
 * ──────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const API = '/api/flasher';
  const FLASH_JOB_KEY = 'sa02m-flash-job-id';
  const SCAN_JOB_KEY = 'sa02m-scan-job-id';
  const state = {
    initialised: false,
    ports: [],
    devices: [],        // последний результат сканирования
    selectedDeviceIndices: new Set(), // индексы выбранных строк таблицы (multi-select)
    firmware: [],       // список прошивок (entries)
    latestStableVersion: '', // max stable manifest, kind=app
    latestBootloaderVersion: '', // max stable manifest, kind=bootloader
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
    configBackgroundBusy: false,
    configDeviceIdx: -1,
    configTab: '',
    configSnapshot: null,
    configPollTimer: null,
    configNetworkDirty: false,
    configPortReleased: false,
    scanArbitrationActive: false,
  };

  // Счётчик поколений опроса: позволяет отбросить устаревший ответ, если
  // был запущен более новый запрос (переключение вкладки, явное обновление).
  let _configPollSeq = 0;
  // Промис текущей операции restore-порта; release ждёт его завершения.
  let _portOpPromise = null;
  /** Очередь /device_config/* — один Modbus-сеанс на порт. */
  let _configApiTail = Promise.resolve();
  const CONFIG_API_TIMEOUT_MS = 120000;
  const CONFIG_BG_POLL_WAIT_MS = 120000;
  const CONFIG_POLL_INTERVAL_MS = 1000;
  /** AI sensor select под сохранением/редактированием — не перерисовывать вкладку. */
  const _aiSensorEditGuard = new Set();
  const _aiSensorEditGuardTimers = Object.create(null);
  const _aiSensorPending = Object.create(null);
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

  async function apiPost(path, body) {
    const res = await fetch(API + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
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
          setFlashButtons();
          hideProgress();
          if (state2 === 'error' || state2 === 'cancelled') {
            await loadPorts();
            if (state2 === 'error') toast('Прошивка прервана или завершилась с ошибкой. Выполните сканирование.', 'error');
            else toast('Прошивка отменена', 'warn');
          } else {
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
        state.scanJobId = null;
        clearPersistedJobId('scan');
        setScanButtons();
        hideProgress();
        try {
          const snap2 = await apiGet('/jobs/' + jobId);
          replaceScannedDevices((snap2.devices || []).map(d => Object.assign({}, d)));
          renderDevices();
        } catch (_) {}
        const portRec = state.ports.find(p => p.key === portKey);
        if (state2 === 'done' && portHasCompleteLineState(portRec)) {
          markPortIdleAfterJob(portKey);
        } else {
          await loadPorts();
        }
        if (state2 === 'error') setScanStatus('Сканирование завершилось с ошибкой', 'error');
        else if (state2 === 'cancelled') setScanStatus('Сканирование отменено', 'warn');
        else setScanStatus('Сканирование завершено. Найдено ' + state.devices.length + ' устройств.', 'success');
      },
    });
    toast('Восстановлено отслеживание сканирования', 'info');
  }

  async function apiUpload(path, file) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(API + path, { method: 'POST', credentials: 'same-origin', body: fd });
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

  async function loadPorts() {
    try {
      try {
        const quick = await apiGet('/ports?quick=1');
        if (quick && (quick.ports || []).length) {
          state.ports = quick.ports;
          updateGlobalBusyFromPorts();
          renderPortSelect();
          updatePortHint();
        }
      } catch (_) {
        /* достаточно полного ответа ниже */
      }
      const data = await apiGet('/ports');
      state.ports = data.ports || [];
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
    updatePortHint();
    return true;
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
    const releasedServices = port && port.released_services ? port.released_services : [];
    const managedN = port && Array.isArray(port.managed_services) ? port.managed_services.length : 0;
    const selectedDevices = selectedDevicesForFlash();
    const anyChecked = selectedDevices.length > 0;
    const fwEntry = selectedFirmwareEntry();
    const hasFw = !!fwEntry;
    const fwReady = hasFw && isFirmwareEntryDownloaded(fwEntry);
    const selectionErr = anyChecked ? validateMultiFlashSelection(selectedDevices) : '';
    const fwMismatch = firmwareSelectionMismatch(selectedDevices, fwEntry);
    const flashBlocked = !!(selectionErr || fwMismatch);

    $('flasher-scan-btn').disabled = !port || !port.exists || scanRunning || flashRunning || jobBusy || state.portActionBusy;
    $('flasher-scan-cancel-btn').disabled = !state.scanJobId || state.scanPending;
    const canStopPollers = !!(port && port.exists && managedN);
    $('flasher-release-port-btn').disabled = !canStopPollers || scanRunning || flashRunning || jobBusy || state.portActionBusy;
    $('flasher-restore-port-btn').disabled = !port || scanRunning || flashRunning || jobBusy || state.portActionBusy || !releasedServices.length;
    $('flasher-flash-btn').disabled = !port || !port.exists || flashRunning || scanRunning || jobBusy || !(anyChecked && fwReady) || flashBlocked;
    $('flasher-flash-cancel-btn').disabled = !state.flashJobId || state.flashPending || state.flashIrreversible;

    const flashBtn = $('flasher-flash-btn');
    if (flashBtn) {
      updateFlashButtonLabel(selectedDevices.length);
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
      if (!hasFw) {
        fwHint.textContent = 'Выберите файл прошивки из списка или загрузите .fw вручную.';
      } else if (!fwReady) {
        fwHint.textContent = 'Выбранный образ не скачан в кеш шлюза. Нажмите «Скачать» или «Выбрать».';
      } else {
        fwHint.textContent = '';
      }
    }
  }

  function updatePortHint() {
    const port = currentPort();
    const hint = $('flasher-port-hint');
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
    else if (port.active_services == null) setBadge('flasher-poller-badge', 'Проверка опроса', 'unk');
    else if (port.active_services.length) setBadge('flasher-poller-badge', 'Опрос активен', 'unk');
    else if (port.released_services && port.released_services.length) setBadge('flasher-poller-badge', 'Опрос освобождён', 'ok');
    else if (port.busy_pids != null && port.busy_pids.length) setBadge('flasher-poller-badge', 'Опрос не определён', 'unk');
    else setBadge('flasher-poller-badge', 'Опрос не активен', 'ok');

    const pendingDetails =
      port.exists && (port.busy_pids == null || port.active_services == null);
    if (pendingDetails) {
      hint.textContent = 'Проверка порта и опроса';
      syncActionButtons();
      return;
    }

    const bits = [];
    if (port.active_services && port.active_services.length) {
      bits.push('Линию сейчас опрашивают: ' + port.active_services.map(unitUiLabel).join(', ') + '. При сканировании опрос будет остановлен автоматически; кнопка «Остановить опрос» делает это вручную.');
    }
    if (port.released_services && port.released_services.length) {
      bits.push('Опрос вручную освобождён: ' + port.released_services.map(unitUiLabel).join(', ') + '.');
    }
    if (port.busy_pids && port.busy_pids.length) {
      bits.push('Порт удерживают PID ' + port.busy_pids.join(', ') + '.' +
        (port.active_services && port.active_services.length
          ? ' Если это не служба опроса, освободите процесс вручную.'
          : ' systemd не сообщает об активном unit опроса — порт может держать другой процесс; при необходимости проверьте systemctl status и fuser на устройстве.'));
    }
    if (port.active_job) bits.push('На линии выполняется активная задача, дождитесь её завершения.');
    if (!port.exists) bits.push('Устройство порта не найдено в системе.');
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

  function renderFirmware(data) {
    const list = $('flasher-fw-list');
    const prevKey = state.selectedFirmwareKey;
    const visible = visibleFirmwareEntries();
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
    if (!confirm('Удалить все скачанные прошивки из кеша шлюза?\nСписок в манифесте сохранится; файлы нужно будет скачать или загрузить заново.')) return;
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
      '6DO8DI', '16DO', '12AO', '6DO', '14DI', '10DICON', '6DO5DI2AO', '6AO6AI', '12AI',
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
    return n.includes('CE02M3') || n.includes('CE02M');
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
    return String(sig || '').trim() || 'Модуль MR/MP-02м';
  }

  /* Синхронизировать с sa02m_flasher.module_profiles._SIGNATURE_HINTS */
  const SIGNATURE_IO_HINTS = {
    '6DO8DI': [6, 8, 0, 0],
    'DO6DI8': [6, 8, 0, 0],
    '16DO': [16, 0, 0, 0],
    '12AO': [0, 0, 12, 0],
    '6DO': [6, 0, 0, 0],
    '14DI': [0, 14, 0, 0],
    '10DICON': [0, 10, 0, 0],
    '6DO5DI2AO': [6, 5, 2, 0],
    '6AO6AI': [0, 0, 6, 6],
    '6AI6AO': [0, 0, 6, 6],
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
    for (const [key, caps] of Object.entries(SIGNATURE_IO_HINTS)) {
      if (n.includes(key) || n.startsWith(key.slice(0, 4))) return caps.slice();
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
    const latest = state.latestStableVersion;
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
    const latest = state.latestBootloaderVersion;
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
    if (state.configPollTimer) {
      clearInterval(state.configPollTimer);
      state.configPollTimer = null;
    }
  }

  function configPollTick() {
    if (!state.configOpen) return;
    if (state.configBusy || state.configBackgroundBusy) return;
    refreshConfigSnapshot(true, 'panel');
  }

  function startConfigPolling() {
    stopConfigPolling();
    if (!state.configOpen) return;
    state.configPollTimer = setInterval(configPollTick, CONFIG_POLL_INTERVAL_MS);
    setTimeout(configPollTick, 250);
  }

  async function waitConfigBackgroundIdle(timeoutMs) {
    const deadline = Date.now() + (timeoutMs != null ? timeoutMs : CONFIG_BG_POLL_WAIT_MS);
    while (state.configBackgroundBusy && Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 40); });
    }
  }

  async function _autoReleasePortForConfig() {
    // Ожидаем завершения любой текущей операции restore, чтобы
    // не получить ситуацию: restore завершается ПОСЛЕ нашего release
    // и перезапускает опросчик, пока окно конфигурации открыто.
    if (_portOpPromise) {
      try { await _portOpPromise; } catch (_) {}
      _portOpPromise = null;
    }
    const portKey = $('flasher-port').value;
    if (!portKey) return;
    try {
      const res = await apiPost('/ports/release', { port: portKey });
      const busy = res && res.port && Array.isArray(res.port.busy_pids) && res.port.busy_pids.length > 0;
      const mqttStopped = res && (
        (Array.isArray(res.stopped_now) && res.stopped_now.some(s => /modbus-mqtt|mqtt/i.test(String(s)))) ||
        (Array.isArray(res.already_released) && res.already_released.some(s => /modbus-mqtt|mqtt/i.test(String(s)))) ||
        (Array.isArray(res.inactive) && res.inactive.some(s => /modbus-mqtt|mqtt/i.test(String(s))))
      );
      if (res && res.ok && !busy) {
        state.configPortReleased = true;
        return;
      }
      if (!busy && mqttStopped) {
        state.configPortReleased = true;
        return;
      }
      const msg = busy
        ? `Порт ${portKey} занят (PID ${res.port.busy_pids.join(', ')}). Остановите MQTT/MPLC4 и повторите.`
        : `Не удалось освободить ${portKey} для настройки (MQTT/MPLC4).`;
      setConfigBanner(msg, 'error');
      toast(msg, 'warn');
    } catch (err) {
      const msg = 'Освобождение порта для настройки: ' + (err && err.message ? err.message : String(err));
      setConfigBanner(msg, 'error');
      toast(msg, 'warn');
    }
  }

  async function _autoRestorePortForConfig() {
    if (!state.configPortReleased) return;
    const portKey = $('flasher-port').value;
    if (!portKey) return;
    state.configPortReleased = false;
    // Сохраняем промис, чтобы следующий _autoReleasePortForConfig мог его дождаться.
    const p = (async () => {
      try {
        await apiPost('/ports/restore', { port: portKey });
        await loadPorts();
      } catch (_) {}
      if (_portOpPromise === p) _portOpPromise = null;
    })();
    _portOpPromise = p;
    // Не await здесь — closeConfigModal синхронный, restore работает фоново.
    // release в следующем openConfigModal дождётся завершения через _portOpPromise.
  }

  function setConfigBanner(text, type) {
    const el = configModalEl('flasher-config-banner');
    if (!el) return;
    const key = 'flasher-config-banner';
    if (!text) {
      cancelInlineStatusAutoClear(key);
      el.hidden = true;
      el.textContent = '';
      el.className = 'flasher-config-banner';
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = 'flasher-config-banner' + (type === 'error' ? ' is-error' : '');
    scheduleInlineStatusAutoClear(key, () => setConfigBanner(''));
  }

  function setConfigBusy(busy) {
    state.configBusy = !!busy;
  }

  function ensureConfigCloseEnabled() {
    const closeBtn = configModalEl('flasher-config-close-btn');
    if (closeBtn) closeBtn.disabled = false;
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

  function aiSensorSetPending(channel, code) {
    const ch = Number(channel);
    if (!Number.isFinite(ch) || ch <= 0) return;
    _aiSensorPending[ch] = Number(code) & 0xFFFF;
  }

  function aiSensorClearPending(channel) {
    delete _aiSensorPending[Number(channel)];
  }

  function aiChannelConfigProtected(channel) {
    const ch = Number(channel);
    return _aiSensorEditGuard.has(ch) || _aiSensorWriteInflight.has(ch) || _aiSensorPending[ch] != null;
  }

  function aiSensorLabelFromCode(code) {
    const c = Number(code) & 0xFFFF;
    const found = MODULE_AI_SENSOR_CHOICES.find(row => Number(row[0]) === c);
    return found ? found[1] : String(c);
  }

  function aiSensorMetaFromCode(code) {
    const c = Number(code) & 0xFFFF;
    return {
      sensor_code: c,
      sensor_label: aiSensorLabelFromCode(c),
      sidebar_tag: aiSidebarTagFromCode(c),
      ui_bucket: aiUiSensorBucket(c),
      calibration_applicable: aiUiCalibrationApplicable(c),
    };
  }

  function mergeAiChannelFromPoll(prevCh, pollCh, channel) {
    const merged = Object.assign({}, prevCh || {}, pollCh || {});
    if (!aiChannelConfigProtected(channel)) return merged;
    const prev = prevCh || {};
    if (_aiSensorPending[channel] != null) {
      Object.assign(merged, aiSensorMetaFromCode(_aiSensorPending[channel]));
    } else if (prev.sensor_code != null) {
      merged.sensor_code = prev.sensor_code;
      merged.sensor_label = prev.sensor_label != null ? prev.sensor_label : aiSensorLabelFromCode(prev.sensor_code);
      merged.sidebar_tag = prev.sidebar_tag != null ? prev.sidebar_tag : aiSidebarTagFromCode(prev.sensor_code);
      merged.ui_bucket = prev.ui_bucket != null ? prev.ui_bucket : aiUiSensorBucket(prev.sensor_code);
      merged.calibration_applicable = aiUiCalibrationApplicable(prev.sensor_code);
    }
    return merged;
  }

  function patchAiChannelSnapshot(channel, patch) {
    const snap = state.configSnapshot;
    const items = snap && snap.mr && snap.mr.ai && snap.mr.ai.channels;
    if (!items) return;
    const ch = items.find(item => Number(item.channel) === Number(channel));
    if (ch && patch) Object.assign(ch, patch);
  }

  function clearAiConfigEditState() {
    _aiSensorEditGuard.clear();
    Object.keys(_aiSensorEditGuardTimers).forEach(function (key) {
      clearTimeout(_aiSensorEditGuardTimers[key]);
      delete _aiSensorEditGuardTimers[key];
    });
    Object.keys(_aiSensorPending).forEach(function (key) { delete _aiSensorPending[key]; });
    _aiSensorWriteInflight.clear();
  }

  async function configApi(path, body) {
    const isDeviceConfig = String(path || '').startsWith('/device_config/');
    const run = async () => {
      const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const timer = ctrl
        ? setTimeout(function () { try { ctrl.abort(); } catch (_) {} }, CONFIG_API_TIMEOUT_MS)
        : null;
      let res;
      try {
        res = await fetch(API + path, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
          body: JSON.stringify(body || {}),
          signal: ctrl ? ctrl.signal : undefined,
        });
      } finally {
        if (timer) clearTimeout(timer);
      }
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { const data = await res.json(); if (data && data.error) msg = data.error; } catch (_) {}
        throw new Error(msg);
      }
      return res.json();
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
    const b = aiUiSensorBucket(sensorCode);
    return b === 'ntc' || b === 'rtd' || b === 'tc_k';
  }

  function aiSidebarTagFromCode(code) {
    const b = aiUiSensorBucket(code);
    if (b === 'off') return 'Выкл';
    const map = { ntc: 'NTC', rtd: 'RTD', volt: 'U', curr: 'I', tc_k: 'TC-K', dry: 'Сух' };
    return map[b] || 'AI';
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
    if (k === 'current')     return (v / 10).toFixed(1) + ' мА';
    if (k === 'voltage_010') return (v / 100).toFixed(2) + ' В';
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

  function mergeMrMinimalIntoFull(prevMr, minMr) {
    if (!prevMr || !minMr) return minMr || prevMr;
    const out = JSON.parse(JSON.stringify(prevMr));
    out.module = Object.assign({}, out.module || {}, minMr.module || {});
    out.inactivity_s = minMr.inactivity_s;
    out.relay = Object.assign({}, out.relay || {}, minMr.relay || {});
    if (minMr.do) {
      const md = minMr.do;
      out.do = out.do || {};
      if (Array.isArray(md.safe) && md.safe.length) {
        out.do = {
          bits: (md.bits || []).slice(),
          counts: (md.counts || []).slice(),
          safe: (md.safe || []).slice(),
          timer_words: (md.timer_words || []).slice(),
          redelay: (md.redelay || []).slice(),
        };
      } else {
        if (md.bits) out.do.bits = md.bits.slice();
        if (md.counts) out.do.counts = md.counts.slice();
      }
    }
    if (minMr.di) {
      const mdi = minMr.di;
      out.di = out.di || {};
      if (Array.isArray(mdi.mode) && mdi.mode.length > 0) {
        out.di = {
          values: (mdi.values || []).slice(),
          counts: (mdi.counts || []).slice(),
          short_counts: (mdi.short_counts || []).slice(),
          long_counts: (mdi.long_counts || []).slice(),
          double_counts: (mdi.double_counts || []).slice(),
          freq: (mdi.freq || []).slice(),
          mode: (mdi.mode || []).slice(),
          debounce: (mdi.debounce || []).slice(),
          long_press: (mdi.long_press || []).slice(),
          double_click: (mdi.double_click || []).slice(),
          freq_mode: (mdi.freq_mode || []).slice(),
        };
      } else if (mdi.values) {
        out.di.values = mdi.values.slice();
      }
    }
    if (minMr.ao) {
      const ma = minMr.ao;
      out.ao = out.ao || {};
      if (Array.isArray(ma.setpoint) && ma.setpoint.length) {
        out.ao = {
          current_raw: (ma.current_raw || []).slice(),
          current_volts: (ma.current_volts || []).slice(),
          setpoint: (ma.setpoint || []).slice(),
          safe: (ma.safe || []).slice(),
          safe_holding_regs: (ma.safe_holding_regs || []).slice(),
        };
      } else if (ma.current_raw && ma.current_raw.length) {
        out.ao.current_raw = ma.current_raw.slice();
        out.ao.current_volts = (ma.current_volts || []).slice();
      }
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
          byCh[ch] = mergeAiChannelFromPoll(byCh[ch], mc, ch);
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
          byCh[ch] = mergeAiChannelFromPoll(base, mc, ch);
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
    const el = document.activeElement;
    if (!el) return false;
    const tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') return false;
    const body = configModalEl('flasher-config-body');
    return body ? body.contains(el) : false;
  }

  function patchConfigLiveReadouts(snap) {
    if (!snap || snap.kind !== 'mr') return;
    const mr = snap.mr || {};
    const mcu = mr.mcu || {};
    const setText = function (id, text) {
      const el = configModalEl(id);
      if (el) el.textContent = text;
    };
    if (mcu.power_v != null) setText('cfg-mr-mcu-power', Number(mcu.power_v).toFixed(2) + ' В');
    if (mcu.temp_c != null) setText('cfg-mr-mcu-temp', Number(mcu.temp_c).toFixed(1) + ' °C');
    if (mcu.uptime_str != null) setText('cfg-mr-mcu-uptime', String(mcu.uptime_str));

    const ao = mr.ao || {};
    (ao.current_volts || []).forEach(function (v, idx) {
      const ch = idx + 1;
      setText('cfg-mr-ao-live-' + ch, formatFloat(v, 2) + ' В');
      if (ao.current_raw) setText('cfg-mr-ao-raw-' + ch, String(ao.current_raw[idx] ?? 0));
    });

    const bits = ((mr.do || {}).bits || []);
    bits.forEach(function (on, idx) {
      const ch = idx + 1;
      const el = configModalEl('cfg-mr-do-state-' + ch);
      if (!el) return;
      el.textContent = on ? 'Вкл' : 'Выкл';
      el.classList.toggle('do-state-on', !!on);
    });

    const diVals = ((mr.di || {}).values || []);
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
    const on = !!bits[channel - 1];
    return { suffix: ` - ${on ? 'ВКЛ' : 'ВЫКЛ'}`, live: on };
  }

  function moduleDiTabInfo(snap, channel) {
    const values = ((((snap || {}).mr || {}).di || {}).values || []);
    const on = Number(values[channel - 1] || 0) !== 0;
    return { suffix: ` - ${on ? 'ВКЛ' : 'ВЫКЛ'}`, live: on };
  }

  function moduleAoTabInfo(snap, channel) {
    const volts = ((((snap || {}).mr || {}).ao || {}).current_volts || []);
    const value = Number(volts[channel - 1]);
    const hasValue = Number.isFinite(value);
    return {
      suffix: hasValue ? ` - ${formatFloat(value, 2)} В` : ' - 0.0 В',
      live: hasValue && Math.abs(value) > 0.01,
    };
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
          <div class="flasher-config-note" style="margin-top:10px">
            Modbus watchdog (рег. 134):
            <input id="cfg-mr-inactivity-global" type="number" min="0" max="255"
              value="${escapeHtml(String(mr.inactivity_s ?? 0))}"
              style="width:70px;margin:0 6px;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border-md);border-radius:var(--radius-xs);color:var(--text);" /> с
            <button class="btn btn-sm" type="button" id="cfg-mr-inactivity-save-btn" style="margin-left:4px">Сохранить</button>
          </div>
        </section>

      </div>
    `;
  }

  function renderModuleRelayTab(snap) {
    const relay = ((snap.mr || {}).relay || {});
    const options = Number(relay.options || 0);
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>Реле и задержки</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-relay-mode">Режим работы</label>
            <select id="cfg-mr-relay-mode">
              ${MODULE_RELAY_MODES.map(item => `<option value="${item.value}" ${Number(item.value) === Number(relay.mode || 0) ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('')}
            </select>
            ${MODULE_RELAY_OPTION_BITS.map(item => `
              <label class="checkbox-line"><input type="checkbox" id="cfg-mr-relay-opt-${item.bit}" ${(options & (1 << item.bit)) ? 'checked' : ''} /> ${escapeHtml(item.label)}</label>
            `).join('')}
            <label for="cfg-mr-relay-stagger">Задержка включения при питании, с</label>
            <input id="cfg-mr-relay-stagger" type="number" min="0" max="65535" value="${escapeHtml(String(relay.power_stagger ?? 0))}" />
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" id="cfg-mr-relay-save-btn">Сохранить</button>
          </div>
        </section>
      </div>
    `;
  }

  function renderModuleDoTab(snap, channel) {
    const mr = snap.mr || {};
    const bits = ((mr.do || {}).bits || []);
    const counts = ((mr.do || {}).counts || []);
    const safe = ((mr.do || {}).safe || []);
    const timerWords = ((mr.do || {}).timer_words || []);
    const redelay = ((mr.do || {}).redelay || []);
    const idx = channel - 1;
    const timerWord = Number(timerWords[idx] || 0);
    const timerMode = timerWord & 0xFF;
    const timerTime = (timerWord >> 8) & 0xFF;
    return `
      <div class="flasher-config-grid">
        <section class="flasher-config-card">
          <h4>DO${channel}</h4>
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Состояние</span><strong id="cfg-mr-do-state-${channel}" class="do-state-value${bits[idx] ? ' do-state-on' : ''}">${bits[idx] ? 'Вкл' : 'Выкл'}</strong></div>
            <div class="flasher-config-row"><span>Счетчик включений</span><strong>${escapeHtml(String(counts[idx] ?? 0))}</strong></div>
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-sm ${bits[idx] ? '' : 'btn-success'}" type="button" data-mr-do-on="${channel}" data-do-channel="${channel}">Включить</button>
            <button class="btn btn-sm ${bits[idx] ? 'btn-danger' : ''}" type="button" data-mr-do-off="${channel}" data-do-channel="${channel}">Выключить</button>
            <button class="btn btn-sm" type="button" data-mr-do-reset="1">Сброс счетчиков DO</button>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Настройки канала</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-do-safe-${channel}">Безопасное состояние, 0/1</label>
            <input id="cfg-mr-do-safe-${channel}" type="number" min="0" max="1" value="${escapeHtml(String(safe[idx] ?? 0))}" />
            ${idx < timerWords.length ? `
              <label for="cfg-mr-do-mode-${channel}">Таймер, режим</label>
              <select id="cfg-mr-do-mode-${channel}">
                ${MODULE_TIMER_MODES.map(item => `<option value="${item.value}" ${Number(item.value) === timerMode ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('')}
              </select>
              <label for="cfg-mr-do-time-${channel}">Таймер, время x0.1 c</label>
              <input id="cfg-mr-do-time-${channel}" type="number" min="0" max="255" value="${escapeHtml(String(timerTime))}" />
              <label for="cfg-mr-do-redelay-${channel}">Пауза повторного включения, с</label>
              <input id="cfg-mr-do-redelay-${channel}" type="number" min="0" max="999" value="${escapeHtml(String(redelay[idx] ?? 0))}" />
            ` : ''}
          </div>
          <div class="flasher-config-note">Таймеры / redelay — только DO1…DO6 (как в desktop).</div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-do-save="${channel}">Сохранить DO${channel}</button>
          </div>
        </section>
      </div>
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
          <div class="flasher-config-list">
            <div class="flasher-config-row"><span>Состояние</span><strong id="cfg-mr-di-state-${channel}" class="${Number((di.values || [])[idx] || 0) ? 'di-state-on' : ''}">${Number((di.values || [])[idx] || 0) ? 'Активен' : 'Неактивен'}</strong></div>
            <div class="flasher-config-row"><span>Счетчик импульсов</span><strong>${escapeHtml(String((di.counts || [])[idx] ?? 0))}</strong></div>
            <div class="flasher-config-row"><span>Коротких / длинных / двойных</span><strong>${escapeHtml(String((di.short_counts || [])[idx] ?? 0))} / ${escapeHtml(String((di.long_counts || [])[idx] ?? 0))} / ${escapeHtml(String((di.double_counts || [])[idx] ?? 0))}</strong></div>
            <div class="flasher-config-row"><span>Частота</span><strong>${escapeHtml(String((di.freq || [])[idx] ?? 0))} Гц</strong></div>
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
              ${MODULE_DI_MODES.map(item => `<option value="${item.value}" ${Number(item.value) === Number((di.mode || [])[idx] || 0) ? 'selected' : ''}>${escapeHtml(item.label)}</option>`).join('')}
            </select>
            <label for="cfg-mr-di-debounce-${channel}">Антидребезг, мс</label>
            <input id="cfg-mr-di-debounce-${channel}" type="number" min="0" max="100" value="${escapeHtml(String((di.debounce || [])[idx] ?? 0))}" />
            <label for="cfg-mr-di-long-${channel}">Длинное нажатие, мс</label>
            <input id="cfg-mr-di-long-${channel}" type="number" min="500" max="5000" value="${escapeHtml(String((di.long_press || [])[idx] ?? 500))}" />
            <label for="cfg-mr-di-double-${channel}">Окно двойного нажатия, мс</label>
            <input id="cfg-mr-di-double-${channel}" type="number" min="0" max="2000" value="${escapeHtml(String((di.double_click || [])[idx] ?? 0))}" />
            ${channel <= 8 ? `<label class="checkbox-line"><input id="cfg-mr-di-freqmode-${channel}" type="checkbox" ${Number((di.freq_mode || [])[idx] || 0) ? 'checked' : ''} /> Режим измерения частоты</label>` : ''}
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-di-save="${channel}">Сохранить DI${channel}</button>
          </div>
        </section>
      </div>
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
            <div class="flasher-config-row"><span>Текущее значение</span><strong id="cfg-mr-ao-live-${channel}">${formatFloat((ao.current_volts || [])[idx], 2)} В</strong></div>
            <div class="flasher-config-row"><span>Raw</span><strong id="cfg-mr-ao-raw-${channel}">${escapeHtml(String((ao.current_raw || [])[idx] ?? 0))}</strong></div>
          </div>
        </section>
        <section class="flasher-config-card">
          <h4>Уставки AO</h4>
          <div class="flasher-config-form">
            <label for="cfg-mr-ao-set-${channel}">Задание, 0..1000</label>
            <input id="cfg-mr-ao-set-${channel}" type="number" min="0" max="1000" value="${escapeHtml(String((ao.setpoint || [])[idx] ?? 0))}" />
            <label for="cfg-mr-ao-safe-${channel}">Безопасное состояние, 0..1000</label>
            <input id="cfg-mr-ao-safe-${channel}" type="number" min="0" max="1000" value="${escapeHtml(String((ao.safe || [])[idx] ?? 0))}" />
            <label for="cfg-mr-ao-inactivity-${channel}">Время без опроса, с</label>
            <input id="cfg-mr-ao-inactivity-${channel}" type="number" min="0" max="255" value="${escapeHtml(String(mr.inactivity_s ?? 0))}" />
          </div>
          <div class="flasher-config-actions">
            <button class="btn btn-primary" type="button" data-mr-ao-save="${channel}">Сохранить AO${channel}</button>
          </div>
        </section>
      </div>
    `;
  }

  function renderModuleAiTab(snap, channel) {
    const ai = moduleAiChannel(snap, channel);
    if (!ai) return '<div class="flasher-empty">Канал AI не найден.</div>';
    const filters = ai.filters || null;
    const sensorCode = Number(ai.sensor_code || 0);
    const bucket = ai.ui_bucket || aiUiSensorBucket(sensorCode);
    const calOk = ai.calibration_applicable != null ? !!ai.calibration_applicable : aiUiCalibrationApplicable(sensorCode);

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
    const refLimits = aiSensorRefLimits(sensorCode);

    const modeRadios = MODULE_AI_UI_BUCKETS.map(b => `
      <label class="ai-mode-radio-label">
        <input type="radio" name="cfg-mr-ai-mode-${channel}" value="${escapeHtml(b.id)}"
          data-mr-ai-bucket="${channel}" ${b.id === bucket ? 'checked' : ''} />
        <span>${escapeHtml(b.label)}</span>
      </label>`).join('');

    const limitsHtml = refLimits ? `
      <div class="ai-limits-row">
        <span>Нижний предел:</span><strong>${escapeHtml(refLimits.lo)}</strong>
      </div>
      <div class="ai-limits-row">
        <span>Верхний предел:</span><strong>${escapeHtml(refLimits.hi)}</strong>
      </div>` : '';

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
          ${Number(filters.kalman || 0) ? 'checked' : ''} data-mr-ai-filter="${channel}" />
        Фильтр Калмана
      </label>
      <div class="ai-filter-row">
        <span>Частота опроса АЦП, выб./сек:</span>
        <select id="cfg-mr-ai-sps-${channel}" data-mr-ai-filter="${channel}">
          ${MODULE_AI_SAMPLE_RATES.map(r => `<option value="${r}" ${Number(r) === Number(filters.sps || 45) ? 'selected' : ''}>${r}</option>`).join('')}
        </select>
      </div>
      <div class="ai-filter-row">
        <span>Число выборок, 0...50:</span>
        <input id="cfg-mr-ai-avg-${channel}" type="number" min="0" max="50"
          value="${escapeHtml(String(filters.avg ?? 0))}" data-mr-ai-filter="${channel}" />
      </div>
      <div class="ai-filter-row">
        <span>Пост. времени НЧ-фильтра, мс.:</span>
        <input id="cfg-mr-ai-tau-${channel}" type="number" min="0" max="65535"
          value="${escapeHtml(String(filters.tau ?? 0))}" data-mr-ai-filter="${channel}" />
      </div>` : '';

    return `
      <div class="ai-channel-panel">
        <h3 class="ai-channel-title">Аналоговый вход AI${channel}</h3>

        <section class="flasher-config-card ai-section-measures">
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
                  <span>Калибровка</span>
                  <button class="ai-cal-btn" data-mr-ai-cal-step="${channel}" data-step="-1">−</button>
                  <input id="cfg-mr-ai-cal-${channel}" type="number" min="-100" max="100"
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
      </div>
    `;
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

  function renderCeSettingsTab(snap) {
    const cfg = (snap.ce && snap.ce.config) || {};
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
            <input id="cfg-ce-kt-a" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_a ?? 1000))}" />

            <label for="cfg-ce-kt-b">Коэффициент ТТ B (K×1000)</label>
            <input id="cfg-ce-kt-b" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_b ?? 1000))}" />

            <label for="cfg-ce-kt-c">Коэффициент ТТ C (K×1000)</label>
            <input id="cfg-ce-kt-c" type="number" min="1" max="20000" value="${escapeHtml(String(cfg.kt_c ?? 1000))}" />
          </div>
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

  function renderConfigBody() {
    const host = configModalEl('flasher-config-body');
    if (!host) return;
    const snap = state.configSnapshot;
    if (!snap) {
      host.innerHTML = '<div class="flasher-empty">Загрузка настроек…</div>';
      return;
    }
    let html = '';
    if (state.configTab === 'network') html = renderNetworkTab(snap);
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
    wireConfigBodyEvents();
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
        renderConfigTabs();
        renderConfigBody();
        await waitConfigBackgroundIdle();
        await refreshConfigSnapshot(true, 'full');
      });
    });
  }

  function aiSensorReconcilePending(mr) {
    if (!mr || !mr.ai || !Array.isArray(mr.ai.channels)) return;
    mr.ai.channels.forEach(ch => {
      const n = Number(ch.channel);
      if (_aiSensorPending[n] == null) return;
      if ((Number(ch.sensor_code) & 0xFFFF) === _aiSensorPending[n]) {
        aiSensorClearPending(n);
      }
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
    if (merged && merged.mr) aiSensorReconcilePending(merged.mr);
    state.configSnapshot = merged;
    configDeviceFromSnapshot(merged);
    renderDevices();
    const title = configModalEl('flasher-config-title');
    const sub = configModalEl('flasher-config-sub');
    const kicker = configModalEl('flasher-config-kicker');
    if (title) title.textContent = deviceConfigTitle(merged.kind, merged.info && merged.info.signature);
    if (kicker) kicker.textContent = merged.kind === 'dtv' ? 'Настройка датчика' : merged.kind === 'ce' ? 'Настройка анализатора сети' : 'Настройка модуля расширения';
    if (sub) {
      const info = merged.info || {};
      sub.textContent = `${info.address ?? '—'} адр. · ${(merged.network && merged.network.baudrate) || '—'} ${(merged.network && merged.network.parity) || 'N'}${(merged.network && merged.network.stopbits) || 1} · ${serialHex(info.serial)}`;
    }
    if (!state.configTab) state.configTab = 'info';
    const available = configTabsForSnapshot(merged).map(t => t.id);
    if (!available.includes(state.configTab)) state.configTab = available[0] || 'info';
    renderConfigTabs();
    if (shouldSkipConfigBodyRerender()) patchConfigLiveReadouts(merged);
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
        active_tab: state.configTab || '',
      });
      if (mySeq !== _configPollSeq) return;
      applyConfigSnapshot(snap, !!silent);
    } catch (err) {
      if (mySeq !== _configPollSeq) return;
      if (silent) {
        // Фоновая ошибка: не блокируем UI баннером
      } else {
        setConfigBanner('Не удалось загрузить настройки: ' + err.message, 'error');
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
    state.configNetworkDirty = false;
    state.configBusy = false;
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
    await _autoReleasePortForConfig();
    await refreshConfigSnapshot(false);
    startConfigPolling();
    ensureConfigCloseEnabled();
  }

  function closeConfigModal() {
    stopConfigPolling();
    ++_configPollSeq; // инвалидируем все текущие in-flight запросы этой сессии
    ensureConfigCloseEnabled();
    state.configOpen = false;
    state.configBusy = false;
    state.configBackgroundBusy = false;
    state.configDeviceIdx = -1;
    state.configSnapshot = null;
    state.configTab = '';
    const modal = configModalEl('flasher-config-modal');
    if (modal) modal.hidden = true;
    document.body.style.overflow = '';
    clearAiConfigEditState();
    _autoRestorePortForConfig();
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

  async function writeConfigHolding(reg, value, successText) {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return null;
    const snap = await configApi('/device_config/holding', { port, device: dev, reg, value });
    applyConfigSnapshot(snap, false);
    if (successText) toast(successText, 'success');
    return snap;
  }

  async function writeConfigCoil(coil, value, successText) {
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return null;
    const snap = await configApi('/device_config/coil', { port, device: dev, coil, value });
    applyConfigSnapshot(snap, false);
    if (successText) toast(successText, 'success');
    return snap;
  }

  async function saveCeSettings() {
    const items = [
      { reg: 553, value: parseInt(configModalEl('cfg-ce-ph-loss').value, 10) || 0 },
      { reg: 554, value: parseInt(configModalEl('cfg-ce-inv-a').value, 10) || 0 },
      { reg: 555, value: parseInt(configModalEl('cfg-ce-inv-b').value, 10) || 0 },
      { reg: 556, value: parseInt(configModalEl('cfg-ce-inv-c').value, 10) || 0 },
      { reg: 557, value: parseInt(configModalEl('cfg-ce-kt-a').value, 10) || 1 },
      { reg: 558, value: parseInt(configModalEl('cfg-ce-kt-b').value, 10) || 1 },
      { reg: 559, value: parseInt(configModalEl('cfg-ce-kt-c').value, 10) || 1 },
    ];
    setConfigBusy(true);
    try {
      for (const item of items) await writeConfigHolding(item.reg, item.value, '');
      toast('Настройки CE-02м-3 сохранены', 'success');
    } catch (err) {
      setConfigBanner('Запись настроек CE-02м-3: ' + err.message, 'error');
      toast('CE-02м-3: ' + err.message, 'error');
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

  async function writeHoldingBatch(items, successText, errorPrefix) {
    setConfigBusy(true);
    try {
      for (const item of items) {
        await writeConfigHolding(item.reg, item.value, '');
      }
      if (successText) toast(successText, 'success');
    } catch (err) {
      setConfigBanner(errorPrefix + err.message, 'error');
      toast(errorPrefix + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  async function saveModuleRelay() {
    let options = 0;
    MODULE_RELAY_OPTION_BITS.forEach((item) => {
      const checkbox = configModalEl('cfg-mr-relay-opt-' + item.bit);
      if (checkbox && checkbox.checked) options |= (1 << item.bit);
    });
    await writeHoldingBatch([
      { reg: 130, value: clampInt(configModalEl('cfg-mr-relay-mode').value, 0, 5, 0) },
      { reg: 131, value: options },
      { reg: 622, value: clampInt(configModalEl('cfg-mr-relay-stagger').value, 0, 65535, 0) },
    ], 'Настройки реле сохранены', 'Реле: ');
  }

  async function toggleModuleDo(channel, on) {
    const prev = state.configSnapshot;
    if (prev && prev.kind === 'mr' && prev.mr && prev.mr.do && Array.isArray(prev.mr.do.bits)) {
      try {
        const copy = JSON.parse(JSON.stringify(prev));
        const bits = copy.mr.do.bits.slice();
        const ix = channel - 1;
        if (ix >= 0 && ix < bits.length) bits[ix] = !!on;
        copy.mr.do.bits = bits;
        state.configSnapshot = copy;
        renderConfigTabs();
        renderConfigBody();
      } catch (_) {}
    }
    setConfigBusy(true);
    try {
      await writeConfigCoil(channel, !!on, on ? `DO${channel} включен` : `DO${channel} выключен`);
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
    await writeHoldingBatch([
      { reg: 134, value: clampInt(configModalEl('cfg-mr-inactivity-global').value, 0, 255, 0) },
    ], 'Таймаут линии сохранён', 'Modbus: ');
  }

  async function saveModuleDo(channel) {
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
    await writeHoldingBatch(items, `Настройки DO${channel} сохранены`, `DO${channel}: `);
  }

  async function resetModuleDiCounters() {
    await writeHoldingBatch([{ reg: 694, value: 1 }], 'Счетчики DI сброшены', 'DI: ');
  }

  async function saveModuleDi(channel) {
    const items = [
      { reg: 630 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-mode-${channel}`).value, 0, 1, 0) },
      { reg: 646 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-debounce-${channel}`).value, 0, 100, 0) },
      { reg: 662 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-long-${channel}`).value, 500, 5000, 500) },
      { reg: 678 + channel - 1, value: clampInt(configModalEl(`cfg-mr-di-double-${channel}`).value, 0, 2000, 0) },
    ];
    const freqMode = configModalEl(`cfg-mr-di-freqmode-${channel}`);
    if (freqMode) items.push({ reg: 750 + channel - 1, value: freqMode.checked ? 1 : 0 });
    await writeHoldingBatch(items, `Настройки DI${channel} сохранены`, `DI${channel}: `);
  }

  async function saveModuleAo(channel) {
    const safeReg = aoSafeHoldingRegForChannel(state.configSnapshot, channel);
    await writeHoldingBatch([
      { reg: 33 + channel - 1, value: clampInt(configModalEl(`cfg-mr-ao-set-${channel}`).value, 0, 1000, 0) },
      { reg: safeReg, value: clampInt(configModalEl(`cfg-mr-ao-safe-${channel}`).value, 0, 1000, 0) },
      { reg: 134, value: clampInt(configModalEl(`cfg-mr-ao-inactivity-${channel}`).value, 0, 255, 0) },
    ], `Настройки AO${channel} сохранены`, `AO${channel}: `);
  }

  async function applyAiSensorCode(channel, desiredCode) {
    const ch = Number(channel);
    if (!Number.isFinite(ch) || ch <= 0) return;
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

    aiSensorSetPending(ch, sensorVal);
    const currentCode = Number(ai.sensor_code) & 0xFFFF;
    if (currentCode === sensorVal) {
      patchAiChannelSnapshot(ch, aiSensorMetaFromCode(sensorVal));
      return;
    }

    aiSensorEditGuardAdd(ch);
    _aiSensorWriteInflight.add(ch);
    setConfigBusy(true);
    try {
      await writeConfigHolding(base, sensorVal, '');
      patchAiChannelSnapshot(ch, aiSensorMetaFromCode(sensorVal));
      toast(`Тип AI${ch} применён`, 'success');
    } catch (err) {
      aiSensorClearPending(ch);
      setConfigBanner(`AI${ch}: ` + err.message, 'error');
      toast(`AI${ch}: ` + err.message, 'error');
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
        `Калибровка AI${ch} применена`, `AI${ch}: `
      );
      patchAiChannelSnapshot(ch, { calibration: wantCal });
    } finally {
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  async function applyAiFilters(channel) {
    const ch = Number(channel);
    const ai = moduleAiChannel(state.configSnapshot, ch);
    if (!ai || !ai.filters) return;
    const stor = Number(ai.filters.stor || 0);
    const spsEl = configModalEl(`cfg-mr-ai-sps-${ch}`);
    const rawSps = clampInt(spsEl && spsEl.value, 20, 1000, 45);
    const sps = MODULE_AI_SAMPLE_RATES.reduce((best, item) => Math.abs(item - rawSps) < Math.abs(best - rawSps) ? item : best, MODULE_AI_SAMPLE_RATES[0]);
    const kalmanEl = configModalEl(`cfg-mr-ai-kalman-${ch}`);
    const avgEl = configModalEl(`cfg-mr-ai-avg-${ch}`);
    const tauEl = configModalEl(`cfg-mr-ai-tau-${ch}`);
    const wantKalman = kalmanEl && kalmanEl.checked ? 1 : 0;
    const wantAvg = avgEl ? clampInt(avgEl.value, 0, 50, 0) : 0;
    const wantTau = tauEl ? clampInt(tauEl.value, 0, 65535, 0) : 0;
    const filters = ai.filters || {};
    if (
      Number(filters.kalman || 0) === wantKalman &&
      Number(filters.sps || 0) === sps &&
      Number(filters.avg || 0) === wantAvg &&
      Number(filters.tau || 0) === wantTau
    ) return;
    const items = [
      { reg: 491 + stor, value: wantKalman },
      { reg: 533 + 3 * stor, value: sps },
      { reg: 534 + 3 * stor, value: wantAvg },
      { reg: 535 + 3 * stor, value: wantTau },
    ];
    aiSensorEditGuardAdd(ch);
    try {
      await writeHoldingBatch(items, `Фильтры AI${ch} применены`, `AI${ch}: `);
    } finally {
      aiSensorEditGuardReleaseLater(ch);
    }
  }

  function refreshAiCalibrationVisibility(channel) {
    const sensorEl = configModalEl(`cfg-mr-ai-sensor-${channel}`);
    const strip = configModalEl(`cfg-mr-ai-cal-strip-${channel}`);
    if (!sensorEl) return;
    const ok = aiUiCalibrationApplicable(parseInt(sensorEl.value, 10) || 0);
    if (strip) strip.hidden = !ok;
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
    const lim = aiSensorRefLimits(code);
    limBox.innerHTML = lim ? `
      <div class="ai-limits-row"><span>Нижний предел:</span><strong>${escapeHtml(lim.lo)}</strong></div>
      <div class="ai-limits-row"><span>Верхний предел:</span><strong>${escapeHtml(lim.hi)}</strong></div>` : '';
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
        aiSensorSetPending(channel, code);
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
        const cur = parseInt(inp.value, 10) || 0;
        inp.value = Math.max(-100, Math.min(100, cur + step));
        applyAiCalibration(channel);
      });
    });

    // Фильтры АЦП: change для select/checkbox, blur для числовых полей
    body.querySelectorAll('[data-mr-ai-filter]').forEach(el => {
      const ch = parseInt(el.dataset.mrAiFilter, 10);
      if (el.tagName === 'SELECT' || el.type === 'checkbox') {
        el.addEventListener('change', () => applyAiFilters(ch));
      } else {
        el.addEventListener('blur', () => applyAiFilters(ch));
      }
    });
  }

  function exportModuleConfig() {
    const snap = state.configSnapshot;
    if (!snap) { toast('Нет данных для экспорта', 'warn'); return; }
    const info = snap.info || {};
    const mr = snap.mr || {};
    const payload = {
      _meta: {
        exported_at: new Date().toISOString(),
        signature: info.signature || '',
        serial_hex: info.serial != null ? ('0x' + (info.serial >>> 0).toString(16).toUpperCase().padStart(8, '0')) : '',
        app_version: info.app_version || '',
      },
      mr: {
        relay: mr.relay || {},
        inactivity_s: mr.inactivity_s ?? 0,
        do: { safe: (mr.do || {}).safe || [], timer_words: (mr.do || {}).timer_words || [], redelay: (mr.do || {}).redelay || [] },
        di: { mode: (mr.di || {}).mode || [], debounce: (mr.di || {}).debounce || [], long_press: (mr.di || {}).long_press || [], double_click: (mr.di || {}).double_click || [] },
        ao: { safe_holding_regs: (mr.ao || {}).safe_holding_regs || [] },
        ai: { channels: ((mr.ai || {}).channels || []).map(ch => ({ channel: ch.channel, sensor_code: ch.sensor_code, calibration: ch.calibration })) },
      },
    };
    const json = JSON.stringify(payload, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const sig = (info.signature || 'module').replace(/[^a-zA-Z0-9_-]/g, '_');
    a.href = url;
    a.download = `config_${sig}_${(info.serial != null ? (info.serial >>> 0).toString(16).toUpperCase() : 'unknown')}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Конфигурация сохранена в файл', 'success');
  }

  async function importModuleConfig(fileInput) {
    if (!fileInput || !fileInput.files || !fileInput.files[0]) return;
    const file = fileInput.files[0];
    fileInput.value = '';
    let json;
    try {
      json = JSON.parse(await file.text());
    } catch (e) {
      toast('Ошибка чтения JSON-файла: ' + e.message, 'error');
      return;
    }
    const snap = state.configSnapshot;
    if (!snap || snap.kind !== 'mr') { toast('Откройте окно модуля MR/MP-02м', 'warn'); return; }
    const mr = json.mr || {};
    const dev = currentConfigDevice();
    const port = $('flasher-port').value;
    if (!dev || !port) return;
    setConfigBusy(true);
    try {
      // inactivity
      if (mr.inactivity_s != null) await configApi('/device_config/holding', { port, device: dev, reg: 134, value: Math.max(0, Math.min(255, parseInt(mr.inactivity_s, 10) || 0)) });
      // relay
      const relay = mr.relay || {};
      if (relay.mode != null) await configApi('/device_config/holding', { port, device: dev, reg: 130, value: parseInt(relay.mode, 10) & 0xFFFF });
      if (relay.options != null) await configApi('/device_config/holding', { port, device: dev, reg: 131, value: parseInt(relay.options, 10) & 0xFFFF });
      if (relay.power_stagger != null) await configApi('/device_config/holding', { port, device: dev, reg: 622, value: Math.max(0, Math.min(65535, parseInt(relay.power_stagger, 10) || 0)) });
      toast('Конфигурация загружена из файла', 'success');
      await refreshConfigSnapshot(false, 'full');
    } catch (err) {
      setConfigBanner('Импорт конфигурации: ' + err.message, 'error');
      toast('Импорт конфигурации: ' + err.message, 'error');
    } finally {
      setConfigBusy(false);
    }
  }

  async function rebootModuleDevice() {
    if (!confirm('Перезагрузить модуль?\nЛиния может быть временно недоступна.')) return;
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
    const saveNet = body.querySelector('#cfg-net-save-btn');
    if (saveNet) saveNet.addEventListener('click', saveConfigNetwork);
    const refreshNet = body.querySelector('#cfg-net-refresh-btn');
    if (refreshNet) refreshNet.addEventListener('click', () => refreshConfigSnapshot(false, 'full'));
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
          state.scanJobId = null;
          clearPersistedJobId('scan');
          setScanButtons(); hideProgress();
          try {
            const snap = await apiGet('/jobs/' + res.job_id);
            replaceScannedDevices((snap.devices || []).map(d => Object.assign({}, d)));
            renderDevices();
          } catch (_) {}
          const portRec = state.ports.find(p => p.key === body.port);
          if (state2 === 'done' && portHasCompleteLineState(portRec)) {
            markPortIdleAfterJob(body.port);
          } else {
            await loadPorts();
          }
          if (state2 === 'error') setScanStatus('Сканирование завершилось с ошибкой', 'error');
          else if (state2 === 'cancelled') setScanStatus('Сканирование отменено', 'warn');
          else setScanStatus('Сканирование завершено. Найдено ' + state.devices.length + ' устройств.', 'success');
        },
      });
      return true;
    } catch (err) {
      state.scanPending = false;
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

  function updateFlashButtonLabel(count) {
    const flashBtn = $('flasher-flash-btn');
    if (!flashBtn) return;
    const n = Number(count) || 0;
    const label = n > 1 ? `Прошить (${n})` : 'Прошить';
    let textEl = flashBtn.querySelector('.flasher-flash-btn-label');
    if (!textEl) {
      textEl = document.createElement('span');
      textEl.className = 'flasher-flash-btn-label';
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
          setFlashButtons(); hideProgress();
          if (state2 === 'error' || state2 === 'cancelled') {
            await loadPorts();
            if (state2 === 'error') toast('Прошивка прервана или завершилась с ошибкой. Выполните сканирование.', 'error');
            else toast('Прошивка отменена', 'warn');
          } else {
            toast('Прошивка завершена', 'success');
            await refreshScanAfterFlash();
          }
        },
      });
    } catch (err) {
      state.flashPending = false;
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

  async function releasePortPollers() {
    const port = currentPort();
    if (!port) return;
    state.portActionBusy = true;
    syncActionButtons();
    try {
      const res = await apiPost('/ports/release', { port: port.key });
      const lab = (a) => (a || []).map(unitUiLabel).join(', ');
      if (res.failed && res.failed.length) {
        throw new Error('не удалось остановить: ' + lab(res.failed));
      }
      const stopped = res.stopped_now || [];
      const already = res.already_released || [];
      const inactive = res.inactive || [];
      if (stopped.length) {
        toast('Службы опроса остановлены: ' + lab(stopped), 'success');
      } else if (already.length) {
        toast('Уже были остановлены ранее (сессия демона): ' + lab(already), 'info');
      } else if (inactive.length) {
        toast('Службы не были в состоянии active (ничего не останавливали): ' + lab(inactive), 'info');
      } else {
        toast('Нет служб для остановки по текущей конфигурации', 'info');
      }
    } catch (err) {
      toast('Освобождение RS-485: ' + err.message, 'error');
    } finally {
      state.portActionBusy = false;
      await loadPorts();
    }
  }

  async function restorePortPollers() {
    const port = currentPort();
    if (!port) return;
    state.portActionBusy = true;
    syncActionButtons();
    try {
      const res = await apiPost('/ports/restore', { port: port.key });
      const lab = (a) => (a || []).map(unitUiLabel).join(', ');
      if (res.failed && res.failed.length) {
        throw new Error('не удалось запустить: ' + lab(res.failed));
      }
      if (res.restarted && res.restarted.length) {
        toast('Опрос восстановлен: ' + lab(res.restarted), 'success');
      } else {
        toast('Штатный опрос уже работает или не был освобождён вручную', 'info');
      }
    } catch (err) {
      toast('Восстановление опроса: ' + err.message, 'error');
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
    $('flasher-scan-btn').addEventListener('click', startScan);
    $('flasher-scan-cancel-btn').addEventListener('click', cancelScan);
    $('flasher-fw-refresh-btn').addEventListener('click', () => refreshManifest(true));
    $('flasher-fw-clear-btn').addEventListener('click', clearFirmwareCache);
    $('flasher-fw-upload').addEventListener('change', (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (f) uploadFirmware(f);
      ev.target.value = '';
    });
    $('flasher-flash-btn').addEventListener('click', startFlash);
    $('flasher-flash-cancel-btn').addEventListener('click', cancelFlash);
    configModalEl('flasher-config-close-btn').addEventListener('click', closeConfigModal);
    configModalEl('flasher-config-modal').addEventListener('click', (ev) => {
      if (ev.target && ev.target.dataset && ev.target.dataset.closeConfigModal === '1') closeConfigModal();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && state.configOpen) closeConfigModal();
    });
    window.addEventListener('beforeunload', (ev) => {
      if (state.scanJobId || state.flashJobId) {
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
