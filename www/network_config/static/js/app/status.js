/* SA-02m Web Interface -- STATUS POLLING (dashboard status widgets: priority
   widgets polled separately, the rest in the background). Extracted from app.js
   (F10 decomposition). Plain classic script sharing the global scope; original
   load order preserved. See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   STATUS POLLING — приоритетные виджеты отдельно, остальное в фоне
   ══════════════════════════════════════════════════════════════════════════ */
const widgetBusy = { priority: false };
const backgroundBusy = {
  main: false,
  storage: false,
  time: false,
  uptime: false,
  network: false,
  load: false,
  system: false,
  services: false,
  hardware: false,
  rs485: false
};
/** Части дашборда, раньше собиравшиеся в одном part=main — rolling scheduler с фазовым сдвигом. */
const BACKGROUND_STATUS_PARTS = ['storage', 'time', 'uptime', 'network', 'load', 'system', 'services', 'hardware'];
/** Лёгкие части (кеш / быстрый CGI) vs тяжёлые (network, services, rs485 cold ~4 s). */
const LIGHT_STATUS_PARTS = ['storage', 'hardware', 'uptime', 'load', 'system'];
const HEAVY_STATUS_PARTS = ['network', 'services'];
const HEAVY_STATUS_PARTS_ALL = HEAVY_STATUS_PARTS.concat(['priority', 'rs485', 'main']);
/** Период light/heavy и фазы (мс): heavy реже и с большим зазором. */
const LIGHT_PART_PERIOD_MS = 6000;
const HEAVY_PART_PERIOD_MS = 12000;
const LIGHT_PART_PHASE_MS = {
  storage: 0,
  // hardware reads a TTL-cached status.cgi part (~0.4 s, no live I2C hit), so it
  // is a LIGHT part, not heavy: it must not sit behind the 2.2 s heavy-queue gap
  // (network/services/priority/rs485/main) that pushed the HW block to ~18 s
  // after load. Early phase → it populates in the first wave.
  hardware: 500,
  uptime: 750,
  load: 1500,
  system: 2250
};
const HEAVY_PART_PHASE_MS = {
  network: 3500,
  services: 6500
};
/** @deprecated — для fetchStatusMain stagger burst */
const BACKGROUND_PART_PERIOD_MS = LIGHT_PART_PERIOD_MS;
const BACKGROUND_PART_PHASE_MS = Object.assign({}, LIGHT_PART_PHASE_MS, HEAVY_PART_PHASE_MS, { time: 3000 });
const PRIORITY_POLL_PHASE_MS = 500;
/** RS-485 first-polls early (before the services list) so its activity data
 *  appears ahead of «Службы»; steady-state interval stays 12 s. Phase kept
 *  >= network (3500) so «Сеть» still paints first, and < services-fast (4000)
 *  so RS-485 lands before the services list. The 2.2 s heavy-queue gap still
 *  serializes it against network/services (shared ARM CPU). */
const RS485_POLL_PHASE_MS = 3600;
/** part=services&fast=1 — instant service list (skips the ~3.5 s `sudo CTL list`);
 *  one-shot on init, just after RS-485, treated as a LIGHT fetch. The full
 *  services heavy poll (phase 6500) still lands second and refines disabled/masked. */
const SERVICES_FAST_PHASE_MS = 4000;
/** Глобальная очередь status.cgi: не более одного запроса одновременно. */
const STATUS_GLOBAL_MIN_GAP_MS = 350;
const STATUS_HEAVY_MIN_GAP_MS = 2200;
/** После hw_set — повторный опрос; если раунд ещё в полёте — не терять повтор. */
let mainBundleRefreshQueued = false;
let statusMainRoundBusy = false;
let rs485RefreshQueued = false;
const backgroundLoaded = {
  main: false,
  storage: false,
  time: false,
  uptime: false,
  network: false,
  load: false,
  system: false,
  services: false,
  hardware: false,
  rs485: false
};
let _lastRs485Ports = null;
const statusFailures = {
  priority: 0,
  main: 0,
  rs485: 0
};
const statusPauseUntil = { main: 0, rs485: 0 };
const STATUS_TIMEOUT_MS = {
  priority: 3000,
  /** Координатор force-refresh (staggered burst всех part=*). */
  main: 6000,
  rs485: 10000,
  storage: 6000,
  time: 4000,
  uptime: 3000,
  network: 5000,
  load: 3000,
  system: 4000,
  services: 8000,
  hardware: 10000
};
/** Интервалы опроса и минимальный зазор между запросами одной части (клиентский rate-limit). */
const STATUS_POLL_INTERVAL_MS = { priority: 6000, main: 6000, rs485: 12000 };
const STATUS_MIN_GAP_MS = {
  priority: 800,
  main: 1500,
  rs485: 1500,
  storage: 1200,
  time: 1200,
  uptime: 1200,
  network: 1200,
  load: 1200,
  system: 1500,
  services: 1500,
  hardware: 1500
};

/** Карта SA02M_STATUS_ENABLE_* с status.cgi?part=blocks (1=опрос, 0=пропуск). */
let _statusBlocksConfig = null;

function isStatusBlockEnabled(part) {
  if (!_statusBlocksConfig) return part !== 'time';
  const v = _statusBlocksConfig[part];
  if (v === 0 || v === false) return false;
  if (v === 1 || v === true) return true;
  return part !== 'time';
}

/** GPIO-виджеты опрашиваем всегда: при hardware=0 CGI отдаёт TTL-кэш (hw_poll_disabled=1). */
function shouldFetchBackgroundPart(part) {
  if (part === 'hardware') return true;
  return isStatusBlockEnabled(part);
}

function activeBackgroundParts() {
  return BACKGROUND_STATUS_PARTS.filter(function (p) { return shouldFetchBackgroundPart(p); });
}

const STATUS_BLOCKS_FETCH_TIMEOUT_MS = 4000;

function fetchStatusBlocksConfig(onReady) {
  const ctrl = new AbortController();
  const timer = setTimeout(function () {
    try { ctrl.abort(); } catch (_) {}
  }, STATUS_BLOCKS_FETCH_TIMEOUT_MS);
  fetch('/cgi-bin/status.cgi?part=blocks', {
    cache: 'no-store',
    credentials: 'same-origin',
    signal: ctrl.signal
  })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (j && typeof j === 'object' && !j.error) _statusBlocksConfig = j;
    })
    .catch(function () { /* keep null → default enable all except time */ })
    .finally(function () {
      clearTimeout(timer);
      if (onReady) onReady();
    });
}

let _statusPollGeneration = 0;
let _statusPollTimers = { priority: null, rs485: null, bootstrap: null };
let _statusInitTimeouts = [];
let _statusFetchQueue = [];
let _statusFetchInFlight = false;
let _statusQueuePumpTimer = null;
let _statusLastGlobalFetchMs = 0;
let _statusLastHeavyStartMs = 0;

function isHeavyStatusPart(part) {
  return HEAVY_STATUS_PARTS_ALL.indexOf(part) >= 0;
}

function clearStatusFetchQueue() {
  _statusFetchQueue = [];
  if (_statusQueuePumpTimer) {
    clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = null;
  }
}

