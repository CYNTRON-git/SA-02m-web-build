#!/usr/bin/env bash
# transient-hygiene — surfaces ORPHANED transient artifacts (a shipped feature's
# plan, review stamp or audit run-note that the ship beat should have deleted)
# so the next auditor confirms and removes them; the row itself NEVER deletes.
# Audit 2026-09-16 (L4): the directories were clean, the row was missing —
# added while inert so the class is measured, not remembered.
#
# comment-mutation-proof-exempt: sweeps directories, pins no source line — there is no needle a comment token could satisfy; its non-vacuity is the age floor below, observed by hand with a back-dated file (recipe in the header).
#
# WHAT IT DOES: for every file under .ai-dev/plans, .ai-dev/reviews and
# .ai-dev/audit, the topic slug is the basename minus `_review.md` / `.md`.
# A file is a CANDIDATE when its slug is neither the current branch name nor
# mentioned anywhere in .ai-dev/state/current.md (the pointer names the active
# plan and the audit run-note it still needs). Candidates are PRINTED; the run
# FAILS only when a candidate is older than AGE_DAYS (14) — a live feature's
# artifacts are younger than that, a shipped feature's orphan is not.
# INERT by design when none of the three directories exists (a clean checkout,
# CI — the directories are gitignored): prints «inert: no transient dirs» and
# exits 0. That is the honest state, not a vacuous pass: there is nothing to
# sweep, and the registry text says exactly this.
#
# Hand-observed RED (2026-09-17): `touch -d '-20 days' .ai-dev/plans/old.md`
# (a slug absent from the pointer and not the branch) -> FAIL naming it;
# removed -> ok. A candidate younger than the floor prints as a candidate and
# exits 0.
#
# Run: bash .ai-dev/quality/checks/transient-hygiene.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

AGE_DAYS=14
POINTER=.ai-dev/state/current.md
fails=0
ok()  { printf 'transient-hygiene: ok    %s\n' "$*"; }
bad() { printf 'transient-hygiene: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

dirs=""
for d in .ai-dev/plans .ai-dev/reviews .ai-dev/audit; do
    [ -d "$d" ] && dirs="$dirs $d"
done
if [ -z "$dirs" ]; then
    echo "transient-hygiene: inert: no transient dirs (.ai-dev/plans, .ai-dev/reviews, .ai-dev/audit absent — a clean checkout)"
    exit 0
fi

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
pointer=""
[ -r "$POINTER" ] && pointer=$(cat "$POINTER")
now=$(date +%s)
n_files=0; n_cand=0
# shellcheck disable=SC2086
while IFS= read -r f; do
    [ -n "$f" ] || continue
    n_files=$((n_files + 1))
    base=${f##*/}
    slug=${base%_review.md}; slug=${slug%.md}
    [ "$slug" = "$branch" ] && continue
    case "$pointer" in *"$slug"*) continue ;; esac
    n_cand=$((n_cand + 1))
    mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo "$now")
    age=$(( (now - mtime) / 86400 ))
    if [ "$age" -gt "$AGE_DAYS" ]; then
        bad "orphan candidate older than $AGE_DAYS days: $f (slug '$slug', ${age} d) — confirm the feature shipped, then delete it by hand"
    else
        printf 'transient-hygiene: candidate %s (slug %s, %s d old — not the branch, not in the pointer; confirm before the floor)\n' "$f" "$slug" "$age"
    fi
done <<<"$(find $dirs -type f 2>/dev/null | sort)"

echo
if [ "$fails" -eq 0 ]; then
    echo "transient-hygiene: ALL OK — $n_files transient file(s) swept, $n_cand candidate(s), none past the ${AGE_DAYS}-day floor (never deletes; the auditor confirms)"
    exit 0
fi
echo "transient-hygiene: $fails FAILURE(S)"
exit 1
