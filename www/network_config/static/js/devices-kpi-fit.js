/* Devices tab — fit each card tile's value to its tile («Устройства»).
 *
 * A tile's value+unit run (.dev-kpi-row) is laid out on one line at the design
 * font size (main.css --kpi-val-fs / --kpi-unit-fs). In the narrow tiles of the
 * 3-column card grid, and of the 3-column metric grid on a ~400 px phone, a
 * long run («780.5 mmHg», «1234.5 кВт·ч», «Остановлена») is wider than the tile;
 * the centred, overflow-hidden row then cut it on BOTH sides («.12 mg/m»,
 * «48.0»). This script fits the run to its tile, per tile:
 *   1. the unit wraps under the number (data-fit="wrap") when the tile has the
 *      spare height for that line anyway and the number is then larger than
 *      it would be on one line — the tile does not grow;
 *   2. otherwise one line, value and unit scaled together (value floor 10 px,
 *      unit floor 9 px);
 *   3. past the value floor the unit wraps regardless and the number is
 *      fitted alone — a meter reading is never cut;
 *   4. only a number wider than the tile at 10 px ellipsizes; its full text
 *      then goes to the value's title.
 * Fonts only ever shrink, and only the step-3 fallback can add a line.
 *
 * Independent of devices.js (no shared state): a MutationObserver on #dev-grid
 * catches every text write (poll refresh, ДТВ sensor rotation, the i18n
 * translation pass, a card rebuild) and a ResizeObserver catches a width change
 * (viewport resize, the tab being shown). Both coalesce into one
 * requestAnimationFrame pass that runs before the frame paints: reset every
 * row → read every row → write every row (two layouts per pass, no thrash).
 */