function scheduleStatusFetch(part, delayMs, runner) {
  const delay = Math.max(0, delayMs || 0);
  const at = Date.now() + delay;
  const dup = _statusFetchQueue.some(function (item) { return item.part === part; });
  if (dup) return;
  _statusFetchQueue.push({ part: part, at: at, queuedAt: Date.now(), runner: runner });
  _statusFetchQueue.sort(function (a, b) { return a.at - b.at; });
  pumpStatusFetchQueue();
}

function pumpStatusFetchQueue() {
  if (_statusFetchInFlight) return;
  const now = Date.now();
  if (_statusFetchQueue.length === 0) return;
  if (_statusFetchQueue[0].at > now) {
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, Math.max(50, _statusFetchQueue[0].at - now));
    return;
  }
  const sinceGlobal = now - _statusLastGlobalFetchMs;
  if (sinceGlobal < STATUS_GLOBAL_MIN_GAP_MS) {
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_GLOBAL_MIN_GAP_MS - sinceGlobal);
    return;
  }
  const item = _statusFetchQueue.shift();
  if (isHeavyStatusPart(item.part)) {
    const sinceHeavy = now - _statusLastHeavyStartMs;
    if (sinceHeavy < STATUS_HEAVY_MIN_GAP_MS) {
      item.at = now + (STATUS_HEAVY_MIN_GAP_MS - sinceHeavy);
      _statusFetchQueue.unshift(item);
      if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
      _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_HEAVY_MIN_GAP_MS - sinceHeavy);
      return;
    }
    _statusLastHeavyStartMs = now;
  }
  _statusFetchInFlight = true;
  _statusLastGlobalFetchMs = now;
  let released = false;
  const release = function () {
    if (released) return;
    released = true;
    _statusFetchInFlight = false;
    if (_statusQueuePumpTimer) clearTimeout(_statusQueuePumpTimer);
    _statusQueuePumpTimer = setTimeout(pumpStatusFetchQueue, STATUS_GLOBAL_MIN_GAP_MS);
  };
  const queueWaitMs = Math.max(0, Date.now() - (item.queuedAt || item.at));
  try {
    item.runner(release, { queueWaitMs: queueWaitMs });
  } catch (_) {
    release();
  }
}
let _statusFetchAbort = { priority: null, main: null, rs485: null };
let _statusLastFetchMs = { priority: 0, main: 0, rs485: 0 };
BACKGROUND_STATUS_PARTS.forEach(function (p) {
  _statusFetchAbort[p] = null;
  _statusLastFetchMs[p] = 0;
});
let _statusLifecycleBound = false;

function statusRequestTimeout(part, queueWaitMs) {
  const base = STATUS_TIMEOUT_MS[part] || 3500;
  const extra = Math.max(0, queueWaitMs || 0);
  return Math.min(base + extra, base * 2);
}

function isBenignStatusFetchError(e, timedOut) {
  if (!e) return true;
  if (e.stale) return true;
  if (e.name === 'AbortError' && !timedOut) return true;
  return false;
}

function hideDashboardPollAlert() {
  const el = document.getElementById('dashboard-poll-alert');
  if (!el) return;
  el.style.display = 'none';
  el.textContent = '';
}

function abortStatusPart(part) {
  const ctrl = _statusFetchAbort[part];
  if (!ctrl) return;
  try { ctrl.abort(); } catch (_) {}
  _statusFetchAbort[part] = null;
}

function abortAllStatusFetches() {
  ['priority', 'main', 'rs485', 'services_fast'].concat(BACKGROUND_STATUS_PARTS).forEach(abortStatusPart);
}

function clearStatusPollTimers() {
  Object.keys(_statusPollTimers).forEach(function (key) {
    if (_statusPollTimers[key]) {
      clearInterval(_statusPollTimers[key]);
      _statusPollTimers[key] = null;
    }
  });
  _statusInitTimeouts.forEach(function (id) { clearTimeout(id); });
  _statusInitTimeouts = [];
}

function resetBackgroundLoadedFlags() {
  Object.keys(backgroundLoaded).forEach(function (k) { backgroundLoaded[k] = false; });
}

function teardownStatusPolling() {
  _statusPollGeneration += 1;
  clearStatusPollTimers();
  clearStatusFetchQueue();
  abortAllStatusFetches();
  widgetBusy.priority = false;
  Object.keys(backgroundBusy).forEach(function (k) { backgroundBusy[k] = false; });
  mainBundleRefreshQueued = false;
  rs485RefreshQueued = false;
  statusMainRoundBusy = false;
  _statusFetchInFlight = false;
}

function canFetchStatusPart(part, force) {
  if (!force && isStatusPartPaused(part)) return false;
  if (!force && part !== 'main' && isStatusPartPaused('main')) return false;
  if (force) return true;
  const gap = STATUS_MIN_GAP_MS[part] || 1000;
  return (Date.now() - (_statusLastFetchMs[part] || 0)) >= gap;
}

/** Fetch status.cgi с AbortController, отменой предыдущего запроса той же части и проверкой поколения. */
function statusFetchJson(part, url, timeoutMs, gen, onTimeout) {
  abortStatusPart(part);
  const ctrl = new AbortController();
  _statusFetchAbort[part] = ctrl;
  const timer = setTimeout(function () {
    if (onTimeout) onTimeout();
    ctrl.abort();
  }, timeoutMs);
  return fetch(url, {
    cache: 'no-store',
    credentials: 'same-origin',
    signal: ctrl.signal
  }).finally(function () {
    clearTimeout(timer);
    if (_statusFetchAbort[part] === ctrl) _statusFetchAbort[part] = null;
  }).then(function (r) {
    if (gen !== _statusPollGeneration) {
      throw Object.assign(new DOMException('Stale poll', 'AbortError'), { stale: true });
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function bindStatusPollingLifecycle() {
  if (_statusLifecycleBound) return;
  _statusLifecycleBound = true;
  window.addEventListener('pagehide', teardownStatusPolling);
  window.addEventListener('beforeunload', teardownStatusPolling);
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) initStatusPolling();
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchPriorityPart('priority');
  });
}

function startPriorityPollTimer(gen) {
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchPriorityPart('priority');
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers.priority = setInterval(tick, STATUS_POLL_INTERVAL_MS.priority);
  }, PRIORITY_POLL_PHASE_MS));
}

function startRs485PollTimer(gen) {
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchStatusRs485();
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers.rs485 = setInterval(tick, STATUS_POLL_INTERVAL_MS.rs485);
  }, RS485_POLL_PHASE_MS));
}

function startLightPartPollTimer(gen, part) {
  const phase = LIGHT_PART_PHASE_MS[part] || 0;
  const timerKey = 'part_' + part;
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchBackgroundPart(part, null, false, null);
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers[timerKey] = setInterval(tick, LIGHT_PART_PERIOD_MS);
  }, phase));
}

function startHeavyPartPollTimer(gen, part) {
  const phase = HEAVY_PART_PHASE_MS[part] || 0;
  const timerKey = 'part_' + part;
  const tick = function () {
    if (gen !== _statusPollGeneration) return;
    fetchBackgroundPart(part, null, false, null);
  };
  _statusInitTimeouts.push(setTimeout(function () {
    if (gen !== _statusPollGeneration) return;
    tick();
    _statusPollTimers[timerKey] = setInterval(tick, HEAVY_PART_PERIOD_MS);
  }, phase));
}

