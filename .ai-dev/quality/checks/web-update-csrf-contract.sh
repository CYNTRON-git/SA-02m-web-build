#!/usr/bin/env bash
# web-update-csrf-contract - authenticated online apply checks CSRF before
# delegating to lib_web_update_control.sh and the fixed root controller.
# The controller owns journals and launches; the CGI owns session/CSRF checks.
# Missing source, delegation, isolated root entry, or live CSRF call fails.
# Comment-out probes cover the CGI, shared library and privileged entrypoint.
# The controller's behavior is exercised by update-control-boundaries and
# py-unit-update; this gate pins the source chain connecting those boundaries.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

CGI=www/network_config/cgi-bin/web_update_apply.cgi
fails=0
ok()  { printf 'web-update-csrf-contract: ok    %s\n' "$*"; }
bad() { printf 'web-update-csrf-contract: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

[ -f "$CGI" ] || { echo "web-update-csrf-contract: FAIL — $CGI absent"; exit 1; }

# Region anchors read from the RAW file (the marker is a comment; the launch is
# code) — stripped_text blanks comment bodies but keeps line numbers, so a check
# needle commented out inside the region disappears while the anchors survive.
launch_ln=$(stripped_text "$CGI" | grep -nF 'web_update_control online' | head -1 | cut -d: -f1)
marker_ln=$(grep -nF 'Legacy GitHub OTA' "$CGI" | tail -1 | cut -d: -f1)

if [ -z "$marker_ln" ]; then
    bad "the 'Legacy GitHub OTA' branch marker is gone — cannot locate the root-launch path (re-anchor this gate)"
elif [ -z "$launch_ln" ]; then
    bad "the fixed controller online call is gone — re-anchor this gate"
elif [ "$marker_ln" -ge "$launch_ln" ]; then
    bad "the legacy marker ($marker_ln) is not before the root launch ($launch_ln) — the branch shape changed"
else
    csrf_ln=$(stripped_text "$CGI" | grep -nE 'web_csrf_(validate|require)' \
        | awk -F: -v a="$marker_ln" -v b="$launch_ln" '$1>a && $1<b {print $1; exit}')
    if [ -n "$csrf_ln" ]; then
        ok "the legacy GitHub-OTA root launch is CSRF-gated (web_csrf check at line $csrf_ln, before the sudo at $launch_ln)"
    else
        bad "no web_csrf_validate/require between the legacy marker ($marker_ln) and the root launch ($launch_ln) — the root OTA runs without a CSRF check (M6)"
    fi
fi

# The CGI delegates transport, but the privilege boundary remains fixed.
LIB=www/network_config/cgi-bin/lib_web_update_control.sh
ROOT_ENTRY=etc/sa02m-update-control.sh
stripped_has "$CGI" 'web_session_check_cookie ||' || bad "session authentication missing"
stripped_has "$CGI" 'lib_web_update_control.sh' || bad "controller library not sourced"
stripped_has "$LIB" 'sudo -n /usr/local/libexec/sa02m-update-control "$@"' || bad "fixed root controller dispatch missing"
stripped_has "$ROOT_ENTRY" 'exec /usr/bin/python3 -I -c' || bad "isolated root interpreter missing"
stripped_has "$ROOT_ENTRY" 'from lib.control import main; sys.exit(main())' || bad "root controller entry missing"

echo
if [ "$fails" -eq 0 ]; then
    echo "web-update-csrf-contract: ALL OK"
    exit 0
fi
echo "web-update-csrf-contract: $fails FAILURE(S)"
exit 1
