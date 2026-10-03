/* Devices tab — live ДТВ / СЭ-02м-3 widgets + MR-02m analog cards + history modal / Excel / events */
import { aiSensorLabel, aiUnitPrecision } from "./ai-sensors.js?v=1.0.7.1";

(function () {
  "use strict";

  const POLL_MS = 5000;
  /** Border turns red when last sample older than this (seconds). */
  const STALE_BORDER_S = 30;
  let timer = null;
  let chartSeries = [];
  let chartMeta = { label: "", unit: "", range: "1h" };
  /** kind: "dtv" | "ce" | "mr" | "carel" | "mtd" */
  let activeDevice = "dtv";
  /** MQTT/cache id, e.g. ce02m3-COM2-14 */
  let activeDeviceId = "";
  let activeDeviceLabel = "";
  let activeMetric = "room_temp";
  /**
   * Chart span state - CONTINUOUS wheel-zoom. `windowSec` (seconds) is the
   * source of truth for a windowed view; the preset buttons are TEMPLATES that
   * set it. `calendarMode` ("" | "mtd" | "month") selects the CE-only calendar
   * spans, which are NOT windowed - while one is active `windowSec` is dormant.
   */
  const PRESET_SEC = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000 };
  const WINDOW_MIN_S = 60; // 1 minute - finest zoom-in
  const WINDOW_MAX_S = 30 * 86400; // 30 days - widest zoom-out
  const ZOOM_IN_FACTOR = 0.8; // wheel up -> shrink the window
  const ZOOM_OUT_FACTOR = 1.25; // wheel down -> grow the window
  let windowSec = PRESET_SEC["1h"];
  let calendarMode = "";
  /** Modal chart mode: "metric" (single) | "overview" (all series for device). */
  let modalMode = "metric";
  /** Signature of last rendered card set (device ids). */
  let cardsSig = "";
  /** Устройства, доступные для повторного добавления (из API). */
  let availableDevices = [];
  let eventsSig = "";
  /** Rows in the last rendered journal — the «Очистить» button is disabled at 0. */
  let eventsCount = 0;
  let eventsClearPending = false;
  /** Hide timer for the inline clear result (same row as «Очистить»). */
  let eventsClearNoteTimer = 0;
  /** Bumped on every clear: a poll that started before it renders nothing. */
  let eventsGen = 0;
  const EVENTS_CLEAR_TIMEOUT_MS = 20000;

  /* Card-title chip icons: one per device kind, saying what the device IS.
     24×24 stroke drawings; stroke width/caps come from the #tab-devices chip
     rule in main.css. ДТВ = thermometer (room climate sensor), СЭ = lightning
     (electricity meter), MR = analog wave (only AI-bearing modules get a card),
     Carel = the «Умный дом» fan symbol (air-handling unit) — referenced from the
     index.html sprite so the unit shows the same fan in both places. */
  const ICO_DTV =
    '<svg viewBox="0 0 24 24"><path d="M10 13.5V5a2 2 0 0 1 4 0v8.5a4 4 0 1 1-4 0z"/><path d="M12 17V9"/></svg>';
  const ICO_CE =
    '<svg viewBox="0 0 24 24"><path d="M13 2L4 14h7l-1 8 10-14h-7l0-6z"/></svg>';
  const ICO_MR =
    '<svg viewBox="0 0 24 24"><path d="M3 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0"/></svg>';
  const ICO_CAREL = '<svg viewBox="0 0 24 24"><use href="#i-fan"/></svg>';
  /* MTDx62-MB presence sensor (person + radar waves) — for its card (1.0.6.69). */
  const ICO_PRESENCE =
    '<svg viewBox="0 0 24 24"><circle cx="8" cy="7" r="3"/><path d="M3 20v-1.5a5 5 0 0 1 10 0V20"/><path d="M16.5 9a4 4 0 0 1 0 6"/><path d="M19.5 6.5a8 8 0 0 1 0 11"/></svg>';

  const DTV_METRICS = [
    ["room_temp", "Температура"],
    ["humidity", "Влажность"],
    ["eco2_ppm", "eCO₂"],
    ["tvoc_mg_m3", "TVOC"],
    ["pressure_mmhg", "Давление"],
    ["light_pct", "Освещённость, %"],
    ["presence", "Присутствие"],
  ];
  const CE_METRICS = [
    ["voltage", "Напряжение"],
    ["current", "Ток"],
    ["power", "Мощность"],
    ["frequency_hz", "Частота"],
    ["energy_kwh_import", "Энергия"],
  ];
  const CAREL_METRICS = [
    ["supply_temp", "Приток"],
    ["return_water_temp", "Обратка"],
    ["room_temp", "Помещение"],
    ["outdoor_temp", "Улица"],
    ["setpoint", "Уставка"],
    ["heat_valve", "Клапан"],
    ["fan_supply", "Приток вент."],
    ["fan_exhaust", "Вытяжка"],
    ["fan_step", "Ступень вент."],
  ];
  /* Chart chips. Prefix matches METRICS (ДТВ already owns `presence`). */
  const MTD_METRICS = [
    ["mtd_illuminance", "Освещённость"],
    ["mtd_target_distance", "Дистанция"],
    ["mtd_presence", "Присутствие"],
  ];

  const COLORS = [
    "#22d3ee",
    "#32d74b",
    "#ffd60a",
    "#ff9f0a",
    "#a78bfa",
    "#ff6b6b",
    "#64d2ff",
    "#bf5af2",
    "#ac8e68",
    "#30d158",
    "#ff453a",
    "#0a84ff",
    "#5e5ce6",
    "#ff375f",
    "#40c8e0",
  ];

  function $(id) {
    return document.getElementById(id);
  }

  function fmt(v, digits) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return Number.isInteger(digits) ? n.toFixed(digits) : String(n);
  }

  function fetchJson(url, opts) {
    const init = Object.assign({ credentials: "same-origin" }, opts || {});
    return fetch(url, init).then((r) => {
      if (!r.ok) {
        return r.text().then((t) => {
          throw new Error((t || r.statusText || "HTTP " + r.status).slice(0, 120));
        });
      }
      return r.json();
    });
  }

  function alertBadgeHtml(alerts) {
    if (!alerts || !alerts.length) return "";
    const a = alerts[0];
    const text = (a.detail_ru || a.detail || a.symptom || "отклонение").slice(0, 80);
    return `<span class="dev-alert-badge" title="${escapeAttr(text)}">⚠ скачок</span>`;
  }

  function escapeAttr(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  function isAgeStale(d) {
    if (!d) return true;
    const age = d.age_s;
    if (age === null || age === undefined || Number.isNaN(Number(age))) {
      return !d.ok;
    }
    return Number(age) > STALE_BORDER_S;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function deviceList(data) {
    const dtv = Array.isArray(data.dtv) ? data.dtv : data.dtv ? [data.dtv] : [];
    const ce = Array.isArray(data.ce) ? data.ce : data.ce ? [data.ce] : [];
    const mr = Array.isArray(data.mr) ? data.mr : data.mr ? [data.mr] : [];
    const carel = Array.isArray(data.carel) ? data.carel : data.carel ? [data.carel] : [];
    const mtd = Array.isArray(data.mtd) ? data.mtd : data.mtd ? [data.mtd] : [];
    // AHU cards first (Operator decision F5); mirrors stand_devices.live_snapshot.
    return [...carel, ...dtv, ...ce, ...mr, ...mtd];
  }

  /* MR-02m analog card body: fixed grid of ai_count cells (a disabled channel
     keeps its cell). Grid columns: >6 AI → 4 (12AI), else 3 (6AI6AO / 6AI2AO).
     Each cell: «AI<n>» mini-label / value+unit / short sensor-type caption. The
     value/unit/caption are filled by updateCard (data-f/data-u/data-s hooks). */
  function mrAiCount(d) {
    const n = Number(d && d.ai_count);
    if (Number.isFinite(n) && n > 0) return n;
    return Array.isArray(d && d.channels) ? d.channels.length : 0;
  }

  /* Last live device object per id — the modal reads channel metadata (units,
     enabled) from this SNAPSHOT at open time, so a concurrent poll can't mutate
     the open modal's chip set. Populated in updateCard. */
  const lastDeviceById = {};

  /* ── ДТВ per-sensor rotation ──────────────────────────────────────────────
     A single module-level 5 s timer advances one shared tick; every visible ДТВ
     card steps its cycling KPIs (temp/rh/eco2/pressure) together, each modulo its
     own sensor count. The value comes from d.<metric>_sensors[tick % len], the
     caption from that entry's chip name. A single-sensor roster shows that one
     entry (no visible motion); an empty roster falls back to «—». TVOC is a single
     ZMOD4410 sensor (static caption in markup); light/presence are untouched. */
  const DTV_ROTATE_MS = 5000;
  let dtvRotTimer = null;
  let dtvRotTick = 0;

  // cell key → { list: per-sensor roster field, scalar: first-available fallback,
  //   digits: display precision }.
  const DTV_CYCLING = {
    temp: { list: "temp_sensors", scalar: "room_temp", digits: 1 },
    rh: { list: "humidity_sensors", scalar: "humidity", digits: 1 },
    eco2: { list: "eco2_sensors", scalar: "eco2_ppm", digits: 0 },
    pressure: { list: "pressure_sensors", scalar: "pressure_mmhg", digits: 1 },
  };

  /** Index into a per-sensor roster of length `len` for a shared `tick`;
      -1 for an empty/absent roster. Wraps safely for any integer tick. */
  function dtvRotationIndex(len, tick) {
    const n = Number(len);
    if (!Number.isFinite(n) || n <= 0) return -1;
    const t = Number(tick);
    const k = Number.isFinite(t) ? Math.trunc(t) : 0;
    return ((k % n) + n) % n;
  }

  /** Fill one cycling ДТВ cell's value + caption from its roster at `tick`.
      No roster (old backend) → first-available scalar, blank caption. */
  function applyDtvCyclingCell(card, key, d, tick) {
    const spec = DTV_CYCLING[key];
    if (!spec || !card) return;
    const valEl = card.querySelector(`[data-f="${key}"]`);
    const subEl = card.querySelector(`[data-s="${key}"]`);
    const roster = d && Array.isArray(d[spec.list]) ? d[spec.list] : null;
    const idx = roster ? dtvRotationIndex(roster.length, tick) : -1;
    if (idx >= 0) {
      const item = roster[idx] || {};
      if (valEl) valEl.textContent = fmt(item.v, spec.digits);
      if (subEl) subEl.textContent = item.t || "";
    } else {
      if (valEl) valEl.textContent = fmt(d ? d[spec.scalar] : null, spec.digits);
      if (subEl) subEl.textContent = "";
    }
  }

  function applyDtvCyclingCells(card, d, tick) {
    Object.keys(DTV_CYCLING).forEach((key) =>
      applyDtvCyclingCell(card, key, d, tick)
    );
  }

  /** Advance the shared tick and re-apply every visible ДТВ card — reads
      lastDeviceById (the live snapshot) so rotation survives a poll re-render. */
  function rotateDtvCards() {
    dtvRotTick += 1;
    const grid = $("dev-grid");
    if (!grid) return;
    grid.querySelectorAll(".dev-card--dtv").forEach((card) => {
      const d = lastDeviceById[card.dataset.deviceId];
      if (d) applyDtvCyclingCells(card, d, dtvRotTick);
    });
  }

  function startDtvRotation() {
    if (dtvRotTimer) return;
    dtvRotTimer = setInterval(() => {
      // Pause work when the page/tab is hidden (tick freezes, no DOM churn).
      if (typeof document !== "undefined" && document.hidden) return;
      rotateDtvCards();
    }, DTV_ROTATE_MS);
  }

  function stopDtvRotation() {
    if (dtvRotTimer) {
      clearInterval(dtvRotTimer);
      dtvRotTimer = null;
    }
  }

  /** «ai_5» → "5" for the API channel= param; "" when not an AI-channel key. */
  function mrChNum(metric) {
    const m = /^ai_(\d+)$/.exec(String(metric || ""));
    return m ? m[1] : "";
  }

  /** Chip list [["ai_N","AI N"], …] for a MR device — ENABLED channels only
      (a disabled channel, sensor_code 0, is hidden from the chart). */
  function mrMetricsFor(id) {
    const d = lastDeviceById[String(id || "")];
    const channels = d && Array.isArray(d.channels) ? d.channels : [];
    return channels
      .filter((c) => c && c.enabled !== false && c.ch != null)
      .map((c) => ["ai_" + c.ch, "AI " + c.ch]);
  }

  function buildMrMetricsHtml(d) {
    const n = mrAiCount(d);
    let cells = "";
    for (let ch = 1; ch <= n; ch++) {
      cells +=
        `<div class="dev-kpi" data-key="ai_${ch}">` +
        `<span class="dev-kpi-idx">AI${ch}</span>` +
        `<span class="dev-kpi-row"><span class="dev-kpi-val" data-f="ai_${ch}">—</span>` +
        `<span class="dev-kpi-unit" data-u="ai_${ch}"></span></span>` +
        `<span class="dev-kpi-sub" data-s="ai_${ch}"></span>` +
        `</div>`;
    }
    return cells;
  }

  function buildDtvMetricsHtml() {
    // [metric, key, unit, label, sub]: sub = "cycle" → a rotating caption span
    // (data-s) the timer fills with the current per-sensor chip name; a literal
    // string → static caption (TVOC = one ZMOD4410 sensor); "" → no caption.
    const rows = [
      ["room_temp", "temp", "°C", "Температура", "cycle"],
      ["humidity", "rh", "%", "Влажность", "cycle"],
      ["eco2_ppm", "eco2", "ppm", "eCO₂", "cycle"],
      ["tvoc_mg_m3", "tvoc", "mg/m³", "TVOC", "ZMOD4410"],
      ["pressure_mmhg", "pressure", "mmHg", "Давление", "cycle"],
      ["light_pct", "light", "%", "Освещённость", ""],
    ];
    return rows
      .map(([metric, key, unit, lbl, sub]) => {
        let subHtml = "";
        if (sub === "cycle") {
          subHtml = `<span class="dev-kpi-sub" data-s="${key}"></span>`;
        } else if (sub) {
          subHtml = `<span class="dev-kpi-sub">${escapeHtml(sub)}</span>`;
        }
        return (
          `<div class="dev-kpi" data-metric="${metric}" data-key="${key}">` +
          `<span class="dev-kpi-lbl">${lbl}</span>` +
          `<span class="dev-kpi-row"><span class="dev-kpi-val" data-f="${key}">—</span>` +
          `<span class="dev-kpi-unit">${unit}</span></span>` +
          subHtml +
          `</div>`
        );
      })
      .join("");
  }

  /* Six cells stay visible. The other four holdings open on a card click
     (.dev-kpi--more). Holding 7..9 are not here. Input 3 (device_status)
     stays polled for the scan fingerprint and is not drawn. */
  const MTD_SETTINGS_MAIN = [
    ["range", "м", "Дальность обнаружения", "detection_distance", "0.01", 2],
    ["leave", "с", "Задержка выключения", "departure_disappearance_delay", "1", 0],
    ["shield", "м", "Зона экранирования", "detection_shielding_distance", "0.01", 2],
  ];
  const MTD_SETTINGS_MORE = [
    ["admit", "с", "Задержка подтверждения входа", "admission_confirmation_delay", "0.01", 2],
    ["trig", "", "Чувствительность срабатывания", "trigger_sensitivity", "1", 0],
    ["hold", "", "Чувствительность удержания", "maintain_sensitivity", "1", 0],
    ["entry", "м", "Сокращение зоны входа", "entrance_distance_reduction", "0.01", 2],
  ];

  function mtdSettingHtml(rows, more) {
    const extra = more ? " dev-kpi--more" : "";
    return rows
      .map(
        ([key, unit, lbl, ctrl, scale]) =>
          `<div class="dev-kpi dev-kpi--set${extra}" data-key="${key}">` +
          `<span class="dev-kpi-lbl" title="${escapeAttr(lbl)}">${lbl}</span>` +
          `<span class="dev-kpi-row">` +
          `<input type="number" class="dev-mtd-set" data-f="${key}" data-ctrl="${ctrl}"` +
          ` data-scale="${scale}" step="${scale}" inputmode="decimal"` +
          ` title="Записать (Enter)" aria-label="${escapeAttr(lbl)}">` +
          (unit ? `<span class="dev-kpi-unit">${unit}</span>` : "") +
          `</span></div>`
      )
      .join("");
  }

  function buildMtdMetricsHtml() {
    const readings = [
      ["mtd_presence", "presence", "", "Присутствие"],
      ["mtd_illuminance", "lux", "lux", "Освещённость"],
      ["mtd_target_distance", "dist", "м", "Дистанция до цели"],
    ];
    const readHtml = readings
      .map(
        ([metric, key, unit, lbl]) =>
          `<div class="dev-kpi" data-metric="${metric}" data-key="${key}">` +
          `<span class="dev-kpi-lbl" title="${escapeAttr(lbl)}">${lbl}</span>` +
          `<span class="dev-kpi-row"><span class="dev-kpi-val" data-f="${key}">—</span>` +
          `<span class="dev-kpi-unit">${unit}</span></span>` +
          `</div>`
      )
      .join("");
    return readHtml + mtdSettingHtml(MTD_SETTINGS_MAIN, false) + mtdSettingHtml(MTD_SETTINGS_MORE, true);
  }

  /* Poll snapshot lags the Modbus echo by up to one devices tick (5 s).
     Hold the value just sent so that snapshot cannot paint the previous
     reading back into the field. Drop it when the register matches, the
     wait expires, or the publish fails.
     Enter on type=number must not preventDefault: that cancels the field's
     own commit and the browser paints the previous number immediately.
     mtdDraft keeps the text the operator typed so a poll (or that revert)
     cannot submit or display the stale register. */
  const mtdPending = {};
  const mtdDraft = {};
  const MTD_PENDING_MS = 12000;
  const MTD_SERVER_FIELD = {
    detection_distance: "detection_distance_m",
    detection_shielding_distance: "detection_shielding_m",
    admission_confirmation_delay: "admission_delay_s",
    departure_disappearance_delay: "departure_delay_s",
    trigger_sensitivity: "trigger_sensitivity",
    maintain_sensitivity: "maintain_sensitivity",
    entrance_distance_reduction: "entrance_reduction_m",
  };

  function mtdPendingKey(id, control) {
    return String(id || "") + "\0" + String(control || "");
  }

  function mtdDropPending(pending, server, digits, now) {
    if (!pending) return false;
    if (!(now <= pending.until)) return true;
    const pn = Number(pending.value);
    if (!Number.isFinite(pn)) return true;
    const sn = Number(server);
    if (!Number.isFinite(sn)) return false;
    const d = digits > 0 ? digits : 0;
    const f = Math.pow(10, d);
    return Math.round(sn * f) === Math.round(pn * f);
  }

  function mtdSettingDisplay(server, pending, digits, now, focused, typed) {
    if (focused) return typed == null ? "" : String(typed);
    const d = digits > 0 ? digits : 0;
    if (pending && !mtdDropPending(pending, server, digits, now)) {
      return Number(pending.value).toFixed(d);
    }
    const sn = Number(server);
    if (server == null || !Number.isFinite(sn)) return "";
    return sn.toFixed(d);
  }

  /* «6,00» / «0,60» / «0,10» — the card shows a comma. type=number usually
     exposes a dot in .value; a text edit or a locale that keeps the comma
     must still parse. */
  function mtdParseSetting(raw) {
    const text = String(raw == null ? "" : raw).trim().replace(/\s/g, "").replace(",", ".");
    if (text === "" || text === "." || text === "-" || text === "-.") return null;
    const n = Number(text);
    return Number.isFinite(n) ? n : null;
  }

  /* Prefer the typed draft when the number field has already snapped back
     to the previous register value. */
  function mtdCommitText(live, draft) {
    if (draft != null && String(draft).trim() !== "") return String(draft).trim();
    return String(live == null ? "" : live).trim();
  }

  function mtdControlDigits(scaleText) {
    const scale = Number(scaleText);
    if (!(scale > 0) || scale >= 1) return 0;
    const s = String(scaleText);
    const dot = s.indexOf(".");
    return dot < 0 ? 0 : s.length - dot - 1;
  }

  function mtdServerNumber(deviceId, ctrl) {
    const d = lastDeviceById[String(deviceId || "")];
    if (!d) return null;
    const field = MTD_SERVER_FIELD[ctrl];
    if (!field || d[field] == null || d[field] === "") return null;
    const n = Number(d[field]);
    return Number.isFinite(n) ? n : null;
  }

  /* null = leave the input alone. A draft or a just-submitted pending value
     wins over the stale snapshot. */
  function mtdPollAssignment(server, pending, digits, now, focused, fieldValue, draft) {
    if (focused) return null;
    if (draft != null) {
      const want = String(draft);
      return String(fieldValue) === want ? null : want;
    }
    const next = mtdSettingDisplay(server, pending, digits, now, false, fieldValue);
    return String(fieldValue) === String(next) ? null : next;
  }

  function commitMtdSetting(deviceId, inp) {
    if (!inp || !deviceId) return;
    const scale = Number(inp.dataset.scale) || 1;
    const ctrl = inp.dataset.ctrl;
    const pkey = mtdPendingKey(deviceId, ctrl);
    const raw = mtdCommitText(inp.value, Object.prototype.hasOwnProperty.call(mtdDraft, pkey) ? mtdDraft[pkey] : null);
    const n = mtdParseSetting(raw);
    if (n == null || !(scale > 0)) {
      delete mtdDraft[pkey];
      inp.setCustomValidity("Вне диапазона регистра (0…65535)");
      inp.reportValidity();
      return;
    }
    const word = Math.round(n / scale);
    if (word < 0 || word > 65535) {
      delete mtdDraft[pkey];
      inp.setCustomValidity("Вне диапазона регистра (0…65535)");
      inp.reportValidity();
      return;
    }
    inp.setCustomValidity("");
    const digits = mtdControlDigits(inp.dataset.scale);
    const f = Math.pow(10, digits > 0 ? digits : 0);
    const same = function (a, b) {
      return Math.round(Number(a) * f) === Math.round(Number(b) * f);
    };
    const prev = mtdPending[pkey];
    if (prev && same(prev.value, n) && Date.now() <= prev.until) {
      delete mtdDraft[pkey];
      return;
    }
    const server = mtdServerNumber(deviceId, ctrl);
    if (server != null && same(server, n)) {
      delete mtdDraft[pkey];
      return;
    }
    mtdPending[pkey] = { value: n, until: Date.now() + MTD_PENDING_MS };
    delete mtdDraft[pkey];
    const shown = Number(n).toFixed(digits > 0 ? digits : 0);
    if (String(inp.value) !== shown) inp.value = shown;
    fetch("cgi-bin/mqtt_set.cgi", {
      method: "POST",
      headers: withCsrfHeaders({
        "Content-Type": "application/x-www-form-urlencoded",
      }),
      body:
        "device=" +
        encodeURIComponent(deviceId) +
        "&control=" +
        encodeURIComponent(ctrl) +
        "&value=" +
        encodeURIComponent(String(n)),
      credentials: "same-origin",
    })
      .then((r) => r.json())
      .then((j) => {
        if (j && j.ok) return;
        delete mtdPending[pkey];
        inp.setCustomValidity((j && j.error) || "Нет связи с сервером");
        inp.reportValidity();
      })
      .catch(() => {
        delete mtdPending[pkey];
        inp.setCustomValidity("Нет связи с сервером");
        inp.reportValidity();
      });
  }

  /* change = blur after an edit, spinner release, and Enter (the number
     field commits first, then fires change). input remembers the text so a
     poll cannot put the register back while that edit is still open.
     Enter is not preventDefault'd — that revert is what snapped the field. */
  function wireMtdSettings(el, id) {
    el.setAttribute("aria-expanded", "false");
    el.addEventListener("click", (e) => {
      if (e.target.closest("input, button, a")) return;
      const open = el.classList.toggle("is-open");
      el.setAttribute("aria-expanded", open ? "true" : "false");
    });
    el.querySelectorAll("input.dev-mtd-set").forEach((inp) => {
      inp.addEventListener("input", () => {
        mtdDraft[mtdPendingKey(id, inp.dataset.ctrl)] = String(inp.value == null ? "" : inp.value);
      });
      inp.addEventListener("change", () => {
        commitMtdSetting(id, inp);
      });
      inp.addEventListener("blur", () => {
        commitMtdSetting(id, inp);
      });
    });
  }

  function buildCeMetricsHtml() {
    const rows = [
      ["voltage", "ua", "V", "Ua"],
      ["voltage", "ub", "V", "Ub"],
      ["voltage", "uc", "V", "Uc"],
      ["current", "ia", "A", "Ia"],
      ["current", "ib", "A", "Ib"],
      ["current", "ic", "A", "Ic"],
      ["power", "p", "W", "Мощность"],
      ["frequency_hz", "f", "Hz", "Частота"],
      ["energy_kwh_import", "e", "кВт·ч", "Энергия"],
    ];
    return rows
      .map(
        ([metric, key, unit, lbl]) =>
          `<div class="dev-kpi" data-metric="${metric}" data-key="${key}">` +
          `<span class="dev-kpi-lbl">${lbl}</span>` +
          `<span class="dev-kpi-row"><span class="dev-kpi-val" data-f="${key}">—</span>` +
          `<span class="dev-kpi-unit">${unit}</span></span></div>`
      )
      .join("");
  }

  /* One stepper for the active season. Winter writes `setpoint`, summer
     writes `setpoint_summer` — the same names on c.pCO and uAria. The other
     row stays in the card (so a season change does not rebuild it) but is
     not shown. No snowflake or sun next to the number. */
  function carelSetpointRowHtml(field, decRole, incRole, decLabel, incLabel) {
    return (
      `<div class="dev-step-row" data-role="sp-row" data-field="${field}" hidden>` +
      `<button type="button" class="dev-step-btn" data-role="${decRole}" aria-label="` +
      decLabel +
      `"><span class="dev-step-glyph">−</span></button>` +
      `<span class="dev-step-val"><span data-f="${field}">—</span>` +
      `<span class="dev-step-unit">°C</span></span>` +
      `<button type="button" class="dev-step-btn" data-role="${incRole}" aria-label="` +
      incLabel +
      `"><span class="dev-step-glyph">+</span></button>` +
      `</div>`
    );
  }

  function carelSetpointTileHtml() {
    return (
      `<div class="dev-kpi dev-kpi--sp">` +
      `<span class="dev-step-empty" data-role="sp-empty">—</span>` +
      carelSetpointRowHtml(
        "setpoint",
        "sp-dec",
        "sp-inc",
        uiT("Уменьшить уставку"),
        uiT("Увеличить уставку")
      ) +
      carelSetpointRowHtml(
        "setpoint_summer",
        "su-dec",
        "su-inc",
        uiT("Уменьшить летнюю уставку"),
        uiT("Увеличить летнюю уставку")
      ) +
      `</div>`
    );
  }

  function carelSeasonTileHtml() {
    return (
      `<div class="dev-kpi" data-key="season">` +
      `<span class="dev-kpi-lbl">Сезон</span>` +
      `<span class="dev-kpi-row"><span class="dev-kpi-val dev-season-val" data-f="season" data-role="season-val">—</span>` +
      `</span></div>`
    );
  }

  function buildCarelMetricsHtml() {
    /* Live tiles, then the setpoint tile pinned to cell 6 (row 2, col 3).
       «Сезон» is an ordinary parameter and fills the cell before it. */
    const rows = [
      ["plant", "", "Состояние"],
      ["supply", "°C", "Приток"],
      ["return", "°C", "Обратка"],
      ["valve", "%", "Клапан"],
    ];
    return (
      rows
        .map(
          ([key, unit, lbl]) =>
            `<div class="dev-kpi" data-key="${key}">` +
            `<span class="dev-kpi-lbl">${lbl}</span>` +
            `<span class="dev-kpi-row"><span class="dev-kpi-val" data-f="${key}">—</span>` +
            `<span class="dev-kpi-unit">${unit}</span></span></div>`
        )
        .join("") +
      carelSeasonTileHtml() +
      carelSetpointTileHtml()
    );
  }

  /* Carel card controls. Ladder values are the settled fan vocabulary
     (sa02m_carel.carel_fan): uAria steps 2/4/7/10, c.pCO percents 20/40/70/100.
     chartOpen is false — the history modal stays shut until «График». */
  function carelControlModel(family) {
    const uaria = family === "uaria";
    const spMax = uaria ? 50 : 99;
    return {
      chartOpen: false,
      controls: [
        { id: "unit_on", kind: "switch", mqtt: "unit_on" },
        {
          id: "setpoint",
          kind: "stepper",
          mqtt: "setpoint",
          unit: "°C",
          min: 0,
          max: spMax,
          step: 0.5,
        },
        {
          id: "setpoint_summer",
          kind: "stepper",
          mqtt: "setpoint_summer",
          unit: "°C",
          min: 0,
          max: spMax,
          step: 0.5,
        },
        {
          id: "fan",
          kind: "steps",
          mqtt: uaria ? "fan_step" : "fan_supply",
          steps: uaria
            ? [
                { id: "low", value: 2 },
                { id: "medium", value: 4 },
                { id: "high", value: 7 },
                { id: "turbo", value: 10 },
              ]
            : [
                { id: "low", value: 20 },
                { id: "medium", value: 40 },
                { id: "high", value: 70 },
                { id: "turbo", value: 100 },
              ],
        },
      ],
    };
  }

  /* Nearest rung by distance; an equal distance picks the HIGHER rung
     (step 6 → high, 55 % → high). Same rule as carel_fan.mode_from_value. */
  function nearestFanStep(steps, raw) {
    if (!steps || !steps.length) return null;
    if (raw == null || raw === "") return null;
    const value = Number(raw);
    if (!Number.isFinite(value)) return null;
    let best = 0;
    for (let i = 1; i < steps.length; i++) {
      if (Math.abs(value - steps[i].value) <= Math.abs(value - steps[best].value)) best = i;
    }
    return steps[best];
  }

  function clampStep(value, min, max, step) {
    const places = String(step).indexOf(".") >= 0 ? 1 : 0;
    let n = Number(value);
    if (!Number.isFinite(n)) n = min;
    n = Math.round(n / step) * step;
    if (n < min) n = min;
    if (n > max) n = max;
    return Number(n.toFixed(places));
  }

  function fanLabel(id) {
    if (id === "low") return uiT("Низк.");
    if (id === "medium") return uiT("Сред.");
    if (id === "high") return uiT("Выс.");
    if (id === "turbo") return uiT("Макс.");
    return "";
  }

  /* Custom device names (pencil rename), stored per browser keyed by device id.
     Overrides the backend label «… № 3 порт 4» without touching the widget config. */
  const CUSTOM_NAMES_KEY = "dev-custom-names";
  const lastLabelById = {};

  function customDeviceNames() {
    try {
      return JSON.parse(localStorage.getItem(CUSTOM_NAMES_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function displayLabel(d) {
    if (d && d.id && (d.label || d.title)) lastLabelById[d.id] = d.label || d.title;
    const custom = customDeviceNames()[d && d.id];
    return custom || (d && (d.label || d.title)) || (d && d.id) || "";
  }

  const WIDGET_NAME_RE = /^[\p{L}\p{N}_ \-./+]{1,64}$/u;
  let titleEditing = false;

  function applyCustomName(id, val) {
    const names = customDeviceNames();
    const def = lastLabelById[id] || "";
    if (val) names[id] = val;
    else delete names[id];
    try {
      localStorage.setItem(CUSTOM_NAMES_KEY, JSON.stringify(names));
    } catch (e) {
      /* ignore */
    }
    const card = document.querySelector('.dev-card[data-device-id="' + id + '"]');
    const titleEl = card && card.querySelector(".dev-card-title");
    if (titleEl) titleEl.textContent = names[id] || def;
    if (activeDeviceId === id) {
      activeDeviceLabel = names[id] || def || activeDeviceLabel;
      const modalTitle = $("dev-modal-title");
      if (modalTitle) {
        modalTitle.textContent = (activeDeviceLabel || "") + " · история";
        modalTitle.dataset.baseName = activeDeviceLabel || "";
      }
    }
  }

  function renameDevice(id) {
    const names = customDeviceNames();
    const def = lastLabelById[id] || "";
    const next = window.prompt(
      "Название виджета (пусто — вернуть стандартное «" + def + "»):",
      names[id] || def
    );
    if (next === null) return;
    applyCustomName(id, next.trim());
  }

  function syncDevPen() {
    const p = $("dev-title-pen");
    if (!p) return;
    const k = titleEditing ? "Сохранить название" : "Переименовать";
    const lab = window.sa02mI18n ? window.sa02mI18n.t(k) : k;
    p.setAttribute("aria-label", lab);
    p.title = lab;
  }

  function startDevRen() {
    const nin = $("dev-title-in"),
      row = document.querySelector("#dev-modal .dev-modal-title-row"),
      ttl = $("dev-modal-title");
    if (!nin || !row || !ttl || !activeDeviceId) return;
    titleEditing = true;
    nin.value = ttl.dataset.baseName || "";
    nin.removeAttribute("aria-invalid");
    nin.setAttribute("tabindex", "0");
    nin.removeAttribute("aria-hidden");
    row.setAttribute("data-edit", "");
    syncDevPen();
    nin.focus();
    nin.select();
  }

  function cancelDevRen() {
    const nin = $("dev-title-in"),
      row = document.querySelector("#dev-modal .dev-modal-title-row");
    titleEditing = false;
    if (row) row.removeAttribute("data-edit");
    if (nin) {
      nin.removeAttribute("aria-invalid");
      nin.setAttribute("tabindex", "-1");
      nin.setAttribute("aria-hidden", "true");
    }
    syncDevPen();
  }

  function commitDevRen() {
    const nin = $("dev-title-in");
    if (!nin || !titleEditing || !activeDeviceId) return;
    const name = nin.value.replace(/^\s+|\s+$/g, "");
    if (!name || !WIDGET_NAME_RE.test(name)) {
      nin.setAttribute("aria-invalid", "true");
      nin.focus();
      return;
    }
    applyCustomName(activeDeviceId, name);
    cancelDevRen();
  }

  function cardKind(d) {
    const k = d && d.kind;
    if (k === "ce" || k === "spodes" || k === "mr" || k === "carel" || k === "dtv" || k === "mtd") return k;
    return "dtv";
  }

  /* Optimistic Carel writes. A poll must not snap the control back before the
     bridge echo; a render never publishes. */
  const carelPending = {};
  const CAREL_PENDING_MS = 8000;

  function carelPendingKey(id, control) {
    return String(id) + "\0" + control;
  }

  function rememberCarelPending(id, control, value) {
    carelPending[carelPendingKey(id, control)] = { value: value, until: Date.now() + CAREL_PENDING_MS };
  }

  function dropCarelPending(id, control) {
    delete carelPending[carelPendingKey(id, control)];
  }

  function carelPendingValue(id, control, server) {
    const key = carelPendingKey(id, control);
    const p = carelPending[key];
    if (!p) return null;
    if (Date.now() > p.until) {
      delete carelPending[key];
      return null;
    }
    const sn = Number(server);
    const pn = Number(p.value);
    if (Number.isFinite(sn) && Number.isFinite(pn) && Math.abs(sn - pn) < 0.05) {
      delete carelPending[key];
      return null;
    }
    return p.value;
  }

  function controlsDead(d) {
    return !d || d.ok === false || isAgeStale(d);
  }

  function shownCarelNumber(d, mqtt, field) {
    const pending = carelPendingValue(d && d.id, mqtt, d ? d[field] : null);
    if (pending != null && Number.isFinite(Number(pending))) return Number(pending);
    const n = Number(d && d[field]);
    return Number.isFinite(n) ? n : null;
  }

  const spodesLoadPending = {};

  function wireSpodesLoad(el, id) {
    const btn = el.querySelector('[data-role="load-off"]');
    if (!btn) return;
    if (spodesLoadPending[id]) btn.disabled = true;
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (btn.disabled) return;
      const ask = uiT("Отключить нагрузку счётчика? Питание потребителя пропадёт.");
      if (typeof window.confirm === "function" && !window.confirm(ask)) return;
      btn.disabled = true;
      spodesLoadPending[id] = true;
      setCtrlErr(el, "");
      fetch("cgi-bin/mqtt_set.cgi", {
        method: "POST",
        headers: typeof withCsrfHeaders === "function"
          ? withCsrfHeaders({ "Content-Type": "application/x-www-form-urlencoded" })
          : { "Content-Type": "application/x-www-form-urlencoded" },
        body: "device=" + encodeURIComponent(id) + "&control=load_disconnect&value=1",
        credentials: "same-origin",
      })
        .then((r) => r.json())
        .then((res) => {
          if (!res || res.ok !== true) {
            setCtrlErr(el, uiT("Команда отключения не выполнена"));
          }
        })
        .catch(() => {
          setCtrlErr(el, uiT("Команда отключения не выполнена"));
        })
        .finally(() => {
          delete spodesLoadPending[id];
          btn.disabled = false;
        });
    });
  }

  function setCtrlErr(card, text) {
    const el = card && card.querySelector('[data-role="ctrl-err"]');
    if (!el) return;
    el.textContent = text || "";
    el.hidden = !text;
  }

  function chartButtonHtml() {
    return (
      `<button type="button" class="btn btn-sm dev-chart-btn" data-role="chart">` +
      uiT("График") +
      `</button>`
    );
  }

  function buildCarelControlsHtml(family) {
    const fan = carelControlModel(family).controls.filter((c) => c.id === "fan")[0];
    const steps = fan.steps
      .map(
        (s) =>
          `<button type="button" class="dev-fan-btn" data-role="fan-step" data-step="${s.id}"` +
          ` data-value="${s.value}" aria-pressed="false">${fanLabel(s.id)}</button>`
      )
      .join("");
    return (
      `<div class="dev-ctrl" data-role="ctrl">` +
      `<div class="dev-fan">` +
      `<span class="dev-ctrl-lbl">` +
      uiT("Вентилятор") +
      `</span>` +
      `<div class="dev-fan-steps" data-mqtt="${fan.mqtt}">${steps}</div>` +
      `</div>` +
      `<p class="dev-ctrl-err" data-role="ctrl-err" hidden></p>` +
      `<div class="dev-act-row">` +
      chartButtonHtml() +
      `<button type="button" class="dev-power" data-role="power" aria-pressed="false">` +
      uiT("ВЫКЛЮЧЕНА") +
      `</button>` +
      `</div>` +
      `</div>`
    );
  }

  function sendCarel(card, id, control, value) {
    fetch("cgi-bin/mqtt_set.cgi", {
      method: "POST",
      headers:
        typeof withCsrfHeaders === "function"
          ? withCsrfHeaders({ "Content-Type": "application/x-www-form-urlencoded" })
          : { "Content-Type": "application/x-www-form-urlencoded" },
      body:
        "device=" +
        encodeURIComponent(id) +
        "&control=" +
        encodeURIComponent(control) +
        "&value=" +
        encodeURIComponent(String(value)),
      credentials: "same-origin",
    })
      .then((r) => r.json())
      .then((j) => {
        if (j && j.ok) {
          setCtrlErr(card, "");
          return;
        }
        dropCarelPending(id, control);
        const d = lastDeviceById[id];
        if (d) paintCarelControls(card, d);
        setCtrlErr(card, uiT("Команда не ушла"));
      })
      .catch(() => {
        dropCarelPending(id, control);
        const d = lastDeviceById[id];
        if (d) paintCarelControls(card, d);
        setCtrlErr(card, uiT("Команда не ушла"));
      });
  }

  function syncFanSteps(card, family) {
    const box = card.querySelector(".dev-fan-steps");
    if (!box) return null;
    const fan = carelControlModel(family).controls.filter((c) => c.id === "fan")[0];
    if (box.dataset.mqtt !== fan.mqtt) {
      box.dataset.mqtt = fan.mqtt;
      box.innerHTML = fan.steps
        .map(
          (s) =>
            `<button type="button" class="dev-fan-btn" data-role="fan-step" data-step="${s.id}"` +
            ` data-value="${s.value}" aria-pressed="false">${fanLabel(s.id)}</button>`
        )
        .join("");
    } else {
      box.querySelectorAll('[data-role="fan-step"]').forEach((btn) => {
        const lab = fanLabel(btn.dataset.step);
        if (btn.textContent !== lab) btn.textContent = lab;
      });
    }
    return fan;
  }

  function paintCarelControls(card, d) {
    if (!card || !d) return;
    const dead = controlsDead(d);
    const model = carelControlModel(d.family);
    const on = shownCarelNumber(d, "unit_on", "unit_on");
    const power = card.querySelector('[data-role="power"]');
    if (power) {
      const pressed = on != null && on >= 1;
      power.setAttribute("aria-pressed", pressed ? "true" : "false");
      const lab = uiT(pressed ? "ВКЛЮЧЕНА" : "ВЫКЛЮЧЕНА");
      if (power.textContent !== lab) power.textContent = lab;
      power.disabled = dead || on == null;
    }
    const rows = [
      ["setpoint", "setpoint", "sp-dec", "sp-inc"],
      ["setpoint_summer", "setpoint_summer", "su-dec", "su-inc"],
    ];
    rows.forEach(([id, field, decRole, incRole]) => {
      const spec = model.controls.filter((c) => c.id === id)[0];
      const val = shownCarelNumber(d, spec.mqtt, field);
      const node = card.querySelector('[data-f="' + field + '"]');
      if (node) node.textContent = val == null ? "—" : val.toFixed(1);
      const off = dead || val == null;
      const dec = card.querySelector('[data-role="' + decRole + '"]');
      const inc = card.querySelector('[data-role="' + incRole + '"]');
      if (dec) dec.disabled = off;
      if (inc) inc.disabled = off;
    });
    const fan = syncFanSteps(card, d.family);
    const raw = shownCarelNumber(d, fan.mqtt, fan.mqtt);
    const near = raw == null ? null : nearestFanStep(fan.steps, raw);
    const fanOff = dead || raw == null;
    card.querySelectorAll('[data-role="fan-step"]').forEach((btn) => {
      btn.setAttribute("aria-pressed", near && btn.dataset.step === near.id ? "true" : "false");
      btn.disabled = fanOff;
    });
    const chart = card.querySelector('[data-role="chart"]');
    if (chart) {
      const lab = uiT("График");
      if (chart.textContent !== lab) chart.textContent = lab;
    }
    paintCarelSeason(card, d);
  }

  /* 0 → ЗИМА, 1 → ЛЕТО. c.pCO is coil 67. uAria is coil 17
     «Нагрев/охлаждение» (0 нагрев, 1 охлаждение), same control name. */
  function carelSeasonNow(d) {
    if (!d || d.season == null || d.season === "") return null;
    const n = Number(d.season);
    if (!Number.isFinite(n)) return null;
    return n >= 1 ? "summer" : "winter";
  }

  /* The number the stepper may show. Winter is `setpoint` on both families
     (c.pCO HR51, uAria HR30). Summer is `setpoint_summer` (HR52 / HR32).
     No reading → neither row. */
  function carelActiveSetpoint(season) {
    if (season === "summer") return "setpoint_summer";
    if (season === "winter") return "setpoint";
    return "";
  }

  function paintCarelSeason(card, d) {
    const el = card.querySelector('[data-role="season-val"]');
    const season = carelSeasonNow(d);
    if (el) {
      if (season !== "winter" && season !== "summer") {
        if (el.textContent !== "—") el.textContent = "—";
        el.removeAttribute("data-season");
      } else {
        el.dataset.season = season;
        const label = uiT(season === "summer" ? "ЛЕТО" : "ЗИМА");
        if (el.textContent !== label) el.textContent = label;
      }
    }
    const field = carelActiveSetpoint(season);
    card.querySelectorAll('[data-role="sp-row"]').forEach((row) => {
      row.hidden = row.dataset.field !== field;
    });
    const empty = card.querySelector('[data-role="sp-empty"]');
    if (empty) empty.hidden = field !== "";
  }

  function nudgeCarel(card, id, specId, dir) {
    const d = lastDeviceById[id];
    if (!d || controlsDead(d)) return;
    const spec = carelControlModel(d.family).controls.filter((c) => c.id === specId)[0];
    const field = spec.mqtt;
    const cur = shownCarelNumber(d, spec.mqtt, field);
    if (cur == null) return;
    const next = clampStep(cur + dir * spec.step, spec.min, spec.max, spec.step);
    if (Math.abs(next - cur) < 0.001) return;
    rememberCarelPending(id, spec.mqtt, next);
    paintCarelControls(card, d);
    sendCarel(card, id, spec.mqtt, next);
  }

  function wireCarelControls(el, id) {
    const power = el.querySelector('[data-role="power"]');
    if (power) {
      power.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const d = lastDeviceById[id];
        if (!d || controlsDead(d) || power.disabled) return;
        const cur = shownCarelNumber(d, "unit_on", "unit_on");
        if (cur == null) return;
        const next = cur >= 1 ? 0 : 1;
        rememberCarelPending(id, "unit_on", next);
        paintCarelControls(el, d);
        sendCarel(el, id, "unit_on", next);
      });
    }
    const nudges = [
      ["sp-dec", "setpoint", -1],
      ["sp-inc", "setpoint", 1],
      ["su-dec", "setpoint_summer", -1],
      ["su-inc", "setpoint_summer", 1],
    ];
    nudges.forEach(([role, specId, dir]) => {
      const btn = el.querySelector('[data-role="' + role + '"]');
      if (!btn) return;
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (btn.disabled) return;
        nudgeCarel(el, id, specId, dir);
      });
    });
    const fanBox = el.querySelector(".dev-fan-steps");
    if (fanBox) {
      fanBox.addEventListener("click", (e) => {
        const btn = e.target.closest && e.target.closest('[data-role="fan-step"]');
        if (!btn || btn.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        const d = lastDeviceById[id];
        if (!d || controlsDead(d)) return;
        const fan = carelControlModel(d.family).controls.filter((c) => c.id === "fan")[0];
        const value = Number(btn.dataset.value);
        if (!Number.isFinite(value)) return;
        const cur = shownCarelNumber(d, fan.mqtt, fan.mqtt);
        if (cur != null && Math.abs(cur - value) < 0.05) return;
        rememberCarelPending(id, fan.mqtt, value);
        paintCarelControls(el, d);
        sendCarel(el, id, fan.mqtt, value);
      });
    }
  }

  function wireChartButton(el, kind, id) {
    const btn = el.querySelector('[data-role="chart"]');
    if (!btn) return;
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const d = lastDeviceById[id];
      openModal(kind, id, d ? displayLabel(d) : id);
    });
  }

  function buildCard(d) {
    const kind = cardKind(d);
    const isMr = kind === "mr";
    const isCarel = kind === "carel";
    const isMtd = kind === "mtd";
    const id = String(d.id || "");
    const title = d.label || d.title || id;
    const el = document.createElement("article");
    el.className =
      "widget dev-card" +
      (kind === "dtv"
        ? " dev-card--dtv"
        : isMr
        ? " dev-card--mr"
        : isCarel
        ? " dev-card--carel"
        : isMtd
        ? " dev-card--mtd"
        : "");
    el.dataset.deviceId = id;
    el.dataset.kind = kind;
    const controllable = isCarel || isMtd;
    // Read-only cards open history from the card itself. A controllable card
    // (Carel, MTD) keeps the chart behind «График» — closed until that button.
    if (!controllable) {
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.title = "Открыть историю";
    }
    const ico =
      kind === "dtv" ? ICO_DTV : isMr ? ICO_MR : isCarel ? ICO_CAREL : isMtd ? ICO_PRESENCE : ICO_CE;
    let metricsCls = "dev-metrics";
    let metricsHtml;
    if (isMr) {
      metricsCls += mrAiCount(d) > 6 ? " dev-metrics--mr4" : " dev-metrics--mr3";
      metricsHtml = buildMrMetricsHtml(d);
    } else if (kind === "dtv") {
      metricsHtml = buildDtvMetricsHtml();
    } else if (isMtd) {
      metricsHtml = buildMtdMetricsHtml();
    } else if (isCarel) {
      metricsHtml = buildCarelMetricsHtml();
    } else {
      metricsHtml = buildCeMetricsHtml();
    }
    el.innerHTML =
      `<div class="widget-title has-ico">` +
      `<span class="w-ico w-ico-cyan" aria-hidden="true">${ico}</span>` +
      `<span class="dev-card-title">${escapeHtml(title)}</span>` +
      `<button type="button" class="dev-card-rename" data-role="rename" title="Переименовать виджет" aria-label="Переименовать виджет">✎</button>` +
      (kind === "dtv"
        ? `<span class="dev-presence" data-role="presence" hidden title="Датчик присутствия LD2412">` +
          `<span class="dev-presence-dot" aria-hidden="true"></span>` +
          `<span class="dev-presence-txt">Присутствие</span>` +
          `<span class="dev-presence-dist" data-role="presence-dist"></span>` +
          `</span>`
        : "") +
      `<span data-role="alerts"></span>` +
      `<span class="dev-head-right">` +
      `<span class="dev-pill ok" data-role="status">…</span>` +
      (isMr || isCarel || isMtd
        ? ""
        : `<button type="button" class="dev-card-remove" data-role="remove" title="Удалить виджет (архив остановится, данные в БД сохранятся)" aria-label="Удалить виджет">×</button>`) +
      `</span>` +
      `</div>` +
      `<div class="widget-body dev-body"><div class="${metricsCls}">` +
      metricsHtml +
      `</div>` +
      (isCarel ? buildCarelControlsHtml(d.family) : isMtd ? chartButtonHtml() : "") +
      (kind === "spodes" && d.association === "configurator"
        ? `<div class="dev-spodes-load"><button type="button" class="btn btn-sm" data-role="load-off">${uiT("Отключить нагрузку")}</button><span data-role="ctrl-err" class="dev-ctrl-err" hidden></span></div>`
        : "") +
      `</div>`;
    wireSpodesLoad(el, id);
    if (!controllable) {
      el.addEventListener("click", (e) => {
        if (e.target.closest('[data-role="remove"], [data-role="rename"], input, button')) return;
        // MR KPIs carry data-key="ai_N" (no data-metric); a KPI click opens that
        // channel's chip. DTV/CE chart KPIs carry data-metric.
        const kpi = isMr
          ? e.target.closest(".dev-kpi[data-key]")
          : e.target.closest(".dev-kpi[data-metric]");
        const shortcut = kpi
          ? isMr
            ? kpi.dataset.key
            : kpi.dataset.metric
          : undefined;
        openModal(kind, id, title, shortcut);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openModal(kind, id, title);
        }
      });
    } else {
      wireChartButton(el, kind, id);
      if (isCarel) wireCarelControls(el, id);
      if (isMtd) wireMtdSettings(el, id);
    }
    const renameBtn = el.querySelector('[data-role="rename"]');
    if (renameBtn) {
      renameBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        renameDevice(id);
      });
    }
    const rm = el.querySelector('[data-role="remove"]');
    if (rm) {
      rm.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeWidget(id, title);
      });
    }
    return el;
  }

  function removeWidget(id, title) {
    const label = title || id;
    if (
      !window.confirm(
        "Удалить виджет «" +
          label +
          "»?\n\nКарточка скрывается, архивирование в БД останавливается. Старые данные не удаляются."
      )
    ) {
      return;
    }
    // Daemon POSTs carry the panel's CSRF token like every CGI POST (1.0.6.65,
    // selective-csrf-policy.md «Демоны»); withCsrfHeaders is app.js's global —
    // the module runs after the classic bundles; the typeof guard (plan §5.2)
    // keeps a page whose app.js failed to load from throwing — the daemon then
    // answers E_CSRF instead. Gate: js-post-csrf-headers.
    fetchJson("api/devices/widgets/remove", {
      method: "POST",
      headers: typeof withCsrfHeaders === "function" ? withCsrfHeaders({ "Content-Type": "application/json" }) : { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id }),
    })
      .then(() => {
        cardsSig = "";
        return refreshLive();
      })
      .catch((err) => {
        window.alert("Не удалось удалить виджет: " + (err && err.message ? err.message : err));
      });
  }

  function addWidget(id) {
    return fetchJson("api/devices/widgets/add", {
      method: "POST",
      headers: typeof withCsrfHeaders === "function" ? withCsrfHeaders({ "Content-Type": "application/json" }) : { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id }),
    })
      .then(() => {
        cardsSig = "";
        closeAddModal();
        return refreshLive();
      })
      .catch((err) => {
        window.alert("Не удалось добавить виджет: " + (err && err.message ? err.message : err));
      });
  }

  function renderAddList() {
    const list = $("dev-add-list");
    const empty = $("dev-add-empty");
    if (!list) return;
    list.innerHTML = "";
    const items = availableDevices || [];
    if (empty) empty.hidden = items.length > 0;
    items.forEach((d) => {
      const id = String(d.id || "");
      const row = document.createElement("button");
      row.type = "button";
      row.className = "dev-add-item";
      row.innerHTML =
        `<span class="dev-add-item-title">${escapeHtml(d.label || id)}</span>` +
        `<span class="dev-add-item-meta muted">${escapeHtml(
          (d.kind === "ce"
            ? "СЭ-02м-3"
            : d.kind === "spodes"
            ? "Меркурий"
            : d.kind === "carel"
            ? "Carel"
            : d.kind === "mr"
            ? "MR-02m"
            : "ДТВ") +
            (d.online ? " · online" : " · offline")
        )}</span>` +
        `<span class="dev-add-item-action">Добавить</span>`;
      row.addEventListener("click", () => addWidget(id));
      list.appendChild(row);
    });
  }

  function openAddModal() {
    const modal = $("dev-add-modal");
    if (!modal) return;
    renderAddList();
    modal.hidden = false;
  }

  function closeAddModal() {
    const modal = $("dev-add-modal");
    if (modal) modal.hidden = true;
  }

  function updateAddButton() {
    const btn = $("dev-widget-add-btn");
    if (!btn) return;
    const n = (availableDevices || []).length;
    btn.disabled = n <= 0;
    btn.textContent = n > 0 ? "+ Добавить виджет (" + n + ")" : "+ Добавить виджет";
    btn.title =
      n > 0
        ? "Доступно для добавления: " + n
        : "Нет удалённых устройств для добавления";
  }

  function setField(card, key, text) {
    const n = card.querySelector(`[data-f="${key}"]`);
    if (n) n.textContent = text;
  }

  function updateCard(card, d) {
    if (!card || !d) return;
    if (d.id) lastDeviceById[String(d.id)] = d;
    const stale = isAgeStale(d);
    card.classList.toggle("dev-stale", stale);
    card.classList.toggle("dev-stale-border", stale);
    card.classList.toggle("dev-has-alert", !!(d.alerts && d.alerts.length));
    const titleEl = card.querySelector(".dev-card-title");
    if (titleEl) {
      titleEl.textContent = displayLabel(d);
    }
    const st = card.querySelector('[data-role="status"]');
    if (st) {
      st.textContent = stale ? "stale" : "online";
      st.className = "dev-pill " + (stale ? "bad" : "ok");
    }
    const al = card.querySelector('[data-role="alerts"]');
    if (al) al.innerHTML = alertBadgeHtml(d.alerts);
    if (d.kind === "carel" || card.dataset.kind === "carel") {
      setField(card, "plant", d.plant_state_text || "—");
      setField(card, "supply", fmt(d.supply_temp, 1));
      setField(card, "return", fmt(d.return_water_temp, 1));
      setField(card, "valve", fmt(d.heat_valve, 0));
      card.classList.toggle("dev-has-alarm", Number(d.alarm) === 1);
      paintCarelControls(card, d);
      return;
    }
    if (d.kind === "mr" || card.dataset.kind === "mr") {
      const channels = Array.isArray(d.channels) ? d.channels : [];
      channels.forEach((c) => {
        const ch = c && c.ch;
        if (ch == null) return;
        const kpi = card.querySelector(`.dev-kpi[data-key="ai_${ch}"]`);
        if (!kpi) return;
        const hasVal =
          c.value !== null && c.value !== undefined && Number.isFinite(Number(c.value));
        const valEl = kpi.querySelector(`[data-f="ai_${ch}"]`);
        const unitEl = kpi.querySelector(`[data-u="ai_${ch}"]`);
        const subEl = kpi.querySelector(`[data-s="ai_${ch}"]`);
        // Render at the unit's WB display precision, keeping trailing zeros
        // (28.0 not 28; a 0–10 V channel «5.000»). Disabled/null → «—».
        if (valEl) {
          valEl.textContent = hasVal
            ? Number(c.value).toFixed(aiUnitPrecision(c.unit || ""))
            : "—";
        }
        if (unitEl) unitEl.textContent = hasVal ? c.unit || "" : "";
        // Short technical caption («NTC 10k», «Ток 4–20 мА», «Выключен» for a
        // code-0 channel); i18n observer translates the RU-source captions. The
        // title mirrors it so a long caption that ellipsis-truncates in a narrow
        // 3-col cell is recoverable on hover (property assignment = text-safe).
        if (subEl) {
          const cap = aiSensorLabel(c.sensor_code, { short: true });
          subEl.textContent = cap;
          subEl.title = cap;
        }
        kpi.classList.toggle("dev-kpi--off", c.enabled === false);
        kpi.classList.toggle("dev-kpi--err", c.ok === false);
      });
      return;
    }
    if (d.kind === "mtd" || card.dataset.kind === "mtd") {
      const present = Number(d.presence);
      setField(
        card,
        "presence",
        d.presence == null || Number.isNaN(present) ? "—" : present > 0 ? "да" : "нет"
      );
      setField(card, "lux", fmt(d.illuminance_lux, 1));
      setField(card, "dist", fmt(d.target_distance_m, 2));
      const sets = [
        ["range", d.detection_distance_m, 2],
        ["shield", d.detection_shielding_m, 2],
        ["admit", d.admission_delay_s, 2],
        ["leave", d.departure_delay_s, 0],
        ["trig", d.trigger_sensitivity, 0],
        ["hold", d.maintain_sensitivity, 0],
        ["entry", d.entrance_reduction_m, 2],
      ];
      const now = Date.now();
      const devId = String(d.id || "");
      sets.forEach(([key, val, digits]) => {
        const inp = card.querySelector('input.dev-mtd-set[data-f="' + key + '"]');
        if (!inp) return;
        const pkey = mtdPendingKey(devId, inp.dataset.ctrl);
        const pending = mtdPending[pkey];
        if (pending && mtdDropPending(pending, val, digits, now)) delete mtdPending[pkey];
        const focused = document.activeElement === inp;
        const draft = Object.prototype.hasOwnProperty.call(mtdDraft, pkey) ? mtdDraft[pkey] : null;
        const assign = mtdPollAssignment(
          val,
          mtdPending[pkey] || null,
          digits,
          now,
          focused,
          inp.value,
          draft
        );
        if (assign != null) inp.value = assign;
      });
      const chart = card.querySelector('[data-role="chart"]');
      if (chart) {
        const lab = uiT("График");
        if (chart.textContent !== lab) chart.textContent = lab;
      }
      return;
    }
    if (d.kind === "ce" || d.kind === "spodes"
        || card.dataset.kind === "ce" || card.dataset.kind === "spodes") {
      const u = d.voltage || {};
      const i = d.current || {};
      const p = d.power_w || {};
      setField(card, "ua", fmt(u.a, 1));
      setField(card, "ub", fmt(u.b, 1));
      setField(card, "uc", fmt(u.c, 1));
      setField(card, "ia", fmt(i.a, 3));
      setField(card, "ib", fmt(i.b, 3));
      setField(card, "ic", fmt(i.c, 3));
      setField(card, "p", fmt(p.total, 0));
      setField(card, "f", fmt(d.frequency_hz, 2));
      setField(card, "e", fmt(d.energy_kwh_import, 1));
    } else {
      // Cycling cells (temp/rh/eco2/pressure) render at the CURRENT shared tick
      // so a 5 s poll refresh keeps the caption the 2 s timer last showed.
      applyDtvCyclingCells(card, d, dtvRotTick);
      setField(card, "tvoc", fmt(d.tvoc_mg_m3, 2));
      setField(card, "light", fmt(d.light_pct, 0));
      renderPresence(card, d, stale);
    }
  }

  /** LD2412 distance (cm) → «2.4 м» / «80 см». */
  function fmtDistance(cm) {
    const n = Number(cm);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n >= 100) return (n / 100).toFixed(1) + " м";
    return Math.round(n) + " см";
  }

  /** Presence badge in the ДТВ card header — visible only on fresh presence. */
  function renderPresence(card, d, stale) {
    const el = card.querySelector('[data-role="presence"]');
    if (!el) return;
    const active = Number(d.presence) > 0 && !stale;
    el.hidden = !active;
    if (!active) return;
    const dist = fmtDistance(d.distance_cm);
    const distEl = el.querySelector('[data-role="presence-dist"]');
    if (distEl) distEl.textContent = dist ? "· " + dist : "";
    // Keep the static translatable base title set in buildCard (has a DICT
    // entry, so the i18n observer translates it); the distance is shown
    // separately in the presence-dist span, never baked into the title (a
    // runtime-assembled string can never match a DICT key — i18n floor).
  }

  /* Columns already used by the devices grid. Not a new breakpoint:
     >1100px is 3, 1025–1100px is 2, ≤1024px is 1. */
  function devGridCols() {
    if (!window.matchMedia) return 3;
    if (window.matchMedia("(max-width: 1024px)").matches) return 1;
    if (window.matchMedia("(max-width: 1100px)").matches) return 2;
    return 3;
  }

  /* Carel shares its row with СЭ-02м (9 parameters). If there is no СЭ,
     the open seats of that row go to the next most loaded cards.
     One column (phone) keeps the incoming order. */
  function orderDeviceCards(list, cols) {
    const items = Array.isArray(list) ? list.slice() : [];
    const columns = cols === 2 || cols === 3 ? cols : 1;
    if (columns < 2) return items;
    const kindOf = function (d) {
      const k = d && d.kind;
      if (k === "ce" || k === "spodes" || k === "mr" || k === "carel" || k === "dtv" || k === "mtd") return k;
      return "dtv";
    };
    const paramsOf = function (d) {
      const k = kindOf(d);
      if (k === "ce" || k === "spodes") return 9;
      if (k === "mr") {
        const n = Number(d && d.ai_count);
        if (Number.isFinite(n) && n > 0) return n;
        return Array.isArray(d && d.channels) ? d.channels.length : 0;
      }
      if (k === "dtv" || k === "mtd") return 6;
      return 0;
    };
    const carel = [];
    const ce = [];
    const rest = [];
    items.forEach((d) => {
      const k = kindOf(d);
      if (k === "carel") carel.push(d);
      else if (k === "ce" || k === "spodes") ce.push(d);
      else rest.push(d);
    });
    if (!carel.length) return items;
    const head = carel.concat(ce);
    const ranked = rest.slice().sort((a, b) => paramsOf(b) - paramsOf(a));
    const rem = head.length % columns;
    const padN = rem === 0 ? 0 : columns - rem;
    const pad = ranked.slice(0, padN);
    const chosen = head.concat(pad);
    const tail = items.filter((d) => chosen.indexOf(d) < 0);
    return chosen.concat(tail);
  }

  let lastRawDevices = null;

  function ensureCards(list) {
    const grid = $("dev-grid");
    if (!grid) return;
    lastRawDevices = list;
    const ordered = orderDeviceCards(list, devGridCols());
    const empty = $("dev-empty");
    const sig = ordered.map((d) => d.id || "").join("|") + "#" + devGridCols();
    if (sig !== cardsSig) {
      cardsSig = sig;
      grid.querySelectorAll(".dev-card").forEach((el) => el.remove());
      if (empty) empty.hidden = ordered.length > 0;
      ordered.forEach((d) => grid.appendChild(buildCard(d)));
    } else if (empty) {
      empty.hidden = ordered.length > 0;
    }
    ordered.forEach((d) => {
      const want = String(d.id || "");
      const card = Array.from(grid.querySelectorAll(".dev-card")).find(
        (c) => c.dataset.deviceId === want
      );
      if (card) updateCard(card, d);
    });
  }

  function fmtBackend(data) {
    const b = (data && data.history_backend) || "";
    const map = { usb: "USB", sd: "SD", emmc: "eMMC", force: "файл" };
    const label = map[b] || (b ? b : "—");
    const arch = data && data.history_archives_count != null
      ? Number(data.history_archives_count)
      : 0;
    let s = "Архив: " + label;
    if (arch > 0) s += " · " + arch + " арх.";
    return s;
  }

  function renderEvents(events) {
    const body = $("dev-events-body");
    const meta = $("dev-events-meta");
    if (!body) return;
    const list = Array.isArray(events) ? events : [];
    eventsCount = list.length;
    syncEventsClearButton();
    const sig = list.map((e) => String(e.id || "") + ":" + String(e.ts || "")).join("|");
    if (sig === eventsSig && body.children.length) return;
    eventsSig = sig;
    if (!list.length) {
      body.innerHTML =
        '<tr class="dev-events-empty"><td colspan="5">Пока нет событий пиковых нагрузок</td></tr>';
      if (meta) meta.textContent = "Пиковые нагрузки СЭ · U/I";
      return;
    }
    body.innerHTML = list
      .map((e) => {
        const kind = String(e.kind || "");
        const addr =
          e.addr != null && e.addr !== "" ? String(e.addr) : "—";
        const port =
          e.port_num != null && e.port_num !== "" ? String(e.port_num) : "—";
        const phase = e.phase ? String(e.phase) : "—";
        const t = e.ts_label || "—";
        const msg = e.message || kind || "—";
        return (
          `<tr class="kind-${escapeAttr(kind)}">` +
          `<td class="dev-events-mono">${escapeHtml(t)}</td>` +
          `<td class="dev-events-mono">${escapeHtml(addr)}</td>` +
          `<td class="dev-events-mono">${escapeHtml(port)}</td>` +
          `<td class="dev-events-mono">${escapeHtml(phase)}</td>` +
          `<td>${escapeHtml(msg)}</td>` +
          `</tr>`
        );
      })
      .join("");
    if (meta) meta.textContent = "Записей: " + list.length + " · обновляется автоматически";
  }

  function refreshEvents() {
    const gen = eventsGen;
    return fetchJson("api/devices/events?limit=80")
      .then((data) => {
        if (!data || !data.ok) return;
        if (gen !== eventsGen) return;
        renderEvents(data.events || []);
      })
      .catch(() => {});
  }

  function syncEventsClearButton() {
    const btn = $("dev-events-clear-btn");
    if (btn) btn.disabled = eventsClearPending || eventsCount === 0;
  }

  function eventsClearErrorText(status, body) {
    if (body && body.error === "busy") {
      return tl("Журнал не очищен: архив занят записью, повторите через несколько секунд");
    }
    if (body && body.error_code === "E_CSRF") {
      return tl("Журнал не очищен: защита сессии отклонила запрос");
    }
    if (status === 401) return tl("Журнал не очищен: сессия истекла");
    if (!status) return tl("Журнал не очищен: нет ответа от сервера");
    return tl("Журнал не очищен: ошибка сервера") + " (HTTP " + status + ")";
  }

  /** Clear result sits on the journal header row, in the gap left of «Очистить».
   *  Other toasts stay on the global top-right stack. */
  function eventsClearToast(msg, type) {
    const note = $("dev-events-clear-note");
    const head = note ? note.closest(".dev-events-head") : null;
    if (!note || !head) {
      if (typeof window.toast === "function") window.toast(msg, type, 6000);
      return;
    }
    note.textContent = msg;
    note.title = msg;
    note.classList.remove("dev-events-clear-note--success", "dev-events-clear-note--error");
    note.classList.add(type === "error" ? "dev-events-clear-note--error" : "dev-events-clear-note--success");
    note.hidden = false;
    head.classList.add("dev-events-head--note");
    if (eventsClearNoteTimer) clearTimeout(eventsClearNoteTimer);
    eventsClearNoteTimer = setTimeout(() => {
      eventsClearNoteTimer = 0;
      note.hidden = true;
      note.textContent = "";
      note.removeAttribute("title");
      head.classList.remove("dev-events-head--note");
    }, 6000);
  }

  /** «Очистить» — deletes the WHOLE journal (every device) on the daemon. The
   *  table changes only on a confirmed `ok:true`; any failure keeps the list. */
  function clearEventsJournal() {
    if (eventsClearPending) return Promise.resolve(false);
    if (
      !window.confirm(
        tl("Очистить журнал событий? Все события будут удалены без возможности восстановления.")
      )
    ) {
      return Promise.resolve(false);
    }
    eventsClearPending = true;
    eventsGen++;
    syncEventsClearButton();
    const ctrl = typeof AbortController === "function" ? new AbortController() : null;
    const abortTimer = ctrl ? setTimeout(() => ctrl.abort(), EVENTS_CLEAR_TIMEOUT_MS) : null;
    return fetch("api/devices/events/clear", {
      method: "POST",
      credentials: "same-origin",
      headers: typeof withCsrfHeaders === "function" ? withCsrfHeaders({}) : {},
      signal: ctrl ? ctrl.signal : undefined,
    })
      .then((r) =>
        r.text().then((t) => {
          let body = null;
          try {
            body = JSON.parse(t);
          } catch (e) {
            body = null;
          }
          return { status: r.status, body: body };
        })
      )
      .catch(() => ({ status: 0, body: null }))
      .then((res) => {
        if (abortTimer) clearTimeout(abortTimer);
        eventsClearPending = false;
        const ok = res.status === 200 && !!res.body && res.body.ok === true;
        if (ok) {
          eventsGen++;
          eventsSig = null;
          renderEvents([]);
          eventsClearToast(tl("Журнал событий очищен"), "success");
        } else {
          syncEventsClearButton();
          eventsClearToast(eventsClearErrorText(res.status, res.body), "error");
        }
        return ok;
      });
  }

  function refreshLive() {
    return fetchJson("api/devices")
      .then((data) => {
        if (!data || !data.ok) return;
        availableDevices = Array.isArray(data.available) ? data.available : [];
        ensureCards(deviceList(data));
        updateAddButton();
        const addModal = $("dev-add-modal");
        if (addModal && !addModal.hidden) renderAddList();
        const be = $("dev-history-backend");
        if (be) be.textContent = fmtBackend(data);
        const empty = $("dev-empty");
        if (empty && deviceList(data).length === 0) {
          empty.textContent =
            availableDevices.length > 0
              ? "Нет отображаемых виджетов. Нажмите «Добавить виджет», чтобы вернуть удалённые, или добавьте устройства на вкладке MQTT."
              : "Нет устройств ДТВ / СЭ-02м-3 / Carel / MR-02m / MTD262-MB в MQTT. Добавьте их на вкладке MQTT — виджеты появятся здесь автоматически.";
        }
      })
      .catch(() => {})
      .then(() => refreshEvents());
  }

  function historyQs(extra) {
    const q = new URLSearchParams(extra || {});
    if (activeDeviceId) q.set("device_id", activeDeviceId);
    return q.toString();
  }

  /* ── Chart helpers ───────────────────────────────────────────────────── */

  function fmtTime(d) {
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return hh + ":" + mm;
  }

  function fmtTimeSec(d) {
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return hh + ":" + mm + ":" + ss;
  }

  function fmtTimeLabel(ms, rangeKey) {
    const d = new Date(ms);
    if (rangeKey === "sec") return fmtTimeSec(d); // sub-minute zoom: HH:MM:SS
    const t = fmtTime(d);
    const dd = String(d.getDate()).padStart(2, "0");
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    if (rangeKey === "mtd" || rangeKey === "month" || rangeKey === "30d" || rangeKey === "7d") {
      if (rangeKey === "7d") return dd + "." + mo + " " + t;
      return dd + "." + mo;
    }
    if (rangeKey === "24h") return dd + "." + mo + " " + t;
    return t;
  }

  /** Время в тултипе графика — всегда с секундами. */
  function fmtTipTimeLabel(ms, rangeKey) {
    const d = new Date(ms);
    const t = fmtTimeSec(d);
    const dd = String(d.getDate()).padStart(2, "0");
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    if (
      rangeKey === "mtd" ||
      rangeKey === "month" ||
      rangeKey === "30d" ||
      rangeKey === "7d" ||
      rangeKey === "24h"
    ) {
      return dd + "." + mo + " " + t;
    }
    return t;
  }

  /**
   * Chart tip/legend decimals = Modbus/stand publish precision.
   * Metric first (disambiguate «%»: RH 1 vs light 0).
   */
  function tipDecimalsFor(unit, metric) {
    const m = String(metric || "").trim();
    // MR-02m AI channel («ai_N»): precision is unit-driven (WB /meta/precision),
    // one home in ai-sensors.js — a channel can be °C / V / mA with its own scale.
    if (/^ai_\d+$/.test(m)) return aiUnitPrecision(String(unit || ""));
    const byMetric = {
      room_temp: 1,
      humidity: 1,
      eco2_ppm: 0,
      tvoc_mg_m3: 2,
      pressure_mmhg: 1,
      light_pct: 0,
      presence: 0,
      mtd_illuminance: 1,
      mtd_target_distance: 2,
      mtd_presence: 0,
      voltage: 1,
      current: 3,
      power: 0,
      frequency_hz: 2,
      energy_kwh_import: 1,
    };
    if (Object.prototype.hasOwnProperty.call(byMetric, m)) return byMetric[m];
    const u = String(unit || "").trim();
    if (u === "V" || u === "В") return 1;
    if (u === "A" || u === "А") return 3;
    if (u === "W" || u === "Вт") return 0;
    if (u === "Hz" || u === "Гц") return 2;
    if (u === "kWh" || u === "кВт·ч") return 1;
    if (u === "°C") return 1;
    if (u === "ppm") return 0;
    if (u === "mg/m³") return 2;
    if (u === "mmHg" || u === "мм рт.ст.") return 1;
    if (u === "%") return 1; // RH default; light uses metric light_pct
    return null;
  }

  /**
   * Chart tip/legend value. Prefer metric/unit decimals (V×10 → 1 знак);
   * else trim up to 6 (bucket AVG float noise).
   */
  function fmtTipValue(v, digits) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    const d = Number.isInteger(digits) ? digits : 6;
    let s = n.toFixed(d);
    if (!Number.isInteger(digits)) {
      s = s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
    }
    if (s === "-0") s = "0";
    return s;
  }

  const KWH_LS_KEY = "sa02m_kwh_rub";
  const KWH_DEFAULT = 10.5;
  let historyReqId = 0;
  let summaryReqId = 0;
  let historyAbort = null;
  let summaryAbort = null;

  function getKwhRub() {
    try {
      const raw = localStorage.getItem(KWH_LS_KEY);
      if (raw != null && raw !== "") {
        const n = Number(String(raw).replace(",", "."));
        if (Number.isFinite(n) && n >= 0) return n;
      }
    } catch (_e) {
      /* ignore */
    }
    return KWH_DEFAULT;
  }

  function setKwhRub(v) {
    const n = Number(v);
    if (!Number.isFinite(n) || n < 0) return getKwhRub();
    try {
      localStorage.setItem(KWH_LS_KEY, String(n));
    } catch (_e) {
      /* ignore */
    }
    return n;
  }

  function setCeSideVisible(on) {
    const side = $("dev-ce-side");
    const panel = $("dev-modal") && $("dev-modal").querySelector(".dev-modal-panel");
    if (side) side.hidden = !on;
    if (panel) panel.classList.toggle("dev-modal-panel--ce", !!on);
    document.querySelectorAll(".dev-range-ce").forEach((btn) => {
      btn.hidden = !on;
    });
    if (!on && calendarMode) {
      // Leaving CE: a calendar mode is no longer selectable -> fall to 1h window.
      calendarMode = "";
      windowSec = PRESET_SEC["1h"];
      updateRangeButtons();
    }
  }

  function setExportVisible(on) {
    const exp = $("dev-export-btn");
    if (exp) exp.hidden = !on;
  }

  function parseDownloadName(cd, fallback) {
    if (!cd) return fallback;
    const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(cd);
    if (star) {
      try {
        return decodeURIComponent(star[1].trim());
      } catch (_e) {
        /* ignore */
      }
    }
    const m = /filename\s*=\s*\"?([^\";]+)\"?/i.exec(cd);
    return m ? m[1].trim() : fallback;
  }

  function exportHistory() {
    const params = { ...rangeReqParams(), format: "xlsx" };
    if (activeDeviceId) params.device_id = activeDeviceId;
    if (activeDevice === "mr") {
      // One MR table = all enabled AI channels (columns), regardless of mode.
      params.kind = "mr";
    } else if (activeDevice === "carel") {
      params.kind = "carel";
    } else if (modalMode === "overview") {
      params.group =
        activeDevice === "dtv" ? "climate" : activeDevice === "mtd" ? "mtd" : "energy";
    } else {
      params.metric = activeMetric;
    }
    const url = "api/devices/history/export?" + historyQs(params);
    const status = $("dev-chart-status");
    if (status) status.textContent = "Экспорт Excel";
    const fallback =
      (activeDevice === "dtv"
        ? "dtv"
        : activeDevice === "mr"
        ? "mr"
        : activeDevice === "carel"
        ? "carel"
        : activeDevice === "mtd"
        ? "mtd"
        : "ce") + "_export.xlsx";
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        const name = parseDownloadName(
          r.headers.get("Content-Disposition") || "",
          fallback
        );
        return r.blob().then((blob) => ({ blob, name }));
      })
      .then(({ blob, name }) => {
        const type =
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
        const file = blob.type
          ? blob
          : new Blob([blob], { type: type });
        const obj = URL.createObjectURL(file);
        const a = document.createElement("a");
        a.href = obj;
        a.download = name.endsWith(".xlsx") ? name : name + ".xlsx";
        a.rel = "noopener";
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
          a.remove();
          URL.revokeObjectURL(obj);
        }, 1500);
        if (status) status.textContent = "Скачан: " + a.download;
      })
      .catch(() => {
        // fallback: прямая навигация (Cookie сессии уйдёт с запросом)
        const a = document.createElement("a");
        a.href = url;
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        a.remove();
        if (status) status.textContent = "Экспорт запущен (xlsx)";
      });
  }

  function renderCeSummary(data) {
    const p = (data && data.power_w) || {};
    const e = (data && data.energy_kwh_import) || {};
    const set = (id, v, digits) => {
      const el = $(id);
      if (el) el.textContent = fmt(v, digits);
    };
    set("dev-ce-pa", p.a, 0);
    set("dev-ce-pb", p.b, 0);
    set("dev-ce-pc", p.c, 0);
    set("dev-ce-pt", p.total, 0);
    set("dev-ce-de", e.delta, 1);
    const costEl = $("dev-ce-cost");
    if (costEl) {
      const cost = data && data.cost_rub;
      costEl.textContent =
        cost === null || cost === undefined || Number.isNaN(Number(cost))
          ? "—"
          : Number(cost).toLocaleString("ru-RU", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            });
    }
    const inp = $("dev-ce-kwh-rub");
    if (inp && document.activeElement !== inp) {
      const tar =
        data && data.kwh_rub != null ? Number(data.kwh_rub) : getKwhRub();
      inp.value = String(tar);
    }
  }

  function loadCeSummary() {
    if (activeDevice !== "ce") {
      setCeSideVisible(false);
      return Promise.resolve();
    }
    setCeSideVisible(true);
    const tariff = getKwhRub();
    const rangeAtStart = rangeToken();
    const idAtStart = activeDeviceId;
    const req = ++summaryReqId;
    if (summaryAbort) {
      try {
        summaryAbort.abort();
      } catch (_e) {
        /* ignore */
      }
    }
    summaryAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
    const url =
      "api/devices/history/summary?" +
      historyQs({ ...rangeReqParams(), kwh_rub: String(tariff) });
    return fetchJson(url, summaryAbort ? { signal: summaryAbort.signal } : {})
      .then((data) => {
        if (req !== summaryReqId) return;
        if (rangeAtStart !== rangeToken() || idAtStart !== activeDeviceId) return;
        if (!data || !data.ok) {
          renderCeSummary({});
          return;
        }
        renderCeSummary(data);
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return;
        if (req !== summaryReqId) return;
        renderCeSummary({});
      });
  }

  function themeColors() {
    const css = getComputedStyle(document.documentElement);
    return {
      grid: css.getPropertyValue("--border").trim() || "#38383a",
      text: css.getPropertyValue("--text-sec").trim() || "#a5a5ac",
      bg: css.getPropertyValue("--bg-panel").trim() || "#2c2c2e",
    };
  }

  function flattenPoints(series) {
    const all = [];
    (series || []).forEach((s) => (s.points || []).forEach((p) => all.push(p)));
    return all;
  }

  /** Round up to a "nice" number for axis tops (1 / 2 / 2.5 / 5 × 10^n). */
  function niceCeil(x) {
    const v = Number(x);
    if (!Number.isFinite(v) || v <= 0) return 1;
    const exp = Math.floor(Math.log10(v));
    const pow = Math.pow(10, exp);
    const f = v / pow;
    let nf;
    if (f <= 1) nf = 1;
    else if (f <= 2) nf = 2;
    else if (f <= 2.5) nf = 2.5;
    else if (f <= 5) nf = 5;
    else nf = 10;
    return nf * pow;
  }

  /**
   * Shared absolute Y for «Общее» multi-metric charts: bottom = 0,
   * top = max(all series) + pad, nice-ceiled. Each series is plotted
   * with real values (T near 28, RH near 50, eCO₂ near 400, …).
   */
  function computeSharedAbsYDomain(dataMax) {
    let maxY = Number(dataMax);
    if (!Number.isFinite(maxY) || maxY < 0) maxY = 0;
    if (maxY === 0) return { minY: 0, maxY: 1 };
    maxY = niceCeil(maxY * 1.08);
    if (!(maxY > 0)) maxY = 1;
    return { minY: 0, maxY };
  }

  /**
   * Y domain for single-metric charts: ensure a usable span so tight
   * series (e.g. frequency 49.98–50.02) do not collapse to a flat line /
   * identical tick labels.
   */
  function computeYDomain(dataMin, dataMax) {
    let minY = Number(dataMin);
    let maxY = Number(dataMax);
    if (!Number.isFinite(minY) || !Number.isFinite(maxY)) {
      return { minY: 0, maxY: 1 };
    }
    if (minY > maxY) {
      const t = minY;
      minY = maxY;
      maxY = t;
    }
    let span = maxY - minY;
    const mid = span === 0 ? minY : (minY + maxY) / 2;
    const absMid = Math.abs(mid);
    // Floor span by magnitude: ~50 Hz → ≥0.1 Hz; humidity/temp similarly.
    let minSpan;
    if (absMid >= 100) minSpan = Math.max(1, absMid * 0.002);
    else if (absMid >= 10) minSpan = Math.max(0.1, absMid * 0.002);
    else if (absMid >= 1) minSpan = Math.max(0.02, absMid * 0.01);
    else minSpan = 0.002;
    if (!(span > 0) || span < minSpan) {
      const half = minSpan / 2;
      minY = mid - half;
      maxY = mid + half;
      span = minSpan;
    }
    const padY = span * 0.1;
    minY -= padY;
    maxY += padY;
    return { minY, maxY };
  }

  /** Enough decimal places so consecutive Y-tick labels are distinct. */
  function yTickDecimals(minY, maxY, tickCount) {
    const span = maxY - minY;
    if (!(span > 0)) return 2;
    const step = span / tickCount;
    let dec = 0;
    if (step < 10) dec = 1;
    if (step < 1) dec = 2;
    if (step < 0.1) dec = 3;
    if (step < 0.01) dec = 4;
    if (step < 0.001) dec = 5;
    for (let d = dec; d <= 6; d++) {
      const seen = new Set();
      let ok = true;
      for (let i = 0; i <= tickCount; i++) {
        const lab = (minY + (span * i) / tickCount).toFixed(d);
        if (seen.has(lab)) {
          ok = false;
          break;
        }
        seen.add(lab);
      }
      if (ok) return d;
    }
    return 6;
  }

  function fmtYTick(yv, decimals) {
    if (!Number.isFinite(yv)) return "";
    let s = yv.toFixed(decimals);
    if (decimals > 0) {
      s = s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
    }
    if (s === "-0") s = "0";
    return s;
  }

  /* ── Legend show/hide + СЭ bar charts ─────────────────────────────────
     A legend entry is a toggle button: a hidden series leaves the plot and the
     Y domain but keeps its colour (bound to its original index, `_ci`), and the
     last visible series can never be hidden. СЭ «Мощность» / «Энергия» draw bars
     over local-clock buckets. The bucket and ΔE math lives here only: bench
     history has no bucket parameter, so the line series are bucketed in the page. */

  /** Identity of a series inside one chart; «Общее» series carry their metric. */
  function seriesKey(ser, idx) {
    const s = ser || {};
    const id = s.field || s.label || String(idx);
    return s.metric ? s.metric + ":" + id : String(id);
  }

  /** Toggle one key in the hidden list; the last visible key is never hidden. */
  function toggleHiddenKey(hidden, key, allKeys) {
    const cur = Array.isArray(hidden) ? hidden.slice() : [];
    const at = cur.indexOf(key);
    if (at >= 0) {
      cur.splice(at, 1);
      return cur;
    }
    const visible = (allKeys || []).filter((k) => cur.indexOf(k) < 0);
    if (visible.length <= 1) return cur;
    cur.push(key);
    return cur;
  }

  /** Visible series as copies carrying their original colour index `_ci`; a
      hidden list that would hide every series shows them all (never a blank plot). */
  function visibleSeriesOf(series, hidden) {
    const list = series || [];
    const off = Array.isArray(hidden) ? hidden : [];
    const tagged = list.map((s, i) =>
      Object.assign({}, s, { _ci: s && s._ci != null ? s._ci : i })
    );
    const vis = tagged.filter((s, i) => off.indexOf(seriesKey(list[i], i)) < 0);
    return vis.length ? vis : tagged;
  }

  /** "avg" (СЭ power) | "delta" (СЭ energy counter → ΔE) | "" (a line chart). */
  function barModeFor(kind, mode, metric) {
    if (kind !== "ce" || mode !== "metric") return "";
    if (metric === "power") return "avg";
    if (metric === "energy_kwh_import") return "delta";
    return "";
  }

  /** Bar width (s): 1 ч→5 мин, 6 ч→15 мин, 24 ч→1 ч, 7 д / 30 д / calendar→1 сут.
      A zoom window between presets takes the largest preset tier not above it;
      under 1 ч → 1 мин. */
  function barBucketSec(windowSec, calendarMode) {
    if (calendarMode) return 86400;
    const w = Number(windowSec) || 0;
    if (w >= 7 * 86400) return 86400;
    if (w >= 86400) return 3600;
    if (w >= 6 * 3600) return 900;
    if (w >= 3600) return 300;
    return 60;
  }

  /** Bucket start (ms) on the LOCAL clock; a day bucket starts at local midnight. */
  function barBucketStart(tMs, bucketSec) {
    const t = Number(tMs);
    if (bucketSec >= 86400) {
      const d = new Date(t);
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    }
    const ms = bucketSec * 1000;
    const off = new Date(t).getTimezoneOffset() * 60000;
    return Math.floor((t - off) / ms) * ms + off;
  }

  function barBucketEnd(startMs, bucketSec) {
    if (bucketSec >= 86400) {
      const d = new Date(startMs);
      d.setDate(d.getDate() + 1);
      return d.getTime();
    }
    return startMs + bucketSec * 1000;
  }

  /** Average of each bucket's finite samples → [[start, avg, end], …] (power bars). */
  function bucketAvgPoints(points, bucketSec) {
    const acc = new Map();
    (points || []).forEach((p) => {
      if (!p || p[1] == null) return;
      const t = Number(p[0]);
      const v = Number(p[1]);
      if (!Number.isFinite(t) || !Number.isFinite(v)) return;
      const b = barBucketStart(t, bucketSec);
      const cur = acc.get(b);
      if (cur) {
        cur.s += v;
        cur.n += 1;
      } else {
        acc.set(b, { s: v, n: 1 });
      }
    });
    return Array.from(acc.keys())
      .sort((a, b) => a - b)
      .map((b) => [b, acc.get(b).s / acc.get(b).n, barBucketEnd(b, bucketSec)]);
  }

  /**
   * ΔE per bucket from the cumulative counter → [[start, ΔE, end], …]. A rise
   * between consecutive samples counts in the later sample's bucket. A drop is
   * held: a recovery to the pre-drop level was a glitch (only the rise above that
   * level counts); a second low sample is a counter reset and rebases. A rise whose
   * earlier sample is not in the same or the previous bucket spans a gap and is not
   * drawn (no fake spike after an outage); a bucket with no counted rise has no bar.
   */
  function bucketEnergyDeltaPoints(points, bucketSec) {
    const pts = (points || [])
      .filter((p) => p && p[1] != null)
      .map((p) => [Number(p[0]), Number(p[1])])
      .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
      .sort((a, b) => a[0] - b[0]);
    const acc = new Map();
    let ref = null;
    let low = 0;
    let dropped = false;
    let prevB = null;
    pts.forEach(([t, v]) => {
      const b = barBucketStart(t, bucketSec);
      if (ref === null) {
        ref = v;
        prevB = b;
        return;
      }
      let inc;
      if (v >= ref) {
        inc = v - ref;
        ref = v;
        dropped = false;
      } else if (!dropped) {
        dropped = true;
        low = v;
        inc = 0;
      } else {
        inc = Math.max(0, v - low);
        ref = v;
        dropped = false;
      }
      if (prevB === b || prevB === barBucketStart(b - 1, bucketSec)) {
        acc.set(b, (acc.get(b) || 0) + inc);
      }
      prevB = b;
    });
    return Array.from(acc.keys())
      .sort((a, b) => a - b)
      .map((b) => [b, acc.get(b), barBucketEnd(b, bucketSec)]);
  }

  /** Line series → bar series (points [start, value, end]) for mode avg | delta. */
  function toBarSeries(series, mode, bucketSec) {
    return (series || []).map((s) =>
      Object.assign({}, s, {
        label: mode === "delta" ? "ΔE" : s.label,
        points:
          mode === "delta"
            ? bucketEnergyDeltaPoints(s.points, bucketSec)
            : bucketAvgPoints(s.points, bucketSec),
        _bars: true,
      })
    );
  }

  /** Points the archive already aggregated to `bucketSec` (mean W or ΔE).
      One bar per returned point — do not average or difference them again. */
  function preparedBarSeries(series, mode, bucketSec) {
    return (series || []).map((s) =>
      Object.assign({}, s, {
        label: mode === "delta" ? "ΔE" : s.label,
        points: (s.points || [])
          .filter((p) => p && p[1] != null)
          .map((p) => [Number(p[0]), Number(p[1])])
          .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
          .map((p) => {
            const start = barBucketStart(p[0], bucketSec);
            return [start, p[1], barBucketEnd(start, bucketSec)];
          }),
        _bars: true,
      })
    );
  }

  /** X-axis label i of 0..nTicks: the edge labels always draw; a middle label
      only with a 6 px gap to the label before it and to the last one. */
  function xTickLabelFits(i, nTicks, left, labelW, prevRight, lastLeft) {
    if (i === 0 || i === nTicks) return true;
    return left >= prevRight + 6 && left + labelW <= lastLeft - 6;
  }

  /** Bar Y domain: bars rise from zero (a cropped baseline misstates the ratio
      between bars); a negative value (export power) gets its own floor. 4 × a
      nice step keeps the 4 grid lines on round values (0.05 / 25 / 100). */
  function barYDomain(dataMin, dataMax) {
    const lo = Math.min(0, Number(dataMin) || 0);
    const hi = Math.max(0, Number(dataMax) || 0);
    const minY = lo < 0 ? -4 * niceCeil((-lo * 1.08) / 4) : 0;
    const maxY = hi > 0 ? 4 * niceCeil((hi * 1.08) / 4) : minY < 0 ? 0 : 1;
    return { minY, maxY };
  }

  /** Side-by-side slots of n bars inside a bucket spanning [x0, x1] px. */
  function barGroupSlots(x0, x1, n) {
    if (!(n > 0)) return [];
    const span = Math.max(0, x1 - x0);
    const inner = span * 0.8;
    const left = x0 + (span - inner) / 2;
    const slot = inner / n;
    const gap = slot > 4 ? 1 : 0;
    const out = [];
    for (let i = 0; i < n; i++) {
      out.push({ x: left + i * slot + gap / 2, w: Math.max(1, slot - gap) });
    }
    return out;
  }

  /** Hover label of a bar bucket «HH:MM–HH:MM», dated once the axis carries dates
      (rangeLabelKey past «1h»); an end on local midnight reads 24:00. */
  function fmtBucketRange(t0, t1, rangeKey) {
    const a = new Date(t0);
    let end = fmtTime(new Date(t1));
    if (end === "00:00" && t1 > t0) end = "24:00";
    const span = fmtTime(a) + "–" + end;
    if (rangeKey === "1h" || rangeKey === "sec") return span;
    const dd = String(a.getDate()).padStart(2, "0");
    const mo = String(a.getMonth() + 1).padStart(2, "0");
    return dd + "." + mo + " " + span;
  }

  /** Status-line note naming what one bar is («расход за 5 мин»). */
  function barStepLabel(mode, bucketSec) {
    const span =
      bucketSec >= 86400
        ? "сутки"
        : bucketSec >= 3600
        ? "1 ч"
        : Math.round(bucketSec / 60) + " мин";
    return (mode === "delta" ? "расход за " : "среднее за ") + span;
  }

  const HIDDEN_LS_KEY = "sa02m_dev_hidden_series";
  /** "<kind>:<metric|overview>" → hidden series keys; loaded once from localStorage. */
  let hiddenSeriesMap = null;

  function hiddenStateKey() {
    return activeDevice + ":" + (modalMode === "overview" ? "overview" : activeMetric);
  }

  function hiddenSeriesFor(stateKey) {
    if (!hiddenSeriesMap) {
      hiddenSeriesMap = {};
      try {
        const raw = JSON.parse(localStorage.getItem(HIDDEN_LS_KEY) || "{}");
        if (raw && typeof raw === "object" && !Array.isArray(raw)) {
          Object.keys(raw).forEach((k) => {
            if (Array.isArray(raw[k])) {
              hiddenSeriesMap[k] = raw[k].filter((x) => typeof x === "string");
            }
          });
        }
      } catch (_e) {
        /* ignore — in-memory state only */
      }
    }
    return (hiddenSeriesMap[stateKey] || []).slice();
  }

  function setHiddenSeries(stateKey, list) {
    hiddenSeriesFor(stateKey);
    if (list.length) hiddenSeriesMap[stateKey] = list.slice();
    else delete hiddenSeriesMap[stateKey];
    try {
      localStorage.setItem(HIDDEN_LS_KEY, JSON.stringify(hiddenSeriesMap));
    } catch (_e) {
      /* ignore — in-memory state only */
    }
  }

  function onLegendToggle(key) {
    if (!key) return;
    const keys = (chartSeries || []).map((s, i) => seriesKey(s, i));
    const stateKey = hiddenStateKey();
    let cur = hiddenSeriesFor(stateKey);
    // A stored list covering every current series is shown as all-visible
    // (visibleSeriesOf); toggle from what the Operator actually sees.
    if (keys.length && keys.every((k) => cur.indexOf(k) >= 0)) {
      cur = cur.filter((k) => keys.indexOf(k) < 0);
    }
    setHiddenSeries(stateKey, toggleHiddenKey(cur, key, keys));
    drawChart();
    const status = $("dev-chart-status");
    if (status && chartMeta.normalize && chartSeries.length) {
      status.textContent = overviewStatusText();
    }
    const legend = $("dev-chart-legend");
    const again =
      legend &&
      Array.from(legend.querySelectorAll(".dev-legend-item")).find(
        (b) => b.getAttribute("data-series-key") === key
      );
    if (again) again.focus();
  }

  function ensureChartOverlay(wrap) {
    if (!wrap) return { tipA: null, tipB: null, delta: null };
    let tipA = wrap.querySelector(".dev-chart-tip[data-tip='a']");
    if (!tipA) {
      tipA = document.createElement("div");
      tipA.className = "dev-chart-tip";
      tipA.dataset.tip = "a";
      tipA.hidden = true;
      wrap.appendChild(tipA);
    }
    let tipB = wrap.querySelector(".dev-chart-tip[data-tip='b']");
    if (!tipB) {
      tipB = document.createElement("div");
      tipB.className = "dev-chart-tip";
      tipB.dataset.tip = "b";
      tipB.hidden = true;
      wrap.appendChild(tipB);
    }
    // Legacy single tip without data-tip — hide if present
    wrap.querySelectorAll(".dev-chart-tip:not([data-tip])").forEach((el) => {
      el.hidden = true;
    });
    let delta = wrap.querySelector(".dev-chart-delta");
    if (!delta) {
      delta = document.createElement("div");
      delta.className = "dev-chart-delta";
      delta.hidden = true;
      wrap.appendChild(delta);
    }
    return { tipA, tipB, delta };
  }

  function nearestPoint(pts, xMs) {
    if (!pts || !pts.length) return null;
    let best = pts[0];
    let bestD = Math.abs(pts[0][0] - xMs);
    for (let i = 1; i < pts.length; i++) {
      const d = Math.abs(pts[i][0] - xMs);
      if (d < bestD) {
        bestD = d;
        best = pts[i];
      }
    }
    return best;
  }

  function hitSeriesAtX(drawSeries, xMs, unitNote, normalize) {
    const rows = [];
    (drawSeries || []).forEach((ser, i) => {
      const idx = ser._ci != null ? ser._ci : i;
      const plotPts = ser.points || [];
      const rawPts = ser._rawPoints || plotPts;
      if (!plotPts.length) return;
      const unit = ser.unit ? ser.unit : unitNote || "";
      if (ser._bars) {
        // A bar answers for its whole bucket: the hit is the bucket holding xMs.
        const bar = plotPts.find((pt) => xMs >= pt[0] && xMs < pt[2]);
        if (!bar) return;
        rows.push({
          idx,
          label: ser.label || ser.field || ser.metric || "",
          t: bar[0],
          t1: bar[2],
          y: bar[1],
          plotY: bar[1],
          unit,
          metric: ser.metric || "",
        });
        return;
      }
      const plotHit = nearestPoint(plotPts, xMs);
      if (!plotHit) return;
      const rawHit = nearestPoint(rawPts, plotHit[0]) || plotHit;
      rows.push({
        idx,
        label: ser.label || ser.field || ser.metric || "",
        t: rawHit[0],
        y: rawHit[1],
        plotY: plotHit[1],
        unit,
        metric: ser.metric || "",
      });
    });
    return rows;
  }

  function fmtDurationMs(ms) {
    const abs = Math.abs(Number(ms) || 0);
    const totalSec = Math.round(abs / 1000);
    if (totalSec < 60) return totalSec + " с";
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    if (m < 60) return s ? m + " мин " + s + " с" : m + " мин";
    const h = Math.floor(m / 60);
    const rm = m % 60;
    if (h < 48) return rm ? h + " ч " + rm + " мин" : h + " ч";
    const d = Math.floor(h / 24);
    const rh = h % 24;
    return rh ? d + " д " + rh + " ч" : d + " д";
  }

  function fmtSignedTipValue(v, digits) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    const body = fmtTipValue(Math.abs(n), digits);
    if (n > 0) return "+" + body;
    if (n < 0) return "−" + body;
    return body;
  }

  function xFromPointer(st, clientX, clientY, rect) {
    const mx = ((clientX - rect.left) / rect.width) * st.w;
    const my = ((clientY - rect.top) / rect.height) * st.h;
    if (
      mx < st.pad.l ||
      mx > st.w - st.pad.r ||
      my < st.pad.t ||
      my > st.pad.t + st.plotH
    ) {
      return null;
    }
    return {
      xMs: st.minX + ((mx - st.pad.l) / st.plotW) * (st.maxX - st.minX),
      css: { x: clientX - rect.left, y: clientY - rect.top },
    };
  }

  function snapMarkX(st, xMs) {
    const rows = hitSeriesAtX(
      visibleSeriesOf(st.srcSeries || [], st.opts && st.opts.hidden),
      xMs,
      (st.opts && st.opts.unitNote) || "",
      !!(st.opts && st.opts.normalize)
    );
    if (rows.length && Number.isFinite(rows[0].t)) return rows[0].t;
    return xMs;
  }

  function bindChartPointer(canvas) {
    if (!canvas || canvas.__chartBound) return;
    canvas.__chartBound = true;
    canvas.style.cursor = "crosshair";
    const onMove = (e) => {
      const st = canvas.__chart;
      if (!st || !st.ok) return;
      const marks = Array.isArray(st.marks) ? st.marks : [];
      if (marks.length >= 2) return;
      const rect = canvas.getBoundingClientRect();
      const hit = xFromPointer(st, e.clientX, e.clientY, rect);
      if (!hit) {
        if (st.hoverX != null) {
          st.hoverX = null;
          st.pointerCss = null;
          drawOntoCanvas(canvas, st.srcSeries, st.opts);
        }
        return;
      }
      st.hoverX = hit.xMs;
      st.pointerCss = hit.css;
      drawOntoCanvas(canvas, st.srcSeries, st.opts);
    };
    const onLeave = () => {
      const st = canvas.__chart;
      if (!st) return;
      if (st.hoverX != null) {
        st.hoverX = null;
        st.pointerCss = null;
        drawOntoCanvas(canvas, st.srcSeries, st.opts);
      }
    };
    const onClick = (e) => {
      const st = canvas.__chart;
      if (!st || !st.ok) return;
      const rect = canvas.getBoundingClientRect();
      const hit = xFromPointer(st, e.clientX, e.clientY, rect);
      if (!hit) return;
      const marks = Array.isArray(st.marks) ? st.marks.slice() : [];
      if (marks.length >= 2) {
        st.marks = [];
        st.hoverX = null;
        st.pointerCss = null;
      } else {
        marks.push(snapMarkX(st, hit.xMs));
        st.marks = marks;
        st.hoverX = marks.length >= 2 ? null : hit.xMs;
        st.pointerCss = hit.css;
      }
      drawOntoCanvas(canvas, st.srcSeries, st.opts);
    };
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("click", onClick);
  }

  /**
   * Draw multi-series line chart onto canvas.
   * opts: { range, normalize, padBottom, legendEl, emptyText, unitNote }
   * normalize=true («Общее»): shared absolute Y 0…max_all, real values, numeric ticks.
   * Click measure: 1st/2nd click place vertical marks; area + Δt/Δvalue between;
   * 3rd click clears. Hover preview while fewer than 2 marks.
   */
  /** Фиксированная высота графика — не брать clientHeight wrap (петля роста с tip/flex). */
  const CHART_CSS_H = 400;

  function tipHtmlFromRows(rows, rangeKey, metric) {
    if (!rows || !rows.length) return "";
    const t0 = rows[0].t;
    const when =
      rows[0].t1 != null
        ? fmtBucketRange(t0, rows[0].t1, rangeKey)
        : fmtTipTimeLabel(t0, rangeKey);
    const lines = [`<div class="dev-chart-tip-time">${escapeAttr(when)}</div>`];
    rows.forEach((row) => {
      const color = COLORS[row.idx % COLORS.length];
      const unit = row.unit ? ` ${row.unit}` : "";
      const dec = tipDecimalsFor(row.unit, row.metric || metric);
      lines.push(
        `<div class="dev-chart-tip-row"><i style="background:${color}"></i>` +
          `<span>${escapeAttr(row.label)}</span>` +
          `<b>${escapeAttr(fmtTipValue(row.y, dec) + unit)}</b></div>`
      );
    });
    return lines.join("");
  }

  function placeTipNearX(tip, html, xPix, w, h, pad, preferLeft) {
    if (!tip || !html) {
      if (tip) tip.hidden = true;
      return;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const tipW = tip.offsetWidth || 160;
    const tipH = tip.offsetHeight || 48;
    let left = preferLeft ? xPix - tipW - 8 : xPix + 8;
    let top = pad.t + 8;
    if (left + tipW > w - 4) left = xPix - tipW - 8;
    if (left < 4) left = 4;
    if (top + tipH > h - 4) top = h - tipH - 4;
    if (top < 4) top = 4;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function placeTipAtPointer(tip, html, xPix, pointerCss, w, h, pad) {
    if (!tip || !html) {
      if (tip) tip.hidden = true;
      return;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const cssX = pointerCss ? pointerCss.x : xPix;
    const cssY = pointerCss ? pointerCss.y : pad.t + 8;
    const tipW = tip.offsetWidth || 160;
    const tipH = tip.offsetHeight || 48;
    let left = cssX + 12;
    let top = cssY - 12;
    if (left + tipW > w - 4) left = cssX - tipW - 12;
    if (left < 4) left = 4;
    if (top + tipH > h - 4) top = h - tipH - 4;
    if (top < 4) top = 4;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function drawCrosshairLine(ctx, xPix, pad, plotH, solid, hoverStroke) {
    ctx.save();
    ctx.strokeStyle = solid
      ? "rgba(10,132,255,0.95)"
      : hoverStroke || "rgba(160,160,170,0.75)";
    ctx.lineWidth = solid ? 1.5 : 1;
    if (!solid) ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(xPix, pad.t);
    ctx.lineTo(xPix, pad.t + plotH);
    ctx.stroke();
    ctx.restore();
  }

  function drawHitMarkers(ctx, tipRows, xScale, yScale) {
    (tipRows || []).forEach((row) => {
      const color = COLORS[row.idx % COLORS.length];
      const px = xScale(row.t);
      const py = yScale(row.plotY);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }

  /* Measure-card placement (2 marks): tipA, tipB and the Δ card can each be tall
     (up to 5 sensor rows), so positions come from MEASURED rects, not fixed px.
     Each card avoids the ones already placed and stays inside the chart wrap. */
  function rectsOverlapArea(a, b) {
    const ox = Math.min(a.left + a.w, b.left + b.w) - Math.max(a.left, b.left);
    const oy = Math.min(a.top + a.h, b.top + b.h) - Math.max(a.top, b.top);
    return ox > 0 && oy > 0 ? ox * oy : 0;
  }

  function clampCardRect(r, b) {
    let left = r.left;
    let top = r.top;
    if (left + r.w > b.right) left = b.right - r.w;
    if (left < b.left) left = b.left;
    if (top + r.h > b.bottom) top = b.bottom - r.h;
    if (top < b.top) top = b.top;
    return { left, top, w: r.w, h: r.h };
  }

  // First candidate (after clamping to bounds) that clears every obstacle; else
  // the clamped candidate with the least total overlap. Non-null for non-empty cands.
  function fitCardRect(cands, obstacles, bounds) {
    let best = null;
    let bestOverlap = Infinity;
    for (let i = 0; i < cands.length; i++) {
      const r = clampCardRect(cands[i], bounds);
      let overlap = 0;
      for (let k = 0; k < obstacles.length; k++) {
        overlap += rectsOverlapArea(r, obstacles[k]);
      }
      if (overlap === 0) return r;
      if (overlap < bestOverlap) {
        bestOverlap = overlap;
        best = r;
      }
    }
    return best;
  }

  // Point card anchored to its mark: outer side of the mark first (spreads the
  // two cards apart), then inner; top row first, then bottom.
  function pointCardCands(xm, isLeft, size, pad, plotH) {
    const gap = 8;
    const outerLeft = isLeft ? xm - gap - size.w : xm + gap;
    const innerLeft = isLeft ? xm + gap : xm - gap - size.w;
    const topY = pad.t + 8;
    const botY = pad.t + plotH - 8 - size.h;
    return [
      { left: outerLeft, top: topY, w: size.w, h: size.h },
      { left: outerLeft, top: botY, w: size.w, h: size.h },
      { left: innerLeft, top: topY, w: size.w, h: size.h },
      { left: innerLeft, top: botY, w: size.w, h: size.h },
    ];
  }

  // Δ card centred between the marks: middle band first (clear of top-anchored
  // point cards), then top / bottom, then flush to a plot edge.
  function deltaCardCands(xMid, size, w, pad, plotH) {
    const centerLeft = xMid - size.w / 2;
    const topY = pad.t + 8;
    const midY = pad.t + plotH / 2 - size.h / 2;
    const botY = pad.t + plotH - 8 - size.h;
    return [
      { left: centerLeft, top: midY, w: size.w, h: size.h },
      { left: centerLeft, top: botY, w: size.w, h: size.h },
      { left: centerLeft, top: topY, w: size.w, h: size.h },
      { left: pad.l + 4, top: midY, w: size.w, h: size.h },
      { left: w - pad.r - 4 - size.w, top: midY, w: size.w, h: size.h },
    ];
  }

  // Content already set on each el; measure the rendered rects and place point-A,
  // point-B, Δ in turn so each later card avoids the earlier ones and the wrap edge.
  function placeMeasureCards(cards, xA, xB, w, h, pad, plotH) {
    const bounds = { left: 4, top: pad.t + 4, right: w - 4, bottom: pad.t + plotH - 4 };
    const midX = (Math.min(xA, xB) + Math.max(xA, xB)) / 2;
    const placed = [];
    cards.forEach((c) => {
      if (!c.el) return;
      if (!c.html) {
        c.el.hidden = true;
        return;
      }
      c.el.innerHTML = c.html;
      c.el.hidden = false;
      const size = {
        w: c.el.offsetWidth || (c.kind === "delta" ? 140 : 160),
        h: c.el.offsetHeight || 48,
      };
      const cands =
        c.kind === "delta"
          ? deltaCardCands(midX, size, w, pad, plotH)
          : pointCardCands(c.x, c.isLeft, size, pad, plotH);
      const r = fitCardRect(cands, placed, bounds);
      c.el.style.left = r.left + "px";
      c.el.style.top = r.top + "px";
      placed.push(r);
    });
  }

  function deltaHtmlFromRows(rows0, rows1, dtMs, metric) {
    const byIdx = {};
    (rows0 || []).forEach((r) => {
      byIdx[r.idx] = { a: r, b: null };
    });
    (rows1 || []).forEach((r) => {
      if (!byIdx[r.idx]) byIdx[r.idx] = { a: null, b: r };
      else byIdx[r.idx].b = r;
    });
    const dLines = [
      `<div class="dev-chart-delta-time">Δt ${escapeAttr(fmtDurationMs(dtMs))}</div>`,
    ];
    Object.keys(byIdx)
      .map((k) => Number(k))
      .sort((a, b) => a - b)
      .forEach((idx) => {
        const pair = byIdx[idx];
        const a = pair.a;
        const b = pair.b;
        if (!a || !b) return;
        const color = COLORS[idx % COLORS.length];
        const unit = a.unit || b.unit || "";
        const unitS = unit ? ` ${unit}` : "";
        const dy = Number(b.y) - Number(a.y);
        const dec = tipDecimalsFor(unit, a.metric || b.metric || metric);
        dLines.push(
          `<div class="dev-chart-delta-row"><i style="background:${color}"></i>` +
            `<b>Δ ${escapeAttr(a.label || "")} ${escapeAttr(
              fmtSignedTipValue(dy, dec) + unitS
            )}</b></div>`
        );
      });
    return dLines.join("");
  }

  function drawOntoCanvas(canvas, series, opts) {
    opts = opts || {};
    if (!canvas) return;
    const wrap = canvas.parentElement;
    const ov = ensureChartOverlay(wrap);
    const tipA = ov.tipA;
    const tipB = ov.tipB;
    const deltaEl = ov.delta;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(280, Math.floor((wrap && wrap.clientWidth) || 600));
    const h = CHART_CSS_H;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const th = themeColors();
    const padB = opts.padBottom != null ? opts.padBottom : 36;
    const pad = { l: 48, r: 14, t: 14, b: padB };
    const rangeKey = opts.range || "1h";
    const prev = canvas.__chart || {};
    const hoverX = prev.hoverX;
    const marks = Array.isArray(prev.marks) ? prev.marks.slice(0, 2) : [];
    const pointerCss = prev.pointerCss || null;

    ctx.fillStyle = th.bg;
    ctx.fillRect(0, 0, w, h);

    // Hidden legend entries leave the plot, the Y domain and the hover rows.
    const drawSeries = visibleSeriesOf(series || [], opts.hidden);
    const bars = !!opts.bars;

    const all = flattenPoints(drawSeries);
    if (!all.length) {
      ctx.fillStyle = th.text;
      ctx.font = "13px ui-sans-serif, system-ui, sans-serif";
      ctx.fillText(opts.emptyText || "Нет данных", pad.l, h / 2);
      // Keep the legend clickable when only the SHOWN series are empty — the
      // Operator must be able to bring a hidden series with data back.
      if (opts.legendEl) {
        renderLegendInto(
          opts.legendEl,
          flattenPoints(series || []).length ? series : [],
          opts.unitNote || "",
          opts.normalize,
          opts.metric || "",
          opts.hidden
        );
      }
      if (tipA) tipA.hidden = true;
      if (tipB) tipB.hidden = true;
      if (deltaEl) deltaEl.hidden = true;
      canvas.__chart = {
        ok: false,
        srcSeries: series || [],
        opts,
        hoverX: null,
        marks: [],
        pointerCss: null,
      };
      bindChartPointer(canvas);
      return;
    }

    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    all.forEach(([x, y, xEnd]) => {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (xEnd != null && xEnd > maxX) maxX = xEnd; // a bar spans to its bucket end
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    });
    // Для 7д/30д ось X = выбранный интервал (данные не «съезжают» и не теряются при смене range)
    if (
      opts.windowMin != null &&
      opts.windowMax != null &&
      Number.isFinite(Number(opts.windowMin)) &&
      Number.isFinite(Number(opts.windowMax)) &&
      Number(opts.windowMax) > Number(opts.windowMin)
    ) {
      minX = Number(opts.windowMin);
      maxX = Number(opts.windowMax);
    }
    if (minX === maxX) maxX = minX + 1;
    let yDecimals = 1;
    if (opts.normalize) {
      // Shared absolute scale: 0 … niceCeil(max_all×1.08). Real values, no per-series stretch.
      const dataMax = maxY;
      const dom = computeSharedAbsYDomain(dataMax);
      minY = dom.minY;
      maxY = dom.maxY;
      yDecimals = yTickDecimals(minY, maxY, 4);
      ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
      const topLab = fmtYTick(maxY, yDecimals);
      pad.l = Math.min(72, Math.max(48, Math.ceil(ctx.measureText(topLab).width) + 12));
    } else if (bars) {
      const dom = barYDomain(minY, maxY);
      minY = dom.minY;
      maxY = dom.maxY;
      yDecimals = yTickDecimals(minY, maxY, 4);
      ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
      const labW = Math.max(
        ctx.measureText(fmtYTick(maxY, yDecimals)).width,
        ctx.measureText(fmtYTick(minY, yDecimals)).width
      );
      pad.l = Math.min(72, Math.max(48, Math.ceil(labW) + 12));
    } else {
      const dom = computeYDomain(minY, maxY);
      minY = dom.minY;
      maxY = dom.maxY;
      yDecimals = yTickDecimals(minY, maxY, 4);
    }

    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;
    const xScale = (x) => pad.l + ((x - minX) / (maxX - minX)) * plotW;
    const yScale = (y) => pad.t + (1 - (y - minY) / (maxY - minY)) * plotH;

    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillStyle = th.text;
    ctx.textAlign = "left";

    // Numeric Y ticks only (ascending). Series identity stays in the legend.
    {
      const yTicks = 4;
      for (let i = 0; i <= yTicks; i++) {
        const yv = minY + ((maxY - minY) * i) / yTicks;
        const y = yScale(yv);
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
        ctx.fillText(fmtYTick(yv, yDecimals), 4, y + 3);
      }
    }

    // X ticks: 4–8 evenly spaced, bottom labels + light vertical grid
    const nTicks = Math.max(4, Math.min(8, Math.floor(plotW / 72)));
    ctx.textAlign = "center";
    // First and last labels always; a middle one only where it clears the label
    // drawn before it and the last one (dated labels collided at 500 px).
    const lastLeft = w - pad.r - ctx.measureText(fmtTimeLabel(maxX, rangeKey)).width;
    let prevRight = -Infinity;
    for (let i = 0; i <= nTicks; i++) {
      const xv = minX + ((maxX - minX) * i) / nTicks;
      const x = xScale(xv);
      ctx.strokeStyle = th.grid;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, pad.t + plotH);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = th.text;
      const label = fmtTimeLabel(xv, rangeKey);
      const labelW = ctx.measureText(label).width;
      let tx = x;
      if (i === 0) {
        ctx.textAlign = "left";
        tx = pad.l;
      } else if (i === nTicks) {
        ctx.textAlign = "right";
        tx = w - pad.r;
      } else {
        ctx.textAlign = "center";
      }
      const left = i === 0 ? tx : i === nTicks ? tx - labelW : tx - labelW / 2;
      if (!xTickLabelFits(i, nTicks, left, labelW, prevRight, lastLeft)) continue;
      ctx.fillText(label, tx, h - 10);
      prevRight = left + labelW;
    }
    ctx.textAlign = "left";

    if (bars) {
      // Visible series side by side inside each bucket (Pa/Pb/Pc/PΣ grouped);
      // an exact zero keeps a 1 px stub so «0» reads apart from «no data».
      const n = drawSeries.length;
      const y0 = yScale(Math.max(minY, Math.min(maxY, 0)));
      ctx.save();
      ctx.beginPath();
      ctx.rect(pad.l, pad.t, plotW, plotH);
      ctx.clip();
      drawSeries.forEach((ser, si) => {
        ctx.fillStyle = COLORS[ser._ci % COLORS.length];
        (ser.points || []).forEach((pt) => {
          const slot = barGroupSlots(xScale(pt[0]), xScale(pt[2]), n)[si];
          const y = yScale(pt[1]);
          const hBar = Math.abs(y0 - y);
          if (hBar >= 1) ctx.fillRect(slot.x, Math.min(y, y0), slot.w, hBar);
          else ctx.fillRect(slot.x, y0 - 1, slot.w, 1);
        });
      });
      ctx.restore();
    }

    drawSeries.forEach((ser) => {
      if (bars) return;
      const idx = ser._ci;
      const pts = ser.points || [];
      if (pts.length < 2) {
        if (pts.length === 1) {
          ctx.fillStyle = COLORS[idx % COLORS.length];
          ctx.beginPath();
          ctx.arc(xScale(pts[0][0]), yScale(pts[0][1]), 3, 0, Math.PI * 2);
          ctx.fill();
        }
        return;
      }
      ctx.strokeStyle = COLORS[idx % COLORS.length];
      ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach(([x, y], i) => {
        const px = xScale(x);
        const py = yScale(y);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    });

    // Measure marks (0→1→2) + hover preview while < 2 marks
    const unitNote = opts.unitNote || "";
    const normalize = !!opts.normalize;
    const metricId = opts.metric || "";
    const markRows = marks.map((mx) =>
      hitSeriesAtX(drawSeries, mx, unitNote, normalize)
    );

    if (marks.length === 2) {
      const xA = xScale(marks[0]);
      const xB = xScale(marks[1]);
      const x0 = Math.min(xA, xB);
      const x1 = Math.max(xA, xB);
      ctx.save();
      ctx.fillStyle = "rgba(10, 132, 255, 0.14)";
      ctx.fillRect(x0, pad.t, Math.max(1, x1 - x0), plotH);
      ctx.restore();
    }

    marks.forEach((mx, i) => {
      if (!Number.isFinite(mx)) return;
      drawCrosshairLine(ctx, xScale(mx), pad, plotH, true);
      if (!bars) drawHitMarkers(ctx, markRows[i], xScale, yScale);
    });

    let hoverRows = [];
    if (marks.length < 2 && hoverX != null && Number.isFinite(hoverX)) {
      hoverRows = hitSeriesAtX(drawSeries, hoverX, unitNote, normalize);
      if (bars) {
        // Hover shades the whole bucket the tip answers for.
        if (hoverRows.length) {
          const bx0 = Math.max(pad.l, xScale(hoverRows[0].t));
          const bx1 = Math.min(w - pad.r, xScale(hoverRows[0].t1));
          ctx.save();
          ctx.globalAlpha = 0.16;
          ctx.fillStyle = th.text;
          ctx.fillRect(bx0, pad.t, Math.max(1, bx1 - bx0), plotH);
          ctx.restore();
        }
      } else {
        drawCrosshairLine(ctx, xScale(hoverX), pad, plotH, false, th.text);
        drawHitMarkers(ctx, hoverRows, xScale, yScale);
      }
    }

    if (tipA) tipA.hidden = true;
    if (tipB) tipB.hidden = true;
    if (deltaEl) deltaEl.hidden = true;

    if (marks.length === 0) {
      if (hoverRows.length) {
        placeTipAtPointer(
          tipA,
          tipHtmlFromRows(hoverRows, rangeKey, metricId),
          xScale(hoverX),
          pointerCss,
          w,
          h,
          pad
        );
      }
    } else if (marks.length === 1) {
      const rows0 = markRows[0] || [];
      if (rows0.length) {
        placeTipNearX(
          tipA,
          tipHtmlFromRows(rows0, rangeKey, metricId),
          xScale(marks[0]),
          w,
          h,
          pad,
          false
        );
      }
      if (hoverRows.length) {
        placeTipAtPointer(
          tipB,
          tipHtmlFromRows(hoverRows, rangeKey, metricId),
          xScale(hoverX),
          pointerCss,
          w,
          h,
          pad
        );
      }
    } else if (marks.length === 2) {
      const rows0 = markRows[0] || [];
      const rows1 = markRows[1] || [];
      const xA = xScale(marks[0]);
      const xB = xScale(marks[1]);
      const htmlA = rows0.length ? tipHtmlFromRows(rows0, rangeKey, metricId) : "";
      const htmlB = rows1.length ? tipHtmlFromRows(rows1, rangeKey, metricId) : "";
      const htmlD =
        rows0.length || rows1.length
          ? deltaHtmlFromRows(
              rows0,
              rows1,
              Math.abs((marks[1] || 0) - (marks[0] || 0)),
              metricId
            )
          : "";
      // Measured, collision-free placement: the two point cards + the Δ card
      // never overlap each other or clip the wrap, for either mark ordering.
      placeMeasureCards(
        [
          { el: tipA, html: htmlA, kind: "point", x: xA, isLeft: xA <= xB },
          { el: tipB, html: htmlB, kind: "point", x: xB, isLeft: xB < xA },
          { el: deltaEl, html: htmlD, kind: "delta" },
        ],
        xA,
        xB,
        w,
        h,
        pad,
        plotH
      );
    }

    if (opts.legendEl) {
      // Every series, hidden ones included — the legend is where they come back.
      renderLegendInto(
        opts.legendEl,
        series || [],
        opts.unitNote || "",
        opts.normalize,
        opts.metric || "",
        opts.hidden
      );
    }

    canvas.__chart = {
      ok: true,
      w,
      h,
      pad,
      plotW,
      plotH,
      minX,
      maxX,
      minY,
      maxY,
      srcSeries: series || [],
      opts,
      hoverX: hoverX != null ? hoverX : null,
      marks,
      pointerCss,
    };
    bindChartPointer(canvas);
  }

  /** Legend = one toggle button per series (aria-pressed = shown). The markup is
      rewritten only when it changes: a hover redraw must not destroy the button
      that holds keyboard focus. */
  function renderLegendInto(el, series, unitNote, normalized, metric, hidden) {
    if (!el) return;
    const list = series || [];
    const off = Array.isArray(hidden) ? hidden : [];
    const keys = list.map((s, i) => seriesKey(s, i));
    let shownN = keys.filter((k) => off.indexOf(k) < 0).length;
    const allOff = shownN === 0; // a stale list hiding everything shows everything
    if (allOff) shownN = keys.length;
    const html = list
      .map((s, i) => {
        const raw = s._rawPoints || s.points || [];
        const last = raw[raw.length - 1];
        const u = s.unit || unitNote || "";
        const unit = u ? ` ${u}` : "";
        const dec = tipDecimalsFor(u, metric || s.metric || "");
        const val = last
          ? (Number.isInteger(dec) ? fmt(last[1], dec) : fmtTipValue(last[1])) + unit
          : "—";
        const name = s.label || s.field || s.metric || "";
        const on = allOff || off.indexOf(keys[i]) < 0;
        const lone = on && shownN <= 1;
        const color = COLORS[i % COLORS.length];
        const hint = lone ? "" : on ? "Скрыть с графика" : "Показать на графике";
        const swatch = (on ? "background:" + color + ";" : "") + "border-color:" + color;
        return (
          '<button type="button" class="dev-legend-item" data-series-key="' +
          escapeAttr(keys[i]) +
          '" aria-pressed="' +
          (on ? "true" : "false") +
          '"' +
          (lone ? ' aria-disabled="true"' : "") +
          (hint ? ' title="' + hint + '"' : "") +
          '><i style="' +
          swatch +
          '"></i>' +
          escapeAttr(tl(name)) +
          " · " +
          escapeAttr(val) +
          "</button>"
        );
      })
      .join("");
    if (el.__legendHtml === html) return;
    el.__legendHtml = html;
    el.innerHTML = html;
  }

  // A label rendered INSIDE a composite string (legend «name · value», the
  // status line) is never a whole text node, so the DICT observer cannot
  // translate it — translate it here (audit 2026-09-08, R3 residual).
  function tl(x) {
    const k = x == null ? "" : String(x);
    return k && window.sa02mI18n ? window.sa02mI18n.t(k) : k;
  }

  function renderLegend(series, hidden) {
    renderLegendInto(
      $("dev-chart-legend"),
      series,
      chartMeta.unit || "",
      false,
      activeMetric,
      hidden
    );
  }

  function flattenMetricSeries(metricsPayload) {
    /* Convert history_batch metrics[] → flat series with unit/label for overview */
    const out = [];
    (metricsPayload || []).forEach((m) => {
      const unit = m.unit || "";
      const series = m.series || [];
      if (!series.length) return;
      series.forEach((s) => {
        out.push({
          field: s.field,
          label: s.label || s.field || m.label || m.metric,
          unit: unit,
          metric: m.metric,
          points: s.points || [],
        });
      });
    });
    return out;
  }

  /* -- Chart span state (continuous) --------------------------------------
     The presets are templates: a click sets `windowSec` to that duration. The
     wheel then scales `windowSec` by a smooth factor between 1 min and 30 days.
     Requests carry an arbitrary `window_s` (or `range` for a calendar mode);
     the chart x-axis label granularity is derived locally, not from the server
     range echo (now a `w:<seconds>` token for a windowed view). */

  /** Request params for the active span - calendar mode vs continuous window. */
  function rangeReqParams() {
    return calendarMode ? { range: calendarMode } : { window_s: String(windowSec) };
  }

  /** Stable token for in-flight request guards + button highlight. */
  function rangeToken() {
    return calendarMode || "w:" + windowSec;
  }

  /** Preset-like key that drives the x-axis / tip label GRANULARITY only. */
  function rangeLabelKey() {
    if (calendarMode) return calendarMode;
    if (windowSec <= 600) return "sec"; // ≤10 min: HH:MM:SS (minute ticks repeat)
    if (windowSec <= 12 * 3600) return "1h"; // time-only labels HH:MM
    if (windowSec <= 7 * 86400) return "24h"; // dd.mm HH:MM
    return "30d"; // dd.mm
  }

  /** Human span label for the status line («90 мин» / «6 ч» / «3 д»). */
  function rangeHumanLabel() {
    if (calendarMode === "mtd") return "с начала месяца";
    if (calendarMode === "month") return "за месяц";
    const s = windowSec;
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " мин";
    if (s < 86400) {
      const h = s / 3600;
      return (Number.isInteger(h) ? h : h.toFixed(1)) + " ч";
    }
    const d = s / 86400;
    return (Number.isInteger(d) ? d : d.toFixed(1)) + " д";
  }

  function clampWindowSec(sec) {
    const n = Math.round(Number(sec));
    if (!Number.isFinite(n)) return WINDOW_MIN_S;
    return Math.max(WINDOW_MIN_S, Math.min(WINDOW_MAX_S, n));
  }

  /* -- Chart wheel-zoom ---------------------------------------------------
     One wheel notch scales `windowSec`: up (deltaY<0) shrinks toward 1 min,
     down (deltaY>0) grows toward 30 days, by a smooth factor. The +/-1 s floor
     keeps each notch changing the integer window at small spans. A wheel over a
     CE calendar mode leaves it and starts continuous zoom at the 30 d end. The
     refetch is debounced (one /api/devices/history load per burst); presets stay
     clickable. preventDefault stops the page scrolling while zooming. */
  function stepWindowSec(sec, deltaY) {
    const cur = clampWindowSec(sec);
    const dir = Number(deltaY) > 0 ? 1 : -1; // down = out (grow), up = in (shrink)
    const factor = dir > 0 ? ZOOM_OUT_FACTOR : ZOOM_IN_FACTOR;
    let next = Math.round(cur * factor);
    if (dir < 0 && next >= cur) next = cur - 1; // force a change zooming in
    if (dir > 0 && next <= cur) next = cur + 1; // force a change zooming out
    return clampWindowSec(next);
  }

  function debounceTrailing(fn, ms) {
    let t = null;
    return function () {
      if (t) clearTimeout(t);
      t = setTimeout(function () {
        t = null;
        fn();
      }, ms);
    };
  }

  const scheduleZoomRefetch = debounceTrailing(function () {
    loadHistory();
  }, 200);

  function updateRangeButtons() {
    document.querySelectorAll("#dev-modal .dev-range-btn").forEach((b) => {
      const on = calendarMode
        ? b.dataset.range === calendarMode
        : PRESET_SEC[b.dataset.range] === windowSec;
      b.classList.toggle("active", !!on);
    });
  }

  /** A preset button click: mtd/month are calendar modes; the rest set windowSec. */
  function setSpanFromPreset(rangeKey) {
    if (rangeKey === "mtd" || rangeKey === "month") {
      calendarMode = rangeKey;
    } else {
      calendarMode = "";
      windowSec = clampWindowSec(PRESET_SEC[rangeKey] || PRESET_SEC["1h"]);
    }
  }

  function onChartWheel(e) {
    const modal = $("dev-modal");
    if (!modal || modal.hidden) return;
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    if (calendarMode) {
      // Enter continuous zoom from a calendar mode at the 30 d (widest) end.
      calendarMode = "";
      windowSec = WINDOW_MAX_S;
    }
    const next = stepWindowSec(windowSec, e ? e.deltaY : 0);
    if (next === windowSec) {
      updateRangeButtons();
      return;
    }
    windowSec = next;
    updateRangeButtons();
    const status = $("dev-chart-status");
    if (status) status.textContent = "Загрузка";
    scheduleZoomRefetch();
  }

  /* ── Modal / chart ───────────────────────────────────────────────────── */

  function setModalMode(mode) {
    modalMode = mode === "overview" ? "overview" : "metric";
    document.querySelectorAll("#dev-mode-tabs .dev-mode-tab").forEach((btn) => {
      const on = btn.dataset.mode === modalMode;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const chips = $("dev-metric-chips");
    if (chips) chips.hidden = modalMode === "overview";
    loadHistory();
  }

  function openModal(kind, deviceId, label, metricId) {
    activeDevice =
      kind === "ce" || kind === "spodes"
        ? "ce"
        : kind === "mr"
        ? "mr"
        : kind === "carel"
        ? "carel"
        : kind === "mtd"
        ? "mtd"
        : "dtv";
    activeDeviceId = String(deviceId || "");
    activeDeviceLabel = String(label || "");
    const metrics =
      activeDevice === "mr"
        ? mrMetricsFor(deviceId)
        : activeDevice === "mtd"
        ? MTD_METRICS
        : activeDevice === "dtv"
        ? DTV_METRICS
        : activeDevice === "carel"
        ? CAREL_METRICS
        : CE_METRICS;
    const ids = metrics.map((m) => m[0]);
    activeMetric =
      metricId && ids.indexOf(metricId) >= 0
        ? metricId
        : metrics.length
        ? metrics[0][0]
        : "";
    calendarMode = "";
    windowSec = PRESET_SEC["1h"];
    modalMode = "metric";
    const modal = $("dev-modal");
    if (!modal) return;
    const titleEl = $("dev-modal-title");
    if (titleEl) {
      const fallback =
        activeDevice === "dtv"
          ? "ДТВ-RS-485"
          : activeDevice === "ce"
          ? "СЭ-02м-3"
          : activeDevice === "carel"
          ? "Carel"
          : activeDevice === "mtd"
          ? "MTD262-MB"
          : "MR-02m";
      const nm = activeDeviceLabel || fallback;
      titleEl.textContent = nm + " · история";
      titleEl.dataset.baseName = nm;
      cancelDevRen();
      syncDevPen();
    }
    const chips = $("dev-metric-chips");
    if (!chips) return;
    chips.hidden = false;
    chips.innerHTML = metrics
      .map(
        ([id, lab]) =>
          `<button type="button" class="dev-chip${
            id === activeMetric ? " active" : ""
          }" data-metric="${id}">${lab}</button>`
      )
      .join("");
    chips.querySelectorAll(".dev-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        chips.querySelectorAll(".dev-chip").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeMetric = btn.dataset.metric;
        modalMode = "metric";
        document.querySelectorAll("#dev-mode-tabs .dev-mode-tab").forEach((b) => {
          const on = b.dataset.mode === "metric";
          b.classList.toggle("active", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        });
        chips.hidden = false;
        loadHistory();
      });
    });
    document.querySelectorAll("#dev-mode-tabs .dev-mode-tab").forEach((btn) => {
      const on = btn.dataset.mode === "metric";
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    updateRangeButtons();
    setCeSideVisible(activeDevice === "ce");
    setExportVisible(true);
    const inp = $("dev-ce-kwh-rub");
    if (inp) inp.value = String(getKwhRub());
    modal.hidden = false;
    document.body.classList.add("dev-modal-open");
    loadHistory();
  }

  function closeModal() {
    cancelDevRen();
    const modal = $("dev-modal");
    if (modal) modal.hidden = true;
    document.body.classList.remove("dev-modal-open");
    setExportVisible(false);
    const canvas = $("dev-chart");
    if (canvas && canvas.__chart) {
      canvas.__chart.hoverX = null;
      canvas.__chart.marks = [];
      canvas.__chart.pointerCss = null;
    }
  }

  function loadHistory() {
    const status = $("dev-chart-status");
    if (status) status.textContent = "Загрузка";
    if (activeDevice === "ce") loadCeSummary();
    else setCeSideVisible(false);
    const rangeAtStart = rangeToken();
    const metricAtStart = activeMetric;
    const modeAtStart = modalMode;
    const idAtStart = activeDeviceId;
    const req = ++historyReqId;
    if (historyAbort) {
      try {
        historyAbort.abort();
      } catch (_e) {
        /* ignore */
      }
    }
    historyAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
    const fetchOpts = historyAbort ? { signal: historyAbort.signal } : {};

    function stillCurrent() {
      return (
        req === historyReqId &&
        rangeAtStart === rangeToken() &&
        metricAtStart === activeMetric &&
        modeAtStart === modalMode &&
        idAtStart === activeDeviceId
      );
    }

    if (modalMode === "overview") {
      const isMr = activeDevice === "mr";
      const isCarel = activeDevice === "carel";
      const isMtd = activeDevice === "mtd";
      const overviewQs =
        isMr
          ? { kind: "mr", group: "all", ...rangeReqParams() }
          : isCarel
          ? { kind: "carel", ...rangeReqParams() }
          : isMtd
          ? { group: "mtd", ...rangeReqParams() }
          : {
              group: activeDevice === "dtv" ? "climate" : "energy",
              ...rangeReqParams(),
            };
      const url = "api/devices/history?" + historyQs(overviewQs);
      fetchJson(url, fetchOpts)
        .then((data) => {
          if (!stillCurrent()) return;
          if (!data || !data.ok) {
            if (status) status.textContent = (data && data.error) || "Нет данных";
            chartSeries = [];
            chartMeta = { label: "Общее", unit: "", range: rangeLabelKey(), normalize: true };
            drawChart();
            return;
          }
          chartSeries = flattenMetricSeries(data.metrics || []);
          chartMeta = {
            label:
              activeDevice === "dtv"
                ? "Климат · Общее"
                : activeDevice === "mr"
                ? "MR-02m · Общее"
                : activeDevice === "carel"
                ? "Carel · Общее"
                : activeDevice === "mtd"
                ? "MTD262-MB · Общее"
                : "Энергия · Общее",
            unit: "",
            range: rangeLabelKey(),
            normalize: true,
            windowMin: data.t0_ms,
            windowMax: data.t1_ms,
          };
          if (status) {
            status.textContent = overviewStatusText();
          }
          drawChart();
        })
        .catch((err) => {
          if (err && err.name === "AbortError") return;
          if (!stillCurrent()) return;
          if (status) status.textContent = "Ошибка загрузки";
          chartSeries = [];
          drawChart();
        });
      return;
    }
    const metricQs =
      activeDevice === "mr"
        ? { kind: "mr", channel: mrChNum(activeMetric), ...rangeReqParams() }
        : activeDevice === "carel"
        ? { kind: "carel", metric: activeMetric, ...rangeReqParams() }
        : { metric: activeMetric, ...rangeReqParams() };
    const url = "api/devices/history?" + historyQs(metricQs);
    fetchJson(url, fetchOpts)
      .then((data) => {
        if (!stillCurrent()) return;
        if (!data || !data.ok) {
          if (status) status.textContent = (data && data.error) || "Нет данных";
          chartSeries = [];
          chartMeta = { label: "", unit: "", range: rangeLabelKey(), normalize: false };
          drawChart();
          return;
        }
        const barMode = barModeFor(activeDevice, modalMode, activeMetric);
        const prepared = !!(barMode && data.prepared && Number(data.bucket_s) > 0);
        chartMeta = {
          label: data.label || "",
          unit: data.unit || "",
          metric: activeMetric,
          range: rangeLabelKey(),
          normalize: false,
          windowMin: data.t0_ms,
          windowMax: data.t1_ms,
          barMode: barMode,
          prepared: prepared,
          bucketSec: prepared
            ? Number(data.bucket_s)
            : barMode
            ? barBucketSec(windowSec, calendarMode)
            : 0,
        };
        chartSeries = data.series || [];
        let n = 0;
        if (prepared) {
          const seen = {};
          chartSeries.forEach((ser) =>
            (ser.points || []).forEach((p) => {
              if (p) seen[p[0]] = 1;
            })
          );
          n = Object.keys(seen).length;
        } else {
          n = chartSeries.reduce((s, ser) => s + (ser.points || []).length, 0);
        }
        if (status) {
          const barNote = barMode
            ? ` · ${tl(barStepLabel(barMode, chartMeta.bucketSec))}`
            : "";
          status.textContent = n
            ? `${tl(data.label || "")} · ${rangeHumanLabel()} · ${n} ${tl("точек")}${barNote}`
            : "Нет точек за выбранный период";
        }
        drawChart();
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return;
        if (!stillCurrent()) return;
        if (status) status.textContent = "Ошибка загрузки";
        chartSeries = [];
        drawChart();
      });
  }

  /** «Общее» status line over the SHOWN series — the Y range must match the
      axis, which rescales when a legend entry is hidden. */
  function overviewStatusText() {
    const shown = visibleSeriesOf(chartSeries, hiddenSeriesFor(hiddenStateKey()));
    const n = shown.reduce((s, ser) => s + (ser.points || []).length, 0);
    if (!n) return "Нет точек за выбранный период";
    let dataMax = -Infinity;
    shown.forEach((ser) =>
      (ser.points || []).forEach(([, y]) => {
        const v = Number(y);
        if (Number.isFinite(v) && v > dataMax) dataMax = v;
      })
    );
    const yDom = Number.isFinite(dataMax)
      ? computeSharedAbsYDomain(dataMax)
      : { minY: 0, maxY: 1 };
    return `${tl(chartMeta.label)} · ${rangeHumanLabel()} · ${shown.length} ${tl("рядов")} · Y: 0…${fmtYTick(
      yDom.maxY,
      yTickDecimals(0, yDom.maxY, 4)
    )} · ${n} ${tl("точек")}`;
  }

  function drawChart() {
    const normalize = !!chartMeta.normalize;
    const hidden = hiddenSeriesFor(hiddenStateKey());
    // СЭ power / energy: bars over the bucket the series was loaded for.
    const barMode = chartMeta.barMode || "";
    const shown = barMode
      ? chartMeta.prepared
        ? preparedBarSeries(chartSeries, barMode, chartMeta.bucketSec)
        : toBarSeries(chartSeries, barMode, chartMeta.bucketSec)
      : chartSeries;
    drawOntoCanvas($("dev-chart"), shown, {
      range: chartMeta.range || rangeLabelKey(),
      normalize: normalize,
      legendEl: $("dev-chart-legend"),
      unitNote: chartMeta.unit || "",
      metric: chartMeta.metric || (normalize ? "" : activeMetric),
      windowMin: chartMeta.windowMin,
      windowMax: chartMeta.windowMax,
      hidden: hidden,
      bars: !!barMode,
    });
    if (!normalize) renderLegend(shown, hidden);
  }

  /* ── Tab lifecycle ───────────────────────────────────────────────────── */

  function bindOnce() {
    if (window.__devicesBound) return;
    window.__devicesBound = true;
    const closeBtn = $("dev-modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    const backdrop = $("dev-modal-backdrop");
    if (backdrop) backdrop.addEventListener("click", closeModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && $("dev-modal") && !$("dev-modal").hidden) {
        if (titleEditing) {
          e.preventDefault();
          cancelDevRen();
          return;
        }
        closeModal();
      }
    });
    const pen = $("dev-title-pen");
    if (pen) {
      pen.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (titleEditing) commitDevRen();
        else startDevRen();
      });
    }
    const nin = $("dev-title-in");
    if (nin) {
      nin.setAttribute("aria-hidden", "true");
      nin.setAttribute("tabindex", "-1");
      nin.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          commitDevRen();
        } else if (ev.key === "Escape") {
          ev.preventDefault();
          ev.stopPropagation();
          cancelDevRen();
        }
      });
    }
    document.querySelectorAll("#dev-modal .dev-range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        setSpanFromPreset(btn.dataset.range);
        updateRangeButtons();
        loadHistory();
      });
    });
    document.querySelectorAll("#dev-mode-tabs .dev-mode-tab").forEach((btn) => {
      btn.addEventListener("click", () => setModalMode(btn.dataset.mode));
    });
    const kwhInp = $("dev-ce-kwh-rub");
    if (kwhInp && !kwhInp.__bound) {
      kwhInp.__bound = true;
      const onTariff = () => {
        setKwhRub(kwhInp.value);
        if (activeDevice === "ce" && $("dev-modal") && !$("dev-modal").hidden) {
          loadCeSummary();
        }
      };
      kwhInp.addEventListener("change", onTariff);
      kwhInp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          onTariff();
        }
      });
    }
    const expBtn = $("dev-export-btn");
    if (expBtn && !expBtn.__bound) {
      expBtn.__bound = true;
      expBtn.addEventListener("click", (e) => {
        e.preventDefault();
        exportHistory();
      });
    }
    const addBtn = $("dev-widget-add-btn");
    if (addBtn && !addBtn.__bound) {
      addBtn.__bound = true;
      addBtn.addEventListener("click", (e) => {
        e.preventDefault();
        openAddModal();
      });
    }
    const clearBtn = $("dev-events-clear-btn");
    if (clearBtn && !clearBtn.__bound) {
      clearBtn.__bound = true;
      clearBtn.addEventListener("click", (e) => {
        e.preventDefault();
        clearEventsJournal();
      });
    }
    const addClose = $("dev-add-modal-close");
    if (addClose && !addClose.__bound) {
      addClose.__bound = true;
      addClose.addEventListener("click", closeAddModal);
    }
    const addBack = $("dev-add-modal-backdrop");
    if (addBack && !addBack.__bound) {
      addBack.__bound = true;
      addBack.addEventListener("click", closeAddModal);
    }
    const legend = $("dev-chart-legend");
    if (legend && !legend.__bound) {
      legend.__bound = true;
      // Delegated: the legend markup is rebuilt by renderLegendInto.
      legend.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest ? e.target.closest(".dev-legend-item") : null;
        if (!btn || !legend.contains(btn)) return;
        e.preventDefault();
        if (btn.getAttribute("aria-disabled") === "true") return;
        onLegendToggle(btn.getAttribute("data-series-key") || "");
      });
    }
    const chartWrap =
      $("dev-modal") && $("dev-modal").querySelector(".dev-chart-wrap");
    if (chartWrap && !chartWrap.__wheelBound) {
      chartWrap.__wheelBound = true;
      // passive:false — the handler calls preventDefault to stop page scroll.
      chartWrap.addEventListener("wheel", onChartWheel, { passive: false });
    }
    window.addEventListener("resize", () => {
      if ($("dev-modal") && !$("dev-modal").hidden) drawChart();
      if (lastRawDevices) ensureCards(lastRawDevices);
    });
  }

  window.devicesTabInit = function () {
    bindOnce();
    refreshLive();
    if (timer) clearInterval(timer);
    timer = setInterval(refreshLive, POLL_MS);
    startDtvRotation();
  };

  window.devicesTabDestroy = function () {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    stopDtvRotation();
    closeModal();
    closeAddModal();
  };
})();