function startBackgroundPartPollTimer(gen, part) {
  if (LIGHT_STATUS_PARTS.indexOf(part) >= 0) {
    startLightPartPollTimer(gen, part);
  } else if (HEAVY_STATUS_PARTS.indexOf(part) >= 0) {
    startHeavyPartPollTimer(gen, part);
  } else {
    const phase = BACKGROUND_PART_PHASE_MS[part] || 0;
    const timerKey = 'part_' + part;
    const tick = function () {
      if (gen !== _statusPollGeneration) return;
      fetchBackgroundPart(part, null, false, null);
    };
    _statusInitTimeouts.push(setTimeout(function () {
      if (gen !== _statusPollGeneration) return;
      tick();
      _statusPollTimers[timerKey] = setInterval(tick, LIGHT_PART_PERIOD_MS);
    }, phase));
  }
}

function startAllBackgroundPartPollTimers(gen) {
  activeBackgroundParts().forEach(function (part) {
    startBackgroundPartPollTimer(gen, part);
  });
}

function initStatusPolling() {
  teardownStatusPolling();
  resetBackgroundLoadedFlags();
  clearHwHintPending();
  _statusBlocksConfig = null;
  const gen = _statusPollGeneration;
  let pollingStarted = false;

  const scheduleInitial = function () {
    if (pollingStarted || gen !== _statusPollGeneration) return;
    pollingStarted = true;
    BACKGROUND_STATUS_PARTS.forEach(function (p) {
      if (!shouldFetchBackgroundPart(p)) backgroundLoaded[p] = true;
    });
    if (!isStatusBlockEnabled('rs485')) backgroundLoaded.rs485 = true;
    startPriorityPollTimer(gen);
    startAllBackgroundPartPollTimers(gen);
    startRs485PollTimer(gen);
    if (isStatusBlockEnabled('services')) {
      _statusInitTimeouts.push(setTimeout(function () {
        if (gen !== _statusPollGeneration) return;
        fetchServicesFast(gen);
      }, SERVICES_FAST_PHASE_MS));
    }
    bootstrapBackgroundWidgets(gen);
  };

  const deferScheduleInitial = function () {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(function () { requestAnimationFrame(scheduleInitial); });
    } else {
      _statusInitTimeouts.push(setTimeout(scheduleInitial, 0));
    }
  };

  fetchStatusBlocksConfig(deferScheduleInitial);
  _statusInitTimeouts.push(setTimeout(scheduleInitial, 600));
}

function isStatusPartPaused(part) {
  return (statusPauseUntil[part] || 0) > Date.now();
}

function setStatusPartPause(part, ms) {
  statusPauseUntil[part] = Date.now() + ms;
}

function noteStatusFailure(part, err) {
  statusFailures[part] = (statusFailures[part] || 0) + 1;
  const isTimeout = err && err.name === 'AbortError';
  const needPauseMain = part === 'main' && statusFailures[part] >= 5;
  const needPauseBg = BACKGROUND_STATUS_PARTS.indexOf(part) >= 0 && statusFailures[part] >= 5;
  const needPauseRs = part === 'rs485' && statusFailures[part] >= 3;
  if (needPauseMain || needPauseBg) {
    setStatusPartPause('main', 25000);
    BACKGROUND_STATUS_PARTS.forEach(function (p) { setStatusPartPause(p, 25000); });
    const el = document.getElementById('dashboard-poll-alert');
    if (el) {
      el.style.display = 'block';
      el.textContent = isTimeout
        ? 'Обновление основных виджетов приостановлено: несколько таймаутов ответа status.cgi. Проверьте нагрузку и nginx/fastcgi.'
        : 'Обновление основных виджетов приостановлено из-за ошибок ответа status.cgi.';
    }
  } else if (needPauseRs) {
    setStatusPartPause(part, 25000);
  }
}

