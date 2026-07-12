# Deployment & ops

> The one home for **how this project reaches production and recovers from a bad deploy** — seeded at inception (day-zero ops) or filled by doc bootstrap from the tree, kept current as the deploy path changes. A deploy/release/ops action follows this doc **step by step** (`src/agents/procedures/deployment.md`); if it is absent or stale, the deploy STOPS until it is created/confirmed — never improvised.
>
> Current state only — supersede a changed step, don't accumulate. Secret **locations**, never values. `[?]` for any bound not yet measured or tested. ~40–80 lines of normal prose. Delete a section that genuinely does not apply.
>
> *Delete this guidance block when filling.*

## 1. Deploy procedure — the exact steps, in order

`<the ordered steps that take a reviewed change to production: build/package → release/push → activate. Per step: its precondition (what must be true before it runs), the command or action, and how to tell it succeeded. This is the runbook the deploy is run AGAINST — concrete enough to follow without improvising.>`

## 2. Environments

`<the environments this deploys to (e.g. staging, production), how they differ, and which one a given step targets. Name the promotion order if a change goes staging → prod.>`

## 3. Rollback — how to undo a bad deploy

`<the exact steps to revert to the last-known-good state, and how fast they run. The production incident this whole doc exists to prevent is a deploy gone wrong with no rehearsed way back — this section is not optional. Note whether rollback has ever actually been tested ([?] if never).>`

## 4. Secrets & credentials — locations, never values

`<where each deploy-time secret lives (the secret store, the env-var name, the key file path) — the LOCATION, never the value. How they are injected at deploy time. A value copied here is a leak.>`

## 5. How a production failure becomes visible

`<the signal that tells you production is unhealthy: the monitor, the alert, the dashboard, the log to watch. Without this, a bad deploy is silent until a user reports it. Name who is notified and how.>`

## 6. Post-deploy verification

`<the concrete check that confirms the deploy worked, run right after every deploy: the smoke test, the health endpoint, the key user path to walk. Pass/fail criteria, not "looks fine".>`

## 7. Ownership & funding

`<who owns ops (runs the deploy, holds the credentials, answers the page), who funds the infrastructure, and the backup owner — and whether restore has ever been tested ([?] if never).>`
