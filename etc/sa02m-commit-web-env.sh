#!/bin/bash
# Вызывается от root через sudo; атомарно ставит /etc/sa02m_web.env из staging-файла www-data.
set -euo pipefail
STAGE=/tmp/sa02m_web.env.new
LIB=/usr/local/lib/sa02m-web-auth-lib.sh
[ -f "$LIB" ] || LIB="$(dirname "$0")/sa02m-web-auth-lib.sh"
# shellcheck disable=SC1090
. "$LIB"

[ -f "$STAGE" ] || exit 1
web_auth_validate_staging "$STAGE" || exit 1
# Preserve whatever the staged file carries (hash or legacy plaintext).
web_auth_write "$SA02M_WEB_USER" "$(web_auth_stored_secret)" > "${STAGE}.norm"
install -m 640 -o root -g www-data "${STAGE}.norm" /etc/sa02m_web.env
rm -f "$STAGE" "${STAGE}.norm"