(function () {
  "use strict";

  // Floors: 10 px is the card's own 0.72rem caption size, so a fitted value
  // is never smaller than the captions around it.
  const VAL_MIN_PX = 10;
  const UNIT_MIN_PX = 9;
  // Headroom under the measured width: glyph advances do not scale exactly
  // linearly with font-size (hinting, subpixel rounding).
  const SAFETY = 0.97;

  /** Scale plan for one value+unit run (all lengths in px).
   *  avail   — the tile's content width the row may occupy;
   *  gap     — the row's column gap (does not scale);
   *  valW / unitW   — natural widths at the design font size (unitW 0 = no unit);
   *  valPx / unitPx — design font sizes;
   *  spare   — free vertical room in the tile at the design size;
   *  unitLine — the unit's line height at the design size.
   *  Returns { val, unit, wrap, truncated }: the value / unit font scale (<= 1),
   *  whether the unit goes under the number, and whether the number still
   *  exceeds the tile at its floor (the CSS ellipsis then shows). */
  function kpiFitScale(avail, gap, valW, unitW, valPx, unitPx, spare, unitLine) {
    const one = { val: 1, unit: 1, wrap: false, truncated: false };
    if (!(avail > 0) || !(valW > 0)) return one;
    const u0 = unitW > 0 ? unitW : 0;
    const g = u0 > 0 ? gap : 0;
    if (valW + u0 + g <= avail) return one;
    const vMin = Math.min(1, VAL_MIN_PX / valPx);
    const uMin = Math.min(1, UNIT_MIN_PX / unitPx);
    // One line: value and unit shrink together, the unit held at its floor.
    const budget = avail * SAFETY - g;
    let v1 = budget / (valW + u0);
    let u1 = v1;
    if (u0 > 0 && u1 < uMin) {
      u1 = uMin;
      v1 = (budget - u0 * u1) / valW;
    }
    // Unit under the number: each fitted to the full tile width on its own line.
    let v2 = Math.min(1, (avail * SAFETY) / valW);
    const u2 = u0 > 0 ? Math.max(uMin, Math.min(1, (avail * SAFETY) / u0)) : 1;
    const wrapFree = u0 > 0 && spare >= unitLine * u2;
    if (wrapFree && v2 > v1) {
      const cut = v2 < vMin;
      return { val: floor3(Math.max(v2, vMin)), unit: floor3(u2), wrap: true, truncated: cut };
    }
    if (v1 >= vMin) return { val: floor3(v1), unit: floor3(u1), wrap: false, truncated: false };
    const truncated = v2 < vMin;
    if (truncated) v2 = vMin;
    return { val: floor3(v2), unit: floor3(u2), wrap: u0 > 0, truncated: truncated };
  }

  function floor3(x) {
    return Math.floor(x * 1000) / 1000;
  }

  function reset(row) {
    row.style.removeProperty("--kpi-fit");
    row.style.removeProperty("--kpi-unit-fit");
    row.removeAttribute("data-fit");
  }

  /** Free vertical room in the tile: its content box minus the extent of its
      children (1 px margins kept aside). */
  function spareHeight(kpi, kcs) {
    const kr = kpi.getBoundingClientRect();
    let top = Infinity;
    let bottom = -Infinity;
    Array.from(kpi.children).forEach((c) => {
      const r = c.getBoundingClientRect();
      if (r.height <= 0) return;
      top = Math.min(top, r.top);
      bottom = Math.max(bottom, r.bottom);
    });
    if (!(bottom > top)) return 0;
    const contentH =
      kr.height -
      (parseFloat(kcs.paddingTop) || 0) -
      (parseFloat(kcs.paddingBottom) || 0) -
      (parseFloat(kcs.borderTopWidth) || 0) -
      (parseFloat(kcs.borderBottomWidth) || 0);
    return contentH - (bottom - top) - 2;
  }

  function measure(row) {
    const kpi = row.parentElement;
    const val = row.querySelector(".dev-kpi-val");
    if (!kpi || !val || kpi.clientWidth <= 0) return null;
    const unit = row.querySelector(".dev-kpi-unit");
    const kcs = getComputedStyle(kpi);
    const avail =
      kpi.clientWidth - (parseFloat(kcs.paddingLeft) || 0) - (parseFloat(kcs.paddingRight) || 0);
    // scrollWidth is the full text even while the CSS ellipsis truncates it;
    // the Range width adds the sub-pixel precision scrollWidth rounds away.
    const rg = document.createRange();
    rg.selectNodeContents(val);
    const valW = Math.max(rg.getBoundingClientRect().width, val.scrollWidth);
    const unitW = unit ? unit.getBoundingClientRect().width : 0;
    const ucs = unit ? getComputedStyle(unit) : null;
    const unitPx = ucs ? parseFloat(ucs.fontSize) || 16 : 16;
    return {
      val: val,
      plan: kpiFitScale(
        avail,
        parseFloat(getComputedStyle(row).columnGap) || 0,
        valW,
        unitW,
        parseFloat(getComputedStyle(val).fontSize) || 16,
        unitPx,
        spareHeight(kpi, kcs),
        ucs ? parseFloat(ucs.lineHeight) || unitPx * 1.15 : 0
      ),
    };
  }

  function apply(row, m) {
    if (!m) return;
    const p = m.plan;
    if (p.val < 1) row.style.setProperty("--kpi-fit", String(p.val));
    if (p.unit < 1) row.style.setProperty("--kpi-unit-fit", String(p.unit));
    if (p.wrap) row.setAttribute("data-fit", "wrap");
    if (p.truncated) m.val.title = m.val.textContent;
    else if (m.val.hasAttribute("title")) m.val.removeAttribute("title");
  }

  let raf = 0;
  function fitAll() {
    raf = 0;
    const grid = document.getElementById("dev-grid");
    if (!grid || grid.offsetWidth <= 0) return; // hidden tab: refit when shown
    const rows = Array.from(grid.querySelectorAll(".dev-kpi-row"));
    rows.forEach(reset);
    const ms = rows.map(measure);
    rows.forEach((row, i) => apply(row, ms[i]));
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(fitAll);
  }

  function init() {
    const grid = document.getElementById("dev-grid");
    if (!grid || typeof MutationObserver === "undefined") return;
    // Attributes are not observed: the title/style writes of a pass never
    // re-trigger it.
    new MutationObserver(schedule).observe(grid, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    if (typeof ResizeObserver !== "undefined") {
      let lastW = -1;
      new ResizeObserver(() => {
        const w = grid.clientWidth;
        if (w !== lastW) {
          lastW = w;
          schedule();
        }
      }).observe(grid);
    }
    schedule();
  }

  init();
})();
