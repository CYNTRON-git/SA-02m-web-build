# CI budget — push economically (Operator rule, 2026-07-23)

Durable project knowledge. The Operator pays for GitHub Actions time and hit
the monthly ceiling in July (308 runs). **Every push is a spend decision.**

## The rule

- **Batch, don't drip.** Land a feature's fixes as one branch with one push,
  never a push per fix. A firefight batches into one branch + one Reviewer
  pass (`PROTOCOL.md` beat 4 already licenses this) and therefore one CI run.
- **No re-push to "fix a typo in the CHANGELOG"** — fold it into the next
  branch unless it blocks the merge.
- **Doc-only / note-only changes never get their own push.** They ride the
  next feature branch. This file itself did.
- Local gates are the substitute for CI iteration: `node
  .ai-dev/quality/run.mjs build` + `review` (full, never `--touched`: CI runs
  the full set, and `--touched` selects a subset) run the
  same registry as the CI job, for free — but not on the same host:
  `quality-gate-environment.md` lists the rows that skip or fail locally, and
  CI stays the authority.

## Where the runs actually go (measured 2026-07-23)

`web-quality` is now the only workflow in the repo at all. (Until 2026-08-06
there was a second one, `build-sa02m-kernel`; it was path-filtered to
`kernel-port/**` and stayed dormant, and it went away with the dead kernel
pipeline — `.ai-dev/notes/kernel-line.md`.) It triggers
on BOTH `pull_request` → main AND `push` → main, so **each shipped feature
costs two runs**: one on the PR, one re-validating the identical tree after
the squash-merge.

- **Saving applied in 1.0.5.52: the `push: branches: [main]` trigger dropped**
  — halves the run count with no gate loss, because branch protection blocks
  a direct push. **Live state, measured 2026-10-24** (`gh api
  …/branches/main/protection`, audit 2026-10-24 «Branch-protection floor»):
  required status check `quality` (= job `quality`, `web-quality.yml`),
  `strict: true`, `enforce_admins: true`, force pushes and deletions off — so
  no commit lands on `main` without a green `quality` check, admins included
  (there is no PR-review rule; the Reviewer stamp is the review). **The gate is CI**: Actions runs execute
  (green since at least 2026-10-14, 1.0.7.2 / 1.0.7.3; PR #217). The local
  suite is the free pre-push iteration, not the gate —
  `quality-gate-environment.md` says where it differs from CI. The one direct
  push that landed before protection was enforced (`1dfa503`, 1.0.6.46) still
  owes its retro-review (backlog).
- **Cost added in 1.0.6.49 (audit H3):** the review beat now installs the
  scripts/dev Playwright harness (`npm --prefix scripts/dev ci`) and chromium
  (`playwright install --with-deps chromium`, ≈150 MB) so the three headless
  rows run for real. The browser download is cached with `actions/cache@v4`
  keyed on `scripts/dev/package-lock.json`, so it is paid once per lockfile
  change, not per run; the apt half of `--with-deps` and the three chromium
  boots are paid every run.
- **Do NOT add `paths-ignore` to that workflow** while `quality` is a required
  status check: a filtered-out run leaves the check permanently "expected but
  not run" and the PR can never merge.

## Unverified premise (flagged, not resolved)

The repository is **public** (`gh api repos/... --jq .private` → `false`), and
GitHub bills no minutes for standard runners on public repos. The July ceiling
the Operator observed may therefore belong to a different repo (the sibling
`cloud` repo is a candidate) or a different meter. Worth confirming before
optimising further — but the two-runs-per-feature waste above is real
regardless, and the batching rule costs nothing to keep.
