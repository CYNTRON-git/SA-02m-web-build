/* ─────────────────────────────────────────────────────────────────────────────
 * flasher.js  •  UI вкладки «Устройства MR-02м»
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
    scanJobId: null,
    flashJobId: null,
    scanStream: null,
    flashStream: null,
  };

  function $(id) { return document.getElementById(id); }

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
      updatePortHint();
    } catch (err) {
      toast('Порты: ' + err.message, 'error');
    }
  }

  function updatePortHint() {
    const sel = $('flasher-port');
    const key = sel.value;
    const port = state.ports.find(p => p.key === key);
    const hint = $('flasher-port-hint');
    if (!port) { hint.textContent = ''; return; }
    const bits = [];
    if (port.mplc_active) bits.push('При сканировании/прошивке служба mplc.service будет остановлена и восстановлена автоматически.');
    if (port.busy_pids && port.busy_pids.length) bits.push('Внимание: порт удерживают PID ' + port.busy_pids.join(', ') + ' — возможна ошибка занятости.');
    hint.textContent = bits.join(' ');
  }

  /* ── Репозиторий прошивок ─────────────────────────────────────────────── */

  async function loadFirmware() {
    try {
      const data = await apiGet('/firmware');
      state.firmware = data.entries || [];
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
        const sig = (e.signatures && e.signatures.length) ? e.signatures.join(', ') : '—';
        row.innerHTML = `<span class="flasher-fw-name">${escapeHtml(e.file)}</span>` +
          `<span class="flasher-fw-meta">ver ${escapeHtml(e.version || '?')} · sig ${escapeHtml(sig)} · ${e.size || '?'} B · ${e.channel}${e.downloaded ? '' : ' · не скачан'}</span>`;
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
        <td>${escapeHtml(d.app_version || '—')}</td>
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
    const anyChecked = state.devices.some(d => d.__selected);
    const hasFw = !!$('flasher-fw-select').value;
    $('flasher-flash-btn').disabled = !(anyChecked && hasFw);
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
    const bauds = Array.from($('flasher-baudrates').selectedOptions).map(o => parseInt(o.value, 10));

    state.devices = [];
    renderDevices();
    logReset('Старт сканирования на ' + port);
    setProgress(0, 'Подготовка порта');
    setScanButtons(true);

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
      state.scanJobId = res.job_id;
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
          state.scanJobId = null; setScanButtons(false); hideProgress();
          try {
            const snap = await apiGet('/jobs/' + res.job_id);
            state.devices = (snap.devices || []).map(d => Object.assign({}, d));
            renderDevices();
          } catch (_) {}
          if (state2 === 'error') toast('Сканирование завершилось с ошибкой', 'error');
          else if (state2 === 'cancelled') toast('Сканирование отменено', 'warn');
          else toast('Сканирование завершено: ' + state.devices.length + ' устройств', 'success');
        },
      });
    } catch (err) {
      setScanButtons(false); hideProgress();
      toast('Сканирование: ' + err.message, 'error');
    }
  }

  async function cancelScan() {
    if (!state.scanJobId) return;
    try { await apiPost('/cancel', { job_id: state.scanJobId }); } catch (_) {}
  }

  function setScanButtons(running) {
    $('flasher-scan-btn').disabled = running;
    $('flasher-scan-cancel-btn').disabled = !running;
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
    setFlashButtons(true);

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
      state.flashJobId = res.job_id;
      state.flashStream = openStream(res.job_id, {
        onEnd: async state2 => {
          state.flashJobId = null; setFlashButtons(false); hideProgress();
          if (state2 === 'error') toast('Прошивка завершилась с ошибкой', 'error');
          else if (state2 === 'cancelled') toast('Прошивка отменена', 'warn');
          else toast('Прошивка завершена', 'success');
        },
      });
    } catch (err) {
      setFlashButtons(false); hideProgress();
      toast('Прошивка: ' + err.message, 'error');
    }
  }

  async function cancelFlash() {
    if (!state.flashJobId) return;
    try { await apiPost('/cancel', { job_id: state.flashJobId }); } catch (_) {}
  }

  function setFlashButtons(running) {
    $('flasher-flash-btn').disabled = running;
    $('flasher-flash-cancel-btn').disabled = !running;
  }

  /* ── Инициализация ────────────────────────────────────────────────────── */

  function wireEvents() {
    $('flasher-port').addEventListener('change', updatePortHint);
    $('flasher-refresh-ports-btn').addEventListener('click', loadPorts);
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
