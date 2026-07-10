#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
web_session_check_cookie "${HTTP_COOKIE:-}" || {
  echo "Content-type: application/json; charset=UTF-8"
  echo ""
  echo '{"error":"unauthorized"}'
  exit 0
}

STATEDIR=/var/lib/sa02m-web-build
CHECK_JSON="$STATEDIR/check.json"

METHOD="${REQUEST_METHOD:-GET}"
QS="${QUERY_STRING:-}"
FORCE=0
if [ "$METHOD" = "POST" ]; then
  FORCE=1
fi
case "$QS" in
  *force=1*) FORCE=1 ;;
esac

if [ "$FORCE" = "1" ]; then
  if ! command -v sudo >/dev/null 2>&1 || ! sudo -n /usr/local/sbin/sa02m-web-update-check --manual >>/var/log/sa02m_install.log 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') web_update_check.cgi: sudo sa02m-web-update-check failed" >>/var/log/sa02m_install.log 2>&1 || true
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
