/* Devices tab — live ДТВ / СЭ-02м-3 widgets + MR-02m analog cards + history modal / Excel / events */
import { aiSensorLabel, aiUnitPrecision } from "./ai-sensors.js?v=1.0.5.84";

(function () {
  "use strict";

  const POLL_MS = 5000;
  /** Border turns red when last sample older than this (seconds). */
  const STALE_BORDER_S = 30;
  let timer = null;
  let chartSeries = [];
  let chartMeta = { label: "", unit: "", range: "1h" };
  /** kind: "dtv" | "ce" */
  let activeDevice = "dtv";
  /** MQTT/cache id, e.g. ce02m3-COM2-14 */
  let activeDeviceId = "";
  let activeDeviceLabel = "";
  let activeMetric = "room_temp";
  let activeRange = "1h";
  /** Modal chart mode: "metric" (single) | "overview" (all series for device). */
  let modalMode = "metric";
  /** Signature of last rendered card set (device ids). */
  let cardsSig = "";
  /** Устройства, доступные для повторного добавления (из API). */
  let availableDevices = [];
  let eventsSig = "";

  const ICO_DTV =
    '<svg viewBox="0 0 24 24"><path d="M12 2v10"/><path d="M8 14a4 4 0 1 0 8 0c0-2.5-2-4-4-6-2 2-4 3.5-4 6z"/></svg>';
  const ICO_CE =
    '<svg viewBox="0 0 24 24"><path d="M13 2L4 14h7l-1 8 10-14h-7l0-6z"/></svg>';
  const ICO_MR =
    '<svg viewBox="0 0 24 24"><path d="M3 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0"/></svg>';

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
    return [...dtv, ...ce, ...mr];
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
    const rows = [
      ["room_temp", "temp", "°C", "Температура"],
      ["humidity", "rh", "%", "Влажность"],
      ["eco2_ppm", "eco2", "ppm", "eCO₂"],
      ["tvoc_mg_m3", "tvoc", "mg/m³", "TVOC"],
      ["pressure_mmhg", "pressure", "mmHg", "Давление"],
      ["light_pct", "light", "%", "Освещённость"],
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

  function renameDevice(id) {
    const names = customDeviceNames();
    const def = lastLabelById[id] || "";
    const next = window.prompt(
      "Название виджета (пусто — вернуть стандартное «" + def + "»):",
      names[id] || def
    );
    if (next === null) return;
    const val = next.trim();
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
  }

  function buildCard(d) {
    const kind = d.kind === "ce" ? "ce" : d.kind === "mr" ? "mr" : "dtv";
    const isMr = kind === "mr";
    const id = String(d.id || "");
    const title = d.label || d.title || id;
    const el = document.createElement("article");
    el.className =
      "widget dev-card" +
      (kind === "dtv" ? " dev-card--dtv" : isMr ? " dev-card--mr" : "");
    el.dataset.deviceId = id;
    el.dataset.kind = kind;
    // MR-02m cards are display-only: no history modal, so no button role.
    if (!isMr) {
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.title = "Открыть историю";
    }
    const ico = kind === "dtv" ? ICO_DTV : isMr ? ICO_MR : ICO_CE;
    let metricsCls = "dev-metrics";
    let metricsHtml;
    if (isMr) {
      metricsCls += mrAiCount(d) > 6 ? " dev-metrics--mr4" : " dev-metrics--mr3";
      metricsHtml = buildMrMetricsHtml(d);
    } else if (kind === "dtv") {
      metricsHtml = buildDtvMetricsHtml();
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
      (isMr
        ? ""
        : `<button type="button" class="dev-card-remove" data-role="remove" title="Удалить виджет (архив остановится, данные в БД сохранятся)" aria-label="Удалить виджет">×</button>`) +
      `</span>` +
      `</div>` +
      `<div class="widget-body dev-body"><div class="${metricsCls}">` +
      metricsHtml +
      `</div></div>`;
    if (!isMr) {
      el.addEventListener("click", (e) => {
        if (e.target.closest('[data-role="remove"], [data-role="rename"]')) return;
        const kpi = e.target.closest(".dev-kpi[data-metric]");
        openModal(kind, id, title, kpi ? kpi.dataset.metric : undefined);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openModal(kind, id, title);
        }
      });
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
    fetchJson("/api/devices/widgets/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    return fetchJson("/api/devices/widgets/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
          (d.kind === "ce" ? "СЭ-02м-3" : "ДТВ") +
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
    if (d.kind === "ce" || card.dataset.kind === "ce") {
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
      setField(card, "temp", fmt(d.room_temp, 1));
      setField(card, "rh", fmt(d.humidity, 1));
      setField(card, "eco2", fmt(d.eco2_ppm, 0));
      setField(card, "tvoc", fmt(d.tvoc_mg_m3, 2));
      setField(card, "pressure", fmt(d.pressure_mmhg, 1));
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

  function ensureCards(list) {
    const grid = $("dev-grid");
    if (!grid) return;
    const empty = $("dev-empty");
    const sig = list.map((d) => d.id || "").join("|");
    if (sig !== cardsSig) {
      cardsSig = sig;
      grid.querySelectorAll(".dev-card").forEach((el) => el.remove());
      if (empty) empty.hidden = list.length > 0;
      list.forEach((d) => grid.appendChild(buildCard(d)));
    } else if (empty) {
      empty.hidden = list.length > 0;
    }
    list.forEach((d) => {
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
    return fetchJson("/api/devices/events?limit=80")
      .then((data) => {
        if (!data || !data.ok) return;
        renderEvents(data.events || []);
      })
      .catch(() => {});
  }

  function refreshLive() {
    return fetchJson("/api/devices")
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
              : "Нет устройств ДТВ / СЭ-02м-3 в MQTT. Добавьте их на вкладке MQTT — виджеты появятся здесь автоматически.";
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
    const byMetric = {
      room_temp: 1,
      humidity: 1,
      eco2_ppm: 0,
      tvoc_mg_m3: 2,
      pressure_mmhg: 1,
      light_pct: 0,
      presence: 0,
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
    if (!on && (activeRange === "mtd" || activeRange === "month")) {
      activeRange = "1h";
      document.querySelectorAll("#dev-modal .dev-range-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.range === activeRange);
      });
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
    const params = { range: activeRange, format: "xlsx" };
    if (activeDeviceId) params.device_id = activeDeviceId;
    if (modalMode === "overview") {
      params.group = activeDevice === "dtv" ? "climate" : "energy";
    } else {
      params.metric = activeMetric;
    }
    const url = "/api/devices/history/export?" + historyQs(params);
    const status = $("dev-chart-status");
    if (status) status.textContent = "Экспорт Excel";
    const fallback =
      (activeDevice === "dtv" ? "dtv" : "ce") + "_export.xlsx";
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
    const rangeAtStart = activeRange;
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
      "/api/devices/history/summary?" +
      historyQs({ range: activeRange, kwh_rub: String(tariff) });
    return fetchJson(url, summaryAbort ? { signal: summaryAbort.signal } : {})
      .then((data) => {
        if (req !== summaryReqId) return;
        if (rangeAtStart !== activeRange || idAtStart !== activeDeviceId) return;
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
    (drawSeries || []).forEach((ser, idx) => {
      const plotPts = ser.points || [];
      const rawPts = ser._rawPoints || plotPts;
      if (!plotPts.length) return;
      const plotHit = nearestPoint(plotPts, xMs);
      if (!plotHit) return;
      const rawHit = nearestPoint(rawPts, plotHit[0]) || plotHit;
      const unit = ser.unit ? ser.unit : unitNote || "";
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
      st.srcSeries || [],
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
    const lines = [
      `<div class="dev-chart-tip-time">${escapeAttr(fmtTipTimeLabel(t0, rangeKey))}</div>`,
    ];
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

    const drawSeries = series || [];

    const all = flattenPoints(drawSeries);
    if (!all.length) {
      ctx.fillStyle = th.text;
      ctx.font = "13px ui-sans-serif, system-ui, sans-serif";
      ctx.fillText(opts.emptyText || "Нет данных", pad.l, h / 2);
      if (opts.legendEl) renderLegendInto(opts.legendEl, [], "");
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
    all.forEach(([x, y]) => {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
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
      // skip if would heavily overlap neighbors (narrow canvas)
      if (i > 0 && i < nTicks && labelW > (plotW / (nTicks + 1)) * 1.35) continue;
      ctx.fillText(label, tx, h - 10);
    }
    ctx.textAlign = "left";

    drawSeries.forEach((ser, idx) => {
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
      drawHitMarkers(ctx, markRows[i], xScale, yScale);
    });

    let hoverRows = [];
    if (marks.length < 2 && hoverX != null && Number.isFinite(hoverX)) {
      hoverRows = hitSeriesAtX(drawSeries, hoverX, unitNote, normalize);
      drawCrosshairLine(ctx, xScale(hoverX), pad, plotH, false, th.text);
      drawHitMarkers(ctx, hoverRows, xScale, yScale);
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
      if (rows0.length) {
        placeTipNearX(
          tipA,
          tipHtmlFromRows(rows0, rangeKey, metricId),
          xA,
          w,
          h,
          pad,
          xA > xB
        );
      }
      if (rows1.length) {
        placeTipNearX(
          tipB,
          tipHtmlFromRows(rows1, rangeKey, metricId),
          xB,
          w,
          h,
          pad,
          xB > xA
        );
      }
      if (deltaEl && (rows0.length || rows1.length)) {
        const dt = Math.abs((marks[1] || 0) - (marks[0] || 0));
        const byIdx = {};
        rows0.forEach((r) => {
          byIdx[r.idx] = { a: r, b: null };
        });
        rows1.forEach((r) => {
          if (!byIdx[r.idx]) byIdx[r.idx] = { a: null, b: r };
          else byIdx[r.idx].b = r;
        });
        const dLines = [
          `<div class="dev-chart-delta-time">Δt ${escapeAttr(fmtDurationMs(dt))}</div>`,
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
            const dec = tipDecimalsFor(unit, a.metric || b.metric || metricId);
            dLines.push(
              `<div class="dev-chart-delta-row"><i style="background:${color}"></i>` +
                `<b>Δ ${escapeAttr(a.label || "")} ${escapeAttr(
                  fmtSignedTipValue(dy, dec) + unitS
                )}</b></div>`
            );
          });
        deltaEl.innerHTML = dLines.join("");
        deltaEl.hidden = false;
        const midX = (Math.min(xA, xB) + Math.max(xA, xB)) / 2;
        const tipW = deltaEl.offsetWidth || 140;
        const tipH = deltaEl.offsetHeight || 48;
        let left = midX - tipW / 2;
        let top = pad.t + plotH / 2 - tipH / 2;
        if (left < pad.l + 4) left = pad.l + 4;
        if (left + tipW > w - pad.r - 4) left = w - pad.r - tipW - 4;
        if (top < pad.t + 4) top = pad.t + 4;
        if (top + tipH > pad.t + plotH - 4) top = pad.t + plotH - tipH - 4;
        deltaEl.style.left = left + "px";
        deltaEl.style.top = top + "px";
      }
    }

    if (opts.legendEl) {
      renderLegendInto(
        opts.legendEl,
        drawSeries,
        opts.unitNote || "",
        opts.normalize,
        opts.metric || ""
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

  function renderLegendInto(el, series, unitNote, normalized, metric) {
    if (!el) return;
    if (!series.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = series
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
        return `<span class="dev-legend-item"><i style="background:${
          COLORS[i % COLORS.length]
        }"></i>${escapeAttr(name)} · ${escapeAttr(val)}</span>`;
      })
      .join("");
  }

  function renderLegend(series) {
    renderLegendInto(
      $("dev-chart-legend"),
      series,
      chartMeta.unit || "",
      false,
      activeMetric
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
    activeDevice = kind === "ce" ? "ce" : "dtv";
    activeDeviceId = String(deviceId || "");
    activeDeviceLabel = String(label || "");
    const metrics = activeDevice === "dtv" ? DTV_METRICS : CE_METRICS;
    const ids = metrics.map((m) => m[0]);
    activeMetric = metricId && ids.indexOf(metricId) >= 0 ? metricId : metrics[0][0];
    activeRange = "1h";
    modalMode = "metric";
    const modal = $("dev-modal");
    if (!modal) return;
    const titleEl = $("dev-modal-title");
    if (titleEl) {
      titleEl.textContent = (activeDeviceLabel || (activeDevice === "dtv" ? "ДТВ-RS-485" : "СЭ-02м-3")) +
        " · история";
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
    document.querySelectorAll("#dev-modal .dev-range-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.range === activeRange);
    });
    setCeSideVisible(activeDevice === "ce");
    setExportVisible(true);
    const inp = $("dev-ce-kwh-rub");
    if (inp) inp.value = String(getKwhRub());
    modal.hidden = false;
    document.body.classList.add("dev-modal-open");
    loadHistory();
  }

  function closeModal() {
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
    const rangeAtStart = activeRange;
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
        rangeAtStart === activeRange &&
        metricAtStart === activeMetric &&
        modeAtStart === modalMode &&
        idAtStart === activeDeviceId
      );
    }

    if (modalMode === "overview") {
      const group = activeDevice === "dtv" ? "climate" : "energy";
      const url =
        "/api/devices/history?" +
        historyQs({ group: group, range: activeRange });
      fetchJson(url, fetchOpts)
        .then((data) => {
          if (!stillCurrent()) return;
          if (!data || !data.ok) {
            if (status) status.textContent = (data && data.error) || "Нет данных";
            chartSeries = [];
            chartMeta = { label: "Общее", unit: "", range: activeRange, normalize: true };
            drawChart();
            return;
          }
          chartSeries = flattenMetricSeries(data.metrics || []);
          chartMeta = {
            label: activeDevice === "dtv" ? "Климат · Общее" : "Энергия · Общее",
            unit: "",
            range: data.range || activeRange,
            normalize: true,
            windowMin: data.t0_ms,
            windowMax: data.t1_ms,
          };
          const n = chartSeries.reduce((s, ser) => s + (ser.points || []).length, 0);
          let dataMax = -Infinity;
          chartSeries.forEach((ser) =>
            (ser.points || []).forEach(([, y]) => {
              const v = Number(y);
              if (Number.isFinite(v) && v > dataMax) dataMax = v;
            })
          );
          const yDom = Number.isFinite(dataMax)
            ? computeSharedAbsYDomain(dataMax)
            : { minY: 0, maxY: 1 };
          if (status) {
            status.textContent = n
              ? `${chartMeta.label} · ${chartMeta.range} · ${chartSeries.length} рядов · Y: 0…${fmtYTick(
                  yDom.maxY,
                  yTickDecimals(0, yDom.maxY, 4)
                )} · ${n} точек`
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
      return;
    }
    const url =
      "/api/devices/history?" +
      historyQs({ metric: activeMetric, range: activeRange });
    fetchJson(url, fetchOpts)
      .then((data) => {
        if (!stillCurrent()) return;
        if (!data || !data.ok) {
          if (status) status.textContent = (data && data.error) || "Нет данных";
          chartSeries = [];
          chartMeta = { label: "", unit: "", range: activeRange, normalize: false };
          drawChart();
          return;
        }
        chartMeta = {
          label: data.label || "",
          unit: data.unit || "",
          metric: activeMetric,
          range: data.range || activeRange,
          normalize: false,
          windowMin: data.t0_ms,
          windowMax: data.t1_ms,
        };
        chartSeries = data.series || [];
        const n = chartSeries.reduce((s, ser) => s + (ser.points || []).length, 0);
        if (status) {
          status.textContent = n
            ? `${data.label || ""} · ${data.range} · ${n} точек`
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

  function drawChart() {
    const normalize = !!chartMeta.normalize;
    drawOntoCanvas($("dev-chart"), chartSeries, {
      range: chartMeta.range || activeRange,
      normalize: normalize,
      legendEl: $("dev-chart-legend"),
      unitNote: chartMeta.unit || "",
      metric: chartMeta.metric || (normalize ? "" : activeMetric),
      windowMin: chartMeta.windowMin,
      windowMax: chartMeta.windowMax,
    });
    if (!normalize) renderLegend(chartSeries);
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
      if (e.key === "Escape" && $("dev-modal") && !$("dev-modal").hidden) closeModal();
    });
    document.querySelectorAll("#dev-modal .dev-range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#dev-modal .dev-range-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeRange = btn.dataset.range;
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
    window.addEventListener("resize", () => {
      if ($("dev-modal") && !$("dev-modal").hidden) drawChart();
    });
  }

  window.devicesTabInit = function () {
    bindOnce();
    refreshLive();
    if (timer) clearInterval(timer);
    timer = setInterval(refreshLive, POLL_MS);
  };

  window.devicesTabDestroy = function () {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    closeModal();
    closeAddModal();
  };
})();
