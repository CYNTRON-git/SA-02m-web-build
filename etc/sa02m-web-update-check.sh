#!/bin/bash
# Сравнение задеплоенного коммита с tip ветки на GitHub. Запуск: root или systemd oneshot.
# Ошибки в /var/log/sa02m_install.log — только при ручной проверке (--manual или SA02M_WEB_UPDATE_CHECK_MANUAL=1).
set -euo pipefail

LIB=/usr/local/lib/sa02m-web-build-lib.sh
[ -f "$LIB" ] && . "$LIB"

MANUAL=0
for arg in "$@"; do
  case "$arg" in
    --manual) MANUAL=1 ;;
  esac
done
[ "${SA02M_WEB_UPDATE_CHECK_MANUAL:-0}" = "1" ] && MANUAL=1

STATEDIR=/var/lib/sa02m-web-build
DEPLOYED_FILE="$STATEDIR/deployed_commit"
CHECK_JSON="$STATEDIR/check.json"
TMP="$CHECK_JSON.tmp.$$"
WEB_VERSION_FILE=/var/www/network_config/VERSION

SA02M_WEB_BUILD_REPO_URL="${SA02M_WEB_BUILD_REPO_URL:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
SA02M_WEB_BUILD_BRANCH="${SA02M_WEB_BUILD_BRANCH:-main}"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/CYNTRON-git/SA-02m-web-build"

mkdir -p -m 755 "$STATEDIR"

if type resolve_web_build_branch >/dev/null 2>&1; then
  SA02M_WEB_BUILD_BRANCH="$(resolve_web_build_branch)"
fi

read_version_from_text() {
    tr -d '\r' | grep -E '^[0-9]+(\.[0-9]+){1,3}$' 2>/dev/null | head -1
}

if ! type read_local_web_version >/dev/null 2>&1; then
  read_local_web_version() {
    [ -f "$WEB_VERSION_FILE" ] || return 1
    read_version_from_text < "$WEB_VERSION_FILE"
  }
fi

get_remote_version_for_commit() {
    local commit=$1 body
    [ -n "$commit" ] || return 1
    command -v curl >/dev/null 2>&1 || return 1
    body="$(timeout 12 curl -fsSL --max-time 10 -A 'sa02m-web-update-check/1.0' \
        "${GITHUB_RAW_BASE}/${commit}/www/network_config/VERSION" 2>/dev/null)" || return 1
    [ -n "$body" ] || return 1
    printf '%s' "$body" | read_version_from_text
}

deployed_raw=""
[ -f "$DEPLOYED_FILE" ] && deployed_raw=$(tr -d ' \n\r\t' < "$DEPLOYED_FILE" || true)

deployed=""
if [ -n "$deployed_raw" ] && [[ "$deployed_raw" =~ ^[a-f0-9]{7,40}$ ]]; then
  deployed="$deployed_raw"
fi

lab_out="null"
if [ -n "$deployed" ]; then
  :
elif [ -n "$deployed_raw" ]; then
  lab="${deployed_raw//\"/}"
  lab_out="\"${lab}\""
fi

remote=""
err=""

get_remote_git() {
  command -v git >/dev/null 2>&1 || return 1
  local line
  line="$(timeout 18 git -c "http.lowSpeedLimit=1000" -c "http.lowSpeedTime=10" \
    ls-remote "$SA02M_WEB_BUILD_REPO_URL" "refs/heads/$SA02M_WEB_BUILD_BRANCH" 2>/dev/null | head -1)" || return 1
  [ -n "$line" ] || return 1
  printf '%s' "$(printf '%s\n' "$line" | awk '{print $1}')"
}

get_remote_curl() {
  command -v curl >/dev/null 2>&1 || return 1
  local url body
  url="https://api.github.com/repos/CYNTRON-git/SA-02m-web-build/commits/${SA02M_WEB_BUILD_BRANCH}"
  body="$(timeout 18 curl -fsSL --max-time 16 -A 'sa02m-web-update-check/1.0' "$url" 2>/dev/null)" || return 1
  [ -n "$body" ] || return 1
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sha') or '')" 2>/dev/null || return 1
    return 0
  fi
  local h
  h="$(printf '%s' "$body" | grep -oE '[a-f0-9]{40}' | head -1)" || return 1
  [ -n "$h" ] || return 1
  printf '%s' "$h"
}

if remote="$(get_remote_git)"; then
  :
elif remote="$(get_remote_curl)"; then
  :
else
  err="network_or_git_failed"
fi

if [ -n "$remote" ] && [[ ! "$remote" =~ ^[a-f0-9]{40}$ ]]; then
  err="invalid_remote_ref"
  remote=""
fi

ua="null"
if [ -n "$deployed" ] && [ -n "$remote" ]; then
  if [ "$remote" = "$deployed" ]; then
    ua="false"
  else
    ua="true"
  fi
fi

deployed_version=""
remote_version=""
deployed_version="$(read_local_web_version 2>/dev/null || true)"
if [ -n "$remote" ]; then
  remote_version="$(get_remote_version_for_commit "$remote" 2>/dev/null || true)"
fi
if [ -n "$deployed_version" ] && [ -n "$remote_version" ] && [ "$deployed_version" = "$remote_version" ]; then
  ua="false"
fi

checked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

dep_ver_out="null"
[ -n "$deployed_version" ] && dep_ver_out="\"$deployed_version\""

rem_ver_out="null"
[ -n "$remote_version" ] && rem_ver_out="\"$remote_version\""

dep_out="null"
[ -n "$deployed" ] && dep_out="\"$deployed\""

rem_out="null"
[ -n "$remote" ] && rem_out="\"$remote\""

err_out="\"\""
[ -n "$err" ] && err_out="\"${err}\""

{
  printf '{"checked_at":"%s","remote_commit":%s,"deployed_commit":%s,"deployed_label":%s,"deployed_version":%s,"remote_version":%s,"update_available":%s,"branch":"%s","error":%s}\n' \
    "$checked_at" "$rem_out" "$dep_out" "$lab_out" "$dep_ver_out" "$rem_ver_out" "$ua" "$SA02M_WEB_BUILD_BRANCH" "$err_out"
} > "$TMP"
chmod 644 "$TMP" 2>/dev/null || true
mv -f "$TMP" "$CHECK_JSON"
chmod 644 "$CHECK_JSON" 2>/dev/null || true

if [ -n "$err" ] && [ "$MANUAL" = "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-update-check: ${err}" >>/var/log/sa02m_install.log 2>/dev/null || true
fi
