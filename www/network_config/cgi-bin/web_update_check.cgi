#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
web_session_check_cookie || {
  echo "Content-type: application/json; charset=UTF-8"
  echo ""
  echo '{"error":"unauthorized"}'
  exit 0
}

# SA02M_WEB_BUILD_STATEDIR / SA02M_INSTALL_LOG: the behavioural harness
# (scripts/dev/test-cgi-csrf-behaviour.sh) redirects the cache and the log
# into a sandbox. Process environment only — nginx/fcgiwrap set no SA02M_*
# name, so a client cannot choose either path (same names as
# web_update_apply.cgi / etc/sa02m-update-runner.sh).
STATEDIR="${SA02M_WEB_BUILD_STATEDIR:-/var/lib/sa02m-web-build}"
CHECK_JSON="$STATEDIR/check.json"
INSTALL_LOG="${SA02M_INSTALL_LOG:-/var/log/sa02m_install.log}"

METHOD="${REQUEST_METHOD:-GET}"
# The root check runs on POST only. A GET never forces — `?force=1` on a GET
# used to run the helper too, a Lax-defeating CSRF vector (top-level navigation
# sends the session cookie); the panel POSTs `?force=1`, so the query part is
# inert now. policy: docs/decisions/selective-csrf-policy.md; gate: cgi-csrf-policy.
FORCE=0
if [ "$METHOD" = "POST" ]; then
  FORCE=1
fi

if [ "$FORCE" = "1" ]; then
  # CSRF BEFORE the mutation; headers are not emitted yet, so web_csrf_require
  # prints its own headers + the shared E_CSRF body and exits on failure.
  web_csrf_require
  if ! command -v sudo >/dev/null 2>&1 || ! sudo -n /usr/local/sbin/sa02m-web-update-check --manual >>"$INSTALL_LOG" 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') web_update_check.cgi: sudo sa02m-web-update-check failed" >>"$INSTALL_LOG" 2>&1 || true
  fi
fi

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

if [ -f "$CHECK_JSON" ]; then
  cat "$CHECK_JSON"
else
  echo '{"checked_at":null,"remote_commit":null,"deployed_commit":null,"deployed_label":null,"deployed_version":null,"remote_version":null,"update_available":null,"branch":"main","error":"no_cache_yet"}'
fi