function noteStatusSuccess(part) {
  statusFailures[part] = 0;
  if (part === 'main' || part === 'rs485' || BACKGROUND_STATUS_PARTS.indexOf(part) >= 0) {
    statusPauseUntil[part] = 0;
    if (part !== 'rs485') {
      statusFailures.main = 0;
      statusPauseUntil.main = 0;
      hideDashboardPollAlert();
    }
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function threshColor(val, warnAt, critAt) {
  return val >= critAt ? cssVar('--meter-red') : val >= warnAt ? cssVar('--meter-yellow') : cssVar('--meter-cyan');
}

/** USB / microSD: префикс полей в JSON — usb_* или sd_* */
function applyRemovableDisk(mounted, base, d) {
  const val = document.getElementById(base + '-val');
  const detail = document.getElementById(base + '-detail');
  if (!val || !detail) return;
  if (!mounted) {
    val.textContent = uiT('НЕ УСТАНОВЛЕН');
    val.classList.add('widget-val-removable-empty');
    detail.style.display = 'none';
    return;
  }
  val.classList.remove('widget-val-removable-empty');
  detail.style.display = '';
  const used = d[base + '_used_kb'];
  const total = d[base + '_total_kb'];
  const free = d[base + '_free_kb'];
  const pct = parseInt(d[base + '_pct'], 10) || 0;
  setText(base + '-val', fmtKB(used));
  setText(base + '-sub', uiT('из') + ' ' + fmtKB(total));
  setText(base + '-pct', pct + '%');
  setText(base + '-free', uiT('свободно') + ' ' + fmtKB(free));
  const bar = document.getElementById(base + '-bar');
  if (bar) {
    bar.style.width = pct + '%';
    bar.style.background = threshColor(pct, 70, 90);
  }
}

let _lastPriorityStatus = null;

function applyPriorityStatus(d) {
  _lastPriorityStatus = d;

  /* CPU */
  if (d.cpu_usage !== undefined) {
    setText('cpu-val', d.cpu_usage + '%');
    const cpuBar = document.getElementById('cpu-bar');
    if (cpuBar) {
      cpuBar.style.width = Math.min(100, Math.max(0, parseFloat(d.cpu_usage) || 0)) + '%';
      cpuBar.style.background = threshColor(d.cpu_usage, 60, 80);
    }
  }

  /* RAM */
  if (d.ram_used_kb !== undefined) {
    setText('ram-val', fmtKB(d.ram_used_kb));
    setText('ram-sub', uiT('из') + ' ' + fmtKB(d.ram_total_kb));
    setText('ram-pct', d.ram_pct + '%');
    setText('ram-free', uiT('свободно') + ' ' + fmtKB(d.ram_free_kb));
    const ramBar = document.getElementById('ram-bar');
    if (ramBar) {
      ramBar.style.width = d.ram_pct + '%';
      ramBar.style.background = threshColor(d.ram_pct, 70, 90);
    }
  }

  /* SWAP — слот зарезервирован до первого ответа priority; без swap после загрузки — убираем */
  const sb = document.getElementById('swap-block');
  if (d.swap_total_kb > 0) {
    if (sb) {
      sb.style.display = '';
      sb.classList.add('swap-active');
      sb.classList.remove('swap-block-collapsed');
      sb.removeAttribute('aria-hidden');
    }
    setText('swap-pct', d.swap_pct + '%');
    setText('swap-lbl', fmtKB(d.swap_used_kb) + ' / ' + fmtKB(d.swap_total_kb));
    const swapBar = document.getElementById('swap-bar');
    if (swapBar) {
      swapBar.style.width = d.swap_pct + '%';
      swapBar.style.background = d.swap_pct > 80 ? cssVar('--meter-red') : cssVar('--meter-orange');
    }
  } else if (sb) {
    sb.classList.remove('swap-active');
    sb.setAttribute('aria-hidden', 'true');
    sb.classList.add('swap-block-collapsed');
    sb.style.display = 'none';
  }

  /* Температура: шкала 30–100 °C; цвет <70 зелёный, 70–80 жёлтый, ≥80 красный */
  if (d.temp_c !== undefined) {
    setText('temp-val', d.temp_c + '°');
    const tempBar = document.getElementById('temp-bar');
    const tempHint = document.getElementById('temp-gauge-hint');
    const tc = parseFloat(d.temp_c) || 0;
    if (tempBar) {
      tempBar.style.width = tempToGaugePct(d.temp_c) + '%';
      tempBar.style.background = tc >= 80 ? cssVar('--meter-red') : tc >= 70 ? cssVar('--meter-yellow') : cssVar('--meter-green');
    }
    if (tempHint) {
      tempHint.textContent = uiT(tc >= 80 ? 'Выше нормы' : 'В норме');
    }
  }

  /* Disk */
  if (d.disk_used_kb !== undefined) {
    setText('disk-val', fmtKB(d.disk_used_kb));
    setText('disk-sub', uiT('из') + ' ' + fmtKB(d.disk_total_kb));
    setText('disk-pct', d.disk_pct + '%');
    setText('disk-free', uiT('свободно') + ' ' + fmtKB(d.disk_free_kb));
    const diskBar = document.getElementById('disk-bar');
    if (diskBar) {
      diskBar.style.width = d.disk_pct + '%';
      diskBar.style.background = threshColor(d.disk_pct, 70, 90);
    }
  }
}

function applyStorageStatus(d) {
  if (d.disk_io_read_b !== undefined) {
    setText('disk-io', uiT('R ' + fmtTrafficBytes(d.disk_io_read_b) + ' / W ' + fmtTrafficBytes(d.disk_io_write_b)));
  }
  if (d.usb_modem_present) {
    applyUsbModem(d);
  } else {
    var sv = document.getElementById('usb-storage-view');
    var mv = document.getElementById('usb-modem-view');
    var tt = document.getElementById('usb-widget-title');
    if (sv) sv.style.display = '';
    if (mv) mv.style.display = 'none';
    if (tt) tt.textContent = uiT('USB-flash');
    setUsbWidgetIcon('storage');
    applyRemovableDisk(!!d.usb_mounted, 'usb', d);
  }
  applyRemovableDisk(!!d.sd_mounted, 'sd', d);
}

/* Иконка-чип USB-виджета: флешка или модем — по фактически подключённому устройству */
var USB_WIDGET_ICONS = {
  storage: '<svg viewBox="0 0 24 24"><rect x="8" y="9" width="8" height="12" rx="1.5"/><path d="M10 9V4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v5"/><path d="M11 6h.01M13 6h.01"/></svg>',
  modem: '<svg viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M8.53 15.61a6 6 0 0 1 6.95 0"/><path d="M12 19h.01"/></svg>'
};

function setUsbWidgetIcon(kind) {
  var ico = document.getElementById('usb-widget-ico');
  if (!ico || ico.dataset.kind === kind) return;
  ico.innerHTML = USB_WIDGET_ICONS[kind] || USB_WIDGET_ICONS.storage;
  ico.dataset.kind = kind;
}

function applyUsbModem(d) {
  var sv = document.getElementById('usb-storage-view');
  var mv = document.getElementById('usb-modem-view');
  var tt = document.getElementById('usb-widget-title');
  if (sv) sv.style.display = 'none';
  if (mv) mv.style.display = '';
  if (tt) tt.textContent = uiT('USB-модем');
  setUsbWidgetIcon('modem');

  var stateEl = document.getElementById('usb-modem-state-val');
  if (stateEl) {
    var st = d.usb_modem_state || '';
    if (st === 'up') {
      stateEl.textContent = uiT('Подключён');
      stateEl.className = 'widget-val on';
    } else if (st === 'init') {
      stateEl.textContent = uiT('Инициализация');
      stateEl.className = 'widget-val';
    } else if (st === 'down' || st === 'unknown' || st === '') {
      stateEl.textContent = uiT('Нет сети');
      stateEl.className = 'widget-val off';
    } else {
      stateEl.textContent = st;
      stateEl.className = 'widget-val';
    }
  }

  var parts = [d.usb_modem_vendor, d.usb_modem_model].filter(function(s) { return s && s.trim(); });
  setText('usb-modem-model', parts.join(' — ') || '');

  var iface = d.usb_modem_iface ? '[' + d.usb_modem_iface + ']' : '';
  setText('usb-modem-ip', d.usb_modem_ip ? d.usb_modem_ip + ' ' + iface : iface);

  var rx = typeof d.usb_modem_rx === 'number' ? fmtBytes(d.usb_modem_rx) : '—';
  var tx = typeof d.usb_modem_tx === 'number' ? fmtBytes(d.usb_modem_tx) : '—';
  setText('usb-modem-traffic', uiT('↓ ' + rx + ' / ↑ ' + tx));
}

function applyTimeStatus(d) {
  if (d.datetime_sys) setText('time-sys-disp', d.datetime_sys);
  else if (d.datetime) setText('time-sys-disp', d.datetime);
  if (document.getElementById('time-rtc-disp')) {
    const rtc = (d.rtc_datetime !== undefined && d.rtc_datetime !== null)
      ? String(d.rtc_datetime).trim()
      : '';
    setText('time-rtc-disp', rtc || '—');
  }
}

function refreshTimeReadouts() {
  fetch('/cgi-bin/config.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      applyTimeStatus({ datetime: d.datetime, rtc_datetime: d.rtc_datetime ?? '' });
    })
    .catch(() => {});
}

window.refreshTimeReadouts = refreshTimeReadouts;

function applyUptimeStatus(d) {
  setText('uptime-val', fmtUptime(d.uptime_sec ?? d.uptime_s));
}

/** Плейсхолдер pill (ширина «Нет линка») — резерв места до ответа status.cgi. */
function ethPillPlaceholderText() {
  return uiT('Нет линка');
}

/** Показывает только «Линк» / «Нет линка»; до ответа status.cgi и при absent — pill невидим, место зарезервировано. */
function applyEthIfaceState(spanId, operstate) {
  const el = document.getElementById(spanId);
  if (!el) return;
  const hide = () => {
    el.textContent = ethPillPlaceholderText();
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('hidden');
    el.className = 'eth-state eth-state-prominent eth-state-hidden';
  };
  if (operstate === undefined || operstate === null || operstate === '') {
    hide();
    return;
  }
  const s = String(operstate).trim().toLowerCase();
  if (s === 'absent' || s === 'unknown') {
    hide();
    return;
  }
  el.removeAttribute('hidden');
  el.removeAttribute('aria-hidden');
  if (s === 'up') {
    el.textContent = uiT('Линк');
    el.className = 'eth-state eth-state-prominent up';
  } else {
    el.textContent = uiT('Нет линка');
    el.className = 'eth-state eth-state-prominent down';
  }
}

/** Dashboard Ethernet widget: «Static: 1.2.3.4» или «DHCP: 1.2.3.4». */
function formatEthIpWidget(ipRaw, modeRaw) {
  const ip = (ipRaw !== undefined && ipRaw !== null) ? String(ipRaw).trim() : '';
  let mode = (modeRaw !== undefined && modeRaw !== null) ? String(modeRaw).trim().toLowerCase() : '';
  if (ip && mode !== 'dhcp' && mode !== 'static') mode = 'static';
  if (mode === 'dhcp') {
    const prefix = uiT('DHCP:');
    return ip ? `${prefix} ${ip}` : prefix;
  }
  if (mode === 'static') {
    const prefix = uiT('Static:');
    return ip ? `${prefix} ${ip}` : prefix;
  }
  return ip || '—';
}

function applyNetworkStatus(d) {
  applyEthIfaceState('eth0-state', d.eth0_operstate);
  applyEthIfaceState('eth1-state', d.eth1_operstate);
  setText('eth0-rx', fmtTrafficBytes(d.net_rx_bytes || 0));
  setText('eth0-tx', fmtTrafficBytes(d.net_tx_bytes || 0));
  setText('eth1-rx', fmtTrafficBytes(d.net1_rx_bytes || 0));
  setText('eth1-tx', fmtTrafficBytes(d.net1_tx_bytes || 0));
  setText('eth0-ip', formatEthIpWidget(d.eth0_ip, d.eth0_mode));
  setText('eth1-ip', formatEthIpWidget(d.eth1_ip, d.eth1_mode));
  if (d.ip) setText('tb-ip', d.ip);
}

function applyLoadStatus(d) {
  setText('load-1',  d.load_1  || '—');
  setText('load-5',  d.load_5  || '—');
  setText('load-15', d.load_15 || '—');
  setText('proc-info', uiT('Процессов: ' + (d.proc_running || 0) + ' / ' + (d.proc_total || 0)));
  if (d.cpu_freq_mhz) {
    const thr = d.cpu_throttle ? ' (' + d.cpu_throttle + '%)' : '';
    setText('cpu-freq', d.cpu_freq_mhz + ' ' + uiT('МГц') + thr);
  } else {
    setText('cpu-freq', '0 ' + uiT('МГц'));
  }
}

function applySystemStatus(d) {
  _lastSystemStatus = d;
  if (d.cpu_model) setText('cpu-model',   d.cpu_model);
  else setText('cpu-model', '\u00a0');
  const armbianEl = document.getElementById('armbian-info');
  if (d.armbian_version) {
    setText('armbian-info', d.armbian_version);
    if (armbianEl) armbianEl.classList.remove('widget-sub-inert');
  } else if (armbianEl) {
    armbianEl.textContent = '\u00a0';
    armbianEl.classList.add('widget-sub-inert');
  }
  if (d.kernel)    setText('kernel-info', uiT('Ядро: ' + d.kernel));
  else setText('kernel-info', '\u00a0');

  updateCpuProfileTile(d);

  const btn = document.getElementById('storage-format-toggle');
  const lbl = document.getElementById('storage-format-toggle-label');
  if (btn) {
    const installed = d.storage_mount_installed === 1;
    const on = d.storage_auto_format === 1;
    btn.dataset.storageOn = on ? '1' : '0';
    if (!installed) {
      btn.disabled = true;
      if (lbl) lbl.textContent = uiT('НЕ УСТАНОВЛЕНО');
      btn.className = 'btn btn-danger';
      btn.title = uiT('Нет storage-mount — выполните установку системы (install.sh)');
    } else {
      btn.disabled = false;
      btn.title = uiT('Нажмите, чтобы переключить: при выкл. раздел без ФС или NTFS не форматируется');
      if (lbl) lbl.textContent = uiT(on ? 'ВКЛЮЧЕНО' : 'ОТКЛЮЧЕНО');
      btn.className = 'btn btn-danger';
    }
  }
}

function toggleStorageAutoFormat() {
  const btn = document.getElementById('storage-format-toggle');
  if (!btn || btn.disabled) return;
  const on = btn.dataset.storageOn === '1';
  setStorageAutoFormat(on ? 0 : 1);
}

function fetchWithTimeout(url, options, timeoutMs) {
  const ms = timeoutMs || 12000;
  const c = new AbortController();
  const t = setTimeout(function () { c.abort(); }, ms);
  return fetch(url, Object.assign({}, options || {}, { signal: c.signal })).finally(function () {
    clearTimeout(t);
  });
}

function setStorageAutoFormat(enabled) {
  const body = 'enabled=' + encodeURIComponent(enabled ? '1' : '0');
  fetchWithTimeout('/cgi-bin/storage_format_set.cgi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
    credentials: 'same-origin'
  }, 12000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.ok) {
        fetchSystemWidget();
        toast(enabled ? 'ВКЛЮЧЕНО' : 'ОТКЛЮЧЕНО', 'success');
      } else if (j.error === 'storage_tools_not_installed') {
        toast('На устройстве не установлен storage-mount (запустите установщик)', 'error');
      } else {
        toast('Ошибка: ' + (j.error || 'unknown'), 'error');
      }
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError') {
        toast('Таймаут запроса — интерфейс не завис, повторите', 'error');
      } else {
        toast('Нет связи с сервером', 'error');
      }
    });
}

