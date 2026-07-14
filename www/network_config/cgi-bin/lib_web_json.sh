#!/bin/bash
# Shared JSON string escaper for SA-02m web CGI endpoints — the ONE home for
# json_escape (web-code-rigor.md ## Bash CGI floors: "any value interpolated
# into JSON goes through the shared escaper"). Sourced by status.cgi and
# mqtt_config.cgi. Escapes backslash and double-quote, strips CR, folds
# newlines to a space so a value stays a single JSON string token.
#
# NOTE: etc/sa02m-web-service-ctl.sh carries a byte-identical local copy — it
# deploys to /etc, a different directory from this cgi-bin lib, so it cannot
# source this file portably. The two MUST stay in sync.

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'
}
