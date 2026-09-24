#!/usr/bin/env bash
# Quality row `sudoers-visudo` (see .ai-dev/quality/tools.json). Runs
# `visudo -cf` over every committed sudoers drop-in (etc/sudoers.d/*), with CR
# stripped first as the legacy launcher does, so a syntax error is caught
# before it reaches a board. On the board a malformed drop-in breaks sudo
# globally — every root CGI helper and the update launcher itself — and every
# deploy path (installer, OTA runner, legacy launcher) now refuses one, but
# only at install time on the device; nothing in the repo checked them before
# (audit 2026-09-24, M2; sudoers-pin-contract.sh pins grants, not syntax).
#
# Where visudo is absent (Windows git-bash) this prints a loud SKIP line and
# exits 0 — run.mjs has no skip status and reports the row as PASS; the skip is
# recorded in .ai-dev/notes/quality-gate-environment.md and WSL/Linux CI is the
# authority. Non-vacuous: no drop-in found FAILS.
# RED proof: SUDOERS_VISUDO_DIR=<dir holding a broken copy> bash this-script
# → FAIL naming the file (recorded in the registry row).
# comment-mutation-proof-exempt: behavioural - every assertion is visudo's own parse of the real files; the script pins no source line a comment could satisfy.
set -u

dir=${SUDOERS_VISUDO_DIR:-etc/sudoers.d}
fails=0
n=0

if ! command -v visudo >/dev/null 2>&1; then
    echo "sudoers-visudo: SKIP  visudo not on PATH here — the $dir drop-ins are NOT validated on this host (WSL/Linux CI is the authority)"
    exit 0
fi

tmp=$(mktemp) || { echo "sudoers-visudo: FAIL  mktemp"; exit 1; }
trap 'rm -f "$tmp"' EXIT
for f in "$dir"/*; do
    [ -f "$f" ] || continue
    n=$((n + 1))
    sed 's/\r$//' "$f" > "$tmp"
    if out=$(visudo -cf "$tmp" 2>&1); then
        echo "sudoers-visudo: ok    $f parses"
    else
        echo "sudoers-visudo: FAIL  $f: ${out//$'\n'/ }"
        fails=$((fails + 1))
    fi
done

if [ "$n" -eq 0 ]; then
    echo "sudoers-visudo: FAIL  no drop-in found under $dir — the sweep checked nothing"
    exit 1
fi
if [ "$fails" -eq 0 ]; then
    echo "sudoers-visudo: ALL OK ($n drop-ins)"
    exit 0
fi
echo "sudoers-visudo: $fails of $n drop-in(s) rejected by visudo"
exit 1
