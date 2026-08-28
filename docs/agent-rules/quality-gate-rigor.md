# Quality-gate rigor — a check is not finished until it has failed on purpose

**Write the check, run it against the real defect and watch it go RED, then fix
the defect and watch it go GREEN.** A check that has only ever seen a healthy
tree proves nothing about a sick one — it proves only that it can print `ok`.
Both states are observed, never inferred.

This is the **one home** for what makes a check trustworthy here — never
restated in a plan or a review; cited. What each row checks is the registry's
(`.ai-dev/quality/tools.json`); *where* a row actually runs is
`.ai-dev/notes/quality-gate-environment.md`; how a mechanism works is in its own
script header. This file carries the RULE and the SHAPES. Applies to every
registry row, every `scripts/dev/test-*.sh` harness, and every sentence in a
doc, contract or comment claiming a guarantee is "gated". Machine-facing
English (`.ai-dev/PROTOCOL.md` invariant 5).

Why it exists: checks in this project keep being found reporting protection they
did not have — the count is not the point and goes stale, the recurrence is. The
2026-08-28 sweep measured the scale — **12 of 14 pinned lines in the static
gates were defeated by putting `#` in front of them; 0 after the fix**
(`4686bf3`). Others: the gate `docs/threat-model.md` cited as proof that root
escalation was closed while it read one of six grant homes (audit 2026-08-28,
the «escalation gate reads 1 of the 6 homes» record in `.ai-dev/backlog.md`;
widened in `373a2f9`); the runner itself (`3a3e0ac`); and — in the very branch
that built the mechanical registration below — a new row claiming a mutation
case that did not exist, in the one directory that enforcement did not yet
enumerate.

---

## The rule

- **RED first, GREEN after** — the RED produced by the defect the check exists
  for, on the real tree or a pristine copy of it, not by a broken invocation.
- **Record the mutation, not the claim.** The commit body's
  **Verification method:** names the mutation and the observed result (format
  home: `git-commits.md`). "Gate added" is not verification.
- **A gate proving a defect closed lands before the fix** where history can
  carry it — `373a2f9` shipped the widened escalation gate alone and recorded it
  RED on the unfixed tree (42 failed assertions, all five predicted failure
  classes printed verbatim); the fix that followed is what made it green.
- **Non-vacuity is part of the check**: a missing target file, a sweep that
  stops seeing files, or a pin matching no live line FAILS. A check that passes
  because it tested nothing is the defect, not the absence of one.

## The hollow shapes

Each shipped here. Recognise the shape rather than memorise the list — it is
not exhaustive.

**(a) Comment-blindness — RED on delete, GREEN on `#`.** The most common way a
developer disables a line was invisible to eleven gates while their registry
text promised the comment case fails: `#firmware/mplc4/mplc_cyntron.so` in the
offline allow-list (the pack silently drops the MPLC RT plugin),
`#web_csrf_require` in `mplc_project_deploy.cgi` (CSRF gone from an endpoint
that launches a root helper) — both GREEN. The audit found five by mutation;
measuring the whole set found six more it had missed and one gate that was
half-safe (audit 2026-08-28, the «five safety gates are defeated by a comment
mark» record in `.ai-dev/backlog.md`; fixed `4686bf3`).

**(b) Incomplete enumeration — the check reads one home of many.**
`sudoers-pin-contract` read `etc/sudoers.d/sa02m-www` while `www-data` root
grants live in **six** homes, two written by installer heredocs and one a
runtime append — the two unpinned grants were structurally invisible to the
check meant to prove escalation closed (same record; `373a2f9`). Same shape
by scope: `py-syntax` compiled `opt/` only, so a syntax error in Python shipped
from `etc/` passed the full build beat and failed first at systemd start on the
board (`b4f6e0c`). Newest instance: `mqtt-install-secret` promised the broker
password is "never written to" the web-served install log while its pin read one
sink — the `log` helper — so a bare `echo "$MQTT_PASS" >> …install.log` left it
ALL OK. The fix is the shape that scales: an ALLOW-LIST of the sanctioned uses,
not a denylist of the sinks you thought of.

