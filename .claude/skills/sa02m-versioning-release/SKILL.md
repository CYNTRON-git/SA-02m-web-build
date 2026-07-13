---
name: sa02m-versioning-release
description: SA-02m web version and release flow — branch name IS the version, +1 branch per release, sync-app-version.py (VERSION / APP_VERSION / ?v= cache-bust), CHANGELOG format, «Обновление веб» semver self-update, deploy paths. Use when creating a release branch, bumping versions, editing CHANGELOG, or debugging stale-cache / update-not-offered symptoms.
---

# SA-02m versioning & release

## The contract

- **git branch name == web version** (`1.0.4.1`). A new release is a new
  branch **+1** from the LATEST version branch (list them:
  `git ls-remote --heads origin | grep -E 'refs/heads/[0-9]'`). Never stack a
  new version's work on an old branch.
- Three version homes must agree (quality row `version-consistency`):
  1. `www/network_config/VERSION` (first non-comment line)
  2. `const APP_VERSION` in `static/js/app.js`
  3. every `?v=` query string in `index.html` + `login.html`
- The ONLY writer is `python3 scripts/sync-app-version.py` (resolves from the
  branch name, else VERSION). `--check` exits 1 on skew. Never hand-edit
  `?v=` strings individually.

## Release checklist

1. `git checkout -b <X.Y.Z.W+1> origin/<latest>`; push with `-u`.
2. `python3 scripts/sync-app-version.py` → commit the three homes.
3. Work; keep commits per `docs/agent-rules/git-commits.md`.
4. `CHANGELOG.md`: prepend `## <version> - <one-line summary> (<месяц> <год>)`
   section, Russian, grouped by subsystem with **file:** bullets (match
   existing sections).
5. Ship gate: `node .ai-dev/quality/run.mjs build` + `review` green.

## Why the cache-bust matters

Devices cache bundles aggressively (embedded browsers). A JS/CSS change
without the `?v=` bump ships a NEW backend against an OLD cached bundle —
the classic "works locally, broken after deploy". Symptom dispatch:
`web-diagnostic-tools.md ## Symptom → tool`.

## Self-update («Обновление веб»)

`web_update_check.cgi` compares deployed vs remote semver — the update is
offered only when remote **>** deployed (`sa02m-web-update-check.sh` logic).
Consequences: never ship a lower/equal version string with different content;
a hotfix needs a version bump to propagate.

## Deploy paths

- Full install/upgrade on device: `sudo ./install.sh` (idempotent; the repo IS
  the overlay).
- Web-only fast deploy: `scripts/update-www-only.sh` (see its header) or SFTP
  of `www/network_config` — allowed for iteration; a release still goes
  through the branch + version flow. Repo-owned files change through git
  (PROTOCOL invariant 4), never edited in place on a device.
