#!/bin/bash
# Stream downloadable config backup (plan §3.1).
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

BACKUP=/usr/local/sbin/sa02m-web-backup.sh
STAMP=$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date '+%Y%m%d%H%M%S')
FNAME="sa02m-backup-${STAMP}.tar.gz"

web_session_check_cookie || {
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
  printf '{"ok":false,"error":"unauthorized"}\n'
  exit 0
}

if [ "${REQUEST_METHOD:-GET}" != "GET" ]; then
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
  printf '{"ok":false,"error":"method_not_allowed"}\n'
  exit 0
fi

if [ ! -f "$BACKUP" ]; then
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
  printf '{"ok":false,"error_code":"E_CMD","error_message":"sa02m-web-backup.sh missing"}\n'
  exit 0
fi

if ! command -v sudo >/dev/null 2>&1; then
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
  printf '{"ok":false,"error_code":"E_CMD","error_message":"sudo not found"}\n'
  exit 0
fi

printf 'Content-type: application/gzip\r\n'
printf 'Content-Disposition: attachment; filename="%s"\r\n' "$FNAME"
printf 'Cache-Control: no-store\r\n'
printf '\r\n'

# Real sudo stream — if sudoers missing, client gets empty/truncated body + nginx may 502.
# Do not pretend success with a fake archive.
sudo -n "$BACKUP"
RC=$?
exit "$RC"
