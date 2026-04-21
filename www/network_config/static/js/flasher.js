/* ─────────────────────────────────────────────────────────────────────────────
 * flasher.js  •  UI вкладки «Устройства RS-485»
 * Работает с демоном sa02m-flasher через /api/flasher/*. SSE-стрим событий
 * по /api/flasher/jobs/<id>/events. Кука session_token прокидывается nginx'ом.
 * ──────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const API = '/api/flasher';
  const state = {
    initialised: false,
    ports: [],
    devices: [],        // последний результат сканирования
    firmware: [],       // список прошивок (entries)
    latestStableVersion: '', // с бэкенда: max stable manifest (сравнение с app_version на модуле)
    scanJobId: null,
    flashJobId: null,
    scanStream: null,
    flashStream: null,
    scanPending: false,
    flashPending: false,
    portActionBusy: false,
  };

  function $(id) { return document.getElementById(id); }

  function unitUiLabel(name) {
    return String(name || '').replace(/\.(service|socket)$/i, '');
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

  function toast(msg, type) {
    if (window.toast) window.toast(msg, type || 'info'); else console.log('[flasher]', msg);
  }

  async function apiGet(path) {
    const res = await fetch(API + path, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
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

  async function loadPorts() {
    try {
      const data = await apiGet('/ports');
      state.ports = data.ports || [];
      const sel = $('flasher-port');
      const prev = sel.value;
      sel.innerHTML = '';
      state.ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.key;
        const status = [];
        if (!p.exists) status.push('нет устройства');
        if (p.busy_pids && p.busy_pids.length) status.push('занят (PID ' + p.busy_pids.join(',') + ')');
        if (p.active_job) status.push('активная задача');
        opt.textContent = `${p.label || p.key} — ${p.device_path}` + (status.length ? ' [' + status.join(', ') + ']' : '');
        opt.disabled = !p.exists;
        sel.appendChild(opt);
      });
      if (state.ports.length) {
        const fallback = state.ports.find(p => p.exists) || state.ports[0];
        sel.value = state.ports.some(p => p.key === prev) ? prev : fallback.key;
      }
      updatePortHint();
    } catch (err) {
      toast('Порты: ' + err.message, 'error');
    }
  }

  function updateScanSummary() {
    const port = currentPort();
    const mode = $('flasher-mode');
    const modeText = mode.options[mode.selectedIndex] ? mode.options[mode.selectedIndex].textContent : '—';
    const addrMin = parseInt($('flasher-addr-min').value, 10) || 1;
    const addrMax = parseInt($('flasher-addr-max').value, 10) || 10;
    const bauds = selectedBaudrates();
    const parts = [];
    if (port) parts.push(`${port.label || port.key} (${port.device_path})`);
    parts.push(modeText);
    parts.push(`адреса ${addrMin}-${addrMax}`);
    parts.push(`скорости ${bauds.length ? bauds.join(', ') : 'не выбраны'}`);
    $('flasher-scan-summary').textContent = parts.join(' · ');
  }

  function syncActionButtons() {
    const port = currentPort();
    const scanRunning = state.scanPending || !!state.scanJobId;
    const flashRunning = state.flashPending || !!state.flashJobId;
    const jobBusy = !!(port && port.active_job);
    const activeServices = port && port.active_services ? port.active_services : [];
    const releasedServices = port && port.released_services ? port.released_services : [];
    const managedN = port && Array.isArray(port.managed_services) ? port.managed_services.length : 0;
    const anyChecked = state.devices.some(d => d.__selected);
    const hasFw = !!$('flasher-fw-select').value;

    $('flasher-scan-btn').disabled = !port || !port.exists || scanRunning || flashRunning || jobBusy || state.portActionBusy;
    $('flasher-scan-cancel-btn').disabled = !state.scanJobId || state.scanPending;
    // Остановка служб из managed_services (конфиг MPLC_STOP_SERVICES). Кнопка не зависит только от
    // active_services: порт может быть занят, а systemd/fuser на стороне UI выглядеть «пусто».
    const canStopPollers = !!(port && port.exists && managedN);
    $('flasher-release-port-btn').disabled = !canStopPollers || scanRunning || flashRunning || jobBusy || state.portActionBusy;
    $('flasher-restore-port-btn').disabled = !port || scanRunning || flashRunning || jobBusy || state.portActionBusy || !releasedServices.length;
    $('flasher-flash-btn').disabled = !port || !port.exists || flashRunning || scanRunning || jobBusy || !(anyChecked && hasFw);
    $('flasher-flash-cancel-btn').disabled = !state.flashJobId || state.flashPending;
  }

  function updatePortHint() {
    const port = currentPort();
    const hint = $('flasher-port-hint');
    if (!port) {
      $('flasher-port-label').textContent = '—';
      $('flasher-port-device').textContent = 'Выберите порт для работы с линией';
      setBadge('flasher-port-badge', 'Нет данных', 'unk');
      setBadge('flasher-poller-badge', 'Опрос не оценён', 'unk');
      hint.textContent = 'Выберите порт, чтобы увидеть состояние линии и опроса.';
      updateScanSummary();
      syncActionButtons();
      return;
    }

    $('flasher-port-label').textContent = port.label || port.key;
    $('flasher-port-device').textContent = port.device_path || '—';

    if (!port.exists) setBadge('flasher-port-badge', 'Нет линии', 'err');
    else if (port.active_job) setBadge('flasher-port-badge', 'Задача активна', 'unk');
    else if (port.busy_pids && port.busy_pids.length) setBadge('flasher-port-badge', 'Порт занят', 'err');
    else setBadge('flasher-port-badge', 'Порт свободен', 'ok');

    if (port.active_services && port.active_services.length) setBadge('flasher-poller-badge', 'Опрос активен', 'unk');
    else if (port.released_services && port.released_services.length) setBadge('flasher-poller-badge', 'Опрос освобождён', 'ok');
    else if (port.busy_pids && port.busy_pids.length) setBadge('flasher-poller-badge', 'Опрос не определён', 'unk');
    else setBadge('flasher-poller-badge', 'Опрос не активен', 'ok');

    const bits = [];
    if (port.active_services && port.active_services.length) {
      bits.push('Линию сейчас опрашивают: ' + port.active_services.map(unitUiLabel).join(', ') + '. При сканировании опрос будет остановлен автоматически; кнопка «Остановить службы опроса» делает это вручную.');
    } else if (port.managed_services && port.managed_services.length) {
      bits.push('Ручная остановка опроса: «Остановить службы опроса» — по списку unit’ов из конфигурации демона (см. active_services / managed_services).');
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
    updateScanSummary();
    syncActionButtons();
  }

  /* ── Репозиторий прошивок ─────────────────────────────────────────────── */

  async function loadFirmware() {
    try {
      const data = await apiGet('/firmware');
      state.firmware = data.entries || [];
      state.latestStableVersion = (data.latest_stable_version || '').trim();
      renderFirmware(data);
      updateFlashControls();
    } catch (err) {
      toast('Манифест: ' + err.message, 'error');
    }
  }

  function renderFirmware(data) {
    const status = $('flasher-fw-status');
    const updated = data.manifest_updated ? `манифест от ${data.manifest_updated}` : 'манифест не загружен';
    status.textContent = `${updated}${data.manifest_error ? ' · ошибка: ' + data.manifest_error : ''} · записей: ${(data.entries || []).length}`;

    const list = $('flasher-fw-list');
    if (!state.firmware.length) {
      list.textContent = 'Прошивки не найдены. Обновите манифест или загрузите .fw вручную.';
    } else {
      list.innerHTML = '';
      state.firmware.forEach(e => {
        const row = document.createElement('div');
        row.className = 'flasher-fw-row';
        const sig = (e.signatures && e.signatures.length)
          ? e.signatures.join(', ')
          : 'все варианты MR-02м (общий образ)';
        row.innerHTML = `<span class="flasher-fw-name">${escapeHtml(e.file)}</span>` +
          `<span class="flasher-fw-meta">ver ${escapeHtml(e.version || '?')} · ${escapeHtml(sig)} · ${e.size || '?'} B · ${e.channel}${e.downloaded ? '' : ' · не скачан'}</span>`;
        list.appendChild(row);
      });
    }

    const sel = $('flasher-fw-select');
    const prev = sel.value;
    sel.innerHTML = '';
    state.firmware.forEach(e => {
      const opt = document.createElement('option');
      opt.value = `${e.channel}::${e.file}`;
      opt.textContent = `[${e.channel}] ${e.file} (v${e.version || '?'})`;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  }

  async function refreshManifest(download) {
    try {
      const res = await apiPost('/firmware/refresh', { download: !!download });
      if (res.error) toast('Манифест: ' + res.error, 'warn');
      else toast('Манифест обновлён (записей: ' + res.entries + ')', 'success');
      await loadFirmware();
    } catch (err) {
      toast('Манифест: ' + err.message, 'error');
    }
  }

  async function uploadFirmware(file) {
    if (!file) return;
    try {
      const res = await apiUpload('/firmware/upload', file);
      toast('Загружено: ' + (res.entry && res.entry.file || file.name), 'success');
      await loadFirmware();
    } catch (err) {
      toast('Загрузка прошивки: ' + err.message, 'error');
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

  function firmwareUpdateHintForDevice(d) {
    const latest = state.latestStableVersion;
    if (!latest) return '';
    const lv = parseVersionTuple(latest);
    const dv = parseVersionTuple(d.app_version);
    if (!lv || !dv) return '';
    if (compareVersionTuple(lv, dv) <= 0) return '';
    return `<div class="flasher-sub flasher-fw-update-hint">есть ${escapeHtml(latest)}</div>`;
  }

  /* ── Таблица устройств ────────────────────────────────────────────────── */

  function renderDevices() {
    const tbody = $('flasher-devices-table').querySelector('tbody');
    tbody.innerHTML = '';
    if (!state.devices.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="flasher-empty">Устройств не найдено.</td></tr>';
      updateFlashControls();
      return;
    }
    state.devices.forEach((d, idx) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><input type="checkbox" class="flasher-dev-chk" data-idx="${idx}" ${d.__selected ? 'checked' : ''} /></td>
        <td>${d.address ?? '—'}</td>
        <td>${d.serial_hex || '—'}<div class="flasher-sub">${d.serial_dec || ''}</div></td>
        <td>${escapeHtml(d.signature || '—')}</td>
        <td>${escapeHtml(d.app_version || '—')}${firmwareUpdateHintForDevice(d)}</td>
        <td>${escapeHtml(d.bootloader_version || '—')}</td>
        <td>${d.baudrate || '—'} ${d.parity || ''}${d.stopbits || ''}</td>
        <td>${d.in_bootloader ? 'в bootloader' : ''}${d.duplicate_address ? ' dup' : ''}</td>
      `;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll('.flasher-dev-chk').forEach(el => {
      el.addEventListener('change', () => {
        const idx = parseInt(el.dataset.idx, 10);
        if (!Number.isNaN(idx) && state.devices[idx]) {
          state.devices[idx].__selected = el.checked;
        }
        updateFlashControls();
      });
    });
    updateFlashControls();
  }

  function updateFlashControls() {
    syncActionButtons();
  }

  /* ── Прогресс/лог SSE ─────────────────────────────────────────────────── */

  function logAppend(line, level) {
    const box = $('flasher-log');
    const ts = new Date().toLocaleTimeString();
    const cls = level === 'error' ? 'log-err' : level === 'warn' ? 'log-warn' : level === 'debug' ? 'log-dim' : '';
    const row = document.createElement('div');
    row.className = 'log-line ' + (cls || '');
    row.textContent = `[${ts}] ${line}`;
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  }

  function logReset(title) {
    const box = $('flasher-log');
    box.innerHTML = '';
    if (title) logAppend(title, 'info');
  }

  function setProgress(pct, message) {
    const wrap = $('flasher-progress');
    wrap.hidden = false;
    $('flasher-progress-fill').style.width = Math.max(0, Math.min(100, pct)) + '%';
    $('flasher-progress-label').textContent = message || `${pct}%`;
  }

  function hideProgress() {
    $('flasher-progress').hidden = true;
  }

  function openStream(jobId, handlers) {
    const url = `${API}/jobs/${jobId}/events`;
    const es = new EventSource(url, { withCredentials: true });
    es.addEventListener('log', ev => {
      const p = safeParse(ev.data);
      if (p) logAppend(p.message || '', p.level);
    });
    es.addEventListener('progress', ev => {
      const p = safeParse(ev.data);
      if (!p) return;
      const pct = (p.data && typeof p.data.progress === 'number') ? p.data.progress : 0;
      setProgress(pct, p.message || '');
    });
    es.addEventListener('device_found', ev => {
      const p = safeParse(ev.data);
      if (p && handlers && handlers.onDeviceFound) handlers.onDeviceFound(p.data || {});
    });
    es.addEventListener('status', ev => {
      const p = safeParse(ev.data);
      if (p) logAppend(p.message || '', p.level || 'info');
    });
    es.addEventListener('error', ev => {
      const p = safeParse(ev.data);
      if (p) logAppend('Ошибка: ' + (p.message || ''), 'error');
    });
    es.addEventListener('end', ev => {
      const p = safeParse(ev.data);
      logAppend(`Готово: ${p && p.state ? p.state : 'done'}`, p && p.state === 'done' ? 'info' : 'warn');
      if (handlers && handlers.onEnd) handlers.onEnd(p ? p.state : 'done');
      es.close();
    });
    es.onerror = () => {
      // соединение будет переоткрыто автоматически; не логируем, чтобы не засорять.
    };
    return es;
  }

  function safeParse(s) { try { return JSON.parse(s); } catch (_) { return null; } }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  /* ── Запуск сканирования ─────────────────────────────────────────────── */

  async function startScan() {
    if (state.scanJobId) { toast('Сканирование уже выполняется', 'warn'); return; }
    const port = $('flasher-port').value;
    const mode = $('flasher-mode').value;
    const addrMin = parseInt($('flasher-addr-min').value, 10) || 1;
    const addrMax = parseInt($('flasher-addr-max').value, 10) || 10;
    const bauds = selectedBaudrates();
    if (!bauds.length) { toast('Выберите хотя бы одну скорость', 'warn'); return; }

    state.devices = [];
    renderDevices();
    logReset('Старт сканирования на ' + port);
    setProgress(0, 'Подготовка порта');
    state.scanPending = true;
    setScanButtons();

    try {
      const res = await apiPost('/scan', {
        port: port,
        mode: mode,
        addr_min: addrMin,
        addr_max: addrMax,
        baudrates: bauds,
        parity: 'N',
        stopbits: 1,
      });
      state.scanPending = false;
      state.scanJobId = res.job_id;
      setScanButtons();
      state.scanStream = openStream(res.job_id, {
        onDeviceFound: dev => {
          const exists = state.devices.find(d => (
            d.address === dev.address && d.serial === dev.serial && d.baudrate === dev.baudrate
          ));
          if (!exists) {
            state.devices.push(dev);
            renderDevices();
          }
        },
        onEnd: async state2 => {
          state.scanJobId = null; setScanButtons(); hideProgress();
          try {
            const snap = await apiGet('/jobs/' + res.job_id);
            state.devices = (snap.devices || []).map(d => Object.assign({}, d));
            renderDevices();
          } catch (_) {}
          await loadPorts();
          if (state2 === 'error') toast('Сканирование завершилось с ошибкой', 'error');
          else if (state2 === 'cancelled') toast('Сканирование отменено', 'warn');
          else toast('Сканирование завершено: ' + state.devices.length + ' устройств', 'success');
        },
      });
    } catch (err) {
      state.scanPending = false;
      setScanButtons(); hideProgress();
      toast('Сканирование: ' + err.message, 'error');
    }
  }

  async function cancelScan() {
    if (!state.scanJobId) return;
    try { await apiPost('/cancel', { job_id: state.scanJobId }); } catch (_) {}
  }

  function setScanButtons() {
    syncActionButtons();
  }

  /* ── Прошивка ─────────────────────────────────────────────────────────── */

  async function startFlash() {
    if (state.flashJobId) { toast('Прошивка уже выполняется', 'warn'); return; }
    const targets = state.devices.filter(d => d.__selected);
    if (!targets.length) { toast('Выберите устройства', 'warn'); return; }
    const fwVal = $('flasher-fw-select').value;
    if (!fwVal) { toast('Выберите файл прошивки', 'warn'); return; }
    const [channel, file] = fwVal.split('::');
    const port = $('flasher-port').value;
    const useFast = $('flasher-use-fast').checked;
    const forceMismatch = $('flasher-force-mismatch').checked;

    logReset(`Прошивка ${targets.length} устройств файлом ${file}`);
    setProgress(0, 'Запуск');
    state.flashPending = true;
    setFlashButtons();

    try {
      const res = await apiPost('/flash_batch', {
        port: port,
        firmware_channel: channel,
        firmware_file: file,
        use_fast_modbus: useFast,
        force_signature_mismatch: forceMismatch,
        targets: targets.map(t => ({
          address: t.address,
          serial: t.serial,
          signature: t.signature,
          in_bootloader: t.in_bootloader,
        })),
      });
      state.flashPending = false;
      state.flashJobId = res.job_id;
      setFlashButtons();
      state.flashStream = openStream(res.job_id, {
        onEnd: async state2 => {
          state.flashJobId = null; setFlashButtons(); hideProgress();
          await loadPorts();
          if (state2 === 'error') toast('Прошивка завершилась с ошибкой', 'error');
          else if (state2 === 'cancelled') toast('Прошивка отменена', 'warn');
          else toast('Прошивка завершена', 'success');
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
    try { await apiPost('/cancel', { job_id: state.flashJobId }); } catch (_) {}
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
    $('flasher-mode').addEventListener('change', updateScanSummary);
    $('flasher-addr-min').addEventListener('input', updateScanSummary);
    $('flasher-addr-max').addEventListener('input', updateScanSummary);
    document.querySelectorAll('#flasher-baudrates input').forEach(el => {
      el.addEventListener('change', updateScanSummary);
    });
    $('flasher-scan-btn').addEventListener('click', startScan);
    $('flasher-scan-cancel-btn').addEventListener('click', cancelScan);
    $('flasher-fw-refresh-btn').addEventListener('click', () => refreshManifest(false));
    $('flasher-fw-upload').addEventListener('change', (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (f) uploadFirmware(f);
      ev.target.value = '';
    });
    $('flasher-fw-select').addEventListener('change', updateFlashControls);
    $('flasher-flash-btn').addEventListener('click', startFlash);
    $('flasher-flash-cancel-btn').addEventListener('click', cancelFlash);
    $('flasher-devices-all').addEventListener('change', (ev) => {
      const on = ev.target.checked;
      state.devices.forEach(d => { d.__selected = on; });
      renderDevices();
    });
  }

  window.flasherInit = function () {
    if (state.initialised) {
      loadPorts();
      loadFirmware();
      return;
    }
    state.initialised = true;
    wireEvents();
    loadPorts();
    loadFirmware();
  };
})();
