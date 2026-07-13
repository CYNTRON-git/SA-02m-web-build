# Dev harness — UI characterization oracle

Dev-only tooling (never deployed to a device). Its job: prove that a
behaviour-preserving refactor of the frontend bundles changed **nothing** the
user can observe. Used as the net for backlog **F10** (decomposing `app.js` /
`flasher.js` into per-responsibility plain scripts).

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

# 1) capture the oracle BEFORE touching any bundle:
npm run baseline            # writes baseline/manifest.json (committed)

# 2) after each split step, re-run and diff:
npm run check               # exit 0 = PASS, exit 1 = FAIL (revert the step)
```

`baseline/manifest.json` is committed (small, structural). Per-run screenshots
and manifests land in `artifacts/` (git-ignored).

## Notes

- Read-only against the board: only GET polling + one login POST. The harness
  never clicks apply/submit/flash controls.
- `applyVariantVisibility()` is called client-side to exercise both `sa02m-1eth`
  and `sa02m-2eth` without POSTing a variant change to the device.
- The clock is frozen (`Date`) so time-derived rendering is at least run-stable.