let _lastPartStatus = {};
let _lastServicesStatus = null;

function getBackgroundPartApply(part) {
  switch (part) {
    case 'storage': return applyStorageStatus;
    case 'time': return applyTimeStatus;
    case 'uptime': return applyUptimeStatus;
    case 'network': return applyNetworkStatus;
    case 'load': return applyLoadStatus;
    case 'system': return applySystemStatus;
    case 'services': return applyServicesStatus;
    case 'hardware': return applyHardwareStatus;
    default: return null;
  }
}

function allBackgroundPartsLoaded() {
  return activeBackgroundParts().every(function (p) { return backgroundLoaded[p]; });
}

function syncMainLoadedFlag() {
  backgroundLoaded.main = allBackgroundPartsLoaded();
}

function applyCachedBackgroundPartsI18n() {
  BACKGROUND_STATUS_PARTS.forEach(function (part) {
    const d = _lastPartStatus[part];
    const applyFn = getBackgroundPartApply(part);
    if (d && applyFn) applyFn(d);
  });
}

function applyServicesStatus(d) {
  _lastServicesStatus = d;
  renderServicesDynamic(d);
}

window.refreshMainStatusI18n = function () {
  applyCachedBackgroundPartsI18n();
};

window.refreshPriorityStatusI18n = function () {
  if (_lastPriorityStatus) applyPriorityStatus(_lastPriorityStatus);
};

window.refreshServicesDynamicI18n = function () {
  if (_lastServicesStatus) renderServicesDynamic(_lastServicesStatus);
};

function deviceTitleRu(variant) {
  return variant === 'sa02m-2eth'
    ? 'Сервер автоматизации СА-02м-2'
    : 'Сервер автоматизации СА-02м';
}

// Short model name for the mobile topbar (main.css ≤700 shows this instead of
// the full title so the compact cloud-style controls fit on the logo's row).
function deviceTitleShort(variant) {
  return variant === 'sa02m-2eth' ? 'СА-02м-2' : 'СА-02м';
}

