#!/usr/bin/env bash
# web-update-csrf-contract — the legacy GitHub-OTA POST path of
# www/network_config/cgi-bin/web_update_apply.cgi launches a FULL ROOT update
# (`sudo -n /usr/local/sbin/sa02m-web-update-apply`) and was explicitly marked
# "no CSRF" until 1.0.6.24, while docs/decisions/selective-csrf-policy.md and the
# threat model both say EVERY session-authed mutating endpoint carries the
# X-SA02M-CSRF token (audit 2026-08-28, M6). SameSite=Lax blocked the scripted
# cross-site POST, so this was a broken defense-in-depth layer plus a false canon
# claim — this gate makes the claim true and keeps it true.
#
# WHAT IT PINS: a web_csrf_validate (or web_csrf_require) call sits BETWEEN the
# "Legacy GitHub OTA" branch marker and the root-update launch, so the token is
# checked before the sudo. Read comment-stripped via lib_check.sh (a `#`-disabled
# check fails here, not silently); the pin's comment-out case is registered in
# comment-mutation-proof.
#
# NON-VACUOUS: an absent CGI, a missing legacy-branch marker, or a missing
# root-launch line each FAIL rather than passing on nothing.
#
# PROVEN RED (1.0.6.24): on the pre-fix tree the legacy branch had no CSRF call
# between the marker and the launch -> FAIL; commenting the legacy web_csrf_validate
# out (comment-mutation-proof) -> FAIL; with the fix in place -> ok.
#
# Run: bash .ai-dev/quality/checks/web-update-csrf-contract.sh
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
launch_ln=$(grep -nF 'sudo -n /usr/local/sbin/sa02m-web-update-apply' "$CGI" | head -1 | cut -d: -f1)
marker_ln=$(grep -nF 'Legacy GitHub OTA' "$CGI" | tail -1 | cut -d: -f1)

if [ -z "$marker_ln" ]; then
    bad "the 'Legacy GitHub OTA' branch marker is gone — cannot locate the root-launch path (re-anchor this gate)"
elif [ -z "$launch_ln" ]; then
    bad "the 'sudo -n .../sa02m-web-update-apply' root launch is gone — re-anchor this gate"
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

echo
if [ "$fails" -eq 0 ]; then
    echo "web-update-csrf-contract: ALL OK"
    exit 0
fi
echo "web-update-csrf-contract: $fails FAILURE(S)"
exit 1
