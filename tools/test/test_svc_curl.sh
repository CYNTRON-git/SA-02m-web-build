#!/bin/bash
# Test service control via curl on device (localhost:9999)
set -e
CK='session_token=cyntron_session'
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
