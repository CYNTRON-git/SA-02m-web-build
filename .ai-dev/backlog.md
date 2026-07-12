# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Reconciled
2026-07-12 after the whole-project audit + two remediation batches (all changes
on branch `1.0.4.1`; RESOLVED = code landed + reviewer-approved, but device-side
items still need on-device verification before deploy).

## Open

- [OPEN] 2026-07-12 **[LOW] F10 — decompose the god-files.** `flasher.js` (4411)
  and `app.js` (3326) are multi-responsibility. Plan: `.ai-dev/plans/f10-decompose.md`.
  Behaviour-preserving; needs a characterization net + per-tab on-device
  verification (ES modules forbidden — split into plain global-scope scripts).
- [OPEN] 2026-07-12 **[LOW] Y7-b — `set -u` in installer modules.** `set -o pipefail`
  landed; bare `set -u` deferred — an unset-var abort mid-provision could brick a
  fresh install. Add only with an on-device install run.
- [OPEN] 2026-07-12 **[LOW] D8/D9 — vendored ai-dev doc pointers.**
  `.ai-dev/quality/run.mjs` usage line + `.ai-dev/procedures/backlog.md` cite
  paths that don't exist in this repo. These are vendored framework files — do
  NOT edit locally (upstream drift); route as downstream-feedback on the next
  protocol upgrade.
- [OPEN] 2026-07-12 **[task] On-device verification (pre-deploy).** All device-
  side changes tested only locally/logically. Verify on a real SA-02m before
  deploy: login (hashed + legacy plaintext), password change, network apply +
  re-run `install.sh` preserves the static IP, cloud activation, storage
  autoformat off by default. A www-only OTA needs `/etc/sa02m_web.env` present
  (login now fails closed).

## Resolved — security (audit + remediation batches)

- [RESOLVED] 2026-07-12 **[HIGH] S1/S10 static/forgeable session token** →
  per-session random token, server-side store (`/run/sa02m-web-sessions`, TTL),
  fail-closed. (lib_web_auth.sh, login/logout/auth_check + 27 CGIs)
- [RESOLVED] 2026-07-12 **[HIGH] S2 cloud.cgi** unauth + sed injection → auth gate
  + hostname allow-list.
- [RESOLVED] 2026-07-12 **[HIGH] S3 apply.cgi** ifupdown newline injection→root →
  IPv4/DNS allow-list before the interfaces.d write. (lib_web_validate.sh)
- [RESOLVED] 2026-07-12 **[MED] S4 fail-open default creds** → login/web_creds fail
  closed on missing env; no built-in admin/cyntron fallback.
- [RESOLVED] 2026-07-12 **[MED] S5 plaintext password** → `$6$` SHA-512 crypt hash
  (SA02M_WEB_PASS_HASH), backward-compatible with legacy plaintext, best-effort
  fallback (never locks out). Both auth-lib copies + login/web_creds/commit/
  installer.
- [RESOLVED] 2026-07-12 **[MED] S6 mqtt_scan.cgi** → port/baud/max_addr allow-list
  before the root scanner.
- [RESOLVED] 2026-07-12 **[LOW] S7** constant-time-ish password compare.
- [RESOLVED] 2026-07-12 **[LOW] S8 public telemetry** → status.cgi requires a
  session for every part; dead unauth prefetch removed from login.html.
- [RESOLVED] 2026-07-12 **[LOW] S9 eval-on-config** → web_auth_read no longer
  sources the env file; safe literal parser everywhere.

## Resolved — system / installer

- [RESOLVED] 2026-07-12 **[HIGH] Y2 installer IP reset** → eth0.conf rewritten only
  on first install or explicit `--ip` (IP_EXPLICIT guard).
- [RESOLVED] 2026-07-12 **[HIGH] Y3 autoformat fail-open** → internal fallback 0
  (matches shipped conf); no reformat on a missing config.
- [RESOLVED] 2026-07-12 **[MED] Y4 USB autorun as root** → gated behind
  STORAGE_ALLOW_AUTORUN, default off.
- [RESOLVED] 2026-07-12 **[MED] Y5 OTA stale files** → rsync --delete (purge+cp
  fallback).
- [RESOLVED] 2026-07-12 **[MED] Y6 storage-mount@ timeout** → 8 s → 120 s.
- [RESOLVED] 2026-07-12 **[LOW] Y7-a** `set -o pipefail` in modules 01-09 +
  daemon-reload wrapped. (Y7-b `set -u` still Open above.)
- [RESOLVED] 2026-07-12 **[LOW] Y8 probe cadence** → HTTP/CGI probes on a 30 s
  cadence, cached for the snapshot (~6x fewer forks).
- [RESOLVED] 2026-07-12 **[LOW] Y9 installer banner** → version derived from VERSION.

## Resolved — frontend

- [RESOLVED] 2026-07-12 **[MED] F1/F2/F3 i18n gaps** (kernel/CPU/services) → DICT/
  REGEX entries; svcCtl map translates via toast's uiT.
- [RESOLVED] 2026-07-12 **[MED] F4 dead code** → 13 unused wrappers removed from
  app.js.
- [RESOLVED] 2026-07-12 **[MED] F5 flasher i18n** → +183 DICT / +63 REGEX; 0
  uncovered visible strings (documented data/diagnostic residuals only).
- [RESOLVED] 2026-07-12 **[MED] F6 cloud.html** badge() escapes server value.
- [RESOLVED] 2026-07-12 **[LOW] F7** flasher address cell escaped.
- [RESOLVED] 2026-07-12 **[LOW] F8** 'Ethernet № 1/2' DICT keys added.
- [RESOLVED] 2026-07-12 **[LOW] F9** misc untranslated strings added.
- [RESOLVED] 2026-07-12 **[LOW] F11** mqtt.js modal DOM access null-guarded.
- [RESOLVED] 2026-07-12 **[product] KPI row** → removed (Operator decision;
  markup/JS/CSS/i18n deleted).

## Resolved — docs / hygiene (fixed inline during the audit)

- [RESOLVED] 2026-07-12 cookies.txt (committed session_token) removed + gitignored.
- [RESOLVED] 2026-07-12 README badge → 1.0.4.1; CHANGELOG header → 1.0.4/Июль;
  added missing 1.0.4.0 section; KPI honesty note.
- [RESOLVED] 2026-07-12 doc corrections: `main` part + swap in scheduler docs;
  MR-02m slug/path note; tools.json `covers` field documented.

## Protocol adoption (2026-07-12)

- ai-dev protocol v5.67.1 ported from MR-02m; project rulesets in
  `docs/agent-rules/`, skills in `.claude/skills/`, quality registry in
  `.ai-dev/quality/tools.json`. Details: `.ai-dev/notes/protocol-adoption.md`.
