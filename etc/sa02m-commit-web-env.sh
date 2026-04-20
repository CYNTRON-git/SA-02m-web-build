#!/bin/bash
# Вызывается от root через sudo; атомарно ставит /etc/sa02m_web.env из staging-файла www-data.
set -euo pipefail
STAGE=/tmp/sa02m_web.env.new
[ -f "$STAGE" ] || exit 1
grep -qE '^SA02M_WEB_USER=' "$STAGE" || exit 1
grep -qE '^SA02M_WEB_PASS=' "$STAGE" || exit 1
install -m 640 -o root -g www-data "$STAGE" /etc/sa02m_web.env
rm -f "$STAGE"
