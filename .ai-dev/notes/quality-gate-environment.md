# Quality-gate environment — where a local green is not a real green

Durable project knowledge (committed, team-shared). Records which quality rows
tell the truth on which machine, so a session does not mistake a skip for a
pass. The registry itself is the one home for *what* each row checks
(`.ai-dev/quality/tools.json`); this file only records *where it actually runs*.

---

## Rows that SKIP on a Windows dev box

`node .ai-dev/quality/run.mjs review` prints `PASS` for a skipped row. That is
by design — a missing optional tool must not block a dev machine — but it means
**a green local run is not evidence for these rows**:

| Row | Skips when | Real authority |
|---|---|---|
| `shellcheck` | `shellcheck` not on `PATH` | Linux CI |
| `headless-smoke` | playwright deps or `SA02M_WEB_PASS` absent | on-demand, see `scripts/dev/README.md` |
| `ui-layout` | playwright not installed | `npm run ui-layout:install` |

When reporting results, say "skipped", never "passed". A reviewer that reports a
skipped row as a pass is making a false claim about verification.

## shellcheck — version skew is real and it has already cost a CI failure

**Do not substitute `npx shellcheck` for the row and call it green.**

`npx shellcheck` resolves to the **latest** binary (0.11.0 as of 2026-08).
CI's `ubuntu-latest` uses the distro package (0.9.0 family), and on real files
they disagree.

On 2026-08-06 a change passed `npx shellcheck --severity=error` (exit 0) three
rounds running and then failed CI on three genuine `SC2218` errors ("this
function is only defined later") in a new test harness.

**What is established:** 0.9.0 flagged that file, 0.11.0 did not, on the same
bytes with the same flags. **What is NOT established:** *why*. 0.11.0 does emit
`SC2218` in general — a three-line file (`cp a b` above `cp() { :; }`) triggers
it on both versions. So the honest statement is "0.11.0 missed this file's
pattern", not "0.11.0 dropped the check". The trigger resisted isolation:
`unset -f`, repeated definitions, an unresolvable `source`, ANSI-C quoting,
heredocs and multi-line `awk` were all ruled out, and a bisect that appeared to
localise it turned out to be a truncation artifact (the slice cut mid-`awk`, and
the resulting parse error suppressed all analysis).

**Do not treat this as a known-checks difference you can reason around.** The
operational conclusion stands on the observation alone: a newer local shellcheck
can silently miss what CI catches, so a clean local run is not evidence that CI
will be clean.

Worth recording about that specific defect, because it is the reason the miss
mattered: the harness defined injection stubs (`cp`, `mv`, `mktemp`, `wc`) below
some of their call sites, so behaviour depended on textual position. A trace of
every stub invocation showed nothing was actually resolving to the wrong
binary — each stub was bracketed `define; run; unset -f`, so the live window
only ever contained the code under test. It was a **latent hazard**, not a live
wrong result: the first future case added below a definition would have inherited
a stub it never asked for.

### Getting the real row to run on Windows

WSL reproduces CI exactly:

```bash
wsl -d Ubuntu-24.04 -- sudo apt-get install -y shellcheck   # 0.9.0
```

Then put a shim named `shellcheck` on `PATH` that forwards to WSL — WSL
inherits the Windows working directory, so relative paths pass straight
through — and `bash .ai-dev/quality/checks/shellcheck.sh` runs for real instead
of skipping. Keep the shim in the session scratchpad; it is a dev-box
convenience, not a repo artifact.

**Never shim the newer `npx` binary onto `PATH` to make the row report PASS.**
That converts an honest skip into a false green on the one check CI is the
authority for.

## A row that RUNS everywhere but gives the wrong answer per shell — `sed | grep -q` under pipefail

The skip class above is not the only way a local green lies. A row can run on
BOTH machines and still disagree, when its implementation is environment-fragile.

On 2026-08-12 `no-retired-session-token` was RED on CI/WSL (GNU sed) and GREEN on
Windows Git Bash (MSYS sed) — same bytes, same flags, opposite verdict. Root
cause: the gate matched a comment-stripped file with

```sh
strip_comments < "$f" | grep -qF "$needle"      # DO NOT
```

`grep -q` exits on the first match; the still-writing `sed` then takes a SIGPIPE
and exits 141; and under `set -o pipefail` that 141 becomes the pipeline status —
so a token that WAS found reads back as "not found" (`PIPESTATUS=141 0`). GNU sed
dies on the SIGPIPE; MSYS sed did not (buffering/exit-order), so Windows was
falsely green. The race only fires when sed is mid-write as grep quits — a match
past the pipe buffer — which is why the small-`$body` sibling check passed while
the ~200-line file failed, and why it produced BOTH a false positive (pin C) and
a latent false negative (pin B would MISS a real violation).

**Rule:** never pipe a producer straight into `grep -q` (or any early-exit
consumer: `-q`, `-l`, `-m N`, `head`) under `set -o pipefail`. Capture first,
then match in-shell (a `case "$text" in *"$needle"*)` substring test needs no
subprocess at all). The gate's `stripped_has` helper is the fixed form.

## The general rule

A local substitute for a skipping row is **evidence, not proof**. Say which
tool and which version produced a result, and name CI as the authority where it
is. The remote gate exists precisely because a local judgement can be wrong in
ways the local machine cannot see.

## New rows, 1.0.6.24 — what they need and what they cost

- **`comment-mutation-proof`** (review beat) needs `git`, `tar` and `awk`. It extracts
  a pristine copy of HEAD, overlays the working-tree gates, comments out each pinned
  line and requires the gate to fail. **~40 s on Windows** — the largest single row we
  have added; budget it when judging CI time (`.ai-dev/notes/ci-budget.md`).
- **`check-lib`** (build beat) is a pure shell self-test of `checks/lib_check.sh`, no
  external tooling beyond bash. Fast.
- Both are **standing measurements, not one-off sweeps**: they exist so the
  hollow-gate class cannot return through a newly added gate. A future gate that
  greps for a pinned line WILL be caught by `comment-mutation-proof` only if its case
  is registered there — adding a gate means adding its case.