**(c) Scope that never re-runs — `covers` names the origin, not the breakers.**
`py-unit-roster` covers `opt/sa02m-rs485-roster/`; the contracted `modules`
block is assembled by the consumer in `status.cgi`, which the audit found
covered by nothing but bash syntax — deleting it broke the contract with every
gate green (audit 2026-08-28, the «rs485-roster contract is validated
producer-side only» record in `.ai-dev/backlog.md`). `covers` names every file
that can BREAK the guarantee, not only the file that defines it.

**(d) The runner never selects the work.** `--touched` resolved the touched set
from the committed `origin/main..HEAD` diff and read the working tree only when
that came back empty, so on any branch past its first commit a builder's own
uncommitted edits selected no rows: 46 touched files → 19 rows, `js-syntax` not
among them; after the union fix, 20 rows with `js-syntax` (`3a3e0ac`). Earlier
instance of the same class: a bare directory prefix in `covers` compiled to a
regex matching only the directory string, so `etc/` matched nothing under
`etc/` and a JS-only change ran no syntax row at all (`25f616b`).

**(e) Vacuous on empty — the collection stops matching and the check passes.**
Every collection assertion in `ui-layout.mjs` fails on an empty collection by
construction — a selector that stops matching turns the run RED instead of
quietly checking zero elements (the non-vacuity rule ported with the driver,
`3bdf021`); `telemetry-device-id-contract`
fails loudly outside a git checkout because its non-vacuity floor catches the
dead `git ls-files` sweep instead of quietly checking one file (the floor and
its reason are in that script's own header).

**(f) An early-exit pipe under `pipefail` turns "found" into "not found".**
`strip_comments < "$f" | grep -qF "$needle"`: `grep -q` exits on the first
match, the still-writing `sed` takes SIGPIPE and exits 141, and under
`set -o pipefail` that 141 becomes the pipeline status. `no-retired-session-token`
ran RED in CI (GNU sed) and falsely GREEN on Windows git-bash (MSYS sed) on the
same bytes. Capture first, then match in-shell. Full account:
`.ai-dev/notes/quality-gate-environment.md`. The shape survived inside
`lib_check.sh` itself — `stripped_first_line` ended in `grep -nE … | head -1`,
in the file whose header advertises capture-then-match — because every call site
happened to read the value rather than the status. Do not wait for a symptom on
a host that reproduces it: `check-lib` case 9e now pins the lib's own source
against the shape.

**(g) A stale allow-list or ledger entry.** `#cloud-btn-activate` sat in
`CONTRAST_WHITELIST` excusing a pair the harness never measures — the button
lives inside a collapsed `<details>` the driver never opens, so the exemption
covered an element under no test at all (`bb109d7`). An entry that excuses
nothing is stale; the ledger's own non-vacuity is what caught it.

## The floors now in place, and what they do not reach

Three mechanisms — each documents itself in its own header; do not restate them:

| Mechanism | What it actually covers |
|---|---|
| `.ai-dev/quality/checks/lib_check.sh` (row `check-lib`) | The comment-stripping helpers a static gate sources: comments blanked not deleted (ordering pins keep their line numbers), capture-then-match (no early-exit pipe). Self-tested — including a pin on its OWN source against shape (f) — because a bug here breaks every sourcing gate at once. |
| `.ai-dev/quality/checks/comment-mutation-proof.sh` (review beat) | Re-runs the comment-out mutation on a pristine copy of HEAD for the cases in its `CASES` table, **and** enforces that EVERY registry row is either cased there or carries a recorded exemption. |
| `.ai-dev/quality/run.mjs` `computeTouchedFiles` (`run.test.mjs` section C) | `--touched` = committed diff UNION working tree (staged, unstaged, untracked), both halves read from git's `-z` output so a quoted path cannot silently match no `covers` glob. |

**Registration is mechanical, not discipline.** `coverage_check()` enumerates
every row of `tools.json` — whatever directory the check lives in — and fails
the run unless the row carries a mutation case **or** a recorded exemption. It
first enumerated only scripts under `.ai-dev/quality/checks/`, which left 20
`scripts/dev/` rows outside the measurement with no exemption on record; the
branch that added the enforcement reproduced the class inside that blind spot
(a new row claiming a case that did not exist). The enumeration is now every
row, and the exemption has one home per row, decided by the row: a check script
of ours carries a `comment-mutation-proof-exempt: <reason>` marker **above its
first executable line** (measured, not asserted); a row that runs no such script
— an inline `bash -n` / `compileall` / `unittest` command — carries a
non-empty `mutationExempt` reason on the row itself.

**What it still does not reach**, stated so the next sweep does not re-derive
it: the proof measures whether a gate NOTICES its pinned line being commented
out; it cannot tell you the pin is the *right* line. A case naming a pin the
gate never really depended on passes for the wrong reason, and only reading the
gate catches that. A gate whose pins are fail-IF-PRESENT sweeps needs the
opposite mutation (re-introduce the banned pattern) and documents that in its
own header — but "all my pins are fail-IF-PRESENT" is a claim to check, not
accept: two gates recorded exactly that while carrying presence-floored pins
that a comment-out **does** turn RED, and both are cased now.

## Honesty — the description is part of the check

- A row's `checks` text in the registry, and any "gated" / "asserted by" /
  `[mechanical]` claim in a doc, a contract or a code comment, must match what
  the check actually verifies. **Overclaiming is the same defect as a hollow
  check** — worse in effect, because it is what stops the next person looking.
- Worst case found: `docs/threat-model.md` cited `sudoers-pin-contract` as
  proof the B1 root-escalation class was closed while that gate read one of six
  grant homes. The claim «эскалации нет» was false from 1.0.6.10 to 1.0.6.23,
  and unexamined, because a doc said a gate held it.
- Same class, smaller: `mqtt_set.cgi`'s «asserted by the contract check»
  comment named a row that did not exist; the guarantee was live but unguarded,
  and the comment is what made it look guarded (audit 2026-08-28, the «one
  endpoint that switches real relay outputs is guarded only by a comment»
  record in `.ai-dev/backlog.md`; row added in `3b44f24`).
- A skipped row is reported as **skipped**, never as passed; a local substitute
  for a CI-authoritative row is evidence, not proof
  (`.ai-dev/notes/quality-gate-environment.md`).
- Narrowing or weakening a check edits its description in the same commit.

## A deliberate exemption is recorded in the gate's header

A gate left unchanged during a sweep is indistinguishable from a gate that was
overlooked. When a gate is deliberately outside a floor, the reason goes at the
top of the script, above its first executable line. Without it the next sweep
re-asks the same question, or "fixes" a gate that was right. The same applies to
a ledger exemption: it carries its reason and, where the value can drift, a
`floor` ratchet.

Do not count the notes here — the count is what goes stale (`:145` of this file
claimed seven while nine files carried one, drifted inside a single branch).
`grep -rl 'comment-mutation-proof-exempt:' .ai-dev/quality/ scripts/dev/` is the
answer, and `comment-mutation-proof` fails the run when a row has neither a case
nor one of these notes.

**The reason has to be TRUE of the gate, not plausible about its kind.** Two
notes recorded "every pin here is fail-IF-PRESENT" while the gate's non-vacuity
floor was a presence pin that a comment-out turns RED — the conclusion (safe)
was right and the stated reason was wrong, which is exactly what the next sweep
reads and trusts. Where a comment-out really does turn the gate RED, register
the case instead of writing the note.

## Before a check is called done

1. RED against the real defect observed, GREEN after — both in the commit body.
2. Comment-out mutation tried; if the gate pins a line, its case is registered
   in `comment-mutation-proof`.
3. Every home of the thing enumerated — not the first one found.
4. `covers` names the files that can break the guarantee, not just its origin.
5. Non-vacuous: missing file, empty sweep, unmatched pin all FAIL.
6. No producer piped into `grep -q` / `-l` / `-m N` / `head` under `pipefail`
   (pinned for `lib_check.sh` itself by `check-lib` case 9e).
7. Registry description matches the check; deliberate exemptions carry reasons
   that are true of THIS gate, not of its kind.
8. A skip is reported as a skip. An assertion that cannot fail (`-ge 0` on a
   count) is decoration, not evidence — delete it or make it real.
