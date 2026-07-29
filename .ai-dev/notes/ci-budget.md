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
  .ai-dev/quality/run.mjs build` + `review` (full, never `--touched` — see
  the backlog entry on its vacuity) reproduce the CI job exactly, for free.

## Where the runs actually go (measured 2026-07-23)

`web-quality` is the only workflow that fires in practice (the kernel
workflow is path-filtered to `kernel-port/**` and stays dormant). It triggers
on BOTH `pull_request` → main AND `push` → main, so **each shipped feature
costs two runs**: one on the PR, one re-validating the identical tree after
the squash-merge.

- **Saving applied in 1.0.5.52: the `push: branches: [main]` trigger dropped**
  — halves the run count with no gate loss. Branch protection requires the
  `quality` context, which is evaluated on the PR; direct pushes to `main` are
  blocked (`enforce_admins: true`), so nothing reaches `main` unvalidated.
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
