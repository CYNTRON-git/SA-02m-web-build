#!/usr/bin/env bash
# lib_check.sh — shared helpers for the static quality gates. NOT a check row
# itself: it is sourced by the gates in this directory.
#
# WHY IT EXISTS. A gate that greps a source file for a needle is defeated by
# putting a `#` in front of the line it pins: the needle is still there, the
# grep still matches, the gate still prints ok — while the guarantee is gone.
# The 2026-08-28 audit proved that on five gates (a dropped MPLC plugin, a
# dropped CSRF require, a re-introduced 17 s boot hold, an OTA rollback on an
# operator-disabled unit). The repo had solved it once inside
# no-retired-session-token.sh; every other gate re-derived it or did without.
# This file is the one home for the fix, so a new gate inherits it.
#
# TRAPS ENCODED HERE, both already paid for in this repo:
#   1. `strip_comments < f | grep -q needle` is NOT safe. `grep -q` exits on the
#      first match, the still-writing `sed` takes a SIGPIPE and exits 141, and
#      under `set -o pipefail` that 141 becomes the pipeline's status — a needle
#      that WAS found reads back as absent. GNU sed (CI/WSL) dies on the signal,
#      MSYS sed on Windows git-bash does not, so the row ran RED in CI and
#      falsely GREEN locally. Every helper below captures first and matches
#      against a here-string: no upstream process, no signal, no poisoned status.
#      (.ai-dev/notes/quality-gate-environment.md — "A row that RUNS everywhere".)
#   2. Comments are BLANKED, not deleted, so line numbers survive. Gates that
#      pin an ORDER (READY=1 before the constructor, backup before delete) read
#      `grep -n` line numbers off the stripped text.
#
# WHAT IS NOT STRIPPED, deliberately: a line whose first non-space character is
# `*`. That is a block-comment continuation in C/JS/Python-doc style, but it is
# also a shell `case` label (`*sa02m*|6.1.0-rc6*)`) — and a gate that reads a
# case label out of a device script must still see it. A gate that needs the
# block-comment form strips it itself.
#
# Self-test: .ai-dev/quality/checks/lib_check.test.sh (registry row `check-lib`).
# A bug in this file breaks every gate that sources it at once, so it carries
# its own mutation-proven test — the condition on which it was allowed to exist
# (plan decision L, 1.0.6.24).

# Blank whole-line comments (`#` and `//` forms). Reads stdin, writes stdout,
# one output line per input line.
strip_comments() {
    sed -e 's/^[[:space:]]*#.*$//' -e 's#^[[:space:]]*//.*$##'
}

# strip_comments plus a trailing comment that follows whitespace (` # ...`).
# Quote-blind by design: a `#` inside a string literal truncates the line too.
# That is fail-CLOSED for a presence pin — it can only remove a needle the gate
# then reports missing, never invent one. Use it on shell / systemd / sudoers /
# conf files; prefer strip_comments on code where `#` lives inside strings.
strip_comments_inline() {
    sed -e 's/^[[:space:]]*#.*$//' -e 's#^[[:space:]]*//.*$##' -e 's/[[:space:]]#.*$//'
}

# Echo FILE with whole-line comments blanked. Empty output for an unreadable
# file — the caller's own non-vacuity check must catch that (a gate whose
# target file vanished must FAIL, not pass on an empty sweep).
stripped_text() {  # $1=path
    [ -r "$1" ] || return 0   # readability tested here: `< missing` would print a shell error
    strip_comments < "$1"
}

stripped_text_inline() {  # $1=path
    [ -r "$1" ] || return 0
    strip_comments_inline < "$1"
}

# True when TEXT contains the literal needle. Shell-native match: no subprocess,
# so no SIGPIPE and no regex surprises from a needle carrying metacharacters.
text_has() {  # $1=text  $2=literal needle
    case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

# True when TEXT matches the ERE. Here-string, never a pipe (trap 1 above).
text_matches() {  # $1=text  $2=ERE
    grep -qE "$2" <<<"$1"
}

# True when the comment-stripped FILE contains the literal needle.
stripped_has() {  # $1=path  $2=literal needle
    local text
    text=$(stripped_text "$1")
    text_has "$text" "$2"
}

# True when the comment-stripped FILE matches the ERE.
stripped_matches() {  # $1=path  $2=ERE
    local text
    text=$(stripped_text "$1")
    text_matches "$text" "$2"
}

# As stripped_has/stripped_matches, but also dropping trailing comments.
stripped_has_inline() {  # $1=path  $2=literal needle
    local text
    text=$(stripped_text_inline "$1")
    text_has "$text" "$2"
}

stripped_matches_inline() {  # $1=path  $2=ERE
    local text
    text=$(stripped_text_inline "$1")
    text_matches "$text" "$2"
}

# Number of comment-stripped lines of FILE matching the ERE. Prints 0 for an
# unreadable file (again: the caller owns non-vacuity).
stripped_count() {  # $1=path  $2=ERE
    local text
    text=$(stripped_text "$1")
    [ -n "$text" ] || { printf '0\n'; return 0; }
    grep -cE "$2" <<<"$text"
}

# Line number of the FIRST comment-stripped line of FILE matching the ERE
# (empty when none). Blanking, not deleting, is what makes this number line up
# with the real file — the property the ordering pins depend on.
#
# Both helpers CAPTURE the whole match list, then pick the first/last line and
# its number with shell parameter expansion. The obvious `grep -nE … | head -1 |
# cut -d: -f1` is trap 1 above, in the file that encodes trap 1: `head -1` is an
# early-exit consumer, so on a large input grep takes SIGPIPE and under
# `set -o pipefail` the helper returns 141 while still printing the right value.
# Every current call site captures the value, so nothing was broken — the shape
# is banned because the failure is platform-dependent and silent, not because it
# had bitten here yet (review Q9, 1.0.6.24; self-test case 9e pins the shape).
stripped_first_line() {  # $1=path  $2=ERE
    local text matches line
    text=$(stripped_text "$1")
    [ -n "$text" ] || return 0
    matches=$(grep -nE "$2" <<<"$text")
    [ -n "$matches" ] || return 0
    line=${matches%%$'\n'*}          # first matching "N:content"
    printf '%s\n' "${line%%:*}"
}

# Line number of the LAST such line.
stripped_last_line() {  # $1=path  $2=ERE
    local text matches line
    text=$(stripped_text "$1")
    [ -n "$text" ] || return 0
    matches=$(grep -nE "$2" <<<"$text")
    [ -n "$matches" ] || return 0
    line=${matches##*$'\n'}          # last matching "N:content"
    printf '%s\n' "${line%%:*}"
}