function applyDeviceTitle() {
  const title = document.getElementById('device-title');
  if (!title) return;
  const ru = deviceTitleRu(_boardVariant);
  title.textContent = window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
  // Full-text tooltip so the ellipsis (mobile topbar, main.css ≤700) never
  // truncates without a title; mirrors the current variant + language.
  title.setAttribute('title', title.textContent);
  // Short model name — the mobile topbar shows this (СА-02м / СА-02м-2);
  // translated so EN mode shows the Latin SA-02m like the full title. Guarded.
  var shortEl = document.getElementById('device-title-short');
  if (shortEl) {
    var shortRu = deviceTitleShort(_boardVariant);
    shortEl.textContent = window.sa02mI18n ? window.sa02mI18n.t(shortRu) : shortRu;
  }
}

window.applyDeviceTitle = applyDeviceTitle;

function applyVariantVisibility(variant) {
  const v = variant || 'sa02m-1eth';
  _boardVariant = v;
  document.querySelectorAll('[data-hide-for]').forEach(function(el) {
    const hideFor = el.dataset.hideFor.split(/\s+/).filter(Boolean);
    el.style.display = hideFor.includes(v) ? 'none' : '';
  });
  const ethGrid = document.querySelector('.network-eth-grid');
  if (ethGrid) {
    ethGrid.classList.toggle('network-eth-grid-single', v !== 'sa02m-2eth');
  }
  const title = document.getElementById('device-title');
  if (title) applyDeviceTitle();
  const netDesc = document.getElementById('network-page-desc');
  if (netDesc) {
    netDesc.textContent = uiT(v === 'sa02m-2eth'
      ? 'Конфигурация Ethernet № 1 и № 2'
      : 'Конфигурация Ethernet № 1');
  }
  if (!backgroundLoaded.rs485) renderRs485Skeleton();
}

let _lastHwMetrics = null;

function hwMetricsPayloadValid(d) {
  return !!(d && (d.hw_configured !== undefined || d.hw_poll_disabled !== undefined));
}

function clearHwHintPending() {
  const hint = document.getElementById('hw-hint');
  if (!hint) return;
  hint.textContent = '';
  hint.style.display = 'none';
}

function applyHardwareStatus(d) {
  if (!hwMetricsPayloadValid(d)) return;
  _lastHwMetrics = d;
  const hint = document.getElementById('hw-hint');
  if (hint) {
    let msg = '';
    if (d.hw_i2c_busy === 1) {
      msg = 'ШИНА I2C ЗАНЯТА ДРУГОЙ СЛУЖБОЙ';
    } else if (d.hw_i2c_expander_absent === 1) {
      msg = 'НЕТ СВЯЗИ С МИКРОСХЕМОЙ РАСШИРЕНИЯ I2C';
    } else if (d.hw_configured === 0 && d.hw_poll_disabled !== 1) {
      msg = 'Каналы не заданы — отредактируйте /etc/sa02m_hw.conf';
    }
    hint.textContent = msg ? uiT(msg) : '';
    hint.style.display = msg ? '' : 'none';
  }
  if (d.hw_variant !== undefined) applyVariantVisibility(d.hw_variant);
  applyHwChannel('hw-do-st', 'do', d.hw_do);
  applyHwChannel('hw-beep-st', 'beeper', d.hw_beeper);
  applyHwChannel('hw-led-st', 'alarm_led', d.hw_alarm_led);
  let usbPl = d.hw_usb_power;
  clearPendingUsbPowerIfServerMatches(usbPl);
  if (pendingUsbPowerVal !== null && Date.now() < pendingUsbPowerUntil) {
    usbPl = pendingUsbPowerVal;
  }
  applyHwChannel('hw-usb-st', 'usb_power', usbPl);
  const pin = (k, legacy) => (d[k] !== undefined ? !!d[k] : !!legacy);
  const anyHw = !!d.hw_configured;
  setHwChannelBtns('do',        pin('hw_pin_do', anyHw));
  setHwChannelBtns('beeper',    pin('hw_pin_beeper', anyHw));
  setHwChannelBtns('alarm_led', pin('hw_pin_alarm_led', anyHw));
  setHwChannelBtns('usb_power', pin('hw_pin_usb_power', anyHw));
}

function applyRs485Status(d) {
  if (d.rs485 && d.rs485.length) renderRs485(d.rs485);
}

function fetchPriorityPart(_part, persist = true) {
  if (widgetBusy.priority) return;
  if (!canFetchStatusPart('priority', false)) return;
  scheduleStatusFetch('priority', 0, function (release, meta) {
    const gen = _statusPollGeneration;
    if (widgetBusy.priority) { release(); return; }
    if (!canFetchStatusPart('priority', false)) { release(); return; }
    widgetBusy.priority = true;
    _statusLastFetchMs.priority = Date.now();
    let timedOut = false;
    const queueWaitMs = meta && meta.queueWaitMs ? meta.queueWaitMs : 0;
    statusFetchJson('priority', '/cgi-bin/status.cgi?part=priority', statusRequestTimeout('priority', queueWaitMs), gen, function () {
      timedOut = true;
    })
      .then(d => {
        if (d.error) return;
        applyPriorityStatus(d);
        if (persist) ['cpu', 'temp', 'ram', 'disk'].forEach((part) => writePriorityWarmupPart(part, d));
        noteStatusSuccess('priority');
      })
      .catch((e) => {
        if (gen !== _statusPollGeneration) return;
        if (isBenignStatusFetchError(e, timedOut)) return;
        noteStatusFailure('priority', e);
      })
      .finally(() => {
        widgetBusy.priority = false;
        release();
      });
  });
}

/** Поколение опроса HW: увеличивается после hw_set, чтобы отложенный JSON не затирал UI свежими кнопками. */
let mainStatusEpoch = 0;
function bumpMainStatusEpoch() {
  mainStatusEpoch++;
}

