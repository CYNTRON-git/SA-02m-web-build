#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
web_session_check_cookie || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

# POST-only — a GET restart is a Lax-defeating CSRF vector (top-level navigation
# still sends the session cookie). policy: docs/decisions/selective-csrf-policy.md.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo "Content-type: application/json"; echo ""
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

echo "Content-type: application/json"
echo ""
# CSRF BEFORE the mutation (defense-in-depth on top of POST+SameSite=Lax).
# Headers already emitted, so validate inline and print the shared shape.
if ! web_csrf_validate; then
    web_csrf_error_body
    exit 0
fi
# A LIVE update runner (applying / verifying / committing / rolling_back) is
# not torn apart by a reboot — `reboot -f` mid-deploy is a torn tree (1.0.6.52,
# F5a; contract docs/contracts/web-update.md «Жизненный цикл apply»). A STALE
# transaction (the runner is gone) stays rebootable: the reboot IS its recovery
# path — recover hands the tree to sa02m-update-verify at boot. The stage is a
# root-written value matched against the fixed busy set before it is printed.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_update.sh"
_upd_stage=$(web_upd_txn_stage)
if web_upd_stage_busy "$_upd_stage" && web_upd_runner_alive; then
    printf '{"ok":false,"error_code":"E_UPDATE_RUNNING","error_message":"update in progress (stage=%s)"}\n' "$_upd_stage"
    exit 0
fi
# Admission is synchronous; the fixed helper creates the independent worker.
if [[ ! -x /usr/local/sbin/sa02m-web-restart-services.sh ]]; then
    echo '{"ok":false,"error":"helper_missing"}'
    exit 0
fi
if sudo -n /usr/local/sbin/sa02m-web-restart-services.sh >/dev/null 2>&1; then
    echo '{"ok":true,"accepted":true}'
else
    echo '{"ok":false,"error":"admission_failed"}'
fi
