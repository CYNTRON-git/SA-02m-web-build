#!/bin/bash
# Run as root via sudo by web_creds.cgi; installs /etc/sa02m_web.env.
#
# The new credential file arrives on STDIN only — never through a path. The
# former fixed staging file in the shared /tmp could be pre-created by any local
# account, and root installed whatever sat there (audit 2026-10-30 F1;
# docs/threat-model.md §4). Empty, oversized or malformed input → exit 1,
# nothing written. Harness: scripts/dev/test-web-creds-commit.sh (row web-creds-commit).
set -euo pipefail
LIB=/usr/local/lib/sa02m-web-auth-lib.sh
[ -f "$LIB" ] || LIB="$(dirname "$0")/sa02m-web-auth-lib.sh"
# shellcheck disable=SC1090
. "$LIB"

# A real env file is two short lines; the bound only stops an unbounded read.
MAX_BYTES=4096

umask 077
# Explicit template: a root-owned 0700 dir no other account can enter or swap
# (sticky /tmp), whatever TMPDIR the caller's environment carried.
work=$(mktemp -d /tmp/sa02m-commit-web-env.XXXXXX)
trap 'rm -rf -- "$work"' EXIT

head -c "$((MAX_BYTES + 1))" > "$work/in"
size=$(wc -c < "$work/in")
size=${size//[[:space:]]/}
[ "$size" -gt 0 ] || exit 1
[ "$size" -le "$MAX_BYTES" ] || exit 1

web_auth_validate_staging "$work/in" || exit 1
# Preserve whatever the input carries (hash or legacy plaintext), normalised.
web_auth_write "$SA02M_WEB_USER" "$(web_auth_stored_secret)" > "$work/norm"
install -m 640 -o root -g www-data "$work/norm" /etc/sa02m_web.env