function fetchBackgroundPart(part, applyFn, force, onDone) {
  if (BACKGROUND_STATUS_PARTS.indexOf(part) >= 0 && !shouldFetchBackgroundPart(part)) {
    backgroundLoaded[part] = true;
    if (onDone) onDone(false);
    return;
  }
  if (backgroundBusy[part]) {
    if (part === 'rs485') rs485RefreshQueued = true;
    if (onDone) onDone(false);
    return;
  }
  if (!force && !canFetchStatusPart(part, false)) {
    if (onDone) onDone(false);
    return;
  }
  if (!applyFn && part !== 'rs485') applyFn = getBackgroundPartApply(part);
  if (part !== 'rs485' && !applyFn) {
    if (onDone) onDone(false);
    return;
  }
  scheduleStatusFetch(part, 0, function (release, meta) {
    const gen = _statusPollGeneration;
    if (backgroundBusy[part]) {
      if (part === 'rs485') rs485RefreshQueued = true;
      release();
      if (onDone) onDone(false);
      return;
    }
    if (!force && !canFetchStatusPart(part, false)) {
      release();
      if (onDone) onDone(false);
      return;
    }
    backgroundBusy[part] = true;
    _statusLastFetchMs[part] = Date.now();
    let timedOut = false;
    let reported = false;
    const reportDone = function (failed) {
      if (reported) return;
      reported = true;
      if (onDone) onDone(!!failed);
    };
    let url = '/cgi-bin/status.cgi?part=' + encodeURIComponent(part);
    if (force) url += '&no_cache=1';
    const epochAtStart = part === 'hardware' ? mainStatusEpoch : null;
    const apply = applyFn || applyRs485Status;
    const queueWaitMs = meta && meta.queueWaitMs ? meta.queueWaitMs : 0;
    statusFetchJson(part, url, statusRequestTimeout(part, queueWaitMs), gen, function () {
      timedOut = true;
    })
      .then(d => {
        if (epochAtStart !== null && epochAtStart !== mainStatusEpoch) return;
        if (d.error) return;
        if (part === 'rs485') {
          apply(d);
        } else {
          _lastPartStatus[part] = d;
          apply(d);
        }
        backgroundLoaded[part] = true;
        noteStatusSuccess(part);
      })
      .catch((e) => {
        if (gen !== _statusPollGeneration) return;
        if (isBenignStatusFetchError(e, timedOut)) return;
        noteStatusFailure(part, e);
        reportDone(true);
        if (gen === _statusPollGeneration && part === 'hardware' && !backgroundLoaded.hardware) {
          _statusInitTimeouts.push(setTimeout(function () {
            if (gen !== _statusPollGeneration) return;
            fetchBackgroundPart('hardware', applyHardwareStatus, true);
          }, 800));
        }
      })
      .finally(() => {
        backgroundBusy[part] = false;
        if (part === 'rs485' && rs485RefreshQueued) {
          rs485RefreshQueued = false;
          fetchBackgroundPart('rs485', applyRs485Status);
        }
        reportDone(false);
        release();
      });
  });
}

/**
 * One-shot instant paint of the services list on init: hits
 * status.cgi?part=services&fast=1 (skips the heavy `sudo CTL list`, ~3.5 s) so
 * the widget shows its real rows within ~1 s. The recurring full `services`
 * heavy poll (phase 6500) then refines disabled/masked states. Queued through
 * scheduleStatusFetch under a distinct 'services_fast' key so it is NOT deduped
 * against the full 'services' poll and is treated as a LIGHT fetch (no 2.2 s
 * heavy gap), while still respecting the 350 ms global gap. Best-effort: on any
 * failure the full poll paints the list as before. backgroundLoaded.services is
 * left for the full poll to set, so its refinement is never skipped.
 */
function fetchServicesFast(gen) {
  if (!isStatusBlockEnabled('services')) return;
  if (backgroundLoaded.services) return;
  scheduleStatusFetch('services_fast', 0, function (release, meta) {
    if (gen !== _statusPollGeneration || backgroundLoaded.services) {
      release();
      return;
    }
    const url = '/cgi-bin/status.cgi?part=services&fast=1';
    const queueWaitMs = meta && meta.queueWaitMs ? meta.queueWaitMs : 0;
    statusFetchJson('services_fast', url, statusRequestTimeout('services', queueWaitMs), gen, function () {})
      .then(function (d) {
        if (gen !== _statusPollGeneration || !d || d.error || backgroundLoaded.services) return;
        _lastPartStatus.services = d;
        applyServicesStatus(d);
      })
      .catch(function () { /* best-effort — the full services poll is the real one */ })
      .finally(function () { release(); });
  });
}

function fetchStatusMain(force) {
  if (statusMainRoundBusy) {
    if (force) mainBundleRefreshQueued = true;
    return;
  }
  if (!canFetchStatusPart('main', !!force)) return;
  statusMainRoundBusy = true;
  _statusLastFetchMs.main = Date.now();
  let pending = 0;
  let roundHadFailure = false;
  const finishPart = function (failed) {
    if (failed) roundHadFailure = true;
    pending -= 1;
    if (pending > 0) return;
    statusMainRoundBusy = false;
    syncMainLoadedFlag();
    if (!roundHadFailure) noteStatusSuccess('main');
    if (mainBundleRefreshQueued) {
      mainBundleRefreshQueued = false;
      fetchStatusMain(true);
    }
  };
  activeBackgroundParts().forEach(function (part) {
    pending += 1;
    const phase = BACKGROUND_PART_PHASE_MS[part] || 0;
    const launch = function () {
      fetchBackgroundPart(part, null, !!force, finishPart);
    };
    if (phase === 0) {
      launch();
    } else {
      _statusInitTimeouts.push(setTimeout(launch, phase));
    }
  });
  if (pending === 0) statusMainRoundBusy = false;
}

function fetchSystemWidget() {
  fetchBackgroundPart('system', applySystemStatus);
}

function shortGitSha(sha) {
  if (sha == null || typeof sha !== 'string') return '—';
  const s = sha.trim();
  if (!s) return '—';
  return s.length <= 7 ? s : s.slice(0, 7);
}

function deployedRefDisplay(j) {
  if (j && j.deployed_commit != null && typeof j.deployed_commit === 'string' && j.deployed_commit.trim()) {
    return shortGitSha(j.deployed_commit);
  }
  if (j && j.deployed_label != null && String(j.deployed_label).trim()) {
    return String(j.deployed_label).trim();
  }
  return '—';
}

/** ISO UTC → DD.MM.YY HH:mm:ss (например 22.05.26 12:09:35). */
function fmtWebUpdChecked(raw) {
  if (raw == null || raw === '') return '—';
  const s = String(raw).trim();
  if (!s || s === '—') return '—';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (m) {
    return m[3] + '.' + m[2] + '.' + m[1].slice(-2) + ' ' + m[4] + ':' + m[5] + ':' + m[6];
  }
  return s;
}

/** semver M.M.P[.S]: -1 if a<b, 0 equal, 1 if a>b; null if invalid */
function compareSemver(a, b) {
  const re = /^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$/;
  const ma = re.exec(String(a).trim());
  const mb = re.exec(String(b).trim());
  if (!ma || !mb) return null;
  const pa = [+ma[1], +ma[2], +ma[3], ma[4] != null ? +ma[4] : 0];
  const pb = [+mb[1], +mb[2], +mb[3], mb[4] != null ? +mb[4] : 0];
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const na = pa[i] || 0;
    const nb = pb[i] || 0;
    if (na > nb) return 1;
    if (na < nb) return -1;
  }
  return 0;
}

function webUpdResolveAvailable(j) {
  const depVer = j.deployed_version != null ? String(j.deployed_version).trim() : '';
  const remVer = j.remote_version != null ? String(j.remote_version).trim() : '';
  if (depVer && remVer) {
    const cmp = compareSemver(depVer, remVer);
    if (cmp !== null) return cmp < 0;
  }
  if (j.update_available === true) return true;
  if (j.update_available === false) return false;
  const rem = String(j.remote_commit || '').trim().toLowerCase();
  if (!rem) return null;
  const dep = String(j.deployed_commit || '').trim().toLowerCase();
  if (dep) {
    return dep !== rem && !rem.startsWith(dep) && !dep.startsWith(rem.slice(0, 7));
  }
  const lab = deployedRefDisplay(j).toLowerCase();
  if (lab !== '—' && /^[a-f0-9]{7,40}$/.test(lab)) {
    return lab !== rem && !rem.startsWith(lab);
  }
  return null;
}

