# Backlog — SA-02m web interface

Recorded out-of-scope findings and deferred work (`.ai-dev/procedures/backlog.md`
owns the format). One item per line block: `- [OPEN|RESOLVED] <date> <one-line item>`.

## Security (whole-project audit 2026-07-12) — device-side, need Operator decision

- [OPEN] 2026-07-12 **[HIGH] S1/Y1 static session token.** `session_token=cyntron_session` is a hardcoded constant, identical on every device, committed; every CGI + nginx auth_request authorizes on it → any LAN client sets the cookie and is fully authenticated, password decorative. Fix: per-session random token at login, server-side store with expiry, timing-safe compare. (login.cgi, auth_check.cgi + ~28 CGIs, tools/sa02m_common.py)
- [OPEN] 2026-07-12 **[HIGH] S2 cloud.cgi unauth + sed injection.** No check_auth, nginx /cgi-bin/ has no auth_request; POST `SERVER` interpolated raw into `sed -i` → unauthenticated RCE / cloud-endpoint repoint. Fix: add check_auth; hostname allow-list; value-safe config write.
- [OPEN] 2026-07-12 **[HIGH] S3 apply.cgi ifupdown injection→root.** IP/NETMASK/GATEWAY/DNS from POST via decode() (%0a→newline) written unvalidated into /etc/network/interfaces.d/eth0.conf → pre-up hook runs as root. Fix: allow-list every field (regex) before write.
- [OPEN] 2026-07-12 **[MED] S4 default password `cyntron`** hardcoded across deploy/ssh tooling; login.cgi/web_creds.cgi fail-OPEN to admin/cyntron when /etc/sa02m_web.env missing. Fix: env-only defaults; fail-closed on missing auth file.
- [OPEN] 2026-07-12 **[MED] S5 plaintext web password** in /etc/sa02m_web.env, cleartext compare. Fix: salted hash + hash compare.
- [OPEN] 2026-07-12 **[MED] S6 mqtt_scan.cgi** QUERY_STRING port/baud/max_addr → sudo python scanner, no allow-list on port path. Fix: validate port `^/dev/…$` + numeric bounds before root invoke.
- [OPEN] 2026-07-12 **[MED] Y4 USB autorun.sh as root.** storage-mount.sh:180-183 auto-runs ${MOUNT}/autorun.sh on insert → untrusted media = root RCE. Fix: opt-in flag, default off.
- [OPEN] 2026-07-12 **[LOW] S7** login.cgi non-constant-time password compare. **[LOW] S8** status.cgi cpu/temp/ram/disk served unauthenticated (telemetry leak — confirm intended). **[LOW] S9** lib_web_auth.sh legacy `. env` sources config (code-exec-on-config). **[LOW] S10** cookie client-side-only expiry (folds into S1).

## System / installer (audit 2026-07-12) — device-side

- [OPEN] 2026-07-12 **[HIGH] Y2 installer resets static IP.** scripts/02-network.sh:38-46 rewrites eth0.conf unconditionally, default factory 192.168.1.136 → re-running install.sh (the upgrade path) makes a deployed device unreachable. Fix: guard eth0 write like eth1 (`[ ! -f ]` / explicit --ip).
- [OPEN] 2026-07-12 **[HIGH] Y3 autoformat fail-open.** storage-mount.sh:11,16 internal fallback STORAGE_AUTO_FORMAT=1 (opposite of shipped conf =0) → NTFS partition that fails to mount is reformatted to exFAT (data loss) if conf missing. Fix: fallback 0.
- [OPEN] 2026-07-12 **[MED] Y5 OTA leaves stale files.** sa02m-web-update-apply.sh:61 cp -a without clearing WEB_ROOT → removed CGIs persist. Fix: rsync --delete / purge first (mirror 03-webserver.sh).
- [OPEN] 2026-07-12 **[MED] Y6 storage-mount@.service TimeoutStartSec=8** < mkfs+retry budget → kill mid-mkfs risks corruption. Fix: raise timeout / move format off start path.
- [OPEN] 2026-07-12 **[LOW] Y7** module scripts lack `set -euo pipefail` (set -e doesn't cross the per-module bash). **[LOW] Y8** failure-monitor probes status.cgi every 5s (forks CGI 12×/min). **[LOW] Y9** install.sh banner hardcodes v1.0.3 (stale, separate installer-version literal).

## Frontend (audit 2026-07-12) — this repo, code changes

- [OPEN] 2026-07-12 **[MED] F1/F2/F3 i18n gaps** — kernel-control, CPU-frequency, and service-control strings are uiT()-wrapped but absent from i18n.js DICT (render Russian in EN mode); svcCtlErrorMessage map is RU-only. Fix: add DICT/REGEX entries. (app.js ~2648-2991)
- [OPEN] 2026-07-12 **[MED] F4 dead code** — 13 unused fetch*Widget / applyStatus / applyMainStatusBundle wrappers in app.js (~1445-1906); scheduler fetches parts directly. Fix: delete (decompose-adjacent, run through the loop — not fixup).
- [OPEN] 2026-07-12 **[MED] F5 flasher i18n** — AI-channel config modal labels RU in innerHTML, no DICT (~556 Cyrillic lines in flasher.js, broad gap).
- [OPEN] 2026-07-12 **[MED] F6 cloud.html badge()** interpolates server-originated d.service_active into innerHTML unescaped (91,109). Fix: escape/textContent.
- [OPEN] 2026-07-12 **[LOW] F7** flasher.js:1378 d.address unescaped (siblings escaped). **[LOW] F8** 'Ethernet № 1/2' standalone titles no DICT key. **[LOW] F9** misc untranslated (USB-reset title, storage-mount toast, 'Применяю…').
- [OPEN] 2026-07-12 **[LOW] F10 decompose** — flasher.js 4411 / app.js 3464 god-files; split by responsibility (`.ai-dev/procedures/decompose.md`).
- [OPEN] 2026-07-12 **[LOW] F11** mqtt.js raw getElementById().innerHTML chains (~1725-1922), safe only because modals static.

## Docs / hygiene (audit 2026-07-12) — mostly fixed inline; remainder

- [OPEN] 2026-07-12 **[LOW] D8/D9 vendored-doc pointers** — `.ai-dev/quality/run.mjs:11` usage says `src/quality/run.mjs`; `.ai-dev/procedures/backlog.md` cites non-existent `docs/decisions/multi-user-mode.md` + `src/adapter/forge-map.json`. These are vendored ai-dev files — do NOT edit locally (upstream drift); route as a downstream-feedback note on the next protocol upgrade.
- [RESOLVED] 2026-07-12 KPI summary row hidden — documented in CHANGELOG 1.0.4.1; ui-style skill + this backlog record it. Re-enable/remove is a future product decision (kept OPEN as a product question below).
- [OPEN] 2026-07-12 **[product]** KPI-ряд on the dashboard is hidden (`index.html` `.kpi-row` display:none) — decide: rework content to be informative, or delete the markup/JS/CSS.

## Fixed inline during the audit (2026-07-12)
- cookies.txt (committed session_token) removed + gitignored.
- README version badge 1.0.3.20 → 1.0.4.1; CHANGELOG header 1.0.3/Апрель → 1.0.4/Июль.
- CHANGELOG: added missing 1.0.4.0 section; KPI-row honesty note in 1.0.4.1.
- Doc corrections: `main` part + swap added to scheduler docs (skill + domain); MR-02m slug/path note; tools.json _row_shape `covers` field.
