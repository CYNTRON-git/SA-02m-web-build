#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

web_session_check_cookie "${HTTP_COOKIE:-}" || {
  echo '{"error":"unauthorized"}'
  exit 0
}

read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

decode() { printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"; }
get_f() { decode "$(echo "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | cut -d= -f2-)"; }

CUR=$(get_f "current_password")
NEWU=$(get_f "new_username" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
NEWP=$(get_f "new_password")
NEWP2=$(get_f "new_password_confirm")

json_err() { echo "{\"error\":\"$1\"}"; exit 0; }

[ -n "$CUR" ] || json_err "no_current"
[ -n "$NEWU" ] || json_err "no_user"
[ -n "$NEWP" ] || json_err "no_password"
[ "$NEWP" = "$NEWP2" ] || json_err "mismatch"
[[ "$NEWU" =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || json_err "bad_username"
[[ ${#NEWP} -ge 4 && ${#NEWP} -le 128 ]] || json_err "bad_password_len"
[[ "$NEWP" != *$'\n'* ]] || json_err "bad_password"
case "$NEWP" in
  *"'"*|*'$'*|*'`'*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*|*'('*|*')'*) json_err "bad_password_char" ;;
esac

# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

AUTH=/etc/sa02m_web.env
if [ ! -f "$AUTH" ]; then
  json_err "no_auth_file"
fi
SA02M_WEB_USER=admin
SA02M_WEB_PASS=cyntron
SA02M_WEB_PASS_HASH=""
web_auth_read "$AUTH" || json_err "no_auth_file"
: "${SA02M_WEB_USER:=admin}"

web_auth_verify_password "$CUR" || json_err "wrong_password"

NEWP_HASH=$(web_auth_hash_password "$NEWP")
[ -n "$NEWP_HASH" ] || json_err "hash_failed"
web_auth_write_hashed "$NEWU" "$NEWP_HASH" > /tmp/sa02m_web.env.new
chmod 600 /tmp/sa02m_web.env.new

if ! sudo /usr/local/sbin/sa02m-commit-web-env 2>/dev/null; then
  rm -f /tmp/sa02m_web.env.new
  json_err "save_failed"
fi

echo '{"ok":true}'
