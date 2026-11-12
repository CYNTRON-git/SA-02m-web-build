#!/bin/bash
# Shared JSON string escaper for SA-02m web CGI endpoints — the ONE home for
# json_escape (web-code-rigor.md ## Bash CGI floors: "any value interpolated
# into JSON goes through the shared escaper"). Sourced by status.cgi and
# mqtt_config.cgi. Escapes backslash and double-quote, strips CR, folds
# newlines to a space so a value stays a single JSON string token.
#
# Pure bash: no sed fork per value — this runs for every value of every status
# poll on the shared ARM target (A8-06). Every call site reads it through
# `$(json_escape …)`, and in that context the output is byte-identical to the
# former `printf | sed` escaper, except that `\`, `"` and CR on line 2+ are now
# escaped too (the sed loop jumped back past its substitutions, so they leaked
# raw). Control characters other than CR/LF pass through unchanged.
# Pinned by scripts/dev/test-json-escape-equiv.sh.
#
# NOTE: etc/sa02m-web-service-ctl.sh carries a POSIX-sh copy (sed) — it
# deploys to /etc under /bin/sh, so it can neither source this file portably
# nor use bash pattern substitution. The two MUST produce the same bytes; the
# same test compares them.

json_escape() {
    local s=${1-}
    s=${s%$'\n'}
    s=${s//'\'/'\\'}
    s=${s//'"'/'\"'}
    s=${s//$'\r'/}
    s=${s//$'\n'/ }
    printf '%s' "$s"
}
