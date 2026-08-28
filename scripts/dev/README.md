# Dev harness — UI characterization oracle

Dev-only tooling (never deployed to a device). Its job: prove that a
behaviour-preserving refactor of the frontend bundles changed **nothing** the
user can observe. Used as the net for backlog **F10** (decomposing `app.js` /
`flasher.js` into per-responsibility plain scripts).

**It is not a quality-registry row, and cannot be one.** It needs a live board
(it proxies every `/cgi-bin/*` call to `DEVICE_URL`) and the web password, so
it can never run in CI. It carried a registry row (`headless-smoke`) from
1.0.5.x until 1.0.6.24; that row skipped in every environment it was ever
invoked in — including CI, which installs neither playwright nor the secret —
so it reported PASS without checking anything (2026-08-28 audit, finding C4).
The row was retired rather than left claiming coverage. The CI-runnable floors
it was nominally standing in for are now real rows that need no browser:
`html-id-contract` (the DOM id contract) and `i18n-dict-contract` (visible
strings); rendered-geometry and contrast stay with `ui-layout`, honestly
labelled dev-only.

**The baseline is per-refactor, never a committed standing artifact.** Capture
it yourself, from the tree you are about to change, immediately before the
first split step — a baseline taken weeks earlier reports every legitimate
change since as a regression, which is how the committed one (last written at
1.0.5.81, against a tree that had moved on to 1.0.6.23) became worse than
useless. `baseline/` is git-ignored for that reason.

## How it works

`characterize-ui.mjs`

1. Serves the **repo** copy of `www/network_config` on `127.0.0.1:8099` (so it
   characterizes the exact code you are refactoring), and **proxies** every
   `/cgi-bin/*` call — and any non-static path — to a live board. The backend
   responses are therefore real.
2. Logs in through the real `login.cgi` and drives headless Chromium across the
   whole surface: every **tab × theme × board-variant**.
3. Records per cell, and diffs against a saved baseline:

   | signal     | what it catches                                   | gate  |
   |------------|---------------------------------------------------|-------|
   | `globals`  | a `window.*` name that stops being defined / changes type — the #1 load-order failure | **hard** |
   | `errors`   | new `console.error` / `pageerror` / failed request | **hard** |
   | `dom`      | a container (topbar / sidebar / active pane) whose **structure** changed (text & volatile attrs stripped) | **hard** |
   | `shot`     | full-page screenshot hash — live data makes pixels non-deterministic, so this is eyeball-only | soft |

Headless is necessary but **not sufficient** for a global-scope reorg — the ship
beat still requires on-device click-through.

## Run

```sh
cd scripts/dev
npm install                 # once
npx playwright install chromium   # once

export SA02M_WEB_PASS='<web-ui password for admin>'
# optional: DEVICE_URL=http://192.168.1.136:9999  PORT=8099

# 1) capture the oracle BEFORE touching any bundle — every time, from the tree
#    you are about to refactor. There is no committed baseline to reuse.
npm run baseline            # writes baseline/manifest.json (git-ignored)

# 2) after each split step, re-run and diff:
npm run check               # exit 0 = PASS, exit 1 = FAIL (revert the step)

# 3) on-device end-to-end check (drives the DEPLOYED board directly, no local
#    serve/proxy) — the ship-beat verification headless-local can't stand in for:
node characterize-ui.mjs --target device --compare baseline
```

The `globals` oracle compares only the names OUR scripts add (dashboard globals
minus the login-page environment reference), so a localhost baseline (a secure
context) and a plain-HTTP device run (not a secure context, so ~250 Web-API
constructors are absent) still compare cleanly.

`baseline/manifest.json` and the per-run screenshots and manifests in
`artifacts/` are all git-ignored: both are captured per session against a
specific board, and a committed copy of either drifts silently.

## Notes

- Read-only against the board: only GET polling + one login POST. The harness
  never clicks apply/submit/flash controls.
- `applyVariantVisibility()` is called client-side to exercise both `sa02m-1eth`
  and `sa02m-2eth` without POSTing a variant change to the device.
- The clock is frozen (`Date`) so time-derived rendering is at least run-stable.
