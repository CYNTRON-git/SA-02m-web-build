# Web code rigor — project rules

Project-local agent rules for the SA-02m web stack (Bash CGI + vanilla JS/CSS,
no build step, embedded ARM target). Loaded into the orchestrator session via
`CLAUDE.md` `@`-import. Applies to every plan and every review touching
`www/`, `etc/`, `install.sh`, `scripts/`. Machine-facing: English; code,
comments, commits stay English (`PROTOCOL.md` invariant 5); the Operator
conversation is Russian per `docLanguage: ru`. Adapted from the MR-02m
`stm32-embedded-rigor.md` (same floors philosophy, web failure classes).

This is the **one home** for project web rigor — never restate it in a plan or
a review; cite it.

---

## Architecture (planner + reviewer)

- **Layering** — device scripts (`etc/*.sh`, systemd units) → CGI endpoint
  (`cgi-bin/*.cgi` + `lib_*.sh`) → frontend fetch (`app.js` schedulers) →
  DOM render (`apply*`/`render*`). Each layer knows only the layer beneath;
  a frontend change that needs new data adds a CGI field, never scrapes.
- **Resource model** — the target is a small ARM SoC also running CODESYS,
  MPLC4, mosquitto. CGI calls fork bash: keep endpoints O(1) processes, cache
  expensive reads (`status.cgi` part model), never add per-request `find`/
  network scans to a polled endpoint. Frontend: no polling interval below the
  existing scheduler's; no unbounded DOM growth (logs/monitors are capped).
- **No new runtime dependencies** without an explicit plan decision: no npm
  packages in the served page (self-contained JS), no new Python deps on the
  device beyond what `install.sh` provisions.

## Bash CGI floors (planner + reviewer)

- `set -u` discipline or explicit `${var:-}` defaults — an unset var must not
  silently expand to empty inside a path or a JSON value.
- **Quote every expansion**; any value interpolated into JSON goes through the
  shared escaper in `lib_*.sh` (or `printf '%s'`-based escaping) — a raw
  `echo "\"$VAL\""` with user-influenced content is a finding.
- **Untrusted input**: everything from `QUERY_STRING`, POST body, cookies is
  attacker-controlled on the LAN. Validate against an allow-list (regex/enum)
  BEFORE using in a path, a `systemctl` unit name, or a shell word. A CGI
  interpolating a request value into a command line unvalidated is a
  **security finding**, not a style nit.
- **Auth first**: every state-mutating endpoint sources the auth lib and
  answers an invalid session with an error JSON (`ok:false`,
  `unauthorized`) and exits BEFORE doing work (transport stays HTTP 200 —
  the project idiom; see `docs/contracts/mqtt-set-endpoint.md`).
- **Timeouts everywhere**: any call that can hang (curl to the flasher daemon,
  `systemctl` on a wedged unit, an i2c read) carries `timeout N` or the tool's
  own timeout flag. fcgiwrap/nginx read timeouts are finite — a hung endpoint
  blocks the UI widget it feeds.
- Long operations return `pending` immediately and finish in the background
  (`services_ctrl.cgi` pattern); a polled endpoint must answer within its
  poll period.

## Frontend floors (planner + reviewer)

- **id contract** — every `getElementById` id in JS exists in `index.html`
  (and vice versa for dynamic content); a rename touches both plus any CSS
  hook. The reviewer greps the id across `www/` on every rename.
- **Null-safe DOM access** — helpers (`setText`, `setHtml`) already guard;
  raw `document.getElementById(x).foo` without a guard in new code is a
  finding (widgets are hidden per HW variant — the element may not exist).
- **Escape before innerHTML** — any server- or user-originated string rendered
  via `innerHTML` goes through `escHtml()`. `textContent` is the default.
- **Re-render survival** — content inside containers that `render*` functions
  rebuild (services list, RS-485 grid, USB widget title) is owned by the
  renderer; static decorations live outside those nodes (see
  `sa02m-domain.md ## Dashboard status polling`).
- **i18n completeness** — every new visible Russian string: static markup may
  rely on the DICT observer, dynamic JS strings use `uiT()`; both need a DICT
  entry in `i18n.js`. A shipped string with no EN translation is a finding.
- **No layout jumps** — widgets reserve space for late-arriving data
  (`*-reserved` classes, min-heights); a new widget that reflows the grid on
  first poll response is a finding.
- **ES5-leaning syntax** in device-served JS: the UI is opened from embedded
  browsers and older WebKit — avoid optional chaining/nullish coalescing in
  new code unless already present in that file (app.js mixes; match the
  surrounding file), and never add module syntax (`import`) — bundles are
  plain scripts.

## CSS / UI floors (planner + reviewer)

- **Design tokens only** — colors come from `:root` custom properties in
  `main.css` (`--cyan`, `--chip-*`, `--meter-*`, …), with BOTH themes updated:
  every new token gets a dark value and a `html[data-theme="light"]` value.
  A hard-coded hex in a component rule is a finding (exception: the token
  definitions themselves).
- **Contrast floor — WCAG 2.1 AA**: normal text ≥ 4.5:1 against its actual
  background, large/bold display text ≥ 3:1, in BOTH themes. Verify with the
  checker recipe in `web-diagnostic-tools.md ## Contrast`; state the ratios in
  the plan/review for any new color pair (precedent: 1.0.4.1 KPI/chips work).
- **One-line labels** — widget titles and status hints must fit one line at
  the dashboard's minimum card width (~200 px); prefer renaming/shortening
  over wrapping (Operator rule, 1.0.4.1 series).
- Respect the existing scale: radius/shadow/spacing tokens, uppercase 13 px
  widget titles, `widget-val` 26 px values. New UI copies an existing widget's
  skeleton before inventing a new pattern.

## System scripts / installer floors (reviewer)

- `etc/*.sh` and `install.sh` changes state their idempotency: re-running the
  installer or a boot script on an already-configured device must not corrupt
  state (the installer is the upgrade path).
- A new systemd unit names its ordering/deps (`After=`, `Wants=`) and its
  failure mode; anything touching RS-485 ports respects the port-lease
  protocol (`sa02m-domain.md ## Subsystems`).
- Never edit a file on a device that the repo owns — change the repo, deploy
  via the documented path (invariant 4; deploy recipes in
  `web-diagnostic-tools.md`).

## Reviewer floor summary

An SA-02m web review passes only when:

- Every CGI input reaching a shell word / path / unit name is allow-listed;
  every JSON value escaped; auth checked before mutation; every hangable call
  bounded by a timeout.
- The id contract holds (grep on renames); DOM access is null-safe; innerHTML
  content is escaped; re-rendered containers own their content.
- Every new visible string has its i18n DICT entry; RU and EN both fit their
  one-line homes.
- New colors are token-based, themed both ways, and meet the AA contrast
  floor with stated ratios.
- The quality registry is green (`node .ai-dev/quality/run.mjs build`) —
  js-syntax + bash-cgi-syntax + version-consistency.
- A change visible in the UI was verified by the headless-render recipe
  (`web-diagnostic-tools.md ## Headless UI verification`) or the reviewer
  states why it could not be.
