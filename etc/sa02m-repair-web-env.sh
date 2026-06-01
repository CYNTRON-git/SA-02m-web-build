#!/bin/bash
# Нормализация /etc/sa02m_web.env (quoted формат, без $(...) в файле).
set -euo pipefail
AUTH=/etc/sa02m_web.env
LIB=/usr/local/lib/sa02m-web-auth-lib.sh
[ -f "$LIB" ] || LIB="$(dirname "$0")/sa02m-web-auth-lib.sh"
# shellcheck disable=SC1090
. "$LIB"
web_auth_repair_file "$AUTH"