function webUpdVersionDisplay(ver, commitOrLabel, fallbackLabel) {
  const v = ver != null && String(ver).trim() ? String(ver).trim() : '';
  if (v) return v;
  if (commitOrLabel != null && typeof commitOrLabel === 'string' && commitOrLabel.trim()) {
    return shortGitSha(commitOrLabel);
  }
  if (fallbackLabel != null && String(fallbackLabel).trim() && String(fallbackLabel).trim() !== '—') {
    return String(fallbackLabel).trim();
  }
  return '—';
}

function applyWebUpdateCheckUI(j) {
  if (!j || typeof j !== 'object') return;
  if (j.error === 'unauthorized') return;
  const depVer = webUpdVersionDisplay(j.deployed_version, j.deployed_commit, deployedRefDisplay(j));
  const remVer = webUpdVersionDisplay(j.remote_version, j.remote_commit, null);
  setText('web-upd-deployed-ver', depVer);
  setText('web-upd-remote-ver', remVer);
  setText('web-upd-checked', fmtWebUpdChecked(j.checked_at));
  const st = document.getElementById('web-upd-status');
  const applyBtn = document.getElementById('web-upd-apply-btn');
  if (!st) return;
  st.classList.remove('is-ok', 'is-warn', 'is-err', 'is-muted');
  const emsg = j.error && j.error !== 'no_cache_yet' ? String(j.error) : '';
  const ua = webUpdResolveAvailable(j);
  if (ua === true) {
    st.textContent = 'Доступно обновление';
    st.classList.add('is-ok');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = false;
  } else if (ua === false) {
    st.textContent = 'Обновлений нет';
    st.classList.add('is-muted');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = true;
  } else if (emsg && !j.remote_commit) {
    st.textContent = 'Проверка не удалась.';
    st.classList.add('is-err');
    st.hidden = false;
    if (applyBtn) applyBtn.hidden = true;
  } else {
    st.textContent = '';
    st.hidden = true;
    if (applyBtn) applyBtn.hidden = true;
  }
}

function loadWebUpdateStatus() {
  fetchWithTimeout('/cgi-bin/web_update_check.cgi', {
    credentials: 'same-origin',
    cache: 'no-store'
  }, 8000)
    .then(function (r) { return r.json(); })
    .then(applyWebUpdateCheckUI)
    .catch(function () { /* вкладка открыта без бэкенда */ });
}

function checkWebUpdatesManual() {
  const btn = document.getElementById('web-upd-check-btn');
  if (btn) btn.disabled = true;
  fetchWithTimeout('/cgi-bin/web_update_check.cgi?force=1', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store'
  }, 25000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      applyWebUpdateCheckUI(j);
      if (j.error === 'unauthorized') {
        toast('Сессия истекла', 'error');
      } else if (j.error && j.error !== 'no_cache_yet' && !j.remote_commit) {
        toast('Проверка не удалась: ' + j.error, 'error');
      } else {
        const ua = webUpdResolveAvailable(j);
        if (ua === true) toast('Доступно обновление', 'info');
        else if (ua === false) toast('Обновлений нет', 'success');
        else toast('Проверка выполнена', 'info');
      }
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError') {
        toast('Таймаут — повторите', 'error');
      } else {
        toast('Нет связи с сервером', 'error');
      }
    })
    .finally(function () { if (btn) btn.disabled = false; });
}

var _webUpdPollTimer = null;

function applyWebUpdate() {
  const applyBtn = document.getElementById('web-upd-apply-btn');
  const checkBtn = document.getElementById('web-upd-check-btn');
  const logEl = document.getElementById('web-upd-log');
  const st = document.getElementById('web-upd-status');
  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = 'Применяется…'; }
  if (checkBtn) checkBtn.disabled = true;
  if (logEl) { logEl.textContent = ''; logEl.hidden = false; }
  if (st) { st.textContent = 'Загрузка и установка обновления…'; st.className = 'web-upd-status is-warn'; st.hidden = false; }

  fetchWithTimeout('/cgi-bin/web_update_apply.cgi', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store'
  }, 10000)
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.status === 'running' || j.status === 'idle') {
        _webUpdStartPolling();
      } else {
        _webUpdFinish(j.status, j.log || '');
      }
    })
    .catch(function () {
      _webUpdFinish('error', 'Нет ответа от сервера');
    });
}

function _webUpdStartPolling() {
  if (_webUpdPollTimer) clearInterval(_webUpdPollTimer);
  _webUpdPollTimer = setInterval(function () {
    fetchWithTimeout('/cgi-bin/web_update_apply.cgi', {
      credentials: 'same-origin', cache: 'no-store'
    }, 8000)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var logEl = document.getElementById('web-upd-log');
        if (logEl && j.log) logEl.textContent = j.log;
        if (j.status !== 'running') {
          clearInterval(_webUpdPollTimer);
          _webUpdPollTimer = null;
          _webUpdFinish(j.status, j.log || '');
        }
      })
      .catch(function () {});
  }, 2000);
}

function _webUpdFinish(status, log) {
  var applyBtn = document.getElementById('web-upd-apply-btn');
  var checkBtn = document.getElementById('web-upd-check-btn');
  var st = document.getElementById('web-upd-status');
  var logEl = document.getElementById('web-upd-log');
  if (checkBtn) checkBtn.disabled = false;
  if (logEl && log) logEl.textContent = log;
  if (status === 'done') {
    if (st) { st.textContent = 'Обновление применено. Страница перезагрузится через 5 секунд…'; st.className = 'web-upd-status is-ok'; st.hidden = false; }
    if (applyBtn) applyBtn.hidden = true;
    toast('Обновление применено успешно', 'success');
    setTimeout(function () { location.reload(); }, 5000);
  } else {
    if (st) { st.textContent = 'Ошибка обновления. Проверьте лог ниже.'; st.className = 'web-upd-status is-err'; st.hidden = false; }
    if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = 'Применить'; }
    toast('Ошибка обновления', 'error');
  }
}

function fetchStatusRs485(force) {
  fetchBackgroundPart('rs485', applyRs485Status, !!force);
}

function fetchStatus() {
  fetchPriorityPart('priority');
  fetchStatusMain();
  fetchStatusRs485();
}

function allBackgroundWidgetsLoaded() {
  return Object.values(backgroundLoaded).every(Boolean);
}

function bootstrapBackgroundWidgets(pollGen) {
  let attempts = 0;
  if (_statusPollTimers.bootstrap) {
    clearInterval(_statusPollTimers.bootstrap);
    _statusPollTimers.bootstrap = null;
  }
  _statusInitTimeouts.push(setTimeout(function () {
    if (pollGen !== _statusPollGeneration) return;
    _statusPollTimers.bootstrap = setInterval(function () {
      if (pollGen !== _statusPollGeneration) {
        clearInterval(_statusPollTimers.bootstrap);
        _statusPollTimers.bootstrap = null;
        return;
      }
      if (allBackgroundWidgetsLoaded() || attempts >= 8) {
        clearInterval(_statusPollTimers.bootstrap);
        _statusPollTimers.bootstrap = null;
        return;
      }
      const missing = activeBackgroundParts().filter(function (p) { return !backgroundLoaded[p]; });
      if (missing.length > 0) {
        fetchBackgroundPart(missing[0], null, false, null);
      } else if (!backgroundLoaded.rs485 && isStatusBlockEnabled('rs485')) {
        fetchStatusRs485();
      }
      attempts += 1;
    }, 2500);
  }, 4000));
}

