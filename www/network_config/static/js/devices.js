/* Devices tab — live ДТВ / СЭ-02м-3 widgets + Influx history modal + Общий overview */
(function () {
  "use strict";

  const POLL_MS = 5000;
  /** Border turns red when last sample older than this (seconds). */
  const STALE_BORDER_S = 30;
  let timer = null;
  let overviewTimer = null;
  let chartSeries = [];
  let chartMeta = { label: "", unit: "", range: "1h" };
  let activeDevice = "dtv";
  let activeMetric = "room_temp";
  let activeRange = "1h";
  /** Modal chart mode: "metric" (single) | "overview" (all series for device). */
  let modalMode = "metric";
  let overviewRange = "1h";
  let overviewClimate = [];
  let overviewEnergy = [];

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

  function fetchJson(url) {
    return fetch(url, { credentials: "same-origin" }).then((r) => r.json());
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

  function renderDtv(d) {
    const el = $("dev-card-dtv");
    if (!el || !d) return;
    const stale = isAgeStale(d);
    el.classList.toggle("dev-stale", stale);
    el.classList.toggle("dev-stale-border", stale);
    el.classList.toggle("dev-has-alert", !!(d.alerts && d.alerts.length));
    const stDtv = $("dev-dtv-status");
    if (stDtv) {
      stDtv.textContent = stale ? "stale" : "online";
      stDtv.className = "dev-pill " + (stale ? "bad" : "ok");
    }
    const alDtv = $("dev-dtv-alerts");
    if (alDtv) alDtv.innerHTML = alertBadgeHtml(d.alerts);
    const setDtv = (id, v) => {
      const n = $(id);
      if (n) n.textContent = v;
    };
    setDtv("dev-dtv-temp", fmt(d.room_temp, 1));
    setDtv("dev-dtv-rh", fmt(d.humidity, 1));
    setDtv("dev-dtv-eco2", fmt(d.eco2_ppm, 0));
    setDtv("dev-dtv-tvoc", fmt(d.tvoc_mg_m3, 2));
    setDtv("dev-dtv-pressure", fmt(d.pressure_mmhg, 1));
    setDtv("dev-dtv-light", fmt(d.light_pct, 0));
  }

  function renderCe(d) {
    const el = $("dev-card-ce");
    if (!el || !d) return;
    const stale = isAgeStale(d);
    el.classList.toggle("dev-stale", stale);
    el.classList.toggle("dev-stale-border", stale);
    el.classList.toggle("dev-has-alert", !!(d.alerts && d.alerts.length));
    const stCe = $("dev-ce-status");
    if (stCe) {
      stCe.textContent = stale ? "stale" : "online";
      stCe.className = "dev-pill " + (stale ? "bad" : "ok");
    }
    const alCe = $("dev-ce-alerts");
    if (alCe) alCe.innerHTML = alertBadgeHtml(d.alerts);
    const u = d.voltage || {};
    const i = d.current || {};
    const p = d.power_w || {};
    const setCe = (id, v) => {
      const n = $(id);
      if (n) n.textContent = v;
    };
    setCe("dev-ce-ua", fmt(u.a, 1));
    setCe("dev-ce-ub", fmt(u.b, 1));
    setCe("dev-ce-uc", fmt(u.c, 1));
    setCe("dev-ce-ia", fmt(i.a, 3));
    setCe("dev-ce-ib", fmt(i.b, 3));
    setCe("dev-ce-ic", fmt(i.c, 3));
    setCe("dev-ce-p", fmt(p.total, 0));
    setCe("dev-ce-f", fmt(d.frequency_hz, 2));
    setCe("dev-ce-e", fmt(d.energy_kwh_import, 3));
  }

  function refreshLive() {
    return fetchJson("/api/devices")
      .then((data) => {
        if (!data || !data.ok) return;
        renderDtv(data.dtv);
        renderCe(data.ce);
        // Alert strip above widgets removed (brown/amber banner); badges stay on cards.
      })
      .catch(() => {});
  }

  /* ── Chart helpers ───────────────────────────────────────────────────── */

  function fmtTime(d) {
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return hh + ":" + mm;
  }

  function fmtTimeLabel(ms, rangeKey) {
    const d = new Date(ms);
    const t = fmtTime(d);
    if (rangeKey === "24h") {
      const dd = String(d.getDate()).padStart(2, "0");
      const mo = String(d.getMonth() + 1).padStart(2, "0");
      return dd + "." + mo + " " + t;
    }
    return t;
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

  function ensureChartTip(wrap) {
    if (!wrap) return null;
    let tip = wrap.querySelector(".dev-chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "dev-chart-tip";
      tip.hidden = true;
      wrap.appendChild(tip);
    }
    return tip;
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
      });
    });
    return rows;
  }

  function bindChartPointer(canvas) {
    if (!canvas || canvas.__chartBound) return;
    canvas.__chartBound = true;
    canvas.style.cursor = "crosshair";
    const onMove = (e) => {
      const st = canvas.__chart;
      if (!st || !st.ok) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ((e.clientX - rect.left) / rect.width) * st.w;
      const my = ((e.clientY - rect.top) / rect.height) * st.h;
      if (
        mx < st.pad.l ||
        mx > st.w - st.pad.r ||
        my < st.pad.t ||
        my > st.pad.t + st.plotH
      ) {
        if (st.hoverX != null) {
          st.hoverX = null;
          st.pin = false;
          drawOntoCanvas(canvas, st.srcSeries, st.opts);
        }
        return;
      }
      st.hoverX = st.minX + ((mx - st.pad.l) / st.plotW) * (st.maxX - st.minX);
      st.pointerCss = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      drawOntoCanvas(canvas, st.srcSeries, st.opts);
    };
    const onLeave = () => {
      const st = canvas.__chart;
      if (!st) return;
      if (st.pin) return;
      if (st.hoverX != null) {
        st.hoverX = null;
        drawOntoCanvas(canvas, st.srcSeries, st.opts);
      }
    };
    const onClick = (e) => {
      const st = canvas.__chart;
      if (!st || !st.ok) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ((e.clientX - rect.left) / rect.width) * st.w;
      if (mx < st.pad.l || mx > st.w - st.pad.r) return;
      st.hoverX = st.minX + ((mx - st.pad.l) / st.plotW) * (st.maxX - st.minX);
      st.pointerCss = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      st.pin = true;
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
   * Hover/click: crosshair + tooltip (time + series + value + unit).
   */
  function drawOntoCanvas(canvas, series, opts) {
    opts = opts || {};
    if (!canvas) return;
    const wrap = canvas.parentElement;
    const tip = ensureChartTip(wrap);
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(280, wrap.clientWidth || 600);
    const h = Math.max(200, wrap.clientHeight || 260);
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
    const pin = !!prev.pin;
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
      if (tip) tip.hidden = true;
      canvas.__chart = {
        ok: false,
        srcSeries: series || [],
        opts,
        hoverX: null,
        pin: false,
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

    // Crosshair + point markers at hover/click X
    let tipRows = [];
    if (hoverX != null && Number.isFinite(hoverX)) {
      tipRows = hitSeriesAtX(drawSeries, hoverX, opts.unitNote || "", !!opts.normalize);
      const xPix = xScale(hoverX);
      ctx.save();
      ctx.strokeStyle = "rgba(255,255,255,0.55)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(xPix, pad.t);
      ctx.lineTo(xPix, pad.t + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      tipRows.forEach((row) => {
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
      ctx.restore();
    }

    if (tip) {
      if (tipRows.length) {
        const t0 = tipRows[0].t;
        const lines = [
          `<div class="dev-chart-tip-time">${escapeAttr(fmtTimeLabel(t0, rangeKey))}${
            pin ? " · закреплено" : ""
          }</div>`,
        ];
        tipRows.forEach((row) => {
          const color = COLORS[row.idx % COLORS.length];
          const digits = Math.abs(row.y) >= 100 || Number.isInteger(row.y) ? 0 : 2;
          const unit = row.unit ? ` ${row.unit}` : "";
          lines.push(
            `<div class="dev-chart-tip-row"><i style="background:${color}"></i>` +
              `<span>${escapeAttr(row.label)}</span>` +
              `<b>${escapeAttr(fmt(row.y, digits) + unit)}</b></div>`
          );
        });
        tip.innerHTML = lines.join("");
        tip.hidden = false;
        const cssX = pointerCss ? pointerCss.x : xScale(hoverX);
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
      } else {
        tip.hidden = true;
      }
    }

    if (opts.legendEl) {
      renderLegendInto(opts.legendEl, drawSeries, opts.unitNote || "", opts.normalize);
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
      pin,
      pointerCss,
    };
    bindChartPointer(canvas);
  }

  function renderLegendInto(el, series, unitNote, normalized) {
    if (!el) return;
    if (!series.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = series
      .map((s, i) => {
        const raw = s._rawPoints || s.points || [];
        const last = raw[raw.length - 1];
        const unit = s.unit ? ` ${s.unit}` : unitNote ? ` ${unitNote}` : "";
        const val = last ? fmt(last[1], 2) + unit : "—";
        const name = s.label || s.field || s.metric || "";
        return `<span class="dev-legend-item"><i style="background:${
          COLORS[i % COLORS.length]
        }"></i>${escapeAttr(name)} · ${escapeAttr(val)}</span>`;
      })
      .join("");
  }

  function renderLegend(series) {
    renderLegendInto($("dev-chart-legend"), series, chartMeta.unit || "", false);
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

  function openModal(device, metricId) {
    activeDevice = device;
    const metrics = device === "dtv" ? DTV_METRICS : CE_METRICS;
    const ids = metrics.map((m) => m[0]);
    activeMetric = metricId && ids.indexOf(metricId) >= 0 ? metricId : metrics[0][0];
    activeRange = "1h";
    modalMode = "metric";
    const modal = $("dev-modal");
    if (!modal) return;
    $("dev-modal-title").textContent =
      device === "dtv"
        ? "ДТВ-RS-485 · история"
        : "СЭ-02м-3 · история";
    const chips = $("dev-metric-chips");
    chips.hidden = false;
    chips.innerHTML = metrics
      .map(
        ([id, label]) =>
          `<button type="button" class="dev-chip${
            id === activeMetric ? " active" : ""
          }" data-metric="${id}">${label}</button>`
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
    modal.hidden = false;
    document.body.classList.add("dev-modal-open");
    loadHistory();
  }

  function closeModal() {
    const modal = $("dev-modal");
    if (modal) modal.hidden = true;
    document.body.classList.remove("dev-modal-open");
    const canvas = $("dev-chart");
    if (canvas && canvas.__chart) {
      canvas.__chart.hoverX = null;
      canvas.__chart.pin = false;
    }
  }

  function loadHistory() {
    const status = $("dev-chart-status");
    if (status) status.textContent = "Загрузка…";
    if (modalMode === "overview") {
      const group = activeDevice === "dtv" ? "climate" : "energy";
      const url =
        "/api/devices/history?group=" +
        encodeURIComponent(group) +
        "&range=" +
        encodeURIComponent(activeRange);
      fetchJson(url)
        .then((data) => {
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
        .catch(() => {
          if (status) status.textContent = "Ошибка загрузки";
          chartSeries = [];
          drawChart();
        });
      return;
    }
    const url =
      "/api/devices/history?metric=" +
      encodeURIComponent(activeMetric) +
      "&range=" +
      encodeURIComponent(activeRange);
    fetchJson(url)
      .then((data) => {
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
          range: data.range || activeRange,
          normalize: false,
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
      .catch(() => {
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
    });
    if (!normalize) renderLegend(chartSeries);
  }

  /* ── Overview «Общий» ────────────────────────────────────────────────── */

  function loadOverview() {
    const stC = $("ov-climate-status");
    const stE = $("ov-energy-status");
    if (stC) stC.textContent = "Загрузка…";
    if (stE) stE.textContent = "Загрузка…";
    const rng = encodeURIComponent(overviewRange);
    Promise.all([
      fetchJson("/api/devices/history?group=climate&range=" + rng),
      fetchJson("/api/devices/history?group=energy&range=" + rng),
    ])
      .then(([clim, ener]) => {
        overviewClimate = flattenMetricSeries(clim && clim.ok ? clim.metrics : []);
        overviewEnergy = flattenMetricSeries(ener && ener.ok ? ener.metrics : []);
        const nC = overviewClimate.reduce((s, x) => s + (x.points || []).length, 0);
        const nE = overviewEnergy.reduce((s, x) => s + (x.points || []).length, 0);
        function seriesDataMax(series) {
          let m = -Infinity;
          (series || []).forEach((ser) =>
            (ser.points || []).forEach(([, y]) => {
              const v = Number(y);
              if (Number.isFinite(v) && v > m) m = v;
            })
          );
          return m;
        }
        const maxC = seriesDataMax(overviewClimate);
        const maxE = seriesDataMax(overviewEnergy);
        const domC = Number.isFinite(maxC) ? computeSharedAbsYDomain(maxC) : { minY: 0, maxY: 1 };
        const domE = Number.isFinite(maxE) ? computeSharedAbsYDomain(maxE) : { minY: 0, maxY: 1 };
        if (stC) {
          stC.textContent = clim && clim.ok
            ? `Климат · ${overviewRange} · ${overviewClimate.length} рядов · Y: 0…${fmtYTick(
                domC.maxY,
                yTickDecimals(0, domC.maxY, 4)
              )}`
            : (clim && clim.error) || "Нет данных";
        }
        if (stE) {
          stE.textContent = ener && ener.ok
            ? `Энергия · ${overviewRange} · ${overviewEnergy.length} рядов · Y: 0…${fmtYTick(
                domE.maxY,
                yTickDecimals(0, domE.maxY, 4)
              )}`
            : (ener && ener.error) || "Нет данных";
        }
        if (!nC && stC && clim && clim.ok) stC.textContent += " · нет точек";
        if (!nE && stE && ener && ener.ok) stE.textContent += " · нет точек";
        drawOverview();
      })
      .catch(() => {
        if (stC) stC.textContent = "Ошибка загрузки";
        if (stE) stE.textContent = "Ошибка загрузки";
        overviewClimate = [];
        overviewEnergy = [];
        drawOverview();
      });
  }

  function drawOverview() {
    drawOntoCanvas($("ov-chart-climate"), overviewClimate, {
      range: overviewRange,
      normalize: true,
      legendEl: $("ov-climate-legend"),
      padBottom: 40,
    });
    drawOntoCanvas($("ov-chart-energy"), overviewEnergy, {
      range: overviewRange,
      normalize: true,
      legendEl: $("ov-energy-legend"),
      padBottom: 40,
    });
  }

  /* ── Tab lifecycle ───────────────────────────────────────────────────── */

  function bindOnce() {
    if (window.__devicesBound) return;
    window.__devicesBound = true;
    const dtv = $("dev-card-dtv");
    const ce = $("dev-card-ce");
    function bindCardOpen(card, device) {
      if (!card) return;
      card.addEventListener("click", (e) => {
        const kpi = e.target.closest(".dev-kpi[data-metric]");
        openModal(device, kpi ? kpi.dataset.metric : undefined);
      });
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openModal(device);
        }
      });
    }
    bindCardOpen(dtv, "dtv");
    bindCardOpen(ce, "ce");
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
    document.querySelectorAll(".ov-range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".ov-range-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        overviewRange = btn.dataset.range;
        loadOverview();
      });
    });
    window.addEventListener("resize", () => {
      if ($("dev-modal") && !$("dev-modal").hidden) drawChart();
      if ($("tab-overview") && $("tab-overview").classList.contains("active")) drawOverview();
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
  };

  window.overviewTabInit = function () {
    if (!$("tab-overview") || !$("ov-chart-climate")) return;
    bindOnce();
    loadOverview();
    if (overviewTimer) clearInterval(overviewTimer);
    overviewTimer = setInterval(loadOverview, 60000);
  };

  window.overviewTabDestroy = function () {
    if (overviewTimer) {
      clearInterval(overviewTimer);
      overviewTimer = null;
    }
  };
})();
