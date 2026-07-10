#!/bin/bash
# Вызывается от root через sudo; атомарно ставит /etc/sa02m_web.env из staging-файла www-data.
set -euo pipefail
STAGE=/tmp/sa02m_web.env.new
LIB=/usr/local/lib/sa02m-web-auth-lib.sh
[ -f "$LIB" ] || LIB="$(dirname "$0")/sa02m-web-auth-lib.sh"
# shellcheck disable=SC1090
. "$LIB"

[ -f "$STAGE" ] || exit 1
# validate_staging заполняет SA02M_WEB_USER / SA02M_WEB_PASS / SA02M_WEB_PASS_HASH.
web_auth_validate_staging "$STAGE" || exit 1
if [ -n "${SA02M_WEB_PASS_HASH:-}" ]; then
    web_auth_write_hashed "$SA02M_WEB_USER" "$SA02M_WEB_PASS_HASH" > "${STAGE}.norm"
else
    web_auth_write "$SA02M_WEB_USER" "$SA02M_WEB_PASS" > "${STAGE}.norm"
fi
install -m 640 -o root -g www-data "${STAGE}.norm" /etc/sa02m_web.env
rm -f "$STAGE" "${STAGE}.norm"
