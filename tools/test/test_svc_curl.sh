#!/bin/bash
# Test service control via curl on device (localhost:9999)
#
# Живой токен панели передаётся окружением — статического токена в
# репозитории больше нет (заменён серверной сессионной моделью 2026-07-12):
#   SA02M_WEB_TOKEN=<hex> bash tools/test/test_svc_curl.sh
#
# Проверки ниже смотрят в ТЕЛО ответа (`"ok":true`), и это правильная форма
# для Bash-CGI: слой отвечает HTTP 200 даже на отказ, поэтому `curl -f` здесь
# ничего бы не поймал. Общий дом правила:
# docs/agent-rules/web-code-rigor.md ## Bash CGI floors.
set -e
: "${SA02M_WEB_TOKEN:?не задан: экспортируйте живой session_token панели (DevTools → Application → Cookies → session_token)}"
CK="session_token=${SA02M_WEB_TOKEN}"
BASE='http://127.0.0.1:9999/cgi-bin/services_ctrl.cgi'

post() {
  local id=$1 action=$2
  echo "=== POST $action $id ==="
  curl -sS -b "$CK" -H 'Content-Type: application/json' \
    -d "{\"id\":\"$id\",\"action\":\"$action\"}" "$BASE"
  echo
}

wait_result() {
  local id=$1
  local i=0
  while [ $i -lt 60 ]; do
    sleep 2
    j=$(curl -sS -b "$CK" "${BASE}?result=1&id=${id}")
    echo "  poll[$i]: $j"
    echo "$j" | grep -q '"pending":true' || break
    i=$((i+1))
  done
  echo "$j" | grep -q '"ok":true' && echo "  OK" || { echo "  FAIL"; return 1; }
}

echo "1) start mplc4"
post mplc4 start
wait_result mplc4

echo "2) stop mplc4"
post mplc4 stop
wait_result mplc4

echo "3) start codesys"
post codesys start
wait_result codesys

echo "4) stop codesys"
post codesys stop
wait_result codesys

echo "5) stop docker"
post docker stop
wait_result docker

echo "6) start docker"
post docker start
wait_result docker

echo "ALL TESTS PASSED"
