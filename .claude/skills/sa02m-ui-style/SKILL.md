---
name: sa02m-ui-style
description: Use BEFORE any visual change — new widget, new color, icon, badge, theme work, or when the Operator reports readability issues — SA-02m web UI design system: dark/light theme tokens, icon chips (w-ico/chip-*), KPI tiles, widget skeletons (title/val/sub/bar-row), status pills with dots, nav states, WCAG AA contrast floors and verified color pairs.
---

# SA-02m UI style

Floors live in `docs/agent-rules/web-code-rigor.md ## CSS / UI floors`; this
skill is the working vocabulary. All styling is design tokens in
`www/network_config/static/css/main.css` `:root` + `html[data-theme="light"]`.

## Themes

Dark is default; light via `data-theme="light"` on `<html>` (persisted in
localStorage `sa02m-theme`, applied by an inline head script pre-CSS).
EVERY new token/color gets values in BOTH blocks. Meter colors (`--meter-*`)
are theme-invariant by design (arc/bar readability).

## Token vocabulary

- Surfaces: `--bg`, `--bg-nav`, `--bg-card`, `--bg-panel`, `--bg-input`,
  `--bg-hover`; borders `--border`, `--border-md`, `--border-li`.
- Text: `--text`, `--text-sec` (dark value `#a5a5ac` — raised for 6.3:1 AA,
  don't regress), `--text-dim`.
- Accents: `--cyan` (+`-btn`,`-hover`,`-dim`), `--green`, `--yellow`, `--red`,
  `--orange`; badges `--badge-ok-*`, `--badge-err-*`; warn button `--warn-btn-*`.
- Icon chips: `--chip-{cyan,green,orange,red}-{bg,fg}` — tinted rounded
  squares; every pair verified ≥4.6:1 in both themes.
- Radius scale `--radius-xs..xl,pill`; shadows `--shadow-sm/-/lg/glow`.

## Component skeletons (copy, don't invent)

- **Widget**: `.widget` card → `.widget-title` (13 px uppercase; `has-ico`
  variant = flex with `.w-ico .w-ico-<color>` 26 px chip + `<span>` label) →
  `.widget-val` 26 px (add `.widget-val-centered` to center; CPU/temp/Uptime
  are centered) → `.widget-sub` hint → `.bar-row` (`.bar-track`>`.bar-fill` +
  `.bar-meta` scale labels). CPU/temp/RAM/disk all follow this ONE pattern.
- **Status pill**: `.eth-state.up/.down` and `.badge-ok/.badge-err` carry a
  ::before currentColor dot («• Линк»). Ethernet titles: `.eth-hdr-title.has-ico`
  wraps the pill to its own row instead of breaking the label.
- **KPI tiles**: `.kpi-row`/`.kpi-tile` exist but are HIDDEN (Operator call,
  1.0.4.1 — display:none inline); JS `kpiSet*` still updates them. Don't
  delete without an Operator decision (backlog item).
- **Nav**: active item = translucent `--cyan-dim` fill + `--cyan` text (the
  Operator explicitly rejected a solid accent fill as too loud).

## Copy rules (Operator-set, 1.0.4.1 series)

- Titles and hints fit ONE line at ~200 px card width. Prefer shorter labels:
  «Температура», «Нагрузка», «USB-flash», «DO», «LED (красный)», «В норме» /
  «Выше нормы», «Загрузка ядер». Full wording goes to `title=` tooltips.
- Values centered on gauge-like widgets (CPU, temp, Uptime).
- Icons: stroke SVG 24×24 viewBox inside chips; recognizable object symbols
  (RJ45 = IEC three-squares-on-bus symbol; USB flash vs modem swap via
  `setUsbWidgetIcon`).

## Contrast floor

WCAG 2.1: ≥4.5:1 normal text, ≥3:1 large/bold, BOTH themes; new pairs get
computed ratios in the plan/review (script recipe:
`web-diagnostic-tools.md ## Contrast`). Known verified pairs: text-sec 6.3:1
dark card; chips 4.6–6.8:1; badge greens/reds 5.2–8.1:1.

## Thresholds (color semantics)

CPU: <60 cyan, ≥60 yellow, ≥80 red (`threshColor(v,60,80)`).
Temp: <70 green, ≥70 yellow, ≥80 red; bar scale 30–100 °C (`tempToGaugePct`).
RAM/disk bars: 70/90. KPI warnings counted at CPU≥80, t≥80, RAM≥90, disk≥90.
Change thresholds only with an Operator decision — they are alarm semantics.
